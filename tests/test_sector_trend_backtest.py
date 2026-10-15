from datetime import date, timedelta
from decimal import Decimal
import unittest

from scripts.backtest_sector_trend import signals, simulate


class SectorTrendBacktestTests(unittest.TestCase):
    def fixture(self, prices, state="steady_up"):
        days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(len(prices))]
        boards = [{"code": "one", "name": "one", "collection_error": None, "prices": dict(zip(days, prices))}]
        entry = {"code": "one", "name": "one", "score": 70, "state": state, "return_pct": 10}
        return days, boards, entry

    def test_signal_day_jump_is_not_earned_before_next_close_execution(self):
        days, boards, entry = self.fixture([100, 200, 220, 242])
        result = simulate(boards, days, {1: [entry]}, method="gated_full", start=0, fee_bps=Decimal(0))
        self.assertAlmostEqual(result["total_return_pct"], 21)
        self.assertAlmostEqual(float(result["final_equity"]), 121000)

    def test_compounding_and_boundary_dates_are_not_duplicated(self):
        days, boards, entry = self.fixture([100, 100, 110, 121, 133.1])
        result = simulate(boards, days, {1: [entry], 3: [entry]}, method="gated_full", start=0, fee_bps=Decimal(0))
        self.assertAlmostEqual(result["total_return_pct"], 33.1)
        self.assertEqual([p["date"] for p in result["equity_curve"]], days)
        for point in result["equity_curve"]:
            self.assertAlmostEqual(float(point["equity"]), float(point["cash"]) + float(point["market_value"]))

    def test_buy_and_sell_costs_and_initial_fee_are_counted(self):
        days, boards, entry = self.fixture([100, 100, 100])
        result = simulate(boards, days, {1: [entry]}, method="gated_full", start=0, fee_bps=Decimal(10))
        expected = 100000 / 1.001 * .999
        self.assertAlmostEqual(float(result["final_equity"]), expected)
        self.assertAlmostEqual(float(result["total_fees"]), 100000 - expected)
        self.assertAlmostEqual(result["max_drawdown_pct"], (expected / 100000 - 1) * 100)

    def test_empty_candidates_keep_cash_and_single_slot_limits_exposure(self):
        days, boards, entry = self.fixture([100, 100, 120])
        empty = simulate(boards, days, {1: []}, method="gated_slots", start=0)
        self.assertEqual(empty["total_return_pct"], 0)
        self.assertEqual(Decimal(empty["total_fees"]), 0)
        small = simulate(boards, days, {1: [entry]}, method="gated_slots", start=0, fee_bps=Decimal(0))
        self.assertAlmostEqual(small["total_return_pct"], 2)

    def test_future_prices_do_not_change_past_signal(self):
        days, boards, _ = self.fixture([100 + i for i in range(45)])
        expected = signals(boards, days, 20, 20)[21]
        boards[0]["prices"][days[21]] = 1000000
        self.assertEqual(expected, signals(boards, days, 20, 20)[21])

    def test_period_drawdown_uses_portfolio_path_and_weak_candidates_are_excluded(self):
        days, boards, entry = self.fixture([100, 100, 50, 100], state="weakening")
        gated = simulate(boards, days, {1: [entry]}, method="gated_full", start=0, fee_bps=Decimal(0))
        self.assertEqual(gated["total_return_pct"], 0)
        ungated = simulate(boards, days, {1: [entry]}, method="trend", start=0, fee_bps=Decimal(0))
        self.assertAlmostEqual(ungated["max_drawdown_pct"], -5)
        self.assertEqual(ungated["total_return_pct"], 0)

    def test_exposure_benchmark_matches_cash_budget_without_selecting_winners(self):
        days, boards, entry = self.fixture([100, 100, 120])
        second = {**boards[0], "code": "two", "prices": {day: 100 for day in days}}
        other = {**entry, "code": "two", "state": "weakening"}
        result = simulate(boards + [second], days, {1: [entry, other]}, method="gated_exposure_benchmark", start=0, fee_bps=Decimal(0))
        self.assertAlmostEqual(result["total_return_pct"], 1)
        self.assertEqual(Decimal(result["holding_cycles"][0]["reserved_cash"]), 90000)

    def test_fractional_equal_allocations_cannot_create_negative_cash(self):
        days, boards, entry = self.fixture([100, 100, 110])
        boards = [{**boards[0], "code": str(i)} for i in range(3)]
        entries = [{**entry, "code": str(i)} for i in range(3)]
        result = simulate(boards, days, {1: entries}, method="gated_full", start=0, fee_bps=Decimal(0))
        self.assertEqual(Decimal(result["holding_cycles"][0]["reserved_cash"]), Decimal(".01"))
        self.assertTrue(all(Decimal(point["cash"]) >= 0 for point in result["equity_curve"]))


if __name__ == "__main__":
    unittest.main()
