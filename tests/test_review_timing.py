import math
import unittest

from scripts.diagnose_review_timing import compare_curves, continuous_year_returns


def account(values):
    return {"account": {"initial_equity": "100", "final_equity": str(values[-1][1]),
                        "equity_curve": [{"date": day, "equity": str(equity)} for day, equity in values]}}


class ReviewTimingTests(unittest.TestCase):
    def test_continuous_partial_year_returns_reconcile_with_terminal_wealth(self):
        data = account([("2024-07-01", 100), ("2024-12-31", 110), ("2025-12-31", 121)])
        years = continuous_year_returns(data["account"])
        self.assertAlmostEqual(years["2024"], .1)
        self.assertAlmostEqual(years["2025"], .1)
        self.assertAlmostEqual(math.prod(1 + value for value in years.values()), 1.21)

    def test_yearly_log_wealth_is_additive_not_sum_of_percentage_returns(self):
        base = account([("2024-07-01", 100), ("2024-12-31", 110), ("2025-12-31", 121)])
        other = account([("2024-07-01", 100), ("2024-12-31", 120), ("2025-12-31", 121)])
        compared = compare_curves(base, other)
        self.assertAlmostEqual(compared["final_relative_log_wealth"], 0)
        self.assertAlmostEqual(sum(v["relative_log_wealth"] for v in compared["annual_continuous"].values()), 0)
        self.assertGreater(compared["annual_continuous"]["2024"]["relative_log_wealth"], 0)
        self.assertLess(compared["annual_continuous"]["2025"]["relative_log_wealth"], 0)
        self.assertEqual(compared["largest_relative_equity_gap"]["date"], "2024-12-31")

    def test_missing_curve_date_and_unreconciled_terminal_balance_are_rejected(self):
        base = account([("2024-07-01", 100), ("2024-12-31", 110)])
        bad = account([("2024-07-01", 100), ("2024-12-30", 110)])
        with self.assertRaises(ValueError):
            compare_curves(base, bad)
        base["account"]["final_equity"] = "111"
        with self.assertRaises(ValueError):
            continuous_year_returns(base["account"])


if __name__ == "__main__":
    unittest.main()
