from copy import deepcopy
from dataclasses import replace
import unittest

import numpy as np

from backend.analysis.portfolio_increment import prepare_economic
from backend.analysis.trend_risk_budget import projected_volatility
from backend.analysis.trend_satellite import SatellitePolicy, TrendSatelliteAllocator, headroom_target, scaled
from scripts.research_trend_satellites import satellite_trial
from scripts.research_local_trend_models import prepare_local
from scripts.validate_trend_satellite import audit_satellite
from tests import test_trend_satellite as fixtures


class HeadroomSizingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SatelliteStateTests()
        self.fixture.setUp()
        self.base = self.fixture.base
        self.risk = self.fixture.data["economic_stats"][252]["risk"]

    def test_unconstrained_tilt_keeps_all_defensive_weights(self):
        goal = headroom_target(self.risk, self.base, ["a", "b"], "a")
        self.assertAlmostEqual(goal["a"], .25)
        self.assertAlmostEqual(goal["b"], .05)
        self.assertEqual(goal["gold"], .3)
        self.assertEqual(goal["bond"], .4)

    def test_partial_headroom_hits_risk_boundary_without_scaling_core(self):
        self.risk["covariance"] = np.diag([.45**2, .45**2, 0, 0])
        goal = headroom_target(self.risk, self.base, ["a", "b"], "a")
        self.assertGreater(goal["a"], .15)
        self.assertLess(goal["a"], .25)
        self.assertAlmostEqual(projected_volatility(self.risk, goal), .1)
        self.assertEqual(goal["gold"], .3)
        self.assertEqual(goal["bond"], .4)
        self.assertAlmostEqual(sum(goal.values()), sum(self.base.values()))

    def test_no_headroom_does_not_dilute_defensive_core_or_fake_entry(self):
        self.risk["covariance"] = np.diag([.5**2, .5**2, 0, 0])
        neutral = scaled(self.fixture.data, 252, self.base)
        goal = headroom_target(self.risk, neutral, ["a", "b"], "a")
        for code in self.base:
            self.assertAlmostEqual(goal[code], neutral[code])
        allocator = TrendSatelliteAllocator(self.fixture.data, SatellitePolicy("headroom", "fixed20", sizing="headroom"))
        self.fixture.call(allocator, 252, neutral)
        self.assertIsNone(allocator.memory.candidate)

    def test_risk_reducing_tilt_is_not_blocked_at_full_neutral_budget(self):
        self.risk["covariance"] = np.diag([.8**2, .1**2, 0, 0])
        neutral = scaled(self.fixture.data, 252, self.base)
        goal = headroom_target(self.risk, neutral, ["a", "b"], "b")
        self.assertGreater(goal["b"], neutral["b"]+.09)
        self.assertLess(projected_volatility(self.risk, goal), .1)
        self.assertEqual(goal["gold"], neutral["gold"])

    def test_invalid_sizing_and_infeasible_neutral_rejected(self):
        with self.assertRaises(ValueError):
            SatellitePolicy("wrong", sizing="other").validate()
        self.risk["covariance"] = np.eye(4)
        with self.assertRaises(ValueError):
            headroom_target(self.risk, self.base, ["a", "b"], "a")


class HeadroomReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SatelliteReplayTests.setUpClass()
        cls.data = fixtures.SatelliteReplayTests.data
        cls.calendar = fixtures.SatelliteReplayTests.calendar
        days = cls.data["days"]
        funds = {code: {"code": code, "daily_start": days[0], "last_date": days[-1],
            "rows": {day: {"unit_nav": str(value**4)} for day, value in zip(days, values)},
            "distributions": dict.fromkeys(days, 0.), "theme": "黄金" if code == "000217" else "other",
            "role": "defensive" if code == "bond" else "sector"}
            for code, values in cls.data["prices"].items()}
        funds["000051"] = dict(funds["a"], code="000051")
        cls.high_risk_data = prepare_economic(prepare_local(funds, list(cls.data["prices"]), cls.calendar))

    def test_both_exit_rules_and_delayed_clock_have_independent_audits(self):
        for rule in ("fixed20", "score"):
            policy = SatellitePolicy("headroom", rule, sizing="headroom")
            for phase, lag in ((0, 1), (5, 2)):
                run = satellite_trial(self.data, self.calendar, policy, phase=phase, lag=lag)
                self.assertEqual(run["signal_validation"]["status"], "passed")

    def test_future_covariance_changes_do_not_change_first_entry(self):
        policy = SatellitePolicy("headroom", "fixed20", sizing="headroom")
        changed = deepcopy(self.data)
        changed["economic_stats"][len(changed["days"])-1]["risk"]["covariance"] *= 100
        # A real replay checks every past target as well as the final account.
        left_run = satellite_trial(self.data, self.calendar, policy)
        right_run = satellite_trial(changed, self.calendar, policy)
        self.assertEqual(left_run, right_run)

    def test_target_tampering_or_whole_core_scaling_rejected(self):
        policy = SatellitePolicy("headroom", "fixed20", sizing="headroom")
        run = satellite_trial(self.high_risk_data, self.calendar, policy)
        checks = deepcopy(run["daily_checks"])
        row = next(r for r in checks if r["targets"] is not None)
        row["targets"]["bond"] *= .9
        with self.assertRaises(ValueError):
            audit_satellite(run["account"], checks, self.high_risk_data, policy)
        with self.assertRaises(ValueError):
            audit_satellite(run["account"], run["daily_checks"], self.high_risk_data, replace(policy, sizing="whole"))


if __name__ == "__main__":
    unittest.main()
