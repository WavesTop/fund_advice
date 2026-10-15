from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import gzip
from hashlib import sha256
import json
import unittest

from backend.analysis.trend_satellite import SatellitePolicy, TrendSatelliteAllocator, market_observation
from scripts.research_satellite_market_gates import gate_trial
from scripts.research_satellite_competition import PROTOCOL
from scripts.export_strategy_trial import checked_metrics
from scripts.validate_trend_satellite import audit_satellite
from tests import test_trend_satellite as fixtures


class CompetitionStateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SatelliteStateTests()
        self.fixture.setUp()
        for index, stats in self.fixture.data["cross_stats"].items():
            stats["signal_index"] = index

    def policy(self, exit_on_gate=False):
        return SatellitePolicy("competition", "fixed20", market_gate="competition", exit_on_gate=exit_on_gate)

    def test_single_qualified_stock_blocks_only_overlay_not_core(self):
        self.fixture.data["frames"][252]["entries"][1]["scores"]["medium"] = 59
        allocator = TrendSatelliteAllocator(self.fixture.data, self.policy())
        self.assertIsNone(self.fixture.call(allocator, 252))
        self.assertIsNone(allocator.memory.candidate)
        self.assertEqual(allocator.memory.raw_core, self.fixture.base)
        self.assertEqual(allocator.checks[-1]["market_observation"]["eligible_codes"], ["a"])

    def test_two_qualified_stocks_at_boundaries_open_gate_without_market_direction(self):
        self.fixture.data["frames"][252]["entries"][1]["scores"]["medium"] = 60
        self.fixture.data["economic_stats"][252]["votes"]["b"] = 2/3
        allocator = TrendSatelliteAllocator(self.fixture.data, self.policy())
        target = self.fixture.call(allocator, 252)
        self.assertIsNotNone(target)
        self.assertEqual(allocator.memory.candidate, "a")
        self.assertEqual(allocator.checks[-1]["market_observation"]["eligible_codes"], ["a", "b"])

    def test_gold_bonds_never_count_and_future_observations_do_not_open_gate(self):
        self.fixture.data["economic_stats"][252]["votes"]["b"] = 0
        entries = {row["code"]: row for row in self.fixture.data["frames"][252]["entries"]}
        before = market_observation(self.fixture.data, 252, entries, "competition")
        self.assertEqual(before["eligible_codes"], ["a"])
        self.fixture.data["economic_stats"][399]["votes"]["b"] = 1
        self.assertEqual(before, market_observation(self.fixture.data, 252, entries, "competition"))

    def test_entry_gate_keeps_lease_but_exit_gate_withdraws_only_tilt(self):
        for exit_on_gate in (False, True):
            allocator = TrendSatelliteAllocator(self.fixture.data, self.policy(exit_on_gate))
            target = self.fixture.call(allocator, 252)
            self.fixture.call(allocator, 253, target.weights)
            self.fixture.data["frames"][257]["entries"][1]["scores"]["medium"] = 59
            withdrawn = self.fixture.call(allocator, 257, target.weights)
            if exit_on_gate:
                self.assertEqual(allocator.checks[-1]["event"], "market_exit")
                self.assertEqual(withdrawn.weights, self.fixture.base)
                self.assertFalse(withdrawn.risk_exits)
            else:
                self.assertIsNone(withdrawn)
                self.assertEqual(allocator.memory.candidate, "a")

    def test_gate_exit_waits_for_check_clock_and_absolute_failure_keeps_priority(self):
        allocator = TrendSatelliteAllocator(self.fixture.data, self.policy(True))
        target = self.fixture.call(allocator, 252)
        self.fixture.call(allocator, 253, target.weights)
        self.fixture.data["frames"][254]["entries"][1]["scores"]["medium"] = 59
        self.assertIsNone(self.fixture.call(allocator, 254, target.weights))
        self.fixture.data["economic_stats"][257]["votes"]["a"] = 0
        self.fixture.call(allocator, 257, target.weights)
        self.assertEqual(allocator.checks[-1]["event"], "absolute_failure")


class CompetitionReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SatelliteReplayTests.setUpClass()
        cls.data = fixtures.SatelliteReplayTests.data
        cls.calendar = fixtures.SatelliteReplayTests.calendar

    def test_entry_and_exit_gates_have_raw_past_audits_with_fees_and_delays(self):
        for exit_on_gate in (False, True):
            for fee, phase, lag in ((10, 0, 1), (0, 5, 2)):
                trial = gate_trial(self.data, self.calendar,
                    SatellitePolicy("competition", "fixed20", market_gate="competition", exit_on_gate=exit_on_gate),
                    fee=Decimal(fee), phase=phase, lag=lag)
                self.assertEqual(trial["signal_validation"]["status"], "passed")
                self.assertEqual(trial["ledger_validation"]["status"], "passed")

    def test_forged_qualified_codes_and_using_other_gate_are_rejected(self):
        policy = SatellitePolicy("competition", "fixed20", market_gate="competition")
        trial = gate_trial(self.data, self.calendar, policy)
        checks = deepcopy(trial["daily_checks"])
        checks[0]["market_observation"]["eligible_codes"].append("bond")
        with self.assertRaises(ValueError):
            audit_satellite(trial["account"], checks, self.data, policy)
        with self.assertRaises(ValueError):
            audit_satellite(trial["account"], trial["daily_checks"], self.data, replace(policy, market_gate="wide"))

    @unittest.skipUnless((PROTOCOL.parents[1] / "runtime/verification/satellite-competition-research-v1.json.gz").exists(), "Frozen competition research unavailable")
    def test_full_real_design_and_qualified_entries_are_retained(self):
        path = PROTOCOL.parents[1] / "runtime/verification/satellite-competition-research-v1.json.gz"
        self.assertEqual(sha256(path.read_bytes()).hexdigest(), "03ea7745015c1a81ff6c06cdcd3125f35dc2d4145da32fa6931aa5bffcd9ef93")
        with gzip.open(path, "rt") as stream:
            report = json.load(stream)
        self.assertEqual((report["formal_account_runs"], report["compatible_old_accounts"],
                          report["new_variant_account_runs"], report["fresh_reused_main"]), (212, 56, 156, 4))
        self.assertEqual(set(report["cohorts"]), {"long4", "older4", "older9", "all13"})
        defaults = 0
        for cohort in report["cohorts"].values():
            self.assertEqual(len(cohort["scenarios"]), 12)
            self.assertEqual(len(cohort["fresh_entry"]), 8)
            for scenario in cohort["scenarios"].values():
                self.assertEqual(len(scenario["episodes"]), 9)
                for policy, trial in scenario["policies"].items():
                    checked_metrics(trial)
                    if policy == "competition_entry":
                        self.assertEqual(trial["events"]["market_exit"], 0)
                    if "account" in trial:
                        defaults += 1
                        for row in trial["daily_checks"]:
                            if row["event"] == "entry":
                                self.assertGreaterEqual(len(row["market_observation"]["eligible_codes"]), 2)
                                self.assertTrue(row["market_observation"]["enabled"])
            for entry in cohort["fresh_entry"].values():
                for trial in entry["policies"].values():
                    checked_metrics(trial)
        self.assertEqual(defaults, 16)
        old = report["cohorts"]["long4"]["original_entry_opportunities"]
        self.assertEqual(old["eligible_count_distribution"], {"1": 58})
        self.assertGreater(report["cohorts"]["long4"]["scenarios"]["fee10_default"]["policies"]["competition_entry"]["events"]["entry"], 0)
        review_path = PROTOCOL.parents[1] / "runtime/verification/satellite-competition-audit-review-v1.json"
        if review_path.exists():
            review = json.loads(review_path.read_text())
            self.assertEqual(review["reference_sha256"], sha256(path.read_bytes()).hexdigest())
            self.assertEqual(review["new_account_replays"], 0)
            self.assertEqual(len(review["checked_frozen_accounts"]), 16)
            self.assertTrue(all(result["ledger"]["status"] == result["signals"]["status"] == "passed"
                                for result in review["checked_frozen_accounts"].values()))


if __name__ == "__main__":
    unittest.main()
