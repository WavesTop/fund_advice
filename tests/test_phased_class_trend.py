from copy import deepcopy
import unittest

from backend.analysis.cross_asset_trend import class_statistics, prepare_class_proxies
from backend.analysis.fund_allocation import AllocationState
from backend.analysis.phased_class_trend import POLICIES, phase_view_indices, phased_class_target
from scripts.validate_phased_class_trend import audit_phased_decision
from scripts.research_phased_class_trends import phased_trial
from tests import test_cross_asset_trend as fixtures
from tests.test_trend_risk_budget import calendar_fixture


class PhasedClassTrendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = fixtures.CrossAssetTests.data(cls)
        cls.base["cross_stats"] = {i: class_statistics(cls.base, i) for i in range(252, 281)}

    def data(self):
        return deepcopy(self.base)

    def target(self, data, policy="dual_return60", weights=None, phase=0):
        weights = dict.fromkeys(data["prices"], 0.) if weights is None else weights
        state = AllocationState(280, data["days"][280], weights)
        return phased_class_target(data, policy, 280, {c for c, w in weights.items() if w > 0},
                                   start=252, phase=phase, portfolio_state=state)

    def test_common_initial_views_and_calendar_updates_use_only_past_dates(self):
        self.assertEqual(phase_view_indices(252, 252), (252, 252, 252, 252))
        self.assertEqual(phase_view_indices(252, 257), (252, 257, 252, 252))
        self.assertEqual(phase_view_indices(252, 280), (272, 277, 262, 267))
        for phase in range(20):
            for at in range(252, 281):
                self.assertTrue(all(252 <= i <= at for i in phase_view_indices(252, at, phase)))

    def test_five_day_phase_alias_is_permutation_not_independent_evidence(self):
        for at in range(252, 281):
            self.assertEqual(phase_view_indices(252, at, 0), phase_view_indices(252, at, 5))
        self.assertNotEqual(phase_view_indices(252, 280, 0), phase_view_indices(252, 280, 1))
        for bad in ((252, 251, 0), (True, 280, 0), (252, 280, True), (252, 280, 20)):
            with self.assertRaises(ValueError):
                phase_view_indices(*bad)

    def test_mixed_winners_share_budget_and_latest_failure_cannot_be_stale(self):
        data = self.data()
        for position, at in enumerate(phase_view_indices(252, 280)):
            signals = fixtures.CrossAssetTests.signals(self)
            signals["stock" if position < 2 else "gold"]["normalised_return_score"] = 100.
            data["cross_stats"][at]["signals"] = signals
        data["cross_stats"][280]["signals"] = fixtures.CrossAssetTests.signals(self)
        target, meta = self.target(data)
        self.assertAlmostEqual(meta["class_proposal"]["stock"], .3)
        self.assertAlmostEqual(meta["class_proposal"]["gold"], .3)
        self.assertAlmostEqual(meta["class_proposal"]["bond"], .4)
        data["cross_stats"][280]["signals"]["stock"]["vote"] = 1/3
        stock = data["class_groups"]["stock"][0]
        weights = dict.fromkeys(data["prices"], 0.) | {stock: .1}
        target, meta = self.target(data, weights=weights)
        self.assertEqual(meta["class_proposal"]["stock"], 0.)
        self.assertEqual(target.weights[stock], 0.)
        self.assertIn(stock, target.risk_exits)
        self.assertTrue(target.enforce_risk_exits)

    def test_static_control_does_not_acquire_a_trend_gate(self):
        data = self.data()
        for at in {*phase_view_indices(252, 280), 280}:
            signals = fixtures.CrossAssetTests.signals(self)
            for entry in signals.values():
                entry["vote"] = 0.
            data["cross_stats"][at]["signals"] = signals
        _, meta = self.target(data, "risky_pair_static60")
        self.assertAlmostEqual(meta["class_proposal"]["stock"], .2)
        self.assertAlmostEqual(meta["class_proposal"]["gold"], .4)
        self.assertEqual(meta["class_proposal"]["bond"], .4)
        self.assertFalse(meta["phase_mix"]["latest_absolute_gate_applied"])

    def test_current_covariance_and_each_past_view_are_independently_checked(self):
        data = self.data()
        weights = dict.fromkeys(data["prices"], .1)
        for policy in POLICIES:
            target, meta = self.target(data, policy, weights=weights)
            self.assertLessEqual(meta["projected_volatility"], .1 + 1e-10)
            self.assertLessEqual(sum(target.weights.values()), 1 + 1e-12)
            audit_phased_decision(meta | {"targets": target.weights}, data, 280, policy, start=252, phase=0)

    def test_future_nav_suffix_cannot_change_past_views_targets_or_urgency(self):
        data = self.data()
        changed = deepcopy(data)
        for marks in changed["marks"].values():
            marks[281:] = [value * 10 for value in marks[281:]]
        prepare_class_proxies(changed)
        changed["cross_stats"] = {i: class_statistics(changed, i) for i in range(252, 281)}
        for policy in POLICIES:
            self.assertEqual(self.target(data, policy), self.target(changed, policy))

    def test_future_view_wrong_phase_and_blend_tampering_are_rejected(self):
        data = self.data()
        target, meta = self.target(data)
        entry = meta | {"targets": target.weights}
        for field, value in (("phase", 1), ("cycle_sessions", 10), ("latest_absolute_gate_applied", False),
                             ("raw_blended_proposal", {"stock": float("nan"), "gold": 0., "bond": .4})):
            bad = deepcopy(entry)
            bad["phase_mix"][field] = value
            with self.assertRaises(ValueError):
                audit_phased_decision(bad, data, 280, "dual_return60", start=252, phase=0)
        bad = deepcopy(entry)
        bad["phase_mix"]["views"][0]["signal_index"] = 281
        with self.assertRaises(ValueError):
            audit_phased_decision(bad, data, 280, "dual_return60", start=252, phase=0)

    def test_mismatched_cached_view_date_fails_before_allocation(self):
        data = self.data()
        data["cross_stats"][272]["signal_index"] = 273
        with self.assertRaises(ValueError):
            self.target(data)

    def test_shared_cash_fifo_account_and_all_three_variants_are_audited(self):
        data = self.data()
        for variant in ("grid20", "grid5", "phaseblend"):
            trial = phased_trial(data, calendar_fixture(), "dual_return60", variant, end=280)
            self.assertEqual(trial["ledger_validation"]["status"], "passed")
            self.assertEqual(trial["current_holdings_validation"]["status"], "passed")
            self.assertEqual(trial["signal_and_risk_validation"]["status"], "passed")
            self.assertEqual(trial["account"]["initial_equity"], "100000")
            self.assertEqual(trial["account"]["equity_curve"][-1]["positions"], 0)
