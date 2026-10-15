from copy import deepcopy
from datetime import date
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import unittest

import numpy as np

from backend.analysis.sector_forecast import FEATURES, audit_fit, features, fit_period, predictions
from backend.analysis.sector_trend import trend_metrics
from backend.core.trading_calendar import get_calendar
from scripts.benchmark_sector_trend import rank_at
from scripts.research_sector_forecasts import audit_records, forecast_choices, training_records


class SectorForecastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.days = [day.isoformat() for day in get_calendar().sessions(date(2025, 1, 2), date(2026, 10, 14))]
        cls.codes = ["a", "b"]
        cls.records = [{"signal_date": cls.days[i], "exit_date": cls.days[i + 1], "ready_date": cls.days[i + 2],
            "codes": cls.codes, "features": [[(i + j + k) / 100 for k in range(len(FEATURES))] for j in range(2)],
            "outcomes": [[i / 1000, .001], [i / 1000 + .002, -.001]]} for i in range(25)]

    def test_24th_exit_on_current_date_is_not_yet_available(self):
        model, _, metadata = fit_period(self.records, self.days[24], self.codes)
        self.assertIsNone(model)
        self.assertEqual(metadata["distinct_dates"], 23)
        self.assertEqual(audit_fit(self.records, metadata, self.codes)["status"], "not_fitted")
        model, _, metadata = fit_period(self.records, self.days[25], self.codes)
        self.assertIsNotNone(model)
        self.assertEqual(metadata["distinct_dates"], 24)
        self.assertEqual(metadata["latest_label_end"], self.days[24])
        self.assertEqual(metadata["latest_ready_date"], self.days[25])
        self.assertEqual(audit_fit(self.records, metadata, self.codes)["status"], "passed")

    def test_unmatured_targets_and_features_cannot_affect_past_model(self):
        _, _, expected = fit_period(self.records, self.days[25], self.codes)
        changed = deepcopy(self.records)
        changed[-1]["features"] = [[math.nan] * len(FEATURES)] * 2
        changed[-1]["outcomes"] = [[math.inf, math.nan]] * 2
        _, _, actual = fit_period(changed, self.days[25], self.codes)
        self.assertEqual(actual, expected)

    def test_fit_audit_rejects_dates_counts_coefficients_and_feature_names(self):
        _, _, original = fit_period(self.records, self.days[25], self.codes)
        for field in ("coefficients", "scales", "observations", "features", "training_dates"):
            metadata = deepcopy(original)
            if field == "coefficients":
                metadata[field][0][0] += .1
            elif field == "scales":
                metadata[field][0] *= 2
            elif field == "observations":
                metadata[field] += 1
            elif field == "features":
                metadata[field][0] = "future_return"
            else:
                metadata[field].append(self.days[24])
            with self.subTest(field=field), self.assertRaises(ValueError):
                audit_fit(self.records, metadata, self.codes)

    def test_prediction_excess_is_centered_and_past_window_is_bounded(self):
        model, _, _ = fit_period(self.records, self.days[25], self.codes)
        values = predictions(model, [[0.] * len(FEATURES), [1.] * len(FEATURES)])
        self.assertAlmostEqual(values[:, 1].mean(), 0)
        old = deepcopy(self.records)
        for row in old:
            for key in ("signal_date", "exit_date", "ready_date"):
                row[key] = row[key].replace("2025", "2020")
        model, _, metadata = fit_period(old, self.days[25], self.codes)
        self.assertIsNone(model)
        self.assertEqual(metadata["distinct_dates"], 0)

    def test_descriptor_units_and_single_peer_rejection(self):
        entry = trend_metrics([100 * math.exp(.002 * i) for i in range(21)]) | {"relative_percentile": 75}
        vector = features(entry)
        self.assertEqual(len(vector), len(FEATURES))
        self.assertEqual(vector[-1], .75)
        self.assertAlmostEqual(vector[0], entry["return_pct"] / 100)
        with self.assertRaises(ValueError):
            features({**entry, "relative_percentile": None})

    def test_relative_winner_is_not_an_absolute_profit_and_count_control_matches(self):
        codes = ["a", "b", "c", "d"]
        entries = [{"code": code, "score": 80, "state": "steady_up", "return_pct": i}
                   for i, code in enumerate(codes)]
        values = np.asarray([[-.1, .1], [.001, .05], [.01, -.1], [.003, .01]])
        chosen = forecast_choices(entries, codes, values, values)
        self.assertEqual(chosen["ridge_top10"][0], "a")
        self.assertEqual(chosen["ridge_positive"], ["d"])
        self.assertEqual(chosen["positive_count_return"], ["d"])
        chosen = forecast_choices(entries, codes, values * [0, 1], values)
        self.assertEqual(chosen["ridge_positive"], [])
        self.assertEqual(chosen["positive_count_return"], [])

    def test_training_horizons_maturity_and_raw_label_tamper(self):
        boards = [{"code": code, "name": code, "collection_error": None,
                   "prices": {day: 100 * math.exp(.001 * (j + 1) * i) for i, day in enumerate(self.days)}}
                  for j, code in enumerate(self.codes)]
        for window in (20, 60, 120):
            records = training_records(boards, self.days, window,
                                       lambda i: rank_at(boards, self.days, i, window))
            self.assertTrue(records)
            first = records[0]
            self.assertEqual(first["exit_date"], self.days[2 * window + 1])
            self.assertEqual(first["ready_date"], self.days[2 * window + 2])
            self.assertEqual(audit_records(records, boards, self.days, window)["status"], "passed")
            changed = deepcopy(records)
            changed[0]["outcomes"][0][1] += .01
            with self.assertRaises(ValueError):
                audit_records(changed, boards, self.days, window)
            changed = deepcopy(records)
            changed[0]["ready_date"] = changed[0]["exit_date"]
            with self.assertRaises(ValueError):
                audit_records(changed, boards, self.days, window)

    @unittest.skipUnless(Path("runtime/verification/sector-forecast-research-v1.json.gz").exists(),
                         "Frozen sector forecast report unavailable")
    def test_real_maturity_controls_and_primary_labels_from_saved_prices(self):
        path = Path("runtime/verification/sector-forecast-research-v1.json.gz")
        self.assertEqual(sha256(path.read_bytes()).hexdigest(),
                         "cbe0019e34d37c10e4117d0a362452038d125c208db2676991c168ed73c3b501")
        with gzip.open(path, "rt") as stream:
            report = json.load(stream)
        with gzip.open("runtime/verification/sector-horizon-long-recovered-v1-reviewed.json.gz", "rt") as stream:
            reference = json.load(stream)
        self.assertFalse(report["investment_eligible"])
        self.assertFalse(report["production_ranking_changed"])
        inputs = report["input"]
        prices = {board["code"]: board["prices"] for board in inputs["boards"]}
        codes = list(prices)
        lookup = {day: i for i, day in enumerate(inputs["days"])}
        checked, cache = 0, {}

        def expected(selected, grid):
            key = (tuple(sorted(selected)), grid[0], grid[-1])
            if key not in cache:
                curve = [math.fsum(prices[code][day] / prices[code][grid[0]] for code in selected) / len(selected) for day in grid]
                peak, drawdown = 1., 0.
                for value in curve:
                    peak = max(peak, value)
                    drawdown = min(drawdown, value / peak - 1)
                cache[key] = ((curve[-1] - 1) * 100, drawdown * 100)
            return cache[key]

        for period, value in report["periods"].items():
            self.assertEqual(len(value["episodes"]), 9)
            window = value["forward_sessions"]
            records = value["training_records"]
            self.assertEqual(audit_records(records, inputs["boards"], inputs["days"], window)["status"], "passed")
            for batch, old in zip(value["cases"], reference["periods"][period]["cases"]):
                for method in ("return_top10", "trend_top10", "trend_gated"):
                    self.assertEqual(batch["methods"][method], old["methods"][method])
                metadata = batch["learning"]
                if metadata["status"] == "fitted":
                    self.assertLess(metadata["latest_label_end"], metadata["as_of"])
                    self.assertLessEqual(metadata["latest_ready_date"], metadata["as_of"])
                    self.assertGreaterEqual(metadata["distinct_dates"], 24)
                    self.assertEqual(batch["fit_validation"]["status"], "passed")
                else:
                    self.assertNotIn("predictions", batch)
                positive = batch["methods"]["ridge_positive"]
                control = batch["methods"]["positive_count_return"]
                self.assertEqual(len(positive["selected_codes"]), len(control["selected_codes"]))
                if not batch["primary"]:
                    continue
                for case in batch["methods"].values():
                    if case["status"] != "matured":
                        self.assertNotIn("selected_return_pct", case)
                        continue
                    start, end = lookup[case["entry_date"]], lookup[case["exit_date"]]
                    self.assertEqual(end - start, window)
                    grid = inputs["days"][start:end + 1]
                    selected, control_value = expected(case["selected_codes"], grid), expected(codes, grid)
                    for key, result in (("selected_return_pct", selected[0]), ("control_return_pct", control_value[0]),
                                        ("excess_return_pp", selected[0] - control_value[0]),
                                        ("selected_max_drawdown_pct", selected[1]), ("control_max_drawdown_pct", control_value[1])):
                        self.assertAlmostEqual(case[key], result, places=10)
                    checked += 1
        self.assertGreater(checked, 800)


if __name__ == "__main__":
    unittest.main()
