import math
import unittest

from backend.analysis.sector_trend import midpoint_percentiles, trend_metrics, trend_score
from scripts.benchmark_sector_trend import attach_smoothed_scores, rank_at, selections


class SectorTrendTests(unittest.TestCase):
    def score(self, prices, percentile=50):
        metrics = trend_metrics(prices)
        return metrics, trend_score(metrics, percentile)

    def test_steady_path_beats_larger_spike_then_pullback(self):
        steady = [100 * math.exp(.003 * i) for i in range(21)]
        spike = [100 + i for i in range(11)] + [140 - 2 * i for i in range(10)]
        calm, calm_score = self.score(steady, 40)
        reversal, reversal_score = self.score(spike, 90)
        self.assertGreater(reversal["return_pct"], calm["return_pct"])
        self.assertEqual(reversal["state"], "weakening")
        self.assertEqual(calm["state"], "steady_up")
        self.assertGreater(calm_score, reversal_score)

    def test_recovery_is_distinct_from_established_uptrend(self):
        prices = [100 - 2 * i for i in range(16)] + [72, 75, 78, 81, 84]
        metrics, _ = self.score(prices)
        self.assertEqual(metrics["state"], "recovery")
        self.assertLess(metrics["return_pct"], 0)
        self.assertGreater(metrics["recent_fitted_return_pct"], 0)

    def test_equal_endpoints_have_different_path_quality(self):
        steady = [100 + i / 2 for i in range(21)]
        noisy = [100] + [100 + i / 2 + (8 if i % 2 else -8) for i in range(1, 20)] + [110]
        calm, calm_score = self.score(steady)
        rough, rough_score = self.score(noisy)
        self.assertAlmostEqual(calm["return_pct"], rough["return_pct"])
        self.assertGreater(calm["r2"], rough["r2"])
        self.assertGreater(calm_score, rough_score)

    def test_flat_decline_and_scale_invariance(self):
        flat, score = self.score([100] * 21)
        self.assertEqual(flat["state"], "range")
        self.assertEqual(score, 50)
        declining = [100 * math.exp(-.003 * i) for i in range(21)]
        down, down_score = self.score(declining, 100)
        self.assertEqual(down["state"], "down")
        self.assertLess(down_score, 50)
        scaled, scaled_score = self.score([p * 1000 for p in declining], 100)
        self.assertEqual(scaled["state"], down["state"])
        self.assertAlmostEqual(down_score, scaled_score)

    def test_ties_and_missing_peers(self):
        self.assertEqual(midpoint_percentiles([3, 1, 3]), [75, 0, 75])
        self.assertEqual(midpoint_percentiles([1, 1]), [50, 50])
        self.assertEqual(midpoint_percentiles([1]), [None])
        metrics = trend_metrics([100 + i for i in range(21)])
        self.assertGreater(trend_score(metrics, None), 50)

    def test_invalid_prices_fail_without_filling(self):
        for bad in (0, -1, math.nan, math.inf):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                trend_metrics([100] * 20 + [bad])
        with self.assertRaises(ValueError):
            trend_metrics([100] * 20)

    def test_old_drawdown_does_not_automatically_mark_current_rise_volatile(self):
        prices = [100 * math.exp(-.012 * i) for i in range(16)]
        prices += [prices[-1] * math.exp(.006 * i) for i in range(1, 106)]
        metrics = trend_metrics(prices)
        self.assertLess(metrics["max_drawdown_pct"], -10)
        self.assertEqual(metrics["state"], "steady_up")
        self.assertEqual(metrics["recent_drawdown_pct"], 0)

    def test_future_changes_cannot_affect_signal_and_missing_grid_is_excluded(self):
        days = [f"session-{i:03}" for i in range(30)]
        board = {"code": "one", "name": "one", "collection_error": None,
                 "prices": {day: 100 + i for i, day in enumerate(days)}}
        expected = rank_at([board], days, 20, 20)
        board["prices"][days[21]] = 1000000
        self.assertEqual(expected, rank_at([board], days, 20, 20))
        del board["prices"][days[10]]
        self.assertEqual(rank_at([board], days, 20, 20), [])

    def test_relative_leaders_in_falling_market_do_not_pass_state_gate(self):
        days = [f"session-{i:03}" for i in range(21)]
        boards = [{"code": str(j), "name": str(j), "collection_error": None,
                   "prices": {day: 100 * math.exp(-(.001 + j * .001) * i) for i, day in enumerate(days)}} for j in range(12)]
        entries = rank_at(boards, days, 20, 20)
        attach_smoothed_scores(entries, {})
        self.assertEqual(selections(entries)["trend_gated"], [])
        self.assertEqual(len(selections(entries)["trend"]), 10)

    def test_smoothing_uses_only_previous_score_and_short_history_stays_unranked(self):
        entries = [{"code": "one", "score": 60}, {"code": "new", "score": 80}]
        self.assertEqual(attach_smoothed_scores(entries, {"one": 40}), {"one": 50, "new": 80})
        self.assertEqual(rank_at([], [], 10, 20), [])


if __name__ == "__main__":
    unittest.main()
