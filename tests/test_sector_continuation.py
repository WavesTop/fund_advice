from copy import deepcopy
from datetime import date
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import unittest

import numpy as np

from backend.analysis.sector_continuation import continuation_choices
from backend.analysis.sector_forecast import audit_fit, features, fit_period
from backend.analysis.sector_direction_screen import DIRECTION_FIELDS
from backend.core.trading_calendar import get_calendar
from scripts.benchmark_sector_trend import rank_at
from scripts.research_sector_continuation import NEW, OLD, QUALITY_FIELDS, audit_targets, retarget
from scripts.research_sector_forecasts import training_records

REPORT = Path("runtime/verification/sector-continuation-research-v1.json.gz")
REPORT_SHA256 = "430b4974f7914fdbb8f9fb207c5f015077981b169f41426473088e3c64aaccf3"


def entry(code, **values):
    return {"code": code, **dict.fromkeys(DIRECTION_FIELDS, 1.), "state": "steady_up", "daily_volatility_pct": 1., **values}


class SectorContinuationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.days = [day.isoformat() for day in get_calendar().sessions(date(2025, 1, 2), date(2026, 10, 14))]
        cls.boards = [{"code": code, "name": code, "collection_error": None,
                       "prices": {day: 100 * math.exp(.001 * (j + 1) * i) for i, day in enumerate(cls.days)}}
                      for j, code in enumerate("ab")]

    def records(self, boards, window):
        return training_records(boards, self.days, window, lambda i: rank_at(boards, self.days, i, window))

    def test_model_order_uses_full_pool_and_positive_gate_is_separate(self):
        entries = [entry(code, fast_ma_bias_pct=1 if code in "ab" else -1,
                         state="steady_up" if code in "ab" else "down") for code in "abcdefg"]
        values = dict(zip("abcdefg", [(0, 10), (5, 5), (10, 0), (6, 1), (7, 2), (8, 3), (9, 4)]))
        result = continuation_choices(entries, values, values)
        self.assertEqual(result["state_continuation"], ["b", "a"])
        self.assertEqual(result["state_positive_continuation"], ["b"])
        self.assertEqual(len(result["positive_count_state_return"]), 1)
        self.assertEqual(len(result["positive_count_state_low_vol"]), 1)
        self.assertEqual(len(result["positive_count_universe_return"]), 1)
        # Independently scaling raw outputs does not change either ordering.
        scaled = {code: [first * .001, second * 100] for code, (first, second) in values.items()}
        self.assertEqual(result, continuation_choices(entries, scaled, scaled))

    def test_empty_positive_lists_are_not_forced_fill_or_unrestricted_replacements(self):
        entries = [entry("up"), entry("down", state="down", return_pct=-1)]
        values = {"up": [-.1, -.1], "down": [.1, .1]}
        selected = continuation_choices(entries, values, values)
        self.assertEqual(selected["state_continuation"], ["up"])
        self.assertEqual(selected["continuation_top10"], ["down", "up"])
        for method in ("state_positive_continuation", "positive_count_state_return", "positive_count_state_low_vol", "positive_count_universe_return"):
            self.assertEqual(selected[method], [])
        entries[0]["state"] = "recovery"
        self.assertEqual(continuation_choices(entries, values, values)["state_continuation"], [])

    def test_top10_ties_finite_values_and_peer_membership(self):
        entries = [entry(f"{i:02d}") for i in range(15)]
        values = {row["code"]: [1., 1.] for row in entries}
        selected = continuation_choices(entries, values, values)
        self.assertTrue(all(len(codes) == 10 for codes in selected.values()))
        self.assertEqual(selected["state_continuation"], [f"{i:02d}" for i in range(10)])
        with self.assertRaises(ValueError):
            continuation_choices(entries, {}, values)
        with self.assertRaises(ValueError):
            continuation_choices(entries, {**values, "00": [math.nan, 1]}, values)
        with self.assertRaises(ValueError):
            continuation_choices(entries, values, {**values, "00": [1]})
        with self.assertRaises(ValueError):
            continuation_choices([entries[0], entries[0]], values, values)

    def test_direct_path_targets_preserve_past_features_and_all_three_maturity_clocks(self):
        for window in (20, 60, 120):
            old = self.records(self.boards, window)
            records = retarget(old, self.boards, self.days, window)
            self.assertEqual(audit_targets(records, old, self.boards, self.days, window)["status"], "passed")
            self.assertEqual(records[0]["features"], old[0]["features"])
            self.assertEqual(records[0]["ready_date"], old[0]["ready_date"])
            np.testing.assert_allclose(records[0]["outcomes"], [[.5, 1.], [.5, 1.]], rtol=0, atol=1e-12)
            forged = deepcopy(records)
            forged[0]["outcomes"][0][0] = -.5
            with self.assertRaises(ValueError):
                audit_targets(forged, old, self.boards, self.days, window)
            forged = deepcopy(records)
            forged[0]["features"][0][0] += 1
            with self.assertRaises(ValueError):
                audit_targets(forged, old, self.boards, self.days, window)
            forged = deepcopy(records)
            forged[0]["ready_date"] = forged[0]["exit_date"]
            with self.assertRaises(ValueError):
                audit_targets(forged, old, self.boards, self.days, window)

    def test_terminal_spike_is_not_a_sustained_label_and_flat_is_not_positive(self):
        window = 20
        boards = deepcopy(self.boards)
        for board in boards:
            board["prices"] = dict.fromkeys(self.days, 100.)
        original = self.records(boards, window)
        first = self.days.index(original[0]["signal_date"]) + 1
        for i, day in enumerate(self.days[first:first + window + 1]):
            boards[0]["prices"][day] = 100 if i == 0 else 110 if i == window else 99
        # Regenerate past descriptors so the producer only retargets outcomes.
        original = self.records(boards, window)
        records = retarget(original, boards, self.days, window)
        self.assertEqual(records[0]["outcomes"][0][0], -.5)
        self.assertGreater(records[0]["outcomes"][0][1], 0)
        self.assertEqual(records[0]["outcomes"][1], [-.5, 0.])
        self.assertEqual(audit_targets(records, original, boards, self.days, window)["status"], "passed")

    def test_future_prices_cannot_change_visible_training_or_absolute_predictions(self):
        window, at = 20, 300
        records = retarget(self.records(self.boards, window), self.boards, self.days, window)
        model, _, expected = fit_period(records, self.days[at], ["a", "b"])
        self.assertIsNotNone(model)
        changed = deepcopy(self.boards)
        for j, board in enumerate(changed):
            for day in self.days[at + 1:]:
                board["prices"][day] *= 1000 if j else .001
        changed_records = retarget(self.records(changed, window), changed, self.days, window)
        altered, _, actual = fit_period(changed_records, self.days[at], ["a", "b"])
        self.assertEqual(expected, actual)
        matrix = [features(row) for row in rank_at(self.boards, self.days, at, window)]
        np.testing.assert_array_equal(model.predict(matrix), altered.predict(matrix))
        # These absolute path scores must not be recentered to erase their sign.
        np.testing.assert_allclose(model.predict(matrix), [[.5, 1.], [.5, 1.]], atol=1e-12, rtol=0)

    @unittest.skipUnless(REPORT.exists(), "Frozen continuation report unavailable")
    def test_frozen_training_fits_predictions_selections_and_controls(self):
        self.assertEqual(sha256(REPORT.read_bytes()).hexdigest(), REPORT_SHA256)
        with gzip.open(REPORT, "rt") as stream:
            report = json.load(stream)
        with gzip.open(report["protocol"]["reference"], "rt") as stream:
            reference = json.load(stream)
        with gzip.open(report["protocol"]["training_reference"], "rt") as stream:
            training = json.load(stream)
        for path, source in report["source_snapshots"].items():
            self.assertEqual(sha256(source.encode()).hexdigest(), report["source_hashes"][path])
        self.assertFalse(report["investment_eligible"])
        self.assertFalse(report["production_ranking_changed"])
        boards, days = report["input"]["boards"], report["input"]["days"]
        indexes, codes = {day: i for i, day in enumerate(days)}, [board["code"] for board in boards]
        fitted = 0
        for period, value in report["periods"].items():
            window = value["forward_sessions"]
            self.assertEqual(len(value["episodes"]), 9)
            self.assertEqual(audit_targets(value["training_records"], training["periods"][period]["training_records"], boards, days, window), value["record_validation"])
            self.assertEqual(len(value["cases"]), len(reference["periods"][period]["cases"]))
            for case, old, prior in zip(value["cases"], reference["periods"][period]["cases"], training["periods"][period]["cases"]):
                for method in OLD:
                    self.assertEqual(case["methods"][method], old["methods"][method])
                for field in ("training_dates", "as_of", "distinct_dates", "status"):
                    self.assertEqual(case["learning"][field], prior["learning"][field])
                audit_fit(value["training_records"], case["learning"], codes)
                if "predictions" not in case:
                    continue
                self.assertEqual(case["fit_validation"]["status"], "passed")
                self.assertLess(case["learning"]["latest_label_end"], case["as_of"])
                entries = rank_at(boards, days, indexes[case["as_of"]], window)
                x = np.asarray([features(entry) for entry in entries])
                meta = case["learning"]
                predicted = np.clip((x - meta["means"]) / np.asarray(meta["scales"]), -5, 5) @ meta["coefficients"] + meta["intercept"]
                np.testing.assert_allclose(predicted, [case["predictions"][code] for code in codes], rtol=0, atol=1e-12)
                selected = continuation_choices(entries, case["predictions"], case["historical_predictions"])
                for method, chosen in selected.items():
                    self.assertEqual(case["methods"][method]["selected_codes"], chosen)
                count = len(selected["state_positive_continuation"])
                for method in ("positive_count_state_return", "positive_count_state_low_vol", "positive_count_universe_return"):
                    self.assertEqual(len(selected[method]), count)
                fitted += 1
        self.assertEqual(fitted, 1001)
        self.assertIsNone(report["periods"]["medium"]["primary"]["methods"]["state_positive_continuation"]["mean_selected_return_pct"])

    @unittest.skipUnless(REPORT.exists(), "Frozen continuation report unavailable")
    def test_primary_price_baskets_and_persistence_labels_independently_rebuild(self):
        with gzip.open(REPORT, "rt") as stream:
            report = json.load(stream)
        boards, days = report["input"]["boards"], report["input"]["days"]
        prices = {board["code"]: board["prices"] for board in boards}
        indexes = {day: i for i, day in enumerate(days)}
        checked = 0
        for value in report["periods"].values():
            window = value["forward_sessions"]
            for case in value["cases"]:
                for method in NEW:
                    batch = case["methods"][method]
                    if batch["status"] != "matured":
                        self.assertNotIn("selected_return_pct", batch)
                        for field in QUALITY_FIELDS:
                            self.assertNotIn(field, batch)
                if not case["primary"]:
                    continue
                for batch in case["methods"].values():
                    if batch["status"] != "matured":
                        continue
                    start, end = indexes[batch["entry_date"]], indexes[batch["exit_date"]]
                    self.assertEqual(end - start, window)
                    paths = [[prices[code][day] / prices[code][days[start]] for day in days[start:end + 1]] for code in batch["selected_codes"]]
                    curve = [math.fsum(path[i] for path in paths) / len(paths) for i in range(window + 1)]
                    peak, drawdown = 1., 0.
                    for point in curve:
                        peak = max(peak, point)
                        drawdown = min(drawdown, point / peak - 1)
                    self.assertAlmostEqual((curve[-1] - 1) * 100, batch["selected_return_pct"], places=9)
                    self.assertAlmostEqual(drawdown * 100, batch["selected_max_drawdown_pct"], places=9)
                    occupancy = [(sum(p > 1 for p in path[1:]) + .5 * sum(p == 1 for p in path[1:])) / window for path in paths]
                    persistent = [path[-1] > 1 and fraction > .5 for path, fraction in zip(paths, occupancy)]
                    self.assertAlmostEqual(sum(persistent) / len(paths) * 100, batch["selected_persistent_up_fraction_pct"], places=10)
                    checked += 1
        self.assertGreater(checked, 900)


if __name__ == "__main__":
    unittest.main()
