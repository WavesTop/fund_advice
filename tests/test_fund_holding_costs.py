from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import unittest

from backend.analysis.fund_holding_costs import FundHoldingCosts
from backend.analysis.sector_rotation import RotationPolicy, entry_ready, exit_reason, market_ready, simulate_rotation
from backend.core.trading_calendar import TradingCalendar
from scripts.validate_sector_rotation import validate_ledger

ROOT = Path(__file__).resolve().parents[1]


class FundHoldingCostsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = TradingCalendar(json.loads((ROOT / "config/research-exchange-calendar-2009-2026.json").read_text()))

    def test_calendar_day_boundary_and_fee_is_replaced_not_added(self):
        costs = FundHoldingCosts(confirmation_lag_sessions=0, settlement_lag_sessions=0)
        short = costs.redemption("2024-07-01", "2024-07-07", Decimal(10), None)
        normal = costs.redemption("2024-07-01", "2024-07-08", Decimal(10), None)
        self.assertEqual(short["sell_fee_bps"], "150")
        self.assertEqual(short["holding_calendar_days"], 6)
        self.assertEqual(normal["sell_fee_bps"], "10")
        self.assertFalse(normal["short_redemption_fee_applied"])

    def test_regulator_confirmation_example_and_holiday_boundary(self):
        costs = FundHoldingCosts()
        short = costs.redemption("2024-07-02", "2024-07-08", Decimal(10), self.calendar)
        normal = costs.redemption("2024-07-02", "2024-07-09", Decimal(10), self.calendar)
        self.assertEqual(short["holding_calendar_days"], 6)
        self.assertEqual(normal["holding_calendar_days"], 7)
        holiday = costs.redemption("2024-09-30", "2024-10-11", Decimal(10), self.calendar)
        self.assertEqual(holiday["purchase_confirmation_date"], "2024-10-08")
        self.assertEqual(holiday["redemption_confirmation_date"], "2024-10-14")
        self.assertEqual(holiday["holding_calendar_days"], 6)
        self.assertEqual(holiday["sell_fee_bps"], "150")

    def fixture(self):
        days = [d.isoformat() for d in self.calendar.sessions(date(2025, 7, 1), date(2025, 7, 16))]
        prices = {"one": [100] * len(days), "two": [100] * len(days)}
        frames = {}
        for i, _ in enumerate(days):
            entries = [{"code": code, "price": 100, "ma20": 99, "return5_pct": 1,
                        "scores": dict.fromkeys(("short", "medium", "long"), 70 if code == "one" and i < 2 or code == "two" and i >= 2 else 40)}
                       for code in prices]
            frames[i] = {"entries": entries, "context": {"breadth_above_ma60": .8, "benchmark_above_ma60": True}}
        policy = RotationPolicy(max_positions=1, review_sessions=1, trailing_stop=0, ma_exit=False)
        return days, prices, frames, policy

    def test_short_sale_and_terminal_sale_both_charge_lot_specific_fees(self):
        days, prices, frames, policy = self.fixture()
        result = simulate_rotation(prices, days, frames, policy=policy, start=0, end=4,
                                   holding_costs=FundHoldingCosts(), calendar=self.calendar)
        sales = [t for t in result["trades"] if t["action"] == "SELL"]
        self.assertEqual(sales[0]["sell_fee_bps"], "150")
        self.assertAlmostEqual(float(result["final_equity"]), 100000 / 1.001 * .985)
        self.assertEqual(result["short_redemption_sales"], 1)
        self.assertGreater(Decimal(result["final_receivables"]), 0)

    def test_unsettled_redemption_cash_cannot_buy_another_fund(self):
        days, prices, frames, policy = self.fixture()
        result = simulate_rotation(prices, days, frames, policy=policy, start=0,
                                   holding_costs=FundHoldingCosts(), calendar=self.calendar)
        sold = next(t for t in result["trades"] if t["action"] == "SELL")
        bought_two = next(t for t in result["trades"] if t["action"] == "BUY" and t["code"] == "two")
        self.assertGreaterEqual(bought_two["execution_date"], sold["settlement_date"])
        for point in result["equity_curve"]:
            self.assertAlmostEqual(float(point["equity"]), float(point["cash"]) + float(point["market_value"]) + float(point["receivables"]))
        self.assertEqual(validate_ledger(result, days, prices, calendar=self.calendar)["status"], "passed")

    def test_soft_exits_wait_until_seven_days_but_risk_stop_does_not(self):
        days, prices, frames, policy = self.fixture()
        result = simulate_rotation(prices, days, frames, policy=replace(policy, soft_exit_min_calendar_days=7), start=0,
                                   holding_costs=FundHoldingCosts(), calendar=self.calendar)
        first_sale = next(t for t in result["trades"] if t["action"] == "SELL")
        self.assertGreaterEqual(first_sale["holding_calendar_days"], 7)
        prices["one"][2] = 80
        frames[2]["entries"][0]["price"] = 80
        stopped = simulate_rotation(prices, days, frames, policy=replace(policy, soft_exit_min_calendar_days=7, trailing_stop=.1), start=0,
                                    holding_costs=FundHoldingCosts(), calendar=self.calendar)
        sale = next(t for t in stopped["trades"] if t["action"] == "SELL")
        self.assertEqual(sale["reason"], "trailing_stop")
        self.assertEqual(sale["sell_fee_bps"], "150")

    def test_distribution_cash_and_adjusted_signal_do_not_create_false_stop(self):
        days, prices, frames, policy = self.fixture()
        for frame in frames.values():
            frame["entries"] = [frame["entries"][0]]
            frame["entries"][0]["scores"] = dict.fromkeys(("short", "medium", "long"), 70)
        prices = {"one": [100, 100] + [90] * (len(days) - 2)}
        signals = {"one": [100] * len(days)}
        dividend = {"one": [0, 0, 10] + [0] * (len(days) - 3)}
        result = simulate_rotation(prices, days, frames, policy=replace(policy, trailing_stop=.08), start=0,
                                   fee_bps=Decimal(0), distributions=dividend, signal_prices=signals)
        self.assertAlmostEqual(result["total_return_pct"], 0)
        self.assertEqual(result["trade_count"], 2)
        self.assertAlmostEqual(float(result["distribution_income"]), 10000)

    def test_exception_calendars_and_original_calendar_unchanged(self):
        self.assertFalse(self.calendar.is_session(date(2015, 9, 3)))
        self.assertFalse(self.calendar.is_session(date(2019, 5, 2)))
        self.assertFalse(self.calendar.is_session(date(2020, 1, 31)))
        self.assertFalse(self.calendar.is_session(date(2024, 2, 9)))
        from backend.core.trading_calendar import get_calendar
        self.assertEqual(get_calendar().years, frozenset((2025, 2026)))

    def test_long_windows_and_gold_exception_do_not_bypass_own_trend(self):
        policy = RotationPolicy(regime="benchmark", benchmark_ma_sessions=200, entry_ma_sessions=60,
                                exit_ma_sessions=120, recent_return_sessions=30, allow_defensive_assets=True)
        context = {"breadth_above_ma60": .9, "benchmark_above_ma60": True, "benchmark_above_ma200": False}
        entry = {"price": 110, "ma60": 100, "ma120": 115, "return30_pct": 2,
                 "scores": dict.fromkeys(("short", "medium", "long"), 70)}
        self.assertFalse(market_ready(context, "benchmark", 200))
        self.assertFalse(entry_ready(entry, context, policy))
        entry["defensive_asset"] = True
        self.assertTrue(entry_ready(entry, context, policy))
        entry["return30_pct"] = -1
        self.assertFalse(entry_ready(entry, context, policy))
        self.assertEqual(exit_reason(entry, {"peak": 110}, policy), "ma_weakening")

    def test_known_deadline_guard_and_minimum_purchase(self):
        days, prices, frames, policy = self.fixture()
        guarded = simulate_rotation(prices, days, frames, policy=replace(policy, entry_min_days_to_liquidation=7),
                                    start=0, end=4, holding_costs=FundHoldingCosts(), calendar=self.calendar)
        self.assertEqual(guarded["trade_count"], 0)
        dust = simulate_rotation(prices, days, frames, policy=policy, initial=Decimal(".50"), start=0,
                                 holding_costs=FundHoldingCosts(), calendar=self.calendar)
        self.assertEqual(dust["trade_count"], 0)
        self.assertEqual(dust["total_return_pct"], 0)

    def test_validator_rejects_undercharged_short_sale(self):
        days, prices, frames, policy = self.fixture()
        result = simulate_rotation(prices, days, frames, policy=policy, start=0, end=4,
                                   holding_costs=FundHoldingCosts(), calendar=self.calendar)
        sale = next(t for t in result["trades"] if t["action"] == "SELL")
        sale["sell_fee_bps"] = "10"
        with self.assertRaises((ValueError, AssertionError)):
            validate_ledger(result, days, prices, calendar=self.calendar)


if __name__ == "__main__":
    unittest.main()
