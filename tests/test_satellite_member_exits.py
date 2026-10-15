from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import gzip
from hashlib import sha256
import json
from pathlib import Path
import unittest

import numpy as np

from backend.analysis.trend_satellite import SatellitePolicy, TrendSatelliteAllocator, surviving_basket
from scripts.research_trend_satellites import satellite_trial
from scripts.export_strategy_trial import checked_metrics
from scripts.validate_trend_satellite import audit_satellite
from tests import test_satellite_basket as fixtures


class MemberExitStateTests(unittest.TestCase):
    def setUp(self):
        self.basket = fixtures.BasketStateTests()
        self.basket.setUp()
        self.fixture = self.basket.fixture
        self.base = self.basket.base

    def allocator(self, mode):
        return TrendSatelliteAllocator(self.fixture.data,
            SatellitePolicy("members", "fixed20", basket_size=2, member_exit=mode))

    def test_raw_survivor_quota_is_not_redistributed_and_retired_budget_returns_to_donors(self):
        target, full = surviving_basket(self.base, ["a", "b", "c", "d"], ["a", "b"], ["b"])
        self.assertEqual(target["b"], full["b"])
        self.assertEqual(target["a"], self.base["a"])
        self.assertAlmostEqual(target["c"], .05)
        self.assertAlmostEqual(sum(target[c] for c in ("a", "b", "c", "d")), .3)
        self.assertEqual((target["gold"], target["bond"]), (.3, .4))
        for active in ([], ["b", "b"], ["d"]):
            with self.assertRaises(ValueError):
                surviving_basket(self.base, ["a", "b", "c", "d"], ["a", "b"], active)

    def test_partial_failure_keeps_shared_expiry_and_cannot_refill_retired_slot(self):
        allocator = self.allocator("funded")
        full = self.fixture.call(allocator, 252)
        self.fixture.call(allocator, 253, full.weights)
        self.fixture.data["economic_stats"][254]["votes"]["a"] = 0
        partial = self.fixture.call(allocator, 254, full.weights)
        self.assertEqual(allocator.memory.members, ["b"])
        self.assertEqual(allocator.memory.plan_members, ["a", "b"])
        self.assertEqual(allocator.memory.funded_index, 253)
        self.assertEqual(partial.weights["b"], full.weights["b"])
        self.assertEqual(partial.risk_exits, frozenset({"a"}))
        self.fixture.call(allocator, 257, partial.weights)
        self.assertEqual(allocator.memory.members, ["b"])
        expired = self.fixture.call(allocator, 273, partial.weights)
        self.assertEqual(expired.weights, self.base)
        self.assertEqual(allocator.memory.members, [])
        self.assertEqual(allocator.memory.ready_at, 278)

    def test_shared_mode_does_not_keep_healthy_member_before_group_funding(self):
        allocator = self.allocator("funded")
        full = self.fixture.call(allocator, 252)
        incomplete = {**full.weights, "b": .075}
        self.fixture.call(allocator, 253, incomplete)
        self.fixture.data["economic_stats"][254]["votes"]["b"] = 0
        reset = self.fixture.call(allocator, 254, incomplete)
        self.assertEqual(allocator.memory.members, [])
        self.assertEqual(reset.risk_exits, frozenset({"b"}))
        self.assertEqual(reset.weights, self.base)
        self.assertEqual(allocator.checks[-1]["member_events"],
                         [{"code": "a", "reason": "basket_failure"}, {"code": "b", "reason": "absolute_failure"}])

    def test_individual_clock_uses_each_observed_funding_day(self):
        allocator = self.allocator("individual")
        full = self.fixture.call(allocator, 252)
        incomplete = {**full.weights, "b": .075}
        self.fixture.call(allocator, 253, incomplete)
        self.assertEqual(allocator.memory.funded_members, {"a": 253, "b": None})
        self.fixture.call(allocator, 256, full.weights)
        first_expiry = self.fixture.call(allocator, 273, full.weights)
        self.assertEqual(allocator.memory.members, ["b"])
        self.assertEqual(first_expiry.weights["b"], full.weights["b"])
        self.assertEqual(allocator.checks[-1]["member_events"], [{"code": "a", "reason": "expiry"}])
        last_expiry = self.fixture.call(allocator, 276, first_expiry.weights)
        self.assertEqual(last_expiry.weights, self.base)
        self.assertEqual(allocator.memory.members, [])

    def test_individual_timeout_does_not_cancel_already_funded_member(self):
        allocator = self.allocator("individual")
        full = self.fixture.call(allocator, 252)
        incomplete = {**full.weights, "b": .075}
        self.fixture.call(allocator, 253, incomplete)
        timeout = self.fixture.call(allocator, 262, incomplete)
        self.assertEqual(allocator.memory.members, ["a"])
        self.assertEqual(timeout.weights["a"], full.weights["a"])
        self.assertEqual(allocator.checks[-1]["member_events"], [{"code": "b", "reason": "funding_timeout"}])

    def test_risk_capacity_release_cannot_raise_survivor_above_full_plan_same_day(self):
        allocator = self.allocator("individual")
        full = self.fixture.call(allocator, 252)
        self.fixture.call(allocator, 253, full.weights)
        self.fixture.data["economic_stats"][254]["votes"]["a"] = 0
        self.fixture.call(allocator, 254, full.weights)
        self.fixture.data["economic_stats"][255]["risk"]["covariance"] = np.diag([.16, .16, .01, .01, .04, .04])
        active = allocator.goal(255, allocator.memory.candidate)
        full_reference = allocator.goal(255, allocator.memory.candidate, allocator.memory.plan_members)
        self.assertLessEqual(active["b"], full_reference["b"])

    def test_actual_urgent_risk_still_overrides_partial_member_withdrawal(self):
        allocator = self.allocator("individual")
        full = self.fixture.call(allocator, 252)
        self.fixture.call(allocator, 253, full.weights)
        self.fixture.data["economic_stats"][254]["votes"]["a"] = 0
        self.fixture.data["economic_stats"][254]["risk"]["covariance"] = np.eye(6)
        urgent = self.fixture.call(allocator, 254, full.weights)
        self.assertEqual(allocator.checks[-1]["action"], "risk")
        self.assertEqual(urgent.risk_exits, frozenset(self.base))
        self.assertTrue(urgent.enforce_risk_exits)

    def test_invalid_member_mode_or_single_member_policy_is_rejected(self):
        for mode in ("unknown", None, True):
            with self.assertRaises(ValueError):
                SatellitePolicy("bad", "fixed20", basket_size=2, member_exit=mode).validate()
        with self.assertRaises(ValueError):
            SatellitePolicy("bad", "fixed20", member_exit="individual").validate()


class MemberExitReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.BasketReplayTests.setUpClass()
        cls.data = fixtures.BasketReplayTests.data
        cls.calendar = fixtures.BasketReplayTests.calendar

    def test_all_modes_sizes_costs_and_queued_orders_are_independently_audited(self):
        for mode in ("funded", "individual"):
            for size in (2, 3):
                for fee, phase, lag in ((10, 0, 1), (0, 5, 2)):
                    trial = satellite_trial(self.data, self.calendar,
                        SatellitePolicy("members", "fixed20", basket_size=size, member_exit=mode),
                        fee=Decimal(fee), phase=phase, lag=lag)
                    self.assertEqual(trial["signal_validation"]["status"], "passed")
                    self.assertEqual(trial["ledger_validation"]["status"], "passed")

    def test_forged_funding_date_original_plan_and_events_are_rejected(self):
        policy = SatellitePolicy("members", "fixed20", basket_size=3, member_exit="individual")
        trial = satellite_trial(self.data, self.calendar, policy)
        for field in ("funded_members", "plan_members", "member_events"):
            checks = deepcopy(trial["daily_checks"])
            row = next(row for row in checks if row["event"] == "funded")
            if field == "member_events":
                row[field].append({"code": "a", "reason": "expiry"})
            elif field == "plan_members":
                row["memory_after"][field].append("bond")
            else:
                row["memory_after"][field]["a"] = 0
            with self.assertRaises(ValueError):
                audit_satellite(trial["account"], checks, self.data, policy)
        with self.assertRaises(ValueError):
            audit_satellite(trial["account"], trial["daily_checks"], self.data, replace(policy, member_exit="funded"))

    @unittest.skipUnless(Path("runtime/verification/satellite-member-exit-research-v1.json.gz").exists(), "Frozen member-exit report unavailable")
    def test_real_design_retirements_and_per_member_expiry_are_preserved(self):
        path = Path("runtime/verification/satellite-member-exit-research-v1.json.gz")
        self.assertEqual(sha256(path.read_bytes()).hexdigest(), "4ab5930a1345b47f075a54ab28ae5e67be5577bae5fe93259ff6c730ff7c65c8")
        with gzip.open(path, "rt") as stream:
            report = json.load(stream)
        self.assertEqual((report["formal_account_runs"], report["compatible_old_accounts"],
                          report["new_variant_account_runs"], report["fresh_reused_main"]), (400, 88, 312, 8))
        self.assertEqual(set(report["cohorts"]), {"long4", "older4", "older9", "all13"})
        full, partial = 0, 0
        for cohort in report["cohorts"].values():
            self.assertEqual(len(cohort["scenarios"]), 12)
            self.assertEqual(len(cohort["fresh_entry"]), 8)
            for scenario in cohort["scenarios"].values():
                self.assertEqual(len(scenario["episodes"]), 9)
                self.assertEqual(len(scenario["policies"]), 4)
                for key, trial in scenario["policies"].items():
                    checked_metrics(trial)
                    if "account" not in trial:
                        continue
                    full += 1
                    for row in trial["daily_checks"]:
                        before, after = row["memory_before"], row["memory_after"]
                        if after["members"]:
                            self.assertTrue(set(after["members"]) <= set(after["plan_members"]))
                        partial += bool(row["member_events"] and after["members"])
                        if key.startswith("individual"):
                            for event in row["member_events"]:
                                if event["reason"] == "expiry":
                                    funded_at = before["funded_members"][event["code"]]
                                    self.assertIsNotNone(funded_at)
                                    self.assertGreaterEqual(row["signal_index"] - funded_at, 20)
                                elif event["reason"] == "funding_timeout":
                                    self.assertIsNone(before["funded_members"][event["code"]])
                                    self.assertGreaterEqual(row["signal_index"] - before["entry_index"], 10)
            for entry in cohort["fresh_entry"].values():
                for trial in entry["policies"].values():
                    checked_metrics(trial)
        self.assertEqual(full, 32)
        self.assertGreater(partial, 0)


if __name__ == "__main__":
    unittest.main()
