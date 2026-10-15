from copy import deepcopy
from datetime import date
from decimal import Decimal, localcontext
import json
from pathlib import Path
import unittest

from backend.analysis.fund_allocation import AllocationExecution, AllocationTargets, simulate_allocations
from backend.core.trading_calendar import TradingCalendar
from scripts.validate_fund_allocation import validate_allocation
from scripts.validate_allocation_checkpoint import validate_checkpoint_lineage


class AllocationCheckpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "config/research-exchange-calendar-2009-2026-v2.json"
        cls.calendar = TradingCalendar(json.loads(path.read_text()))

    def setUp(self):
        days = [d.isoformat() for d in self.calendar.sessions(date(2024, 7, 1), date(2024, 8, 30))]
        self.data = {"days": days, "prices": {"one": [100.] * len(days), "two": [100.] * len(days)},
                     "distributions": {"one": [0.] * len(days), "two": [0.] * len(days)}}
        self.data["distributions"]["one"][11] = 1.
        self.execution = AllocationExecution(review_sessions=10, execution_lag_sessions=2, rebalance_band=.03)

    @staticmethod
    def allocator(i, held):
        return {"one": .6, "two": .2} if i < 10 else {"two": .8}

    def run_account(self, allocator=None, **kwargs):
        return simulate_allocations(self.data, self.calendar, allocator or self.allocator,
                                    start=kwargs.pop("start", 0), execution=kwargs.pop("execution", self.execution), **kwargs)

    def test_split_matches_whole_with_pending_orders_receipts_dividends_and_old_lots(self):
        whole = self.run_account(liquidate_at_end=False, capture_state_indices=frozenset({1, 3, 11, 13, 14, 21}))
        for split in (1, 3, 11, 13, 14, 21):
            state = whole["account_checkpoints"][self.data["days"][split]]
            before = deepcopy(state)
            resumed = self.run_account(start=split, initial_state=state, liquidate_at_end=False)
            self.assertEqual(state, before)
            self.assertEqual(resumed["equity_curve"], whole["equity_curve"][split:])
            self.assertEqual(resumed["trades"], [t for t in whole["trades"] if t["execution_date"] >= state["resume_date"]])
            self.assertEqual(resumed["final_state"], whole["final_state"])
            self.assertEqual(validate_allocation(resumed, self.data, self.calendar)["status"], "passed")
        self.assertEqual(validate_allocation(whole, self.data, self.calendar)["status"], "passed")

    def test_prefix_then_resume_is_exact_and_fees_add(self):
        whole = self.run_account(liquidate_at_end=False)
        left = self.run_account(end=10, liquidate_at_end=False)
        right = self.run_account(start=11, initial_state=left["final_state"], liquidate_at_end=False)
        self.assertEqual(left["equity_curve"] + right["equity_curve"], whole["equity_curve"])
        self.assertEqual(left["trades"] + right["trades"], whole["trades"])
        with localcontext() as ctx:
            ctx.prec = 40
            self.assertEqual(Decimal(left["total_fees"]) + Decimal(right["total_fees"]), Decimal(whole["total_fees"]))
        self.assertEqual(validate_allocation(left, self.data, self.calendar)["status"], "passed")

    def test_terminal_resume_keeps_original_review_clock(self):
        whole = self.run_account(capture_state_indices=frozenset({14}))
        right = self.run_account(start=14, initial_state=whole["account_checkpoints"][self.data["days"][14]])
        self.assertEqual(right["equity_curve"], whole["equity_curve"][14:])
        self.assertEqual(right["trades"], [t for t in whole["trades"] if t["execution_date"] >= self.data["days"][14]])
        self.assertEqual(validate_allocation(right, self.data, self.calendar)["status"], "passed")

    def test_marked_end_does_not_sell_or_block_a_late_purchase(self):
        marked = self.run_account(lambda i, held: {"one": .5} if i == 8 else None,
                                  end=10, liquidate_at_end=False, extra_review_indices=frozenset({8}))
        self.assertEqual(marked["trade_count"], 1)
        self.assertEqual(marked["trades"][0]["action"], "BUY")
        self.assertTrue(marked["final_state"]["lots"])
        self.assertGreater(Decimal(marked["equity_curve"][-1]["market_value"]), 0)
        self.assertEqual(validate_allocation(marked, self.data, self.calendar)["status"], "passed")

    def test_seed_day_one_loss_is_in_drawdown(self):
        prefix = self.run_account(lambda i, held: {"one": 1.} if i == 0 else None, end=5, liquidate_at_end=False)
        self.data["prices"]["one"][6:] = [50.] * (len(self.data["days"]) - 6)
        right = self.run_account(lambda i, held: None, start=6, initial_state=prefix["final_state"], liquidate_at_end=False)
        self.assertAlmostEqual(right["max_drawdown_pct"], -50.)
        self.assertEqual(validate_allocation(right, self.data, self.calendar)["status"], "passed")

    def test_urgent_fork_exit_retains_age_and_short_fee(self):
        prefix = self.run_account(lambda i, held: {"one": 1.} if i == 0 else None, end=2, liquidate_at_end=False)
        right = self.run_account(lambda i, held: AllocationTargets({}, frozenset({"one"})), start=3,
                                  initial_state=prefix["final_state"], liquidate_at_end=False, resume_review=True)
        sale = next(t for t in right["trades"] if t["action"] == "SELL")
        self.assertEqual(sale["purchase_date"], self.data["days"][2])
        self.assertEqual(sale["sell_fee_bps"], "150")
        self.assertEqual(validate_allocation(right, self.data, self.calendar)["status"], "passed")

    def test_tampering_and_invented_but_balanced_seed_rejected_by_lineage(self):
        parent = self.run_account(liquidate_at_end=False, capture_state_indices=frozenset({11, 13}))
        state = parent["account_checkpoints"][self.data["days"][11]]
        for mutate in (lambda s: s.update(cash="100000"), lambda s: s.update(calendar_hash="bad"),
                       lambda s: s["prices_asof"].update(one="99"),
                       lambda s: s["queued_orders"][0]["orders"][0].update(signal_date=self.data["days"][11])):
            changed = deepcopy(state); mutate(changed)
            with self.assertRaises(ValueError):
                self.run_account(start=11, initial_state=changed, liquidate_at_end=False)
        changed = deepcopy(parent["account_checkpoints"][self.data["days"][13]])
        changed["cash"] = str(Decimal(changed["cash"]) + 100)
        changed["reference_equity"] = str(Decimal(changed["reference_equity"]) + 100)
        with self.assertRaises(ValueError):
            validate_checkpoint_lineage(changed, parent, self.data, self.calendar)

    def test_future_nav_does_not_change_checkpoint_or_pending_plan(self):
        left = self.run_account(end=10, liquidate_at_end=False)
        self.data["prices"]["one"][11:] = [200.] * (len(self.data["days"]) - 11)
        changed = self.run_account(end=10, liquidate_at_end=False)
        self.assertEqual(left, changed)

    def test_default_shape_and_numbers_unchanged_with_explicit_defaults(self):
        old = self.run_account()
        explicit = self.run_account(initial_state=None, liquidate_at_end=True, capture_state_indices=frozenset(), resume_review=False)
        self.assertEqual(old, explicit)
        self.assertNotIn("final_state", old)
        self.assertNotIn("phase", old)
        self.assertNotIn("planned_orders", old["decisions"][0])
        captured = self.run_account(capture_state_indices=frozenset({11}))
        for field in ("equity_curve", "trades", "final_equity", "total_fees", "max_drawdown_pct"):
            self.assertEqual(old[field], captured[field])

    def test_options_fail_closed_for_unrepresented_private_clocks(self):
        for execution in (AllocationExecution(review_on_holding_release=True), AllocationExecution(deadline_entry_guard_days=7)):
            with self.assertRaises(ValueError):
                self.run_account(execution=execution, liquidate_at_end=False)
        with self.assertRaises(ValueError):
            self.run_account(resume_review=True)

    def test_cloned_forks_do_not_change_each_other_or_seed(self):
        prefix = self.run_account(end=5, liquidate_at_end=False)
        seed = deepcopy(prefix["final_state"])
        holding = self.run_account(lambda i, held: None, start=6, initial_state=seed, liquidate_at_end=False)
        exiting = self.run_account(lambda i, held: AllocationTargets({}, frozenset(held)), start=6,
                                   initial_state=seed, liquidate_at_end=False, resume_review=True)
        self.assertEqual(seed, prefix["final_state"])
        self.assertTrue(holding["final_state"]["lots"])
        self.assertFalse(exiting["final_state"]["lots"])

    def test_final_lots_receipts_and_pending_plan_tampering_is_audited(self):
        parent = self.run_account(end=10, liquidate_at_end=False)
        for mutate in (lambda s: s["lots"]["one"][0].update(units="1"),
                       lambda s: s["queued_orders"][0]["origin_decision"].update(targets={"two": .5}),
                       lambda s: s.update(lot_counter=999)):
            changed = deepcopy(parent); mutate(changed["final_state"])
            with self.assertRaises(ValueError):
                validate_allocation(changed, self.data, self.calendar)
        source = self.run_account(end=13, liquidate_at_end=False)
        changed = deepcopy(source)
        changed["final_state"]["receivables"][0]["amount"] = "1"
        with self.assertRaises(ValueError):
            validate_allocation(changed, self.data, self.calendar)

    def test_duplicate_lot_ids_and_gapped_calendar_rejected(self):
        prefix = self.run_account(end=5, liquidate_at_end=False)
        changed = deepcopy(prefix["final_state"])
        changed["lots"]["two"][0]["id"] = changed["lots"]["one"][0]["id"]
        with self.assertRaises(ValueError):
            self.run_account(start=6, initial_state=changed, liquidate_at_end=False)
        self.data["days"].pop(10)
        for group in ("prices", "distributions"):
            for values in self.data[group].values():
                values.pop(10)
        with self.assertRaises(ValueError):
            self.run_account(liquidate_at_end=False)

    def test_marked_basis_and_checkpoint_schema_cannot_be_mislabeled(self):
        parent = self.run_account(end=5, liquidate_at_end=False)
        changed = deepcopy(parent)
        changed["valuation_basis"] = "liquidated_cash"
        with self.assertRaises(ValueError):
            validate_allocation(changed, self.data, self.calendar)
        changed = deepcopy(parent)
        changed["final_state"]["version"] = "unknown"
        with self.assertRaises(ValueError):
            validate_allocation(changed, self.data, self.calendar)


if __name__ == "__main__":
    unittest.main()
