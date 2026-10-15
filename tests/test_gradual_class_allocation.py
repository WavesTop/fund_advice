from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import unittest

import numpy as np

from backend.analysis.fund_allocation import AllocationState, AllocationTargets, simulate_allocations
from backend.analysis.gradual_class_allocation import GradualClassAllocator, gradual_candidates
from scripts.research_gradual_class_allocations import gradual_trial
from scripts.research_cross_asset_trends import EXECUTION
from scripts.validate_fund_allocation import validate_allocation
from scripts.validate_gradual_class_allocation import audit_gradual_choice
from tests import test_class_return_forecast as fixtures


class GradualClassAllocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.ClassReturnForecastTests.setUpClass()
        cls.calendar = fixtures.ClassReturnForecastTests.calendar

    def data(self):
        return fixtures.ClassReturnForecastTests().data()

    def decision(self, action="partial_choice", index=650):
        data = self.data()
        allocator = GradualClassAllocator(data, "trend_ridge", data["class_records"]["trend_ridge"], action=action)
        state = AllocationState(index, data["days"][index], dict.fromkeys(data["prices"], 0.))
        target = allocator(index, set(), state)
        return data, allocator, target

    def test_actual_elapsed_clock_and_extra_callbacks(self):
        data, allocator, _ = self.decision()
        for at, previous, expected in ((655, 650, 5/60), (657, 655, 2/60), (660, 657, 3/60)):
            allocator(at, set(), AllocationState(at, data["days"][at], dict.fromkeys(data["prices"], 0.)))
            entry = allocator.trace[-1]
            self.assertAlmostEqual(entry["gradual_choice"]["fraction"], expected)
            audit_gradual_choice(entry, data, at, "trend_ridge", "partial_choice", data["class_records"]["trend_ridge"], previous)
        with self.assertRaises(ValueError):
            allocator(660, set(), AllocationState(660, data["days"][660], dict.fromkeys(data["prices"], 0.)))

    def test_incumbent_preserves_actual_fund_weights_and_costs(self):
        data = self.data()
        stats = deepcopy(data["cross_stats"][650])
        for group in stats["signals"]:
            stats["signals"][group]["vote"] = 1.
        data["class_groups"]["stock"].append("stock2")
        data["prices"]["stock2"] = data["prices"]["stock"]
        stats["fund_risk"]["codes"].append("stock2")
        stats["fund_risk"]["covariance"] = np.eye(4) * .0001
        current = {"stock": .3, "stock2": .1, "000217": .3, "bond": .2}
        chosen, rows, safe = gradual_candidates(data, stats, current, dict.fromkeys(stats["signals"], 0.), 10., 5/60)
        self.assertEqual(chosen, "incumbent")
        self.assertEqual(safe, current)
        self.assertEqual(rows[chosen]["targets"], current)
        self.assertEqual(rows[chosen]["ordinary_cost_proxy"], 0.)

    def test_failed_classes_are_not_gradually_retained(self):
        data = self.data()
        stats = deepcopy(data["cross_stats"][650])
        stats["signals"]["stock"]["vote"] = 0.
        current = {"stock": .8, "000217": .1, "bond": .1}
        _, rows, safe = gradual_candidates(data, stats, current, dict.fromkeys(stats["signals"], .1), 10., 1/60)
        self.assertEqual(safe["stock"], 0.)
        self.assertTrue(all(r["targets"]["stock"] == 0. for r in rows.values()))

    def test_risk_reduction_precedes_slow_movement(self):
        data = self.data()
        stats = deepcopy(data["cross_stats"][650])
        for group in stats["signals"]:
            stats["signals"][group]["vote"] = 1.
        stats["fund_risk"]["covariance"] = np.eye(3) * .25
        current = {"stock": .8, "000217": .1, "bond": .1}
        _, rows, safe = gradual_candidates(data, stats, current, dict.fromkeys(stats["signals"], .1), 10., 1/60)
        self.assertLess(safe["stock"], .8)
        self.assertTrue(all(np.sqrt(sum(w*w for w in row["targets"].values())*.25) <= .1+1e-12 for row in rows.values()))

    def test_unfitted_fallback_and_first_full_step(self):
        data, allocator, _ = self.decision(index=252)
        entry = allocator.trace[0]
        self.assertEqual(entry["gradual_choice"]["chosen"], "raw_fallback")
        self.assertEqual(entry["gradual_choice"]["fraction"], 1.)
        audit_gradual_choice(entry, data, 252, "trend_ridge", "partial_choice", data["class_records"]["trend_ridge"], None)

    def test_weight_clock_and_selection_tampering_rejected(self):
        data, allocator, _ = self.decision()
        for field in ("weights", "clock", "selection"):
            entry = deepcopy(allocator.trace[0]); g = entry["gradual_choice"]
            if field == "weights":
                g["candidates"]["incumbent"]["targets"]["stock"] += .1
            elif field == "clock":
                g["fraction"] = .1
            else:
                g["chosen"] = "invented"
            with self.assertRaises(ValueError):
                audit_gradual_choice(entry, data, 650, "trend_ridge", "partial_choice", data["class_records"]["trend_ridge"], None)

    def test_final_targets_risk_and_exit_metadata_are_audited(self):
        data, allocator, _ = self.decision()
        for field in ("targets", "current_projected_volatility", "target_cash_weight", "risk_exit_codes", "signal_date"):
            entry = deepcopy(allocator.trace[0])
            if field == "targets":
                entry[field]["stock"] += .1
            elif field == "risk_exit_codes":
                entry[field] = ["invented"]
            elif field == "signal_date":
                entry[field] = data["days"][651]
            else:
                entry[field] += .1
            with self.assertRaises(ValueError):
                audit_gradual_choice(entry, data, 650, "trend_ridge", "partial_choice", data["class_records"]["trend_ridge"], None)

    def test_gradual_candidates_reject_invalid_inputs(self):
        data = self.data(); stats = data["cross_stats"][650]
        predicted = dict.fromkeys(data["class_groups"], 0.)
        for weights, fee, fraction in (({}, 10., .1), (dict.fromkeys(data["prices"], 1.), 10., .1),
            (dict.fromkeys(data["prices"], float("nan")), 10., .1), (dict.fromkeys(data["prices"], 0.), 10., 0.),
            (dict.fromkeys(data["prices"], 0.), float("nan"), .1)):
            with self.assertRaises(ValueError):
                gradual_candidates(data, stats, weights, predicted, fee, fraction)

    def test_actual_account_and_both_actions_are_audited(self):
        for mode in ("class_mean", "trend_ridge"):
            for action in ("stay_choice", "partial_choice", "intent_choice"):
                result = gradual_trial(self.data(), self.calendar, mode, action, 5, start=620, end=680)
                self.assertEqual(result["ledger_validation"]["status"], "passed")
                self.assertEqual(result["current_holdings_validation"]["status"], "passed")
                self.assertGreater(result["gradual_validation"]["fitted_decisions"], 0)

    def band_account(self, use_anchor=False):
        data = self.data()
        for c in data["prices"]:
            data["prices"][c] = np.ones(len(data["days"]))
            data["marks"][c] = np.ones(len(data["days"]))
        previous, anchor = None, None

        def decide(i, held, state):
            nonlocal previous, anchor
            elapsed = 1 if previous is None else i - previous
            previous = i
            if i < 670:
                return AllocationTargets({"stock": .2, "000217": .4, "bond": .4})
            stats = deepcopy(data["cross_stats"][i])
            for s in stats["signals"].values():
                s["vote"], s["floored_annual_vol"] = 1., .1
            stats["fund_risk"]["covariance"] = np.eye(3) * .0001
            kwargs = {"anchor": anchor} if use_anchor else {}
            chosen, rows, _ = gradual_candidates(data, stats, dict(state.weights),
                {"stock": 0., "gold": .1, "bond": 0.}, 10., elapsed/60, **kwargs)
            target = rows[chosen]["targets"]
            anchor = dict(target)
            return AllocationTargets(target)

        account = simulate_allocations(data, self.calendar, decide, start=650, end=750, fee_bps=Decimal(10),
            execution=replace(EXECUTION, initial_review=True, review_sessions=5), include_portfolio_state=True)
        self.assertEqual(validate_allocation(account, data, self.calendar)["status"], "passed")
        return account, data

    def test_rebased_small_steps_can_stall_inside_existing_band(self):
        account, data = self.band_account()
        ordinary = [t for t in account["trades"] if t["action"] == "SELL" and t["reason"] != "terminal_liquidation"
                    and t["signal_date"] >= data["days"][670]]
        self.assertEqual(ordinary, [])
        self.assertGreater(sum(Decimal(d["overweight_ignored_by_band"]) > 1 for d in account["decisions"]), 10)

    def test_accumulated_intent_crosses_band_without_lowering_it(self):
        account, data = self.band_account(use_anchor=True)
        ordinary = [t for t in account["trades"] if t["action"] == "SELL" and t["reason"] != "terminal_liquidation"
                    and t["signal_date"] >= data["days"][670]]
        self.assertGreater(len(ordinary), 0)
        self.assertLessEqual(ordinary[0]["signal_date"], data["days"][680])
        self.assertEqual(account["execution"]["rebalance_band"], .03)
        self.assertEqual(account["execution"]["relative_rebalance_band"], .25)

    def test_intent_is_audited_against_previous_decision_not_future_plan(self):
        data, allocator, target = self.decision(action="intent_choice")
        old = dict(target.weights)
        allocator(655, set(), AllocationState(655, data["days"][655], dict.fromkeys(data["prices"], 0.)))
        entry = allocator.trace[-1]
        audit_gradual_choice(entry, data, 655, "trend_ridge", "intent_choice", data["class_records"]["trend_ridge"], 650, previous_targets=old)
        damaged = deepcopy(entry)
        damaged["gradual_choice"]["previous_fund_targets"]["stock"] += .1
        with self.assertRaises(ValueError):
            audit_gradual_choice(damaged, data, 655, "trend_ridge", "intent_choice", data["class_records"]["trend_ridge"], 650, previous_targets=old)

    def test_stale_intent_cannot_keep_failed_classes(self):
        data = self.data(); stats = deepcopy(data["cross_stats"][650])
        stats["signals"]["stock"]["vote"] = 0.
        current = {"stock": .2, "000217": .4, "bond": .4}
        _, rows, _ = gradual_candidates(data, stats, current, dict.fromkeys(data["class_groups"], .1), 10., 1/60,
                                        anchor={"stock": .6, "000217": 0., "bond": .4})
        self.assertTrue(all(row["targets"]["stock"] == 0. for row in rows.values()))

    def test_intent_rejects_invalid_stored_weights(self):
        data = self.data(); stats = data["cross_stats"][650]
        for anchor in ({}, dict.fromkeys(data["prices"], 1.), dict.fromkeys(data["prices"], float("nan"))):
            with self.assertRaises(ValueError):
                gradual_candidates(data, stats, dict.fromkeys(data["prices"], 0.), dict.fromkeys(data["class_groups"], .1), 10., 1/60, anchor=anchor)


if __name__ == "__main__":
    unittest.main()
