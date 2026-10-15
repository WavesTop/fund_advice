from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import gzip
from hashlib import sha256
import json
import unittest

import numpy as np

from backend.analysis.trend_satellite import SatellitePolicy, TrendSatelliteAllocator
from scripts.research_trend_satellites import satellite_trial
from scripts.research_satellite_separate_repair import PROTOCOL, reference_case
from scripts.validate_trend_satellite import audit_satellite
from tests import test_trend_satellite as fixtures


class SeparateRepairStateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SatelliteStateTests()
        self.fixture.setUp()

    def delayed_entry(self, scope):
        allocator = TrendSatelliteAllocator(self.fixture.data,
            SatellitePolicy("fixed", "fixed20", repair_scope=scope))
        for row in self.fixture.data["frames"][252]["entries"]:
            row["scores"]["medium"] = 0
        self.fixture.call(allocator, 252)
        self.fixture.call(allocator, 253)
        drift = {**self.fixture.base, "gold": .269}
        target = self.fixture.call(allocator, 257, drift)
        return allocator, target

    def test_satellite_completion_ignores_defensive_gap_but_core_review_does_not(self):
        allocator, goal = self.delayed_entry("separate")
        filled = {**goal.weights, "gold": .269}
        self.assertFalse(allocator.memory.core_work)
        self.assertTrue(allocator.memory.satellite_work)
        self.assertIsNone(self.fixture.call(allocator, 258, filled))
        self.assertFalse(allocator.memory.work)
        self.assertEqual(allocator.memory.funded_index, 258)
        renewed = self.fixture.call(allocator, 272, filled)
        self.assertIsNotNone(renewed)
        self.assertEqual(renewed.weights["gold"], .3)
        self.assertTrue(allocator.memory.core_work)

    def test_default_combined_completion_still_repairs_defensive_gap(self):
        allocator, goal = self.delayed_entry("combined")
        renewed = self.fixture.call(allocator, 258, {**goal.weights, "gold": .269})
        self.assertIsNotNone(renewed)
        self.assertEqual(renewed.weights["gold"], .3)
        self.assertNotIn("core_work", allocator.checks[-1]["memory_after"])

    def test_stock_gap_still_uses_full_original_risk_limited_target(self):
        allocator, goal = self.delayed_entry("separate")
        incomplete = {**goal.weights, "a": .17, "b": .13, "gold": .269}
        continued = self.fixture.call(allocator, 258, incomplete)
        self.assertEqual(continued.weights, goal.weights)
        self.assertTrue(allocator.memory.satellite_work)

    def test_exit_waits_for_stock_restoration_and_keeps_cooldown(self):
        allocator, goal = self.delayed_entry("separate")
        self.fixture.call(allocator, 258, {**goal.weights, "gold": .269})
        self.fixture.call(allocator, 279, goal.weights)
        self.assertEqual(allocator.checks[-1]["event"], "expiry")
        restored = {**self.fixture.base, "gold": .269}
        self.fixture.call(allocator, 280, restored)
        self.assertIsNone(allocator.memory.closing_candidate)
        self.assertIsNone(allocator.memory.candidate)
        self.fixture.call(allocator, 282, restored)
        self.assertIsNone(allocator.memory.candidate)
        self.fixture.call(allocator, 287, restored)
        self.assertEqual(allocator.memory.candidate, "a")

    def test_risk_priority_and_repair_mode_are_not_bypassed(self):
        for repair in ("immediate", "event"):
            allocator = TrendSatelliteAllocator(self.fixture.data,
                SatellitePolicy("split", "fixed20", repair_scope="separate", risk_repair=repair))
            goal = self.fixture.call(allocator, 252)
            self.fixture.call(allocator, 253, goal.weights)
            self.fixture.data["economic_stats"][254]["risk"]["covariance"] = np.eye(4)
            urgent = self.fixture.call(allocator, 254, goal.weights)
            self.assertEqual(allocator.checks[-1]["action"], "risk")
            self.assertEqual(urgent.risk_exits, frozenset(self.fixture.base))
            self.assertTrue(urgent.enforce_risk_exits)
            self.assertEqual(allocator.memory.core_work, repair == "immediate")
            self.assertFalse(allocator.memory.satellite_work)
            result = self.fixture.call(allocator, 255, urgent.weights)
            self.assertEqual(result is None, repair == "event")

    def test_invalid_repair_scope_rejected(self):
        for value in ("other", None, True):
            with self.assertRaises(ValueError):
                SatellitePolicy("bad", repair_scope=value).validate()


class SeparateRepairReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SatelliteReplayTests.setUpClass()
        cls.data = fixtures.SatelliteReplayTests.data
        cls.calendar = fixtures.SatelliteReplayTests.calendar

    def test_core_only_matches_combined_actual_account_for_costs_and_lags(self):
        for phase, lag, fee in ((0, 1, 10), (5, 2, 0)):
            core = SatellitePolicy("core", "none")
            a = satellite_trial(self.data, self.calendar, core, phase=phase, lag=lag, fee=Decimal(fee))
            b = satellite_trial(self.data, self.calendar, replace(core, repair_scope="separate"), phase=phase, lag=lag, fee=Decimal(fee))
            self.assertEqual(a["account"], b["account"])
            self.assertEqual(a["metrics"], b["metrics"])

    def test_separate_overlay_has_raw_data_audits_for_fees_and_delays(self):
        for phase, lag, fee in ((0, 1, 10), (5, 2, 0)):
            run = satellite_trial(self.data, self.calendar,
                SatellitePolicy("split", "fixed20", repair_scope="separate"), phase=phase, lag=lag, fee=Decimal(fee))
            self.assertEqual(run["ledger_validation"]["status"], "passed")
            self.assertEqual(run["signal_validation"]["status"], "passed")

    def test_forged_queue_flags_and_combined_audit_are_rejected(self):
        policy = SatellitePolicy("split", "fixed20", repair_scope="separate")
        run = satellite_trial(self.data, self.calendar, policy)
        checks = deepcopy(run["daily_checks"])
        checks[0]["memory_after"]["core_work"] = not checks[0]["memory_after"]["core_work"]
        with self.assertRaises(ValueError):
            audit_satellite(run["account"], checks, self.data, policy)
        with self.assertRaises(ValueError):
            audit_satellite(run["account"], run["daily_checks"], self.data, replace(policy, repair_scope="combined"))

    @unittest.skipUnless((PROTOCOL.parents[1] / "runtime/verification/satellite-separate-repair-research-v1.json.gz").exists(), "Frozen separate-repair report unavailable")
    def test_real_all_condition_core_metrics_match_and_missing_controls_stay_missing(self):
        root = PROTOCOL.parents[1]
        path = root / "runtime/verification/satellite-separate-repair-research-v1.json.gz"
        self.assertEqual(sha256(path.read_bytes()).hexdigest(), "e39e5426a594c455027345055c97241747b54dbb1bcbafbb3f41408ee683f58f")
        with gzip.open(path, "rt") as stream:
            report = json.load(stream)
        references = {}
        for label, definition in report["protocol"]["references"].items():
            path = root / definition["path"]
            self.assertEqual(sha256(path.read_bytes()).hexdigest(), definition["sha256"])
            with gzip.open(path, "rt") as stream:
                references[label] = json.load(stream)
        self.assertEqual((report["formal_account_runs"], report["compatible_old_accounts"],
                          report["new_variant_account_runs"], report["fresh_reused_main"]), (196, 40, 156, 4))
        self.assertEqual(set(report["cohorts"]), {"long4", "older4", "older9", "all13"})
        for name, cohort in report["cohorts"].items():
            normal = references["ordinary10"]["cohorts"][name]
            low = references["ordinary0"]["cohorts"][name]
            self.assertEqual(len(cohort["scenarios"]), 12)
            self.assertEqual(len(cohort["fresh_entry"]), 8)
            for key, scenario in cohort["scenarios"].items():
                fee_text, timing = key.split("_", 1)
                base, benchmark, overlay = reference_case(normal, low, int(fee_text[3:]), timing)
                self.assertEqual(scenario["policies"]["separate_core"]["metrics"], base["metrics"])
                self.assertEqual(len(scenario["episodes"]), 9)
                if overlay is None:
                    self.assertIsNone(scenario["comparisons"]["trend_minus_original_overlay_pp"])
                for trial in scenario["policies"].values():
                    audits = [value for field, value in trial.items() if field.endswith("validation")]
                    self.assertTrue(audits)
                    self.assertTrue(all(value["status"] == "passed" for value in audits))
            for key, entry in cohort["fresh_entry"].items():
                fee_text, day = key.split("_", 1)
                base = (low if fee_text == "fee0" else normal)["fresh_entry"][day]["policies"]["matched_core"]
                self.assertEqual(entry["policies"]["separate_core"]["metrics"], base["metrics"])


if __name__ == "__main__":
    unittest.main()
