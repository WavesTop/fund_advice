from copy import deepcopy
from datetime import date, timedelta
import math
from pathlib import Path
import unittest

from scripts.diagnose_satellite_increment import aligned_curves, checked_episode, factorial_returns, matched_policy, paired_curves, paired_interval, run


def curves(a, b):
    start = date(2025, 12, 29)
    return [[{"date": (start + timedelta(days=i)).isoformat(), "equity": value}
             for i, value in enumerate(values)] for values in (a, b)]


class SatelliteIncrementTests(unittest.TestCase):
    def test_factorial_identity_not_sum_of_compounded_returns(self):
        result = factorial_returns([10., 15., 12., 14., 11.])
        self.assertEqual(result["original_trend_increment_pp"], 5)
        self.assertEqual(result["core_repair_increment_pp"], 2)
        self.assertEqual(result["repair_trend_interaction_pp"], -3)
        self.assertEqual(result["event_trend_increment_pp"], 2)
        self.assertEqual(result["event_trend_vs_inverse_pp"], 3)

    def test_invalid_returns_rejected(self):
        for values in ([1], [1, 2, 3, 4, math.nan], [1, 2, 3, 4, -100]):
            with self.assertRaises(ValueError):
                factorial_returns(values)

    def test_positive_increment_does_not_mean_positive_absolute_return(self):
        a, b = curves([100, 90], [100, 80])
        days, av, bv = aligned_curves(a, b)
        result = paired_interval(days, av, bv, 0, 1)
        self.assertAlmostEqual(result["trend_return_pct"], -10)
        self.assertAlmostEqual(result["increment_pp"], 10)
        self.assertAlmostEqual(result["relative_wealth_return_pct"], 12.5)

    def test_blocks_use_previous_close_and_keep_partial_tail(self):
        a, b = curves([100, 110, 121, 133.1, 146.41, 161.051], [100] * 6)
        result = paired_curves(a, b, 2)
        blocks = result["nonoverlapping_blocks"]
        self.assertEqual([row["return_intervals"] for row in blocks], [2, 2, 1])
        self.assertEqual([row["complete_block"] for row in blocks], [True, True, False])
        self.assertAlmostEqual(blocks[1]["trend_return_pct"], 21)
        self.assertEqual(blocks[1]["reference_date"], a[2]["date"])
        self.assertEqual(result["complete_blocks_trend_ahead"], 2)
        self.assertAlmostEqual(math.prod(1 + row["trend_return_pct"] / 100 for row in blocks), 1.61051)

    def test_annual_slice_carries_wealth_not_restarted_capital(self):
        a, b = curves([100, 110, 121, 133.1, 146.41], [100] * 5)
        result = paired_curves(a, b, 2)
        self.assertAlmostEqual(result["annual"]["2026"]["trend_return_pct"], 21)
        self.assertEqual(result["annual"]["2026"]["reference_date"], "2025-12-31")
        self.assertTrue(result["annual"]["2026"]["partial_calendar_year"])

    def test_relative_drawdown_is_not_portfolio_drawdown(self):
        a, b = curves([100, 110, 100], [100, 120, 120])
        result = paired_curves(a, b, 2)["full"]
        self.assertAlmostEqual(result["relative_wealth_max_drawdown_pct"], 100 / 6)

    def test_invalid_curve_dates_wealth_and_block_length_rejected(self):
        a, b = curves([100, 101], [100, 102])
        for value in (0, math.nan, math.inf, -1):
            invalid = deepcopy(a)
            invalid[1]["equity"] = value
            with self.assertRaises(ValueError):
                paired_curves(invalid, b, 2)
        invalid = deepcopy(b)
        invalid[1]["date"] = invalid[0]["date"]
        with self.assertRaises(ValueError):
            paired_curves(a, invalid, 2)
        for value in (0, True, 2.):
            with self.assertRaises(ValueError):
                paired_curves(a, b, value)

    def test_controller_cadence_or_repair_mismatch_rejected(self):
        core = {"name": "core", "exit_rule": "none", "check_sessions": 5, "risk_repair": "event"}
        trend = {**core, "name": "trend", "exit_rule": "fixed20"}
        matched_policy(trend, core)
        for key, value in (("check_sessions", 20), ("risk_repair", "immediate")):
            with self.assertRaises(ValueError):
                matched_policy({**trend, key: value}, core)

    def test_episode_is_rebuilt_and_forged_saved_return_rejected(self):
        a, b = curves([100, 110, 121], [100] * 3)
        coverage = {"start_date": a[1]["date"], "end_date": a[2]["date"]}
        result = checked_episode(a, b, coverage, {"total_return_pct": 21}, {"total_return_pct": 0})
        self.assertAlmostEqual(result["increment_pp"], 21)
        with self.assertRaises(ValueError):
            checked_episode(a, b, coverage, {"total_return_pct": 99}, {"total_return_pct": 0})

    @unittest.skipUnless(Path("runtime/verification/satellite-event-repair-research-v1.json.gz").exists(), "Frozen local report unavailable")
    def test_all_real_cohorts_and_scenarios_are_retained_without_new_replays(self):
        result = run()
        self.assertEqual(result["new_account_replays"], 0)
        self.assertEqual(set(result["cohorts"]), {"long4", "older4", "older9", "all13"})
        for cohort in result["cohorts"].values():
            self.assertEqual(len(cohort["stress"]), 6)
            self.assertEqual(len(cohort["fresh_entry"]), 4)
            self.assertEqual(len(cohort["episodes"]), 9)
            for comparison in cohort["main"].values():
                self.assertAlmostEqual(comparison["metrics"]["increment_pp"], comparison["full"]["increment_pp"], places=8)


if __name__ == "__main__":
    unittest.main()
