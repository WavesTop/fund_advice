from copy import deepcopy
import unittest

from backend.analysis.market_review_cadence import adaptive_review_indices, market_context
from scripts.research_market_regimes import carry_slice, effective_episode


class MarketCadenceTests(unittest.TestCase):
    def benchmark(self, prices):
        return {"days": [f"{i:04d}" for i in range(len(prices))], "prices": prices}

    def test_up_down_and_flat_are_distinct_past_only_contexts(self):
        for prices, expected in ((list(range(1, 301)), "up"), (list(range(400, 100, -1)), "down"), ([100.] * 300, "neutral")):
            benchmark = self.benchmark(prices)
            self.assertEqual(market_context(benchmark, ["0299"])[0]["state"], expected)
            self.assertEqual(market_context(benchmark, ["0100"])[0]["state"], "unknown")

    def test_future_reference_suffix_cannot_change_past_direction(self):
        benchmark = self.benchmark(list(range(1, 401)))
        changed = deepcopy(benchmark)
        changed["prices"][301:] = [p * 10 for p in changed["prices"][301:]]
        self.assertEqual(market_context(benchmark, ["0252", "0300"]), market_context(changed, ["0252", "0300"]))

    def test_adaptive_clock_uses_current_direction_and_keeps_initial_review(self):
        context = {i: {"state": "up" if i < 6 else "down"} for i in range(31)}
        reviews = adaptive_review_indices(context, 0, 30)
        self.assertEqual(reviews, frozenset({0, 6, 11, 16, 21, 26}))
        altered = deepcopy(context)
        for i in range(20, 31):
            altered[i]["state"] = "up"
        self.assertEqual({i for i in reviews if i < 20}, {i for i in adaptive_review_indices(altered, 0, 30) if i < 20})

    def test_unknown_context_and_missing_reference_do_not_silently_trade(self):
        with self.assertRaises(ValueError):
            adaptive_review_indices({0: {"state": "up"}, 1: {"state": "unknown"}}, 0, 1)
        with self.assertRaises(ValueError):
            market_context(self.benchmark([100.] * 300), ["9999"])

    def test_carry_slice_includes_first_day_loss_from_prior_equity(self):
        account = {"initial_equity": "100", "equity_curve": [
            {"date": "2024-01-02", "equity": "100"}, {"date": "2024-01-03", "equity": "90"},
            {"date": "2024-01-04", "equity": "100"}]}
        result = carry_slice(account, "2024-01-03", "2024-01-04")
        self.assertEqual(result["reference_date"], "2024-01-02")
        self.assertAlmostEqual(result["total_return_pct"], 0)
        self.assertAlmostEqual(result["max_drawdown_pct"], -10)

    def test_nonexistent_funds_and_partial_regime_history_are_explicit(self):
        days = [f"2024-{i:04d}" for i in range(400)]
        absent = effective_episode(days, {"start": "2020-01-01", "end": "2021-01-01"})
        self.assertEqual(absent["status"], "insufficient_shared_history")
        partial = effective_episode(days, {"start": "2020-01-01", "end": days[-1]})
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["start_index"], 252)


if __name__ == "__main__":
    unittest.main()
