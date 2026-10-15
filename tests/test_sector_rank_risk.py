from copy import deepcopy
from datetime import date
from decimal import Decimal
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import unittest

import numpy as np

from backend.analysis.sector_forecast import fit_period
from backend.analysis.sector_rank_risk import centered_predictions, choices, path_outcomes, rank_targets
from backend.core.trading_calendar import get_calendar
from scripts.benchmark_sector_trend import rank_at
from scripts.research_sector_forecasts import training_records
from scripts.research_sector_rank_risk import audit_targets, comparison, retarget


class SectorRankRiskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.days = [day.isoformat() for day in get_calendar().sessions(date(2025, 1, 2), date(2026, 10, 14))]
        cls.boards = [{"code": code, "name": code, "collection_error": None,
                       "prices": {day: 100 * math.exp(.001 * (j + 1) * i) for i, day in enumerate(cls.days)}}
                      for j, code in enumerate(("a", "b"))]

    def records(self, boards, window):
        return training_records(boards, self.days, window, lambda i: rank_at(boards, self.days, i, window))

    def test_exact_price_path_and_return_ties_do_not_hide_drawdown(self):
        result = path_outcomes([100, 120, 90, 110])
        self.assertEqual(result, (Decimal(".1"), Decimal("-.25")))
        targets = rank_targets([result[0], Decimal(".1")], [result[1], Decimal("0")])
        np.testing.assert_array_equal(targets, [[0, -.5], [0, .5]])
        self.assertEqual(path_outcomes([10, 12, 9, 11]), result)

    def test_midpoint_ties_and_safer_direction_are_centered(self):
        targets = rank_targets([.1, .2, .2], [-.1, -.1, -.3])
        np.testing.assert_array_equal(targets, [[-.5, .25], [.25, .25], [.25, -.5]])
        np.testing.assert_allclose(targets.mean(axis=0), 0)
        for bad in (0, -1, math.nan, math.inf):
            with self.assertRaises(ValueError):
                path_outcomes([100, bad])
        with self.assertRaises(ValueError):
            rank_targets([.1], [-.1])

    def test_maximin_is_scale_invariant_and_gate_does_not_force_fill(self):
        codes = ["a", "b", "c"]
        entries = [{"code": code, "return_pct": 1, "recent_fitted_return_pct": 1,
                    "daily_volatility_pct": i + 1} for i, code in enumerate(codes)]
        predicted = np.asarray([[.4, -.4], [.1, .1], [-.4, .4]])
        result = choices(entries, codes, predicted, np.zeros((3, 2)))
        self.assertEqual(result["maximin_top10"][0], "b")
        self.assertEqual(result["upward_quality"], ["b"])
        self.assertEqual(len(result["upward_count_return"]), 1)
        scaled = choices(entries, codes, predicted * [1000, .001], np.zeros((3, 2)))
        self.assertEqual(result["maximin_top10"], scaled["maximin_top10"])
        changed = [{**entry, "return_pct": -1} for entry in entries]
        self.assertEqual(choices(changed, codes, predicted, predicted)["upward_quality"], [])
        with self.assertRaises(ValueError):
            choices(entries, ["a", "a", "c"], predicted, predicted)

    def test_retargeting_preserves_all_past_features_maturity_and_rejects_forged_ranks(self):
        for window in (20, 60, 120):
            original = self.records(self.boards, window)
            ranked = retarget(original, self.boards, self.days, window)
            self.assertEqual(audit_targets(ranked, original, self.boards, self.days, window)["status"], "passed")
            self.assertEqual(ranked[0]["features"], original[0]["features"])
            self.assertEqual(ranked[0]["ready_date"], original[0]["ready_date"])
            altered = deepcopy(ranked)
            altered[0]["outcomes"][0][1] += .01
            with self.assertRaises(ValueError):
                audit_targets(altered, original, self.boards, self.days, window)

    def test_future_price_changes_cannot_change_fitted_ordinal_model(self):
        window, index = 20, 300
        original = self.records(self.boards, window)
        ranked = retarget(original, self.boards, self.days, window)
        model, _, expected = fit_period(ranked, self.days[index], ["a", "b"])
        self.assertIsNotNone(model)
        changed = deepcopy(self.boards)
        for j, board in enumerate(changed):
            for day in self.days[index + 1:]:
                board["prices"][day] *= 1000 if j else .001
        altered = retarget(self.records(changed, window), changed, self.days, window)
        _, _, actual = fit_period(altered, self.days[index], ["a", "b"])
        self.assertEqual(expected, actual)
        centered = centered_predictions(model, [[0.] * 11, [1.] * 11])
        np.testing.assert_allclose(centered.mean(axis=0), 0, atol=1e-12)

    def test_joint_comparison_requires_better_return_and_lower_basket_drawdown(self):
        left = {"status": "matured", "selected_codes": ["a"], "selected_return_pct": 5, "selected_max_drawdown_pct": -4}
        right = {"status": "matured", "selected_codes": ["b"], "selected_return_pct": 3, "selected_max_drawdown_pct": -6}
        result = comparison([{"methods": {"left": left, "right": right}}], "left", "right")
        self.assertEqual(result["mean_delta_pp"], 2)
        self.assertEqual(result["mean_drawdown_magnitude_delta_pp"], -2)
        self.assertEqual(result["joint_return_and_drawdown_improvements"], 1)

    @unittest.skipUnless(Path("runtime/verification/sector-rank-risk-research-v1.json.gz").exists(),
                         "Frozen ordinal sector report unavailable")
    def test_frozen_targets_controls_and_primary_price_baskets(self):
        path = Path("runtime/verification/sector-rank-risk-research-v1.json.gz")
        self.assertEqual(sha256(path.read_bytes()).hexdigest(),
                         "78ba73766f907838b94a307c54969ce7f3f2ffe96af4292d211d3aeae1024d55")
        with gzip.open(path, "rt") as stream:
            report = json.load(stream)
        with gzip.open("runtime/verification/sector-forecast-research-v1.json.gz", "rt") as stream:
            reference = json.load(stream)
        self.assertFalse(report["investment_eligible"])
        self.assertFalse(report["production_ranking_changed"])
        inputs = report["input"]
        lookup = {day: i for i, day in enumerate(inputs["days"])}
        prices = {board["code"]: board["prices"] for board in inputs["boards"]}
        cache, checked = {}, 0

        def expected(codes, grid):
            key = (tuple(sorted(codes)), grid[0], grid[-1])
            if key not in cache:
                curve = [math.fsum(prices[code][day] / prices[code][grid[0]] for code in codes) / len(codes) for day in grid]
                peak, drawdown = 1., 0.
                for value in curve:
                    peak = max(peak, value)
                    drawdown = min(drawdown, value / peak - 1)
                cache[key] = ((curve[-1] - 1) * 100, drawdown * 100)
            return cache[key]

        for period, value in report["periods"].items():
            old = reference["periods"][period]
            window = value["forward_sessions"]
            self.assertEqual(len(value["episodes"]), 9)
            audit_targets(value["training_records"], old["training_records"], inputs["boards"], inputs["days"], window)
            for batch, previous in zip(value["cases"], old["cases"]):
                self.assertEqual(batch["methods"]["return_top10"], previous["methods"]["return_top10"])
                self.assertEqual(batch["methods"]["trend_gated"], previous["methods"]["trend_gated"])
                self.assertEqual(batch["methods"]["magnitude_ridge_top10"], previous["methods"]["ridge_top10"])
                self.assertEqual(batch["learning"]["training_dates"], previous["learning"]["training_dates"])
                if batch["learning"]["status"] == "fitted":
                    self.assertLess(batch["learning"]["latest_label_end"], batch["learning"]["as_of"])
                    self.assertEqual(batch["fit_validation"]["status"], "passed")
                self.assertEqual(len(batch["methods"]["upward_quality"]["selected_codes"]),
                                 len(batch["methods"]["upward_count_return"]["selected_codes"]))
                if not batch["primary"]:
                    continue
                for case in batch["methods"].values():
                    if case["status"] != "matured":
                        self.assertNotIn("selected_return_pct", case)
                        continue
                    start, end = lookup[case["entry_date"]], lookup[case["exit_date"]]
                    self.assertEqual(end - start, window)
                    grid = inputs["days"][start:end + 1]
                    selected, control = expected(case["selected_codes"], grid), expected(list(prices), grid)
                    for key, actual in (("selected_return_pct", selected[0]), ("control_return_pct", control[0]),
                                        ("excess_return_pp", selected[0] - control[0]),
                                        ("selected_max_drawdown_pct", selected[1]), ("control_max_drawdown_pct", control[1])):
                        self.assertAlmostEqual(case[key], actual, places=10)
                    checked += 1
        self.assertGreater(checked, 900)


if __name__ == "__main__":
    unittest.main()
