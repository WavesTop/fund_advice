from copy import deepcopy
from dataclasses import replace
import unittest

import numpy as np

from backend.analysis.trend_satellite import SatellitePolicy, TrendSatelliteAllocator
from scripts.research_trend_satellites import satellite_trial
from scripts.validate_trend_satellite import audit_satellite
from tests import test_trend_satellite as fixtures


class EventRepairStateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SatelliteStateTests()
        self.fixture.setUp()

    def test_risk_does_not_immediately_repair_old_core_but_review_can(self):
        allocator = TrendSatelliteAllocator(self.fixture.data, SatellitePolicy("core", "none", risk_repair="event"))
        self.fixture.call(allocator, 252)
        self.fixture.data["economic_stats"][253]["risk"]["covariance"] = np.eye(4)
        urgent = self.fixture.call(allocator, 253)
        self.assertEqual(allocator.checks[-1]["action"], "risk")
        self.assertFalse(allocator.memory.work)
        self.assertEqual(urgent.risk_exits, frozenset(self.fixture.base))
        self.assertIsNone(self.fixture.call(allocator, 254, urgent.weights))
        renewed = self.fixture.call(allocator, 272, urgent.weights)
        self.assertEqual(allocator.checks[-1]["action"], "repair")
        self.assertEqual(renewed.weights, self.fixture.base)

    def test_default_immediate_repair_is_unchanged(self):
        allocator = TrendSatelliteAllocator(self.fixture.data, SatellitePolicy("core", "none"))
        self.fixture.call(allocator, 252)
        self.fixture.data["economic_stats"][253]["risk"]["covariance"] = np.eye(4)
        urgent = self.fixture.call(allocator, 253)
        self.assertTrue(allocator.memory.work)
        self.assertEqual(self.fixture.call(allocator, 254, urgent.weights).weights, self.fixture.base)

    def test_actual_urgent_risk_still_overrides_satellite_goal(self):
        allocator = TrendSatelliteAllocator(self.fixture.data, SatellitePolicy("sat", "fixed20", risk_repair="event"))
        entered = self.fixture.call(allocator, 252)
        self.fixture.call(allocator, 253, entered.weights)
        self.fixture.data["economic_stats"][254]["risk"]["covariance"] = np.eye(4)
        urgent = self.fixture.call(allocator, 254, entered.weights)
        self.assertEqual(allocator.memory.funded_index, 253)
        self.assertFalse(allocator.memory.work)
        self.assertEqual(allocator.checks[-1]["action"], "risk")
        self.assertTrue(urgent.enforce_risk_exits)
        self.assertIsNone(self.fixture.call(allocator, 255, urgent.weights))

    def test_future_entry_event_can_restore_work_not_a_fixed_cash_lock(self):
        allocator = TrendSatelliteAllocator(self.fixture.data, SatellitePolicy("sat", "fixed20", risk_repair="event"))
        self.fixture.data["frames"][252]["entries"][0]["scores"]["medium"] = 0.
        self.fixture.data["frames"][252]["entries"][1]["scores"]["medium"] = 0.
        self.fixture.call(allocator, 252)
        self.fixture.data["economic_stats"][253]["risk"]["covariance"] = np.eye(4)
        urgent = self.fixture.call(allocator, 253)
        entry = self.fixture.call(allocator, 257, urgent.weights)
        self.assertEqual(allocator.checks[-1]["event"], "entry")
        self.assertEqual(allocator.checks[-1]["action"], "repair")
        self.assertGreater(entry.weights["a"], self.fixture.base["a"])

    def test_unknown_repair_rule_rejected(self):
        for value in ("other", None, True):
            with self.assertRaises(ValueError):
                SatellitePolicy("bad", risk_repair=value).validate()


class EventRepairReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SatelliteReplayTests.setUpClass()
        cls.data = fixtures.SatelliteReplayTests.data
        cls.calendar = fixtures.SatelliteReplayTests.calendar

    def test_variants_phases_and_delays_are_audited(self):
        for rule, sizing in (("none", "whole"), ("fixed20", "whole"), ("fixed20", "headroom")):
            for phase, lag in ((0, 1), (5, 2)):
                result = satellite_trial(self.data, self.calendar,
                    SatellitePolicy("event", rule, sizing=sizing, risk_repair="event"), phase=phase, lag=lag)
                self.assertEqual(result["signal_validation"]["status"], "passed")
                self.assertEqual(result["ledger_validation"]["status"], "passed")

    def test_wrong_post_risk_work_flag_is_rejected_on_real_returns(self):
        # Scale raw prices and reconstruct every feature, rather than invent a risk cache.
        from tests import test_satellite_headroom as headroom
        headroom.HeadroomReplayTests.setUpClass()
        changed = headroom.HeadroomReplayTests.high_risk_data
        policy = SatellitePolicy("event", "fixed20", risk_repair="event")
        result = satellite_trial(changed, self.calendar, policy)
        checks = deepcopy(result["daily_checks"])
        row = next(r for r in checks if r["action"] == "risk")
        row["memory_after"]["work"] = True
        with self.assertRaises(ValueError):
            audit_satellite(result["account"], checks, changed, policy)
        with self.assertRaises(ValueError):
            audit_satellite(result["account"], result["daily_checks"], changed, replace(policy, risk_repair="immediate"))


if __name__ == "__main__":
    unittest.main()
