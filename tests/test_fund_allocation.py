from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
import json
from pathlib import Path
import unittest

from backend.analysis.fund_allocation import AllocationExecution, AllocationTargets, simulate_allocations
from backend.analysis.fund_holding_costs import FundHoldingCosts
from backend.core.trading_calendar import TradingCalendar
from scripts.validate_fund_allocation import validate_allocation
from scripts.validate_cross_asset_trend import audit_current_holdings


class FundAllocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "config/research-exchange-calendar-2009-2026.json"
        cls.calendar = TradingCalendar(json.loads(path.read_text()))

    def data(self):
        days = [d.isoformat() for d in self.calendar.sessions(date(2024, 7, 1), date(2024, 8, 30))]
        return {"days": days, "prices": {"one": [100.] * len(days), "two": [100.] * len(days)},
                "distributions": {"one": [0.] * len(days), "two": [0.] * len(days)}}

    def test_buy_hold_costs_and_independent_ledger(self):
        data = self.data()
        result = simulate_allocations(data, self.calendar, lambda i, held: {"one": 1} if i == 0 else None, start=0)
        self.assertAlmostEqual(result["total_return_pct"], (1 / 1.001 * .999 - 1) * 100)
        self.assertEqual(result["trade_count"], 2)
        self.assertEqual(validate_allocation(result, data, self.calendar)["status"], "passed")

    def test_multiple_lots_and_partial_fifo_sales(self):
        data = self.data()
        def targets(i, held):
            return {"one": .3 if i < 10 else .8 if i < 12 else .6 if i < 14 else 0}
        result = simulate_allocations(data, self.calendar, targets, start=0,
                                      execution=AllocationExecution(review_sessions=1, rebalance_band=.1, minimum_holding_days=0))
        buys = [t for t in result["trades"] if t["action"] == "BUY"]
        sales = [t for t in result["trades"] if t["action"] == "SELL"]
        self.assertGreaterEqual(len(buys), 2)
        self.assertEqual(sales[0]["lot_id"], buys[0]["lot_id"])
        self.assertLess(Decimal(sales[0]["units"]), Decimal(buys[0]["units"]))
        self.assertTrue(any(t["short_redemption_fee_applied"] for t in sales))
        self.assertEqual(validate_allocation(result, data, self.calendar)["status"], "passed")

    def test_cash_release_review_does_not_spend_unsettled_cash(self):
        data = self.data()
        def targets(i, held):
            return {"one": 1} if i < 10 else {"two": 1}
        result = simulate_allocations(data, self.calendar, targets, start=0,
                                      execution=AllocationExecution(review_sessions=10, rebalance_band=.05))
        sale = next(t for t in result["trades"] if t["action"] == "SELL")
        buy = next(t for t in result["trades"] if t["action"] == "BUY" and t["code"] == "two")
        self.assertGreater(buy["execution_date"], sale["settlement_date"])
        self.assertLess(data["days"].index(buy["execution_date"]), 21)
        self.assertEqual(validate_allocation(result, data, self.calendar)["status"], "passed")

    def test_risk_exit_bypasses_fee_wait(self):
        data = self.data()
        def targets(i, held):
            return AllocationTargets({"one": 1}) if i == 0 else AllocationTargets({}, frozenset({"one"}))
        result = simulate_allocations(data, self.calendar, targets, start=0,
                                      execution=AllocationExecution(review_sessions=1, minimum_holding_days=30))
        sale = next(t for t in result["trades"] if t["action"] == "SELL")
        self.assertEqual(sale["reason"], "risk_exit")
        self.assertEqual(sale["sell_fee_bps"], "150")

    def test_explicit_current_risk_exit_can_bypass_band_without_changing_default(self):
        data = self.data()
        def run(enforce):
            def targets(i, held):
                return (AllocationTargets({"one": .5}) if i == 0 else
                        AllocationTargets({"one": .49}, frozenset({"one"}), enforce_risk_exits=enforce))
            return simulate_allocations(data, self.calendar, targets, start=0,
                                        execution=AllocationExecution(review_sessions=1, rebalance_band=.05))
        default, enforced = run(False), run(True)
        self.assertTrue(all(t["reason"] == "terminal_liquidation" for t in default["trades"] if t["action"] == "SELL"))
        first = next(t for t in enforced["trades"] if t["action"] == "SELL")
        self.assertEqual(first["reason"], "risk_exit")
        self.assertEqual(first["sell_fee_bps"], "150")
        self.assertEqual(validate_allocation(enforced, data, self.calendar)["status"], "passed")

    def test_portfolio_state_is_read_only_current_and_independently_rebuilt(self):
        data = self.data()
        data["distributions"]["one"][3] = 2.
        trace = []
        def allocate(i, held, state):
            self.assertEqual((state.signal_index, state.signal_date), (i, data["days"][i]))
            self.assertEqual(held, {c for c, w in state.weights.items() if w > 0})
            with self.assertRaises(TypeError):
                state.weights["one"] = 1.
            trace.append({"signal_date": state.signal_date, "current_fund_weights": dict(state.weights), "risk_exit_codes": []})
            return {"one": .5} if i == 0 else {"two": .5}
        account = simulate_allocations(data, self.calendar, allocate, start=0, include_portfolio_state=True,
                                       execution=AllocationExecution(review_sessions=2))
        self.assertEqual(audit_current_holdings(account, trace, data)["status"], "passed")
        changed = deepcopy(trace)
        changed[1]["current_fund_weights"]["one"] = .9
        with self.assertRaises(ValueError):
            audit_current_holdings(account, changed, data)
        changed = deepcopy(account)
        next(t for t in changed["trades"] if t["reason"] == "allocation_reduction")["reason"] = "risk_exit"
        with self.assertRaises(ValueError):
            audit_current_holdings(changed, trace, data)
        for invalid in (None, 1, "yes"):
            with self.assertRaises(ValueError):
                simulate_allocations(data, self.calendar, allocate, start=0, include_portfolio_state=invalid)

    def test_signal_money_budget_not_future_nav_targeting(self):
        data = self.data()
        changed = deepcopy(data)
        changed["prices"]["one"][2] = 200
        execution = AllocationExecution(execution_lag_sessions=2)
        allocator = lambda i, held: {"one": .5} if i == 0 else None
        original = simulate_allocations(data, self.calendar, allocator, start=0, execution=execution)
        altered = simulate_allocations(changed, self.calendar, allocator, start=0, execution=execution)
        self.assertEqual(original["decisions"], altered["decisions"])
        self.assertLess(abs(Decimal(original["trades"][0]["gross_amount"]) - Decimal(altered["trades"][0]["gross_amount"])), Decimal("1e-20"))
        self.assertEqual(altered["trades"][0]["execution_date"], data["days"][2])
        self.assertEqual(validate_allocation(altered, changed, self.calendar)["status"], "passed")

    def test_dividend_entitlement_and_tampering(self):
        data = self.data()
        data["distributions"]["one"][4] = 10
        data["prices"]["one"][4:] = [90.] * (len(data["days"]) - 4)
        result = simulate_allocations(data, self.calendar, lambda i, held: {"one": 1} if i == 0 else None,
                                      start=0, fee_bps=Decimal(0))
        self.assertAlmostEqual(result["total_return_pct"], 0)
        self.assertEqual(validate_allocation(result, data, self.calendar)["status"], "passed")
        changed = deepcopy(result)
        changed["trades"][-1]["fee"] = "0.5"
        with self.assertRaises(ValueError):
            validate_allocation(changed, data, self.calendar)

    def test_partial_redemption_units_are_fixed_before_unknown_execution_nav(self):
        data = self.data()
        changed = deepcopy(data)
        changed["prices"]["one"][3] = 200.
        execution = AllocationExecution(review_sessions=1, minimum_holding_days=0, rebalance_band=0.)
        allocator = lambda i, held: {"one": .8} if i == 0 else {"one": .6} if i == 2 else None
        original = simulate_allocations(data, self.calendar, allocator, start=0, execution=execution)
        altered = simulate_allocations(changed, self.calendar, allocator, start=0, execution=execution)
        original_sale = next(t for t in original["trades"] if t["reason"] == "allocation_reduction")
        altered_sale = next(t for t in altered["trades"] if t["reason"] == "allocation_reduction")
        self.assertEqual(original["decisions"], altered["decisions"])
        self.assertEqual(original_sale["units"], altered_sale["units"])
        self.assertNotEqual(original_sale["gross_amount"], altered_sale["gross_amount"])
        self.assertEqual(validate_allocation(altered, changed, self.calendar)["status"], "passed")

    def test_invalid_allocation_and_parameters(self):
        data = self.data()
        with self.assertRaises(ValueError):
            simulate_allocations(data, self.calendar, lambda i, held: {"one": 1.2}, start=0)
        with self.assertRaises(ValueError):
            AllocationExecution(review_sessions=0).validate()

    def test_initial_review_equalises_entry_without_resetting_later_grid(self):
        data = self.data()
        calls = []
        def allocator(i, held):
            calls.append(i)
            return {"one": 1.}
        account = simulate_allocations(data, self.calendar, allocator, start=0, phase=5,
                                       execution=AllocationExecution(initial_review=True))
        self.assertEqual(calls, [0, 5, 25])
        buy = next(t for t in account["trades"] if t["action"] == "BUY")
        self.assertEqual(buy["signal_date"], data["days"][0])
        self.assertEqual(buy["execution_date"], data["days"][1])
        self.assertEqual(validate_allocation(account, data, self.calendar)["status"], "passed")

    def test_initial_review_default_preserves_delayed_entry_and_zero_phase(self):
        data = self.data()
        allocator = lambda i, held: {"one": 1.}
        old = simulate_allocations(data, self.calendar, allocator, start=0, phase=5)
        disabled = simulate_allocations(data, self.calendar, allocator, start=0, phase=5,
                                        execution=AllocationExecution(initial_review=False))
        self.assertEqual(old, disabled)
        self.assertEqual(old["trades"][0]["signal_date"], data["days"][5])
        zero = simulate_allocations(data, self.calendar, allocator, start=0)
        enabled = simulate_allocations(data, self.calendar, allocator, start=0,
                                       execution=AllocationExecution(initial_review=True))
        for field in ("trades", "decisions", "equity_curve", "total_return_pct", "max_drawdown_pct"):
            self.assertEqual(zero[field], enabled[field])
        for bad in (None, 1, "yes"):
            with self.assertRaises(ValueError):
                AllocationExecution(initial_review=bad).validate()

    def test_initial_review_still_uses_signal_budget_not_future_execution_price(self):
        data = self.data()
        changed = deepcopy(data)
        changed["prices"]["one"][2] *= 2
        execution = AllocationExecution(initial_review=True, execution_lag_sessions=2)
        allocator = lambda i, held: {"one": .5} if i == 0 else None
        old = simulate_allocations(data, self.calendar, allocator, start=0, phase=5, execution=execution)
        altered = simulate_allocations(changed, self.calendar, allocator, start=0, phase=5, execution=execution)
        self.assertEqual(old["decisions"], altered["decisions"])
        left, right = old["trades"][0], altered["trades"][0]
        self.assertLess(abs(Decimal(left["gross_amount"]) - Decimal(right["gross_amount"])), Decimal("1e-20"))
        for trade in (left, right):
            spent = Decimal(trade["gross_amount"]) + Decimal(trade["fee"])
            self.assertEqual(spent.quantize(Decimal(".01")), Decimal("50000.00"))

    def test_extra_reviews_augment_grid_and_reject_invalid_indices(self):
        data = self.data()
        calls = []
        def allocator(i, held):
            calls.append(i)
            return {"one": 1.}
        result = simulate_allocations(data, self.calendar, allocator, start=0, extra_review_indices=frozenset({3}))
        self.assertEqual(calls, [0, 3, 20, 40])
        self.assertEqual(result["extra_review_dates"], [data["days"][3]])
        self.assertEqual(validate_allocation(result, data, self.calendar)["status"], "passed")
        for bad in (frozenset({True}), frozenset({-1}), frozenset({len(data["days"])}), {3}):
            with self.assertRaises(ValueError):
                simulate_allocations(data, self.calendar, allocator, start=0, extra_review_indices=bad)

    def test_terminal_liquidation_exhausts_fractional_lots(self):
        data = self.data()
        data["prices"]["one"] = [1.23456789 + i * .00123456 for i in range(len(data["days"]))]
        def targets(i, held):
            return {"one": .12 + .1 * (i // 5)} if i < 30 else None
        result = simulate_allocations(data, self.calendar, targets, start=0,
                                      execution=AllocationExecution(review_sessions=5, rebalance_band=.01))
        self.assertEqual(result["equity_curve"][-1]["positions"], 0)
        self.assertEqual(result["equity_curve"][-1]["market_value"], "0")
        self.assertEqual(validate_allocation(result, data, self.calendar)["status"], "passed")

    def test_full_target_exit_exhausts_each_lot_without_phantom_settlement_review(self):
        data = self.data()
        data["prices"]["one"] = [2.157599] * len(data["days"])
        levels = [.380728, .568059, .567401, .823361, .645586, .843156, .78512, .892792]
        calls = []
        def allocate(i, held):
            calls.append(i)
            return {"one": levels[min(i // 3, 7)]} if i < 27 else {}
        result = simulate_allocations(data, self.calendar, allocate, start=0, fee_bps=Decimal(0),
            execution=AllocationExecution(review_sessions=1000, minimum_holding_days=0, rebalance_band=.01),
            extra_review_indices=frozenset(range(3, 31, 3)))
        self.assertEqual(calls, list(range(0, 31, 3)))
        self.assertTrue(all(row["positions"] == 0 and Decimal(row["market_value"]) == 0 for row in result["equity_curve"][28:]))
        self.assertFalse(any(t["action"] == "SELL" and Decimal(t["gross_amount"]) < Decimal("1e-20") for t in result["trades"]))
        self.assertEqual(validate_allocation(result, data, self.calendar)["status"], "passed")
        changed = deepcopy(result)
        trade = next(t for t in changed["trades"] if t["action"] == "SELL" and t["signal_date"] == data["days"][27])
        with localcontext() as ctx:
            ctx.prec = 40
            trade["units"] = str(Decimal(trade["units"]) - Decimal("1e-25"))
            trade["gross_amount"] = str(Decimal(trade["units"]) * Decimal(trade["price"]))
        with self.assertRaisesRegex(ValueError, "Full target exit"):
            validate_allocation(changed, data, self.calendar)


if __name__ == "__main__":
    unittest.main()
