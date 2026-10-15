from copy import deepcopy
import unittest

from backend.analysis.fund_allocation import AllocationExecution, AllocationTargets, simulate_allocations
from scripts.validate_fund_allocation import validate_allocation
from scripts.validate_holding_release import audit_holding_release
from tests import test_fund_allocation as allocation_fixtures


class HoldingReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        allocation_fixtures.FundAllocationTests.setUpClass()
        cls.calendar = allocation_fixtures.FundAllocationTests.calendar

    def data(self):
        return allocation_fixtures.FundAllocationTests.data(self)

    def simulate(self, allocator, *, execution=None, data=None, extras=frozenset({2})):
        data = self.data() if data is None else data
        execution = execution or AllocationExecution(review_sessions=20, review_on_holding_release=True)
        trace = []
        def allocate(index, held):
            target = allocator(index, held)
            trace.append({"signal_date": data["days"][index], "targets": target.weights,
                          "risk_exit_codes": sorted(target.risk_exits)})
            return target
        account = simulate_allocations(data, self.calendar, allocate, start=0, execution=execution, extra_review_indices=extras)
        self.assertEqual(validate_allocation(account, data, self.calendar)["status"], "passed")
        if execution.review_on_holding_release:
            self.assertEqual(audit_holding_release(account, data, self.calendar, trace)["status"], "passed")
        return account, data, trace

    def test_wait_expiry_recomputes_signal_and_uses_normal_fee(self):
        account, data, _ = self.simulate(lambda i, held: AllocationTargets({"one": 1.} if i == 0 else {}))
        event = account["holding_release_events"][0]
        self.assertEqual(event["blocked_date"], data["days"][3])
        self.assertLess(data["days"].index(event["review_date"]), 20)
        first = next(t for t in account["trades"] if t["action"] == "SELL")
        self.assertEqual(first["signal_date"], event["review_date"])
        self.assertEqual(first["execution_date"], event["eligible_execution_date"])
        self.assertEqual(first["sell_fee_bps"], "10")
        self.assertGreaterEqual(first["holding_calendar_days"], 7)

    def test_reversed_latest_signal_does_not_execute_the_old_sale(self):
        account, _, _ = self.simulate(lambda i, held: AllocationTargets({} if i == 2 else {"one": 1.}))
        self.assertTrue(account["holding_release_review_dates"])
        self.assertTrue(all(t["reason"] == "terminal_liquidation" for t in account["trades"] if t["action"] == "SELL"))

    def test_true_risk_exit_still_executes_early_and_pays_short_fee(self):
        account, _, _ = self.simulate(lambda i, held: AllocationTargets({"one": 1.}) if i == 0 else
                                      AllocationTargets({}, frozenset({"one"}), enforce_risk_exits=True))
        self.assertFalse(account["holding_release_events"])
        first = next(t for t in account["trades"] if t["action"] == "SELL")
        self.assertEqual(first["reason"], "risk_exit")
        self.assertEqual(first["sell_fee_bps"], "150")

    def test_default_preserves_account_and_option_rejects_non_boolean(self):
        allocator = lambda i, held: AllocationTargets({"one": 1.} if i == 0 else {})
        a, _, _ = self.simulate(allocator, execution=AllocationExecution())
        b, _, _ = self.simulate(allocator, execution=AllocationExecution(review_on_holding_release=False))
        self.assertEqual(a, b)
        self.assertNotIn("holding_release_events", a)
        for invalid in (None, 1, "yes"):
            with self.assertRaises(ValueError):
                AllocationExecution(review_on_holding_release=invalid).validate()

    def test_future_nav_suffix_does_not_set_earlier_release_deadline(self):
        allocator = lambda i, held: AllocationTargets({"one": 1.} if i == 0 else {})
        account, data, _ = self.simulate(allocator)
        changed = deepcopy(data)
        cutoff = data["days"].index(account["holding_release_events"][0]["review_date"])
        for code in changed["prices"]:
            changed["prices"][code][cutoff + 1:] = [p * 5 for p in changed["prices"][code][cutoff + 1:]]
        altered, _, _ = self.simulate(allocator, data=changed)
        self.assertEqual(account["holding_release_events"][0], altered["holding_release_events"][0])
        self.assertEqual(account["decisions"][0], altered["decisions"][0])

    def test_release_auditor_rejects_early_dates_and_stale_targets(self):
        account, data, trace = self.simulate(lambda i, held: AllocationTargets({"one": 1.} if i == 0 else {}))
        changed = deepcopy(account)
        changed["holding_release_events"][0]["review_date"] = changed["holding_release_events"][0]["blocked_date"]
        with self.assertRaises(ValueError):
            audit_holding_release(changed, data, self.calendar, trace)
        changed = deepcopy(account)
        changed["holding_release_review_dates"] = []
        with self.assertRaises(ValueError):
            audit_holding_release(changed, data, self.calendar, trace)
        changed = deepcopy(account)
        day = changed["holding_release_review_dates"][0]
        next(d for d in changed["decisions"] if d["signal_date"] == day)["targets"] = {"one": 1.}
        with self.assertRaises(ValueError):
            audit_holding_release(changed, data, self.calendar, trace)

    def test_pending_order_delays_but_does_not_drop_the_due_review(self):
        account, _, _ = self.simulate(lambda i, held: AllocationTargets({"one": 1.} if i == 0 else {}),
            execution=AllocationExecution(execution_lag_sessions=2, review_on_holding_release=True), extras=frozenset({2, 4}))
        self.assertGreater(account["holding_release_review_dates"][0], account["holding_release_events"][0]["review_date"])
        first = next(t for t in account["trades"] if t["action"] == "SELL")
        self.assertEqual(first["signal_date"], account["holding_release_review_dates"][0])
        self.assertFalse(first["short_redemption_fee_applied"])

    def test_risk_exit_before_maturity_cancels_the_dead_lot_review(self):
        def allocate(i, held):
            return (AllocationTargets({"one": 1.}) if i == 0 else
                    AllocationTargets({}, frozenset({"one"}) if i == 4 else frozenset()))
        account, _, _ = self.simulate(allocate, extras=frozenset({2, 4}))
        self.assertTrue(account["holding_release_events"])
        self.assertFalse(account["holding_release_review_dates"])
        first = next(t for t in account["trades"] if t["action"] == "SELL")
        self.assertEqual(first["reason"], "risk_exit")

    def test_no_room_for_fresh_execution_keeps_terminal_short_fee(self):
        data = self.data()
        data["days"] = data["days"][:5]
        for key in ("prices", "distributions"):
            data[key] = {code: values[:5] for code, values in data[key].items()}
        account, _, _ = self.simulate(lambda i, held: AllocationTargets({"one": 1.} if i == 0 else {}), data=data)
        self.assertIsNone(account["holding_release_events"][0]["review_date"])
        self.assertFalse(account["holding_release_review_dates"])
        first = next(t for t in account["trades"] if t["action"] == "SELL")
        self.assertEqual(first["reason"], "terminal_liquidation")
        self.assertEqual(first["sell_fee_bps"], "150")
