from copy import deepcopy
from dataclasses import replace
import unittest

from backend.analysis.trend_satellite import SatellitePolicy, TrendSatelliteAllocator, market_observation
from scripts.research_satellite_market_gates import gate_policies, gate_trial
from scripts.validate_trend_satellite import audit_satellite
from tests import test_trend_satellite as fixtures


class MarketGateStateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SatelliteStateTests()
        self.fixture.setUp()
        self.data, self.base = self.fixture.data, self.fixture.base
        for i, value in self.data["cross_stats"].items():
            value["signal_index"] = i
            value["signals"]["stock"]["log_returns"] = {21: .01, 63: .02}
        for frame in self.data["frames"].values():
            for entry in frame["entries"]:
                entry.update(price=100., ma120=99.)

    def call(self, allocator, index, weights=None):
        return self.fixture.call(allocator, index, weights)

    def test_closed_direction_blocks_new_tilt_but_preserves_core(self):
        self.data["cross_stats"][252]["signals"]["stock"]["log_returns"][21] = -.01
        allocator = TrendSatelliteAllocator(self.data, gate_policies()["direction_entry"])
        self.assertIsNone(self.call(allocator, 252))
        self.assertIsNone(allocator.memory.candidate)
        self.assertFalse(allocator.checks[-1]["market_observation"]["enabled"])
        self.assertTrue(all(w > 0 for w in allocator.memory.raw_core.values()))

    def test_breadth_excludes_gold_bond_and_direction_alone_is_different(self):
        self.data["frames"][252]["entries"][1]["ma120"] = 101.
        wide = TrendSatelliteAllocator(self.data, gate_policies()["wide_entry"])
        self.assertIsNone(self.call(wide, 252))
        self.assertEqual(wide.checks[-1]["market_observation"]["breadth_ma120"], .5)
        direction = TrendSatelliteAllocator(self.data, gate_policies()["direction_entry"])
        self.assertIsNotNone(self.call(direction, 252))

    def test_entry_only_does_not_force_early_exit_but_exit_gate_withdraws_only_tilt(self):
        for key, should_exit in (("wide_entry", False), ("wide_exit", True)):
            self.data["cross_stats"][257]["signals"]["stock"]["log_returns"][21] = .01
            allocator = TrendSatelliteAllocator(self.data, gate_policies()[key])
            target = self.call(allocator, 252)
            self.call(allocator, 253, target.weights)
            self.data["cross_stats"][257]["signals"]["stock"]["log_returns"][21] = -.01
            exited = self.call(allocator, 257, target.weights)
            if should_exit:
                self.assertEqual(allocator.checks[-1]["event"], "market_exit")
                self.assertEqual(exited.weights, self.base)
                self.assertFalse(exited.risk_exits)
                self.assertIsNone(allocator.memory.candidate)
            else:
                self.assertIsNone(exited)
                self.assertEqual(allocator.memory.candidate, "a")

    def test_gate_is_checked_on_declared_clock_and_own_failure_keeps_priority(self):
        allocator = TrendSatelliteAllocator(self.data, gate_policies()["wide_exit"])
        target = self.call(allocator, 252)
        self.call(allocator, 253, target.weights)
        self.data["cross_stats"][254]["signals"]["stock"]["log_returns"][21] = -.01
        self.assertIsNone(self.call(allocator, 254, target.weights))
        self.assertEqual(allocator.memory.candidate, "a")
        self.data["cross_stats"][257]["signals"]["stock"]["log_returns"][21] = -.01
        self.data["economic_stats"][257]["votes"]["a"] = 0.
        self.call(allocator, 257, target.weights)
        self.assertEqual(allocator.checks[-1]["event"], "absolute_failure")

    def test_breadth_boundary_flat_prices_and_future_cache(self):
        data = {"cross_stats": {252: {"signal_index": 252, "signals": {"stock": {"log_returns": {21: .01, 63: .02}}}}},
                "class_groups": {"stock": ["a", "b", "c", "d", "e"]}}
        entries = {c: {"price": 101. if i < 3 else 99., "ma120": 100.} for i, c in enumerate(data["class_groups"]["stock"])}
        self.assertTrue(market_observation(data, 252, entries, "wide")["enabled"])
        entries["c"]["price"] = 100.
        self.assertFalse(market_observation(data, 252, entries, "wide")["enabled"])
        allocator = TrendSatelliteAllocator(self.data, gate_policies()["wide_entry"])
        result = self.call(allocator, 252)
        self.data["cross_stats"][399]["signals"]["stock"]["log_returns"][21] = -10.
        again = TrendSatelliteAllocator(self.data, gate_policies()["wide_entry"])
        self.assertEqual(result, self.call(again, 252))
        self.assertEqual(allocator.checks, again.checks)

    def test_wrong_date_and_invalid_gate_configuration_rejected(self):
        self.data["cross_stats"][252]["signal_index"] = 253
        allocator = TrendSatelliteAllocator(self.data, gate_policies()["wide_entry"])
        with self.assertRaises(ValueError):
            self.call(allocator, 252)
        for policy in (SatellitePolicy("bad", market_gate="unknown"), SatellitePolicy("bad", exit_on_gate=True),
                       SatellitePolicy("bad", market_gate="wide", exit_on_gate=1), SatellitePolicy("bad", "none", market_gate="wide")):
            with self.assertRaises(ValueError):
                policy.validate()


class MarketGateReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SatelliteReplayTests.setUpClass()
        cls.data, cls.calendar = fixtures.SatelliteReplayTests.data, fixtures.SatelliteReplayTests.calendar

    def test_all_gates_and_seeded_controls_pass_raw_account_audit(self):
        for policy in gate_policies().values():
            for seed in (None, 17):
                result = gate_trial(self.data, self.calendar, replace(policy, random_seed=seed))
                self.assertEqual(result["signal_validation"]["status"], "passed")
                self.assertEqual(result["ledger_validation"]["status"], "passed")

    def test_offset_and_delayed_execution_do_not_omit_safety_checks(self):
        result = gate_trial(self.data, self.calendar, gate_policies()["wide_exit"], start=300, end=430, phase=5, lag=2, deadline=True)
        self.assertEqual(result["signal_validation"]["status"], "passed")

    def test_forged_market_observation_or_inconsistent_breadth_cache_is_rejected(self):
        policy = gate_policies()["wide_entry"]
        result = gate_trial(self.data, self.calendar, policy)
        for mutate in (lambda m: m.update(enabled=not m["enabled"]), lambda m: m.update(breadth_ma120=2.),
                       lambda m: m["log_returns"].update({21: 100.})):
            checks = deepcopy(result["daily_checks"])
            mutate(checks[0]["market_observation"])
            with self.assertRaises(ValueError):
                audit_satellite(result["account"], checks, self.data, policy)
        changed = deepcopy(self.data)
        entry = changed["frames"][252]["entries"][0]
        was_above = entry["price"] > entry["ma120"]*(1+1e-12)
        entry["ma120"] = entry["price"]*(2 if was_above else .5)
        with self.assertRaises(ValueError):
            gate_trial(changed, self.calendar, policy)


if __name__ == "__main__":
    unittest.main()
