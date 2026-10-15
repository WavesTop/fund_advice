from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backend.analysis.trend_risk_budget import budget_statistics
from backend.analysis.fund_holding_costs import FundHoldingCosts
from backend.analysis.valuation_trend_budget import ValuationHistory, valuation_budget_target
from backend.core.trading_calendar import TradingCalendar
from scripts.collect_index_valuation import parse_history
from scripts.research_valuation_trend_budget import audit_valuation_decision
from tests import test_macro_trend_budget as fixtures

ROOT = Path(__file__).resolve().parents[1]


class ValuationTrendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = TradingCalendar(json.loads((ROOT / "config/research-exchange-calendar-2009-2026-v2.json").read_text()))
        cls.days = [d.isoformat() for d in cls.calendar.sessions(date(2009, 1, 1), date(2015, 12, 31))]

    def source(self, length=600):
        return {c: {"rows": [{"date": d, "close": 1000., "peg": 20.} for d in self.days[:length]]} for c in ("000300", "000905")}

    def history(self, source):
        return ValuationHistory(source, self.calendar, cutoff=max(r["date"] for i in source.values() for r in i["rows"]))

    def test_lag_minimum_reference_and_midpoint_ties(self):
        source = self.source()
        history = self.history(source)
        self.assertEqual(history.state(self.days[252])["status"], "unavailable")
        state = history.state(self.days[253])
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["percentile"], .5)
        self.assertEqual(state["indices"]["000300"]["quote_date"], self.days[252])
        self.assertEqual(state["indices"]["000300"]["reference_count"], 252)
        for item in source.values():
            item["rows"][252]["peg"] = 30.
        changed = self.history(source)
        self.assertEqual(changed.state(self.days[253])["percentile"], 1.)
        self.assertEqual(changed.state(self.days[253], lag_sessions=2)["status"], "unavailable")

    def test_future_values_do_not_change_asof_signal(self):
        source = self.source()
        old = self.history(source).state(self.days[320])
        for item in source.values():
            for row in item["rows"]:
                if row["date"] > self.days[320]:
                    row["peg"] = 1000.
        self.assertEqual(old, self.history(source).state(self.days[320]))

    def test_missing_data_and_stale_quote_are_not_interpolated(self):
        source = self.source()
        for item in source.values():
            for index in range(298, 301):
                item["rows"][index]["peg"] = None
        state = self.history(source).state(self.days[301])
        self.assertEqual(state["indices"]["000300"]["quote_date"], self.days[297])
        for item in source.values():
            item["rows"][297]["peg"] = None
        self.assertEqual(self.history(source).state(self.days[301])["indices"]["000300"]["status"], "unavailable")
        sparse = self.source()
        for item in sparse.values():
            for index in range(100, 170):
                item["rows"][index]["peg"] = None
        state = self.history(sparse).state(self.days[400])
        self.assertGreater(state["indices"]["000300"]["reference_count"], 252)
        self.assertEqual(state["status"], "unavailable")

    def test_reference_window_is_bounded_and_excludes_current_quote(self):
        source = self.source(length=1600)
        state = self.history(source).state(self.days[1401])
        entry = state["indices"]["000300"]
        self.assertEqual(entry["reference_count"], 1260)
        self.assertEqual(entry["reference_start"], self.days[1400 - 1260])
        self.assertEqual(entry["reference_end"], self.days[1399])

    def test_non_sessions_and_invalid_source_rows(self):
        source = self.source()
        source["000300"]["rows"].append({"date": "2009-01-01", "peg": 25.})
        history = self.history(source)
        self.assertIn("2009-01-01", history.coverage["000300"]["excluded_non_sessions"])
        for value in (float("nan"), 0., -1., True):
            broken = self.source()
            broken["000300"]["rows"][0]["peg"] = value
            with self.assertRaises(ValueError):
                self.history(broken)
        broken = self.source()
        broken["000300"]["rows"].append(broken["000300"]["rows"][0])
        with self.assertRaises(ValueError):
            self.history(broken)
        with self.assertRaises(ValueError):
            history.state("2009-01-01")

    def test_budget_rules_and_unknown_fallback(self):
        data = fixtures.fixture()
        stats = budget_statistics(data, 120)
        state = {"signal_date": data["days"][120], "status": "ready", "percentile": .1}
        positive = stats | {"quality": .5}
        negative = stats | {"quality": -.5}
        for name, current, context, desired in (("value_vol10", positive, state, .56),
                                                ("value_confirmed_vol10", positive, state, .5),
                                                ("value_confirmed_vol10", negative, state, .4),
                                                ("value_confirmed_vol10", negative, state | {"percentile": .9}, .3),
                                                ("value_hybrid_vol10", positive, state, (.56 + .475) / 2),
                                                ("value_vol10", positive, state | {"status": "unavailable"}, .4),
                                                ("value_hybrid_vol10", positive, state | {"status": "unavailable"}, .475)):
            target, meta = valuation_budget_target(data, name, 120, (), context, stats=current)
            self.assertAlmostEqual(meta["proposed_stock_budget"], desired)
            self.assertLessEqual(meta["projected_volatility"], .1 + 1e-10)
            self.assertLessEqual(sum(target.weights.values()), 1 + 1e-12)

    def test_independent_rebuild_rejects_future_quote_and_tampered_percentile(self):
        source = self.source()
        history = self.history(source)
        state = history.state(self.days[320])
        maps = {c: {r["date"]: r["peg"] for r in x["rows"]} for c, x in source.items()}
        entry = {"signal_date": self.days[320], "valuation": state, "quality": .5, "proposed_stock_budget": .4}
        audit_valuation_decision(entry, maps, history.days, "value_vol10")
        for damaged in (entry | {"valuation": state | {"percentile": float("nan")}}, entry | {"proposed_stock_budget": .6}):
            with self.assertRaises(ValueError):
                audit_valuation_decision(damaged, maps, history.days, "value_vol10")
        damaged = deepcopy(entry)
        damaged["valuation"]["indices"]["000300"]["quote_date"] = self.days[400]
        with self.assertRaises(ValueError):
            audit_valuation_decision(damaged, maps, history.days, "value_vol10")

    def test_research_calendar_correction_preserves_original(self):
        from scripts.research_fund_rotation import CALENDAR
        self.assertEqual(CALENDAR.name, "research-exchange-calendar-2009-2026-v2.json")
        old = TradingCalendar(json.loads((ROOT / "config/research-exchange-calendar-2009-2026.json").read_text()))
        self.assertTrue(old.is_session(date(2018, 12, 31)))
        self.assertFalse(self.calendar.is_session(date(2018, 12, 31)))
        self.assertEqual(self.calendar.next_session(date(2018, 12, 28)), date(2019, 1, 2))
        removed = set(old.sessions(date(2009, 1, 1), date(2026, 10, 12))) - set(self.calendar.sessions(date(2009, 1, 1), date(2026, 10, 12)))
        self.assertEqual(removed, {date(2018, 12, 31)})
        costs = FundHoldingCosts().redemption("2018-12-28", "2019-01-04", Decimal(10), self.calendar)
        self.assertEqual(costs["purchase_confirmation_date"], "2019-01-02")
        self.assertEqual(costs["redemption_confirmation_date"], "2019-01-07")
        self.assertEqual(costs["holding_calendar_days"], 5)
        self.assertEqual(costs["sell_fee_bps"], "150")

    def test_fund_loader_excludes_yearend_valuation_as_a_trading_date(self):
        from scripts.research_fund_rotation import load_funds
        source = {"status": "available", "code": "one", "theme": "other", "role": "sector", "name": "TestC",
                  "fee_schedule_status": "scenario", "rows": [{"date": day, "unit_nav": value, "accumulated_nav": value}
                                                              for day, value in (("2018-12-28", "1"), ("2018-12-31", "2"), ("2019-01-02", "3"))]}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text(json.dumps({"items": [source]}))
            funds, _ = load_funds(path, self.calendar)
        self.assertEqual(set(funds["one"]["rows"]), {"2018-12-28", "2019-01-02"})

    def test_structured_history_parser_identity_dates_and_values(self):
        record = {"indexCode": "000300", "indexNameCnAll": "沪深300指数", "tradeDate": "20240102", "close": 3300., "peg": 11.}
        payload = {"code": "200", "success": True, "data": [record]}
        parsed = parse_history(json.dumps(payload).encode(), "000300", date(2024, 1, 1), date(2024, 1, 3))
        self.assertEqual(parsed[0]["peg"], 11.)
        for bad in (record | {"indexCode": "000905"}, record | {"tradeDate": "20230102"}, record | {"close": 0.}, record | {"peg": float("nan")}):
            with self.assertRaises(ValueError):
                parse_history(json.dumps(payload | {"data": [bad]}).encode(), "000300", date(2024, 1, 1), date(2024, 1, 3))
        with self.assertRaises(ValueError):
            parse_history(json.dumps(payload | {"data": [record, record]}).encode(), "000300", date(2024, 1, 1), date(2024, 1, 3))


if __name__ == "__main__":
    unittest.main()
