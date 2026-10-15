from copy import deepcopy
import unittest
import numpy as np

from backend.analysis.fund_allocation import AllocationState
from backend.analysis.cross_asset_trend import POLICIES, class_proposal, class_statistics, cross_asset_target, prepare_class_proxies
from scripts.validate_cross_asset_trend import audit_cross_asset_decision
from tests import test_macro_trend_budget as fixtures


class CrossAssetTests(unittest.TestCase):
    def data(self):
        return prepare_class_proxies(fixtures.fixture())

    def signals(self):
        return {c: {"vote": 1., "floored_annual_vol": v, "normalised_return_score": i, "recent_state": "steady_up"}
                for i, (c, v) in enumerate((("stock", .2), ("gold", .1), ("bond", .01)))}

    def test_proxies_are_signal_only_equal_daily_baskets(self):
        data = self.data()
        codes = data["class_groups"]["stock"]
        expected = sum(data["marks"][c][1] / data["marks"][c][0] - 1 for c in codes) / len(codes)
        self.assertAlmostEqual(data["class_proxies"]["stock"][1], 1 + expected)
        self.assertEqual(data["class_proxies"]["stock"][0], 1.)

    def test_future_suffix_does_not_change_class_signals_or_targets(self):
        original = self.data()
        changed = deepcopy(original)
        for code, marks in changed["marks"].items():
            for i in range(281, len(marks)):
                marks[i] *= 10
        prepare_class_proxies(changed)
        left, right = class_statistics(original, 280), class_statistics(changed, 280)
        self.assertEqual(left["signals"], right["signals"])
        for policy in POLICIES:
            l, lm = cross_asset_target(original, policy, 280, (), stats=left)
            r, rm = cross_asset_target(changed, policy, 280, (), stats=right)
            self.assertEqual(l, r)
            self.assertEqual(lm, rm)
            if policy in ("dual_return60", "risky_pair60", "risky_pair_static60"):
                current = dict.fromkeys(original["prices"], .1)
                state = AllocationState(280, original["days"][280], current)
                l, lm = cross_asset_target(original, policy, 280, set(current), stats=left,
                                          exit_mode="absolute_or_current_risk", portfolio_state=state)
                r, rm = cross_asset_target(changed, policy, 280, set(current), stats=right,
                                          exit_mode="absolute_or_current_risk", portfolio_state=state)
                self.assertEqual(l, r)
                self.assertEqual(lm, rm)

    def test_warmup_and_both_date_caches_are_checked(self):
        data = self.data()
        with self.assertRaises(ValueError):
            class_statistics(data, 251)
        stats = class_statistics(data, 252)
        bad = deepcopy(stats)
        bad["fund_risk"]["signal_index"] = 253
        with self.assertRaises(ValueError):
            cross_asset_target(data, "fixed_cash40", 252, (), stats=bad)

    def test_recent_state_uses_local_marks_not_cumulative_proxy_origin(self):
        data = self.data()
        before = class_statistics(data, 252)
        for name in data["class_proxies"]:
            data["class_proxies"][name] *= 123456.789
        after = class_statistics(data, 252)
        for name in before["signals"]:
            self.assertEqual(before["signals"][name]["recent_state"], after["signals"][name]["recent_state"])

    def test_inverse_volatility_caps_and_no_leverage(self):
        signals = self.signals()
        for policy in POLICIES:
            weights = class_proposal(policy, signals)
            self.assertLessEqual(sum(weights.values()), 1 + 1e-12)
            self.assertTrue(all(v >= 0 for v in weights.values()))
        inverse = class_proposal("inverse_vol_cap60", signals)
        self.assertAlmostEqual(inverse["bond"], .6)
        self.assertGreater(inverse["gold"], inverse["stock"])

    def test_inactive_class_does_not_force_full_investment_or_shorts(self):
        signals = self.signals()
        signals["stock"]["vote"] = signals["gold"]["vote"] = 0.
        anchor = class_proposal("ts_anchor", signals)
        inverse = class_proposal("ts_inverse_vol", signals)
        leader = class_proposal("dual_leader60", signals)
        self.assertAlmostEqual(sum(anchor.values()), 1/3)
        self.assertAlmostEqual(sum(inverse.values()), .6)
        self.assertAlmostEqual(sum(leader.values()), .6)
        signals["bond"]["vote"] = 0.
        for policy in ("ts_anchor", "ts_inverse_vol", "dual_leader60"):
            self.assertEqual(sum(class_proposal(policy, signals).values()), 0.)

    def test_dual_leader_uses_relative_score_and_absolute_gate(self):
        signals = self.signals()
        weights = class_proposal("dual_leader60", signals)
        self.assertEqual(weights["gold"], .6)
        self.assertEqual(weights["bond"], .4)
        signals["gold"]["vote"] = 1/3
        self.assertEqual(class_proposal("dual_leader60", signals)["stock"], .6)
        signals["stock"]["normalised_return_score"] = signals["gold"]["normalised_return_score"] = 1.
        signals["gold"]["vote"] = 1.
        self.assertEqual(class_proposal("dual_leader60", signals)["gold"], .6)

    def test_recent_weakening_gate_does_not_change_original_dual_rule(self):
        signals = self.signals()
        signals["gold"]["recent_state"] = "weakening"
        self.assertEqual(class_proposal("dual_leader60", signals)["gold"], .6)
        self.assertEqual(class_proposal("dual_recent60", signals)["stock"], .6)
        signals["stock"]["recent_state"] = "down"
        self.assertEqual(class_proposal("dual_recent60", signals)["bond"], .6)

    def test_return_rank_removes_volatility_only_from_ordering_not_eligibility(self):
        signals = self.signals()
        signals["stock"]["normalised_return_score"] = signals["gold"]["normalised_return_score"] = 1.
        self.assertEqual(class_proposal("dual_leader60", signals)["gold"], .6)
        self.assertEqual(class_proposal("dual_return60", signals)["stock"], .6)
        signals["stock"]["vote"] = 1/3
        self.assertEqual(class_proposal("dual_return60", signals)["gold"], .6)
        signals["gold"]["recent_state"] = "weakening"
        self.assertEqual(class_proposal("dual_return60", signals)["gold"], .6)

    def test_risky_pair_shares_budget_and_does_not_rotate_on_score_churn(self):
        signals = self.signals()
        weights = class_proposal("risky_pair60", signals)
        self.assertAlmostEqual(weights["stock"], .2)
        self.assertAlmostEqual(weights["gold"], .4)
        self.assertEqual(weights["bond"], .4)
        signals["stock"]["normalised_return_score"] = 100.
        self.assertEqual(class_proposal("risky_pair60", signals), weights)
        signals["stock"]["vote"] = 1/3
        self.assertEqual(class_proposal("risky_pair60", signals)["gold"], .6)
        signals["gold"]["vote"] = 0.
        self.assertEqual(class_proposal("risky_pair60", signals)["bond"], .6)
        signals["bond"]["vote"] = 0.
        self.assertEqual(sum(class_proposal("risky_pair60", signals).values()), 0.)
        control = class_proposal("risky_pair_static60", signals)
        self.assertAlmostEqual(control["stock"], .2)
        self.assertAlmostEqual(control["gold"], .4)
        self.assertEqual(control["bond"], .4)

    def test_target_risk_and_independent_rebuild_for_every_rule(self):
        data = self.data()
        stats = class_statistics(data, 252)
        for policy in POLICIES:
            target, meta = cross_asset_target(data, policy, 252, set(data["prices"]), stats=stats)
            self.assertLessEqual(meta["projected_volatility"], .1 + 1e-10)
            self.assertLessEqual(sum(target.weights.values()), 1 + 1e-12)
            audit_cross_asset_decision(meta | {"targets": target.weights}, data, 252, policy)

    def test_risk_cash_and_future_return_tampering_rejected(self):
        data = self.data()
        stats = class_statistics(data, 252)
        target, meta = cross_asset_target(data, "ts_inverse_vol", 252, (), stats=stats)
        entry = meta | {"targets": target.weights}
        for bad in (entry | {"target_cash_weight": .9}, entry | {"risky_scale": float("nan")}, entry | {"targets": {}}):
            with self.assertRaises(ValueError):
                audit_cross_asset_decision(bad, data, 252, "ts_inverse_vol")
        damaged = deepcopy(entry)
        damaged["class_signals"]["stock"]["log_returns"][252] += .5
        with self.assertRaises(ValueError):
            audit_cross_asset_decision(damaged, data, 252, "ts_inverse_vol")

    def test_exit_urgency_uses_current_risk_not_relative_rank_or_proposal_risk(self):
        data = self.data()
        stats = class_statistics(data, 252)
        stats["signals"] = self.signals()
        stats["signals"]["stock"]["normalised_return_score"] = 10.
        stats["fund_risk"]["covariance"] = np.eye(len(stats["fund_risk"]["codes"])) * .04
        gold = data["class_groups"]["gold"][0]
        def target(weight, policy="dual_return60"):
            current = dict.fromkeys(data["prices"], 0.) | {gold: weight}
            return cross_asset_target(data, policy, 252, {gold}, stats=stats,
                                      exit_mode="absolute_or_current_risk",
                                      portfolio_state=AllocationState(252, data["days"][252], current))
        ordinary, meta = target(.2)
        self.assertGreater(meta["unconstrained_projected_volatility"], .1)
        self.assertEqual(ordinary.weights[gold], 0.)
        self.assertEqual(ordinary.risk_exits, frozenset())
        self.assertTrue(ordinary.enforce_risk_exits)
        excessive, _ = target(.8)
        self.assertEqual(excessive.risk_exits, frozenset({gold}))
        stats["signals"]["gold"]["vote"] = 1/3
        failed, _ = target(.2)
        self.assertEqual(failed.risk_exits, frozenset({gold}))
        static, _ = target(.2, "risky_pair_static60")
        self.assertEqual(static.risk_exits, frozenset())

    def test_current_holdings_mode_requires_matching_unlevered_state(self):
        data = self.data()
        stats = class_statistics(data, 252)
        valid = AllocationState(252, data["days"][252], dict.fromkeys(data["prices"], 0.))
        for state in (None, AllocationState(253, valid.signal_date, valid.weights),
                      AllocationState(252, "wrong", valid.weights), AllocationState(252, valid.signal_date, {}),
                      AllocationState(252, valid.signal_date, {c: 2. for c in valid.weights})):
            with self.assertRaises(ValueError):
                cross_asset_target(data, "dual_return60", 252, (), stats=stats, exit_mode="absolute_or_current_risk", portfolio_state=state)
        with self.assertRaises(ValueError):
            cross_asset_target(data, "fixed_cash_equal", 252, (), stats=stats, exit_mode="absolute_or_current_risk", portfolio_state=valid)

    def test_current_risk_and_exit_reasons_are_independently_checked(self):
        data = self.data()
        stats = class_statistics(data, 252)
        current = dict.fromkeys(data["prices"], .1)
        target, meta = cross_asset_target(data, "dual_return60", 252, set(current), stats=stats,
                                         exit_mode="absolute_or_current_risk",
                                         portfolio_state=AllocationState(252, data["days"][252], current))
        entry = meta | {"targets": target.weights}
        audit_cross_asset_decision(entry, data, 252, "dual_return60", exit_mode="absolute_or_current_risk")
        for field, value in (("current_projected_volatility", float("nan")), ("absolute_failure_codes", ["invented"]),
                             ("risk_exit_codes", ["invented"]), ("exit_mode", "legacy")):
            with self.assertRaises(ValueError):
                audit_cross_asset_decision(entry | {field: value}, data, 252, "dual_return60", exit_mode="absolute_or_current_risk")


if __name__ == "__main__":
    unittest.main()
