from copy import deepcopy
from decimal import Decimal
import unittest

from backend.analysis.fund_allocation import AllocationExecution, AllocationTargets, simulate_allocations
from backend.analysis.fund_holding_costs import FundHoldingCosts
from scripts.diagnose_trend_frictions import friction_trial
from scripts.research_phased_class_trends import phased_trial
from scripts.validate_fund_allocation import validate_allocation
from tests import test_fund_allocation as allocation_fixtures
from tests import test_phased_class_trend as phased_fixtures
from tests.test_trend_risk_budget import calendar_fixture


class FrictionDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        phased_fixtures.PhasedClassTrendTests.setUpClass()
        cls.base = phased_fixtures.PhasedClassTrendTests.base
        cls.calendar = calendar_fixture()

    def data(self):
        return deepcopy(self.base)

    def test_explicit_default_friction_preserves_all_variant_metrics(self):
        data = self.data()
        for variant in ("grid20", "grid5", "phaseblend"):
            default = phased_trial(data, self.calendar, "risky_pair_static60", variant, end=280)
            explicit = phased_trial(data, self.calendar, "risky_pair_static60", variant, end=280,
                                    costs=FundHoldingCosts(), minimum_holding_days=7)
            self.assertEqual(default, explicit)

    def test_zero_fees_are_forwarded_to_single_and_mixed_paths(self):
        data = self.data()
        scenario = {"ordinary_fee_bps": 0, "short_redemption_bps": 0, "minimum_holding_days": 7}
        for variant in ("grid20", "grid5", "phaseblend"):
            trial = friction_trial(data, self.calendar, "risky_pair_static60", variant, scenario, end=280)
            self.assertEqual(Decimal(trial["metrics"]["total_fees"]), 0)
            self.assertEqual(trial["account"]["holding_cost_policy"]["short_redemption_bps"], 0)
            self.assertEqual(trial["account"]["execution"]["minimum_holding_days"], 7)
            self.assertEqual(trial["account"]["holding_cost_policy"]["confirmation_lag_sessions"], 1)
            self.assertEqual(trial["account"]["holding_cost_policy"]["settlement_lag_sessions"], 2)

    def test_ordinary_free_keeps_actual_early_redemption_floor(self):
        allocation_fixtures.FundAllocationTests.setUpClass()
        calendar = allocation_fixtures.FundAllocationTests.calendar
        data = allocation_fixtures.FundAllocationTests.data(self)
        def allocate(i, held):
            return AllocationTargets({"one": 1.}) if i == 0 else AllocationTargets({}, frozenset({"one"}))
        account = simulate_allocations(data, calendar, allocate, start=0, fee_bps=Decimal(0),
                                       execution=AllocationExecution(review_sessions=1))
        buy = next(t for t in account["trades"] if t["action"] == "BUY")
        sell = next(t for t in account["trades"] if t["action"] == "SELL")
        self.assertEqual(Decimal(buy["fee"]), 0)
        self.assertEqual(sell["sell_fee_bps"], "150")
        self.assertGreater(Decimal(sell["fee"]), 0)
        self.assertEqual(validate_allocation(account, data, calendar)["status"], "passed")

    def test_no_wait_does_not_remove_confirmation_or_settlement(self):
        data = self.data()
        trial = friction_trial(data, self.calendar, "dual_return60", "grid5",
            {"ordinary_fee_bps": 0, "short_redemption_bps": 0, "minimum_holding_days": 0}, end=280)
        self.assertEqual(trial["account"]["execution"]["minimum_holding_days"], 0)
        self.assertEqual(trial["account"]["holding_cost_policy"]["confirmation_lag_sessions"], 1)
        self.assertEqual(trial["account"]["holding_cost_policy"]["settlement_lag_sessions"], 2)
        self.assertEqual(trial["ledger_validation"]["status"], "passed")

    def test_friction_change_cannot_change_initial_past_signal(self):
        data = self.data()
        base = friction_trial(data, self.calendar, "dual_return60", "phaseblend",
            {"ordinary_fee_bps": 10, "short_redemption_bps": 150, "minimum_holding_days": 7}, end=280)
        free = friction_trial(data, self.calendar, "dual_return60", "phaseblend",
            {"ordinary_fee_bps": 0, "short_redemption_bps": 0, "minimum_holding_days": 0}, end=280)
        self.assertEqual(base["signal_trace"][0], free["signal_trace"][0])

    def test_invalid_wait_or_fee_floor_is_not_silently_accepted(self):
        data = self.data()
        for wait, floor in ((False, 0), (-1, 0), (0, -1), (0, 10000)):
            with self.assertRaises(ValueError):
                friction_trial(data, self.calendar, "dual_return60", "grid20",
                    {"ordinary_fee_bps": 0, "short_redemption_bps": floor, "minimum_holding_days": wait}, end=280)
