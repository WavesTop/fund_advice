from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import copy
import unittest

from backend.analysis.sector_rotation import RotationPolicy, entry_ready, exit_reason, simulate_rotation
from scripts.backtest_sector_rotation import DEFAULT_POLICY, load_policy
from scripts.validate_sector_rotation import validate_ledger


class SectorRotationTests(unittest.TestCase):
    def fixture(self, values):
        days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(len(values))]
        prices = {"one": values}
        frames = {i: {"entries": [{"code": "one", "price": p, "ma20": p * .99, "return5_pct": 1,
                                   "scores": {"short": 70, "medium": 70, "long": 70},
                                   "up_volume_ratio20": .6, "volume_ratio5_20": 1, "volatility20": .01}],
                      "context": {"breadth_above_ma60": .8, "benchmark_above_ma60": True}}
                  for i, p in enumerate(values)}
        policy = RotationPolicy(max_positions=1, review_sessions=1, trailing_stop=0, ma_exit=False)
        return days, prices, frames, policy

    def run_case(self, values, **changes):
        days, prices, frames, policy = self.fixture(values)
        result = simulate_rotation(prices, days, frames, policy=replace(policy, **changes), start=0, fee_bps=Decimal(0))
        return result, days

    def test_retains_qualified_holding_without_repeated_fees(self):
        result, _ = self.run_case([100, 100, 110, 120, 121])
        self.assertEqual(result["trade_count"], 2)
        self.assertAlmostEqual(result["total_return_pct"], 21)
        self.assertEqual(result["trades"][-1]["reason"], "terminal_liquidation")

    def test_stop_is_detected_at_close_and_executes_next_close_not_stop_price(self):
        result, days = self.run_case([100, 100, 120, 105, 90, 100], trailing_stop=.1)
        sale = next(t for t in result["trades"] if t["action"] == "SELL")
        self.assertEqual(sale["signal_date"], days[3])
        self.assertEqual(sale["execution_date"], days[4])
        self.assertEqual(Decimal(sale["price"]), 90)
        self.assertAlmostEqual(result["total_return_pct"], -10)

    def test_score_hysteresis_retains_below_entry_and_exits_below_exit(self):
        days, prices, frames, policy = self.fixture([100] * 6)
        frames[2]["entries"][0]["scores"] = dict.fromkeys(("short", "medium", "long"), 58)
        frames[3]["entries"][0]["scores"] = dict.fromkeys(("short", "medium", "long"), 40)
        result = simulate_rotation(prices, days, frames, policy=policy, start=0, fee_bps=Decimal(0))
        self.assertEqual(result["trades"][1]["signal_date"], days[3])
        self.assertEqual(result["trades"][1]["reason"], "score_weakening")

    def test_regime_and_volume_gates_use_observed_data(self):
        _, _, frames, policy = self.fixture([100, 100])
        frame = frames[0]
        frame["context"]["benchmark_above_ma60"] = False
        self.assertFalse(entry_ready(frame["entries"][0], frame["context"], replace(policy, regime="benchmark")))
        frame["entries"][0]["up_volume_ratio20"] = .4
        self.assertFalse(entry_ready(frame["entries"][0], frame["context"], replace(policy, volume_gate=True)))
        self.assertTrue(entry_ready(frame["entries"][0], frame["context"], policy))

    def test_lag_and_cash_exposure_cap(self):
        result, _ = self.run_case([100, 200, 220], max_exposure=.5)
        self.assertAlmostEqual(result["total_return_pct"], 5)
        self.assertAlmostEqual(float(result["equity_curve"][1]["cash"]), 50000)

    def test_costs_include_initial_buy_final_sale_and_curve_identity(self):
        days, prices, frames, policy = self.fixture([100, 100, 100])
        result = simulate_rotation(prices, days, frames, policy=policy, start=0)
        expected = 100000 / 1.001 * .999
        self.assertAlmostEqual(float(result["final_equity"]), expected)
        self.assertAlmostEqual(float(result["total_fees"]), 100000 - expected)
        for p in result["equity_curve"]:
            self.assertGreaterEqual(Decimal(p["cash"]), 0)
            self.assertAlmostEqual(float(p["equity"]), float(p["cash"]) + float(p["market_value"]))

    def test_cooldown_prevents_immediate_reentry_after_exit(self):
        result, _ = self.run_case([100, 100, 120, 105, 90, 100, 110, 120, 125], trailing_stop=.1)
        self.assertEqual(sum(t["action"] == "BUY" for t in result["trades"]), 1)

    def test_same_stop_boundary_and_invalid_policy(self):
        _, _, frames, policy = self.fixture([100, 90])
        self.assertEqual(exit_reason(frames[1]["entries"][0], {"peak": 100}, replace(policy, trailing_stop=.1)), "trailing_stop")
        with self.assertRaises(ValueError):
            replace(policy, exit_score=75).validate()

    def test_future_prices_cannot_change_past_orders(self):
        days, prices, frames, policy = self.fixture([100] * 7)
        first = simulate_rotation(prices, days, frames, policy=policy, start=0)
        prices["one"][-1] = 200
        second = simulate_rotation(prices, days, frames, policy=policy, start=0)
        self.assertEqual(first["trades"][0], second["trades"][0])
        self.assertEqual(first["decisions"][:-1], second["decisions"][:-1])

    def test_exit_confirmation_waits_for_two_weak_closes(self):
        days, prices, frames, policy = self.fixture([100, 100, 95, 94, 90, 100])
        for i in (2, 3):
            frames[i]["entries"][0]["scores"] = dict.fromkeys(("short", "medium", "long"), 40)
        result = simulate_rotation(prices, days, frames, policy=replace(policy, exit_confirm_sessions=2), start=0, fee_bps=Decimal(0))
        self.assertEqual(result["trades"][1]["execution_date"], days[4])

    def test_market_exit_and_entry_state_confirmation(self):
        days, prices, frames, policy = self.fixture([100] * 5)
        frames[2]["context"]["benchmark_above_ma60"] = False
        result = simulate_rotation(prices, days, frames, policy=replace(policy, regime="benchmark", regime_exit=True), start=0)
        self.assertEqual(result["trades"][1]["signal_date"], days[2])
        self.assertEqual(result["trades"][1]["reason"], "market_weakening")
        frames[0]["entries"][0]["short_state"] = "recovery"
        self.assertFalse(entry_ready(frames[0]["entries"][0], frames[0]["context"], replace(policy, entry_state_gate=True)))

    def test_frozen_policy_and_independent_ledger_validation(self):
        frozen = load_policy(DEFAULT_POLICY)
        self.assertEqual(frozen.regime, "benchmark")
        self.assertTrue(frozen.regime_exit)
        days, prices, frames, policy = self.fixture([100, 100, 120, 105, 90, 100])
        result = simulate_rotation(prices, days, frames, policy=replace(policy, trailing_stop=.1), start=0)
        self.assertEqual(validate_ledger(result, days, prices)["status"], "passed")
        wrong = copy.deepcopy(result)
        wrong["trades"][0]["price"] = "99"
        with self.assertRaisesRegex(ValueError, "Execution price"):
            validate_ledger(wrong, days, prices)
        wrong = copy.deepcopy(result)
        wrong["equity_curve"][2]["equity"] = "999999"
        with self.assertRaisesRegex(ValueError, "equity"):
            validate_ledger(wrong, days, prices)
        wrong = copy.deepcopy(result)
        wrong["trades"][0]["signal_date"] = wrong["trades"][0]["execution_date"]
        with self.assertRaisesRegex(ValueError, "one session"):
            validate_ledger(wrong, days, prices)
        wrong = copy.deepcopy(result)
        wrong["equity_curve"].pop(2)
        with self.assertRaisesRegex(ValueError, "observation grid"):
            validate_ledger(wrong, days, prices)


if __name__ == "__main__":
    unittest.main()
