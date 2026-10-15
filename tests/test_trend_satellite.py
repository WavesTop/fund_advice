from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
import math
from pathlib import Path
from types import MappingProxyType
import unittest

import numpy as np

from backend.analysis.fund_allocation import AllocationState
from backend.analysis.portfolio_increment import prepare_economic
from backend.analysis.trend_satellite import SatellitePolicy, TrendSatelliteAllocator, policies
from backend.core.trading_calendar import TradingCalendar
from scripts.research_local_trend_models import prepare_local
from scripts.research_trend_satellites import satellite_trial
from scripts.validate_trend_satellite import audit_satellite


class SatelliteStateTests(unittest.TestCase):
    def setUp(self):
        codes = ["a", "b", "gold", "bond"]
        self.base = {"a": .15, "b": .15, "gold": .3, "bond": .4}
        self.data = {"days": [f"day{i}" for i in range(400)], "prices": dict.fromkeys(codes, [100.]*400),
            "marks": dict.fromkeys(codes, [100.]*400), "class_groups": {"stock": ["a", "b"], "gold": ["gold"], "bond": ["bond"]},
            "economic_stats": {i: {"signal_index": i, "votes": dict.fromkeys(codes, 1.),
                "risk": {"signal_index": i, "codes": codes, "covariance": np.zeros((4, 4))}} for i in range(252, 400)},
            "cross_stats": {i: {"signals": {g: {"floored_annual_vol": .1} for g in ("stock", "gold", "bond")}} for i in range(252, 400)},
            "frames": {i: {"entries": [{"code": c, "scores": {"medium": 80. if c == "a" else 70.}, "annual_vol60": .1}
                for c in codes]} for i in range(252, 400)}}

    def call(self, allocator, i, weights=None):
        weights = dict(self.base if weights is None else weights)
        state = AllocationState(i, self.data["days"][i], MappingProxyType(weights))
        return allocator(i, {c for c, w in weights.items() if w > 0}, state)

    def test_entry_transfers_only_stock_budget_with_no_core_expiry(self):
        allocator = TrendSatelliteAllocator(self.data, SatellitePolicy("score"))
        target = self.call(allocator, 252)
        self.assertAlmostEqual(target.weights["a"], .25)
        self.assertAlmostEqual(target.weights["b"], .05)
        self.assertEqual(target.weights["gold"], .3)
        self.assertEqual(target.weights["bond"], .4)
        self.assertIsNone(allocator.memory.funded_index)
        self.call(allocator, 253, target.weights)
        self.assertEqual(allocator.memory.funded_index, 253)
        self.assertIsNone(self.call(allocator, 273, target.weights))
        self.assertEqual(allocator.memory.candidate, "a")

    def test_fixed_age_starts_from_observed_exposure_not_request(self):
        allocator = TrendSatelliteAllocator(self.data, SatellitePolicy("fixed", "fixed20"))
        target = self.call(allocator, 252)
        self.call(allocator, 253)
        self.call(allocator, 255, target.weights)
        self.assertEqual(allocator.memory.funded_index, 255)
        self.call(allocator, 274, target.weights)
        self.assertEqual(allocator.memory.candidate, "a")
        expired = self.call(allocator, 275, target.weights)
        self.assertIsNone(allocator.memory.candidate)
        self.assertAlmostEqual(expired.weights["a"], .15)
        self.assertTrue(all(w > 0 for w in expired.weights.values()))

    def test_score_decay_exit_and_wait_for_actual_core_restoration(self):
        allocator = TrendSatelliteAllocator(self.data, SatellitePolicy("score"))
        goal = self.call(allocator, 252)
        self.call(allocator, 253, goal.weights)
        self.data["frames"][257]["entries"][0]["scores"]["medium"] = 69.
        target = self.call(allocator, 257, goal.weights)
        self.assertEqual(allocator.checks[-1]["event"], "score_decay")
        self.assertEqual(target.weights, self.base)
        self.call(allocator, 262, goal.weights)
        self.assertIsNone(allocator.memory.candidate)
        self.call(allocator, 267, self.base)
        self.assertEqual(allocator.memory.candidate, "a")

    def test_funding_timeout_does_not_create_an_investment(self):
        allocator = TrendSatelliteAllocator(self.data, SatellitePolicy("score"))
        self.call(allocator, 252)
        self.call(allocator, 262)
        self.assertEqual(allocator.checks[-1]["event"], "funding_timeout")
        self.assertIsNone(allocator.memory.candidate)
        self.assertIsNone(allocator.memory.funded_index)

    def test_absolute_failure_only_removes_overlay_not_healthy_core(self):
        allocator = TrendSatelliteAllocator(self.data, SatellitePolicy("score"))
        goal = self.call(allocator, 252)
        self.call(allocator, 253, goal.weights)
        self.data["economic_stats"][254]["votes"]["a"] = 0.
        target = self.call(allocator, 254, goal.weights)
        self.assertEqual(target.risk_exits, frozenset({"a"}))
        self.assertAlmostEqual(target.weights["a"], .15)
        self.assertEqual(target.weights["gold"], .3)

    def test_volatility_trail_and_daily_risk_priority(self):
        allocator = TrendSatelliteAllocator(self.data, SatellitePolicy("price", "price"))
        goal = self.call(allocator, 252)
        self.call(allocator, 253, goal.weights)
        self.data["marks"]["a"] = [100.]*400
        self.data["marks"]["a"][257] = 90.
        self.call(allocator, 257, goal.weights)
        self.assertEqual(allocator.checks[-1]["event"], "volatility_trail")
        self.data["economic_stats"][258]["risk"]["covariance"] = np.eye(4)
        target = self.call(allocator, 258, self.base)
        self.assertEqual(allocator.checks[-1]["action"], "risk")
        self.assertEqual(target.risk_exits, frozenset(self.base))
        self.assertLess(sum(target.weights.values()), 1.)

    def test_future_cache_changes_do_not_change_past_entry(self):
        left = TrendSatelliteAllocator(self.data, SatellitePolicy("score"))
        target = self.call(left, 252)
        self.data["economic_stats"][399]["votes"]["a"] = 0.
        right = TrendSatelliteAllocator(self.data, SatellitePolicy("score"))
        self.assertEqual(target, self.call(right, 252))
        self.assertEqual(left.checks, right.checks)

    def test_random_choice_depends_only_on_seed_date_and_eligible_codes(self):
        policy = SatellitePolicy("random", "fixed20", random_seed=17)
        allocator = TrendSatelliteAllocator(self.data, policy)
        self.call(allocator, 252)
        expected = min(("a", "b"), key=lambda c: sha256(f"17|day252|{c}".encode()).hexdigest())
        self.assertEqual(allocator.memory.candidate, expected)
        self.data["class_groups"]["stock"].reverse()
        self.data["frames"][399]["entries"][0]["scores"]["medium"] = 0.
        repeated = TrendSatelliteAllocator(self.data, policy)
        self.call(repeated, 252)
        self.assertEqual(repeated.memory.candidate, expected)
        self.assertEqual(allocator.checks, repeated.checks)

    def test_random_choice_never_bypasses_same_entry_gate(self):
        self.data["economic_stats"][252]["votes"]["b"] = 0.
        for seed in range(8):
            allocator = TrendSatelliteAllocator(self.data, SatellitePolicy("random", "fixed20", random_seed=seed))
            self.call(allocator, 252)
            self.assertEqual(allocator.memory.candidate, "a")
        for value in (-1, True, 1.2, "1"):
            with self.assertRaises(ValueError):
                SatellitePolicy("random", "fixed20", random_seed=value).validate()
        with self.assertRaises(ValueError):
            SatellitePolicy("ignored", "none", random_seed=1).validate()


class SatelliteReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1]/"config/research-exchange-calendar-2009-2026-v2.json"
        cls.calendar = TradingCalendar(json.loads(path.read_text()))
        days = [d.isoformat() for d in cls.calendar.sessions(date(2020, 1, 1), date(2023, 1, 1))][:520]
        funds = {}
        for j, code in enumerate(("a", "b", "000217", "bond", "000051")):
            prices = [math.exp(.0004*i+.05*math.sin(i/(10+j))) for i in range(len(days))]
            funds[code] = {"code": code, "daily_start": days[0], "last_date": days[-1],
                "rows": {d: {"unit_nav": str(p)} for d, p in zip(days, prices)}, "distributions": dict.fromkeys(days, 0.),
                "theme": "黄金" if code == "000217" else "other", "role": "defensive" if code == "bond" else "sector"}
        cls.data = prepare_economic(prepare_local(funds, ["a", "b", "000217", "bond"], cls.calendar))

    def test_all_main_rules_have_independently_audited_accounts(self):
        for policy in policies():
            result = satellite_trial(self.data, self.calendar, policy)
            self.assertEqual(result["signal_validation"]["status"], "passed")
            self.assertEqual(result["ledger_validation"]["status"], "passed")

    def test_delayed_orders_and_phase_and_terminal_guard(self):
        result = satellite_trial(self.data, self.calendar, policies()[3], start=300, end=430, lag=2, phase=5, deadline=True)
        self.assertEqual(result["signal_validation"]["status"], "passed")

    def test_mutated_state_action_scores_or_missing_daily_check_rejected(self):
        result = satellite_trial(self.data, self.calendar, policies()[3])
        for mutate in (lambda r: r[0]["memory_after"].update(ready_at=9999),
                       lambda r: r[0]["scores"].update(a=0.), lambda r: r[0].update(event="tampered"),
                       lambda r: r.pop(1)):
            changed = deepcopy(result["daily_checks"]); mutate(changed)
            with self.assertRaises(ValueError):
                audit_satellite(result["account"], changed, self.data, policies()[3])

    def test_future_or_corrupt_class_volatility_cache_is_rejected(self):
        changed = deepcopy(self.data)
        changed["cross_stats"][252]["signals"]["stock"]["floored_annual_vol"] *= 10
        with self.assertRaises(ValueError):
            satellite_trial(changed, self.calendar, policies()[3])

    def test_unrebuildable_cached_trend_score_is_rejected(self):
        changed = deepcopy(self.data)
        changed["frames"][252]["entries"][0]["scores"]["medium"] = 101.
        with self.assertRaises(ValueError):
            satellite_trial(changed, self.calendar, policies()[3])

    def test_random_paths_have_independent_audits_and_repeat_exactly(self):
        for rule in ("fixed20", "price"):
            policy = SatellitePolicy("random", rule, random_seed=17)
            first = satellite_trial(self.data, self.calendar, policy)
            repeated = satellite_trial(self.data, self.calendar, policy)
            self.assertEqual(first, repeated)
            self.assertEqual(first["signal_validation"]["status"], "passed")


if __name__ == "__main__":
    unittest.main()
