from copy import deepcopy
import unittest

from scripts.diagnose_satellite_selection import compare_returns, distribution, summarize


class SatelliteSelectionSummaryTests(unittest.TestCase):
    def test_returns_ties_and_percentile_not_winner_selection(self):
        observed = compare_returns([10., 20., 30., 20.], 20.)
        self.assertEqual(observed["random_below_trend"], 1)
        self.assertEqual(observed["random_above_trend"], 1)
        self.assertEqual(observed["ties"], 2)
        self.assertEqual(observed["observed_trend_path_percentile"], 50.)
        self.assertEqual(observed["trend_minus_random_median_pp"], 0.)
        self.assertEqual(distribution([10., 20., 30.])["mean"], 20.)

    def test_drawdown_absolute_and_all_seeds_retained(self):
        trials = [{"seed": i, "metrics": {"total_return_pct": r, "max_drawdown_pct": -d,
                    "realized_annual_volatility_pct": 8., "mean_market_exposure_pct": 95.}}
                  for i, r, d in ((1, 10., 5.), (2, 20., 10.))]
        trend = {"metrics": {"total_return_pct": 15.}}
        result = summarize(trials, trend, {"core": trend})
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["distributions"]["max_drawdown_pct"]["median"], 7.5)
        self.assertEqual(result["random_above_controls"]["core"], 1)
        changed = deepcopy(trials); changed[1]["seed"] = 1
        with self.assertRaises(ValueError):
            summarize(changed, trend, {})

    def test_empty_or_nonfinite_distributions_rejected(self):
        for values in ([], [float("nan")], [float("inf")]):
            with self.assertRaises(ValueError):
                distribution(values)
        with self.assertRaises(ValueError):
            compare_returns([1., 2.], float("nan"))


if __name__ == "__main__":
    unittest.main()
