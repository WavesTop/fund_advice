from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend.analysis.theme_value_tilt import ThemeValuationHistory, POLICIES, stock_distribution, theme_tilt_target
from backend.analysis.trend_risk_budget import TrendBudgetPolicy, budget_statistics, risk_budget_target
from backend.core.trading_calendar import TradingCalendar
from scripts.validate_theme_value import audit_theme_decision
from scripts.collect_index_valuation import archive, parse_history
from tests import test_macro_trend_budget as fixtures

ROOT = Path(__file__).resolve().parents[1]


class ThemeValueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = TradingCalendar(json.loads((ROOT / "config/research-exchange-calendar-2009-2026-v2.json").read_text()))

    def context(self, stocks):
        return {"signal_date": "test", "funds": {c: {"status": "ready", "percentile": .1 if i == 0 else .9} for i, c in enumerate(stocks)}}

    def test_unknown_stocks_keep_neutral_budget_and_known_group_mass(self):
        stocks = ["a", "b", "c", "d", "e", "f"]
        state = self.context(stocks)
        for code in stocks[2:]:
            state["funds"][code] = {"status": "unavailable", "percentile": None}
        weights, trace = stock_distribution(stocks, dict.fromkeys(stocks, .5), state, "theme_value40")
        for code in stocks[2:]:
            self.assertEqual(weights[code], 1 / 6)
        self.assertAlmostEqual(weights["a"] + weights["b"], 2 / 6)
        self.assertGreater(weights["a"], weights["b"])
        self.assertEqual(trace["known_funds"], ["a", "b"])

    def test_single_known_stock_cannot_concentrate_the_missing_group(self):
        stocks = ["a", "b", "c"]
        state = self.context(stocks)
        for code in stocks[1:]:
            state["funds"][code] = {"status": "unavailable", "percentile": None}
        weights, _ = stock_distribution(stocks, dict.fromkeys(stocks, 1.), state, "theme_confirmed40")
        self.assertEqual(weights, dict.fromkeys(stocks, 1 / 3))

    def test_matched_price_control_uses_the_same_current_valuation_coverage(self):
        stocks = ["a", "b", "c"]
        state = self.context(stocks)
        state["funds"]["c"] = {"status": "unavailable", "percentile": None}
        quality = {"a": 1., "b": -1., "c": 1.}
        matched, trace = stock_distribution(stocks, quality, state, "trend_matched40")
        full, _ = stock_distribution(stocks, quality, state, "trend_tilt40")
        self.assertEqual(matched["c"], 1 / 3)
        self.assertGreater(full["c"], 1 / 3)
        self.assertEqual(trace["known_funds"], ["a", "b"])

    def test_caps_positive_weights_and_same_risk_budget(self):
        stocks = list("abcdefghijkl")
        state = self.context(stocks)
        quality = {c: 1. if i == 0 else -1. for i, c in enumerate(stocks)}
        for policy in POLICIES:
            weights, _ = stock_distribution(stocks, quality, state, policy)
            self.assertAlmostEqual(sum(weights.values()), 1.)
            self.assertTrue(all(0 < v <= 2 / len(stocks) + 1e-12 for v in weights.values()))
        data = fixtures.fixture()
        stats = budget_statistics(data, 120)
        context = self.context(stats["stocks"]) | {"signal_date": data["days"][120]}
        for policy in POLICIES:
            targets, meta = theme_tilt_target(data, policy, 120, (), context, stats=stats)
            self.assertEqual(meta["proposed_stock_budget"], .4)
            self.assertLessEqual(meta["projected_volatility"], .1 + 1e-10)
            self.assertLessEqual(sum(targets.weights.values()), 1 + 1e-12)
        base, _ = theme_tilt_target(data, "equal40", 120, (), context, stats=stats)
        legacy, _ = risk_budget_target(data, TrendBudgetPolicy("base", "static40", .1), 120, (), stats=stats)
        self.assertEqual(base, legacy)

    def test_invalid_custom_distributions_and_quality_are_rejected(self):
        data = fixtures.fixture()
        stats = budget_statistics(data, 120)
        for weights in ({"a": 1.}, {"a": .2, "b": .2}, {"a": float("nan"), "b": .5}, {"a": True, "b": 0.}):
            with self.assertRaises(ValueError):
                risk_budget_target(data, TrendBudgetPolicy("base"), 120, (), stats=stats, stock_distribution=weights)
        with self.assertRaises(ValueError):
            stock_distribution(["a"], {"a": float("nan")}, self.context(["a"]), "theme_value40")

    def source(self, days, quality_funds=("a", "b")):
        items, evidence = [], {"checks": {}}
        for i, fund in enumerate(quality_funds):
            code = f"T0000{i}"
            items.append({"fund_code": fund, "relation": {"index_code": code}, "index_publication_date": days[0],
                          "rows": [{"date": d, "peg": 20.} for d in days], "status": "collected"})
            evidence["checks"][fund] = {"index_code": code, "status": "provisional_dated_factsheet_match"}
        return {"items": items, "cutoff": days[-1]}, evidence

    def test_publication_cutoff_quarantine_and_future_values(self):
        days = [d.isoformat() for d in self.calendar.sessions(date(2021, 1, 1), date(2024, 1, 1))]
        payload, evidence = self.source(days)
        history = ThemeValuationHistory(payload, evidence, self.calendar)
        asof = days[350]
        original = history.state(asof)
        altered = deepcopy(payload)
        for item in altered["items"]:
            for row in item["rows"]:
                if row["date"] > asof:
                    row["peg"] = 1000.
        self.assertEqual(original, ThemeValuationHistory(altered, evidence, self.calendar).state(asof))
        quarantine = deepcopy(evidence)
        quarantine["checks"]["a"]["status"] = "field_mismatch"
        self.assertEqual(ThemeValuationHistory(payload, quarantine, self.calendar).state(asof)["funds"]["a"]["status"], "unavailable")
        broken = deepcopy(payload)
        broken["items"][0]["index_publication_date"] = days[10]
        with self.assertRaises(ValueError):
            ThemeValuationHistory(broken, evidence, self.calendar)

    def test_independent_rebuild_rejects_changes_to_missing_weights_and_dates(self):
        data = fixtures.fixture()
        days = [d.isoformat() for d in self.calendar.sessions(date(2021, 1, 1), date.fromisoformat(data["days"][-1]))]
        payload, evidence = self.source(days)
        evidence["checks"]["b"]["status"] = "missing_factsheet"
        history = ThemeValuationHistory(payload, evidence, self.calendar)
        stats = budget_statistics(data, 120)
        state = history.state(data["days"][120])
        targets, meta = theme_tilt_target(data, "theme_value40", 120, (), state, stats=stats)
        entry = meta | {"targets": targets.weights}
        raw = {"T00000": {d: 20. for d in days}, "T00001": {}}
        audit_theme_decision(entry, data, 120, history.mapping, raw, history.history.days, "theme_value40")
        for field, value in (("stock_distribution", {"a": 1., "b": 0.}), ("targets", {}),
                             ("proposed_stock_budget", float("nan"))):
            with self.assertRaises(ValueError):
                audit_theme_decision(entry | {field: value}, data, 120, history.mapping, raw, history.history.days, "theme_value40")
        damaged = deepcopy(entry)
        damaged["theme_valuation"]["funds"]["a"]["quote_date"] = data["days"][121]
        with self.assertRaises(ValueError):
            audit_theme_decision(damaged, data, 120, history.mapping, raw, history.history.days, "theme_value40")

    def test_cache_only_history_never_sends_new_requests(self):
        url = "https://www.csindex.com.cn/public-test"
        body = b'{"data": []}'
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            key = sha256(url.encode()).hexdigest()
            (folder / key).write_bytes(body)
            (folder / (key + ".json")).write_text(json.dumps({"url": url, "path": key, "sha256": sha256(body).hexdigest()}))
            with patch("scripts.collect_index_valuation.urlopen", side_effect=AssertionError("Network must not be used")):
                cached, _ = archive(url, folder, cache_only=True)
                self.assertEqual(cached, body)
                with self.assertRaises(FileNotFoundError):
                    archive(url, folder, cache_only=True, request_body=b'{"query":"different"}')
                with self.assertRaises(FileNotFoundError):
                    archive(url + "-missing", folder, cache_only=True)

    def test_history_parser_preserves_exact_named_theme_identity(self):
        record = {"indexCode": "H30588", "indexNameCnAll": "中证证券保险指数", "tradeDate": "20150202", "close": 1000., "peg": 12.}
        body = json.dumps({"code": "200", "success": True, "data": [record]}).encode()
        values = parse_history(body, "H30588", date(2015, 2, 2), date(2015, 2, 3), expected_name="中证证券保险指数")
        self.assertEqual(values[0]["peg"], 12.)
        with self.assertRaises(ValueError):
            parse_history(body, "H30588", date(2015, 2, 2), date(2015, 2, 3), expected_name="中证证券公司指数")


if __name__ == "__main__":
    unittest.main()
