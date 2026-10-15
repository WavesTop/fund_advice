from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import gzip
from hashlib import sha256
import json
from pathlib import Path
import unittest

import numpy as np

from backend.analysis.trend_satellite import SatellitePolicy, restore_defensive_target
from scripts.research_trend_satellites import satellite_trial
from scripts.export_strategy_trial import checked_metrics
from scripts.validate_trend_satellite import audit_satellite
from tests import test_satellite_member_exits as fixtures


class DefensiveRestoreTests(unittest.TestCase):
    def target(self, base, neutral, gold_vol=0):
        risk = {"codes": list(base), "covariance": np.diag([0., gold_vol**2, 0.])}
        return restore_defensive_target(risk, base, neutral, ["gold", "bond"])

    def test_only_defensive_targets_change(self):
        base = {"stock": .2, "gold": .15, "bond": .2}
        neutral = {"stock": .1, "gold": .3, "bond": .4}
        self.assertEqual(self.target(base, neutral), {"stock": .2, "gold": .3, "bond": .4})
        self.assertEqual(self.target(neutral, base), neutral)

    def test_cash_and_risk_caps(self):
        base = {"stock": .5, "gold": .2, "bond": .2}
        result = self.target(base, {"stock": .2, "gold": .3, "bond": .4})
        self.assertEqual(result["stock"], .5)
        self.assertAlmostEqual(sum(result.values()), 1)
        self.assertAlmostEqual(result["gold"], .2 + .1/3)
        base = {"stock": .2, "gold": .05, "bond": .2}
        result = self.target(base, {**base, "gold": .3}, 1)
        self.assertAlmostEqual(result["gold"], .1)
        self.assertEqual(result["stock"], .2)
        with self.assertRaises(ValueError):
            self.target({**base, "gold": .2}, {**base, "gold": .3}, 1)

    def test_flag_requires_independent_member_exit(self):
        for value in (None, 1, "true"):
            with self.assertRaises(ValueError):
                SatellitePolicy("bad", "fixed20", basket_size=2, member_exit="funded", restore_defensive=value).validate()
        with self.assertRaises(ValueError):
            SatellitePolicy("bad", "fixed20", basket_size=2, restore_defensive=True).validate()

    def test_partial_restoration_leaves_every_stock_target_exactly_unchanged(self):
        fixture = fixtures.MemberExitStateTests()
        fixture.setUp()
        allocator = fixture.allocator("individual")
        full = fixture.fixture.call(allocator, 252)
        fixture.fixture.call(allocator, 253, full.weights)
        allocator.policy = replace(allocator.policy, restore_defensive=True)
        self.assertEqual(allocator.goal(253, allocator.memory.candidate), full.weights)
        fixture.fixture.data["economic_stats"][254]["votes"]["a"] = 0
        fixture.fixture.call(allocator, 254, full.weights)
        fixture.fixture.data["economic_stats"][255]["risk"]["covariance"] = np.diag([.16, .16, .01, .01, .04, .04])
        restored = allocator.goal(255, allocator.memory.candidate)
        allocator.policy = replace(allocator.policy, restore_defensive=False)
        original = allocator.goal(255, allocator.memory.candidate)
        for code in ("a", "b", "c", "d"):
            self.assertEqual(restored[code], original[code])
        self.assertGreater(restored["gold"] + restored["bond"], original["gold"] + original["bond"])
        allocator.policy = replace(allocator.policy, restore_defensive=True)
        fixture.fixture.call(allocator, 273, restored)
        self.assertEqual(allocator.memory.members, [])

    def test_actual_urgent_risk_still_exits_all_held_codes(self):
        fixture = fixtures.MemberExitStateTests()
        fixture.setUp()
        allocator = fixture.allocator("individual")
        allocator.policy = replace(allocator.policy, restore_defensive=True)
        full = fixture.fixture.call(allocator, 252)
        fixture.fixture.call(allocator, 253, full.weights)
        fixture.fixture.data["economic_stats"][254]["votes"]["a"] = 0
        fixture.fixture.data["economic_stats"][254]["risk"]["covariance"] = np.eye(6)
        urgent = fixture.fixture.call(allocator, 254, full.weights)
        self.assertEqual(urgent.risk_exits, frozenset(fixture.base))
        self.assertTrue(urgent.enforce_risk_exits)


class DefensiveRestoreReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.MemberExitReplayTests.setUpClass()
        cls.data = fixtures.MemberExitReplayTests.data
        cls.calendar = fixtures.MemberExitReplayTests.calendar

    def test_all_modes_sizes_costs_and_queued_orders_are_independently_audited(self):
        for mode in ("funded", "individual"):
            for size in (2, 3):
                for fee, phase, lag in ((10, 0, 1), (0, 5, 2)):
                    trial = satellite_trial(self.data, self.calendar,
                        SatellitePolicy("restore", "fixed20", basket_size=size, member_exit=mode, restore_defensive=True),
                        fee=Decimal(fee), phase=phase, lag=lag)
                    self.assertEqual(trial["signal_validation"]["status"], "passed")
                    self.assertEqual(trial["ledger_validation"]["status"], "passed")

    def test_forged_defensive_goal_is_rejected(self):
        policy = SatellitePolicy("restore", "fixed20", basket_size=3,
                                 member_exit="individual", restore_defensive=True)
        trial = satellite_trial(self.data, self.calendar, policy)
        checks = deepcopy(trial["daily_checks"])
        row = next(row for row in checks if row["targets"])
        row["targets"][self.data["class_groups"]["gold"][0]] += .01
        with self.assertRaises(ValueError):
            audit_satellite(trial["account"], checks, self.data, policy)

    @unittest.skipUnless(Path("runtime/verification/satellite-defensive-restore-research-v1.json.gz").exists(),
                         "Frozen restoration report unavailable")
    def test_frozen_full_design_and_curve_metrics(self):
        path = Path("runtime/verification/satellite-defensive-restore-research-v1.json.gz")
        self.assertEqual(sha256(path.read_bytes()).hexdigest(),
                         "1d9d07e95e0919f60c70a13cfa5937b6a9af0fbb7e227fed03197d3236bc9bdb")
        with gzip.open(path, "rt") as stream:
            report = json.load(stream)
        self.assertEqual(tuple(report[key] for key in ("formal_account_runs", "compatible_old_accounts",
                         "new_variant_account_runs", "fresh_reused_main")), (432, 120, 312, 8))
        self.assertEqual(set(report["cohorts"]), {"long4", "older4", "older9", "all13"})
        full = 0
        for cohort in report["cohorts"].values():
            self.assertEqual(len(cohort["scenarios"]), 12)
            self.assertEqual(len(cohort["fresh_entry"]), 8)
            for scenario in cohort["scenarios"].values():
                self.assertEqual(len(scenario["episodes"]), 9)
                self.assertEqual(len(scenario["policies"]), 4)
                self.assertEqual(set(scenario["restoration_comparisons"]), set(scenario["policies"]))
                for trial in scenario["policies"].values():
                    checked_metrics(trial)
                    full += "account" in trial
            for entry in cohort["fresh_entry"].values():
                self.assertEqual(set(entry["restoration_comparisons"]), set(entry["policies"]))
                for trial in entry["policies"].values():
                    checked_metrics(trial)
        self.assertEqual(full, 32)
