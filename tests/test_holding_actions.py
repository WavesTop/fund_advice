from copy import deepcopy
from types import MappingProxyType
import unittest

import numpy as np

from backend.analysis.fund_allocation import AllocationState
from scripts.diagnose_holding_actions import HoldingAction, adjustment_target


class HoldingActionTests(unittest.TestCase):
    def setUp(self):
        self.codes = ["a", "b", "gold", "bond"]
        self.data = {"class_groups": {"stock": ["a", "b"], "gold": ["gold"], "bond": ["bond"]},
                     "economic_stats": {i: {"signal_index": i, "votes": dict.fromkeys(self.codes, 1.),
                         "risk": {"signal_index": i, "codes": self.codes, "covariance": np.zeros((4, 4))}}
                                        for i in range(252, 260)}}

    def state(self, index, weights):
        return AllocationState(index, f"2024-01-{index-250:02}", MappingProxyType(dict(weights)))

    def test_rotation_preserves_healthy_gold_bond_and_stock_budget(self):
        weights = {"a": .2, "b": .2, "gold": .2, "bond": .4}
        target = adjustment_target(self.data, 252, self.state(252, weights), "a")
        self.assertAlmostEqual(target.weights["a"], .3)
        self.assertAlmostEqual(target.weights["b"], .1)
        self.assertEqual(target.weights["gold"], .2)
        self.assertEqual(target.weights["bond"], .4)
        self.assertAlmostEqual(sum(target.weights.values()), 1.)

    def test_cash_action_and_single_fund_cap(self):
        weights = {"a": .34, "b": .06, "gold": .2, "bond": .4}
        self.assertAlmostEqual(adjustment_target(self.data, 252, self.state(252, weights), "a").weights["a"], .35)
        target = adjustment_target(self.data, 252, self.state(252, weights), "cash")
        self.assertAlmostEqual(sum(target.weights[c] for c in ("a", "b")), .3)

    def test_one_shot_does_not_recreate_expiry_or_auto_fill(self):
        weights = {"a": .2, "b": .2, "gold": .2, "bond": .4}
        action = HoldingAction(self.data, "a")
        self.assertIsNotNone(action(252, set(weights), self.state(252, weights)))
        self.assertIsNone(action(253, set(weights), self.state(253, weights)))

    def test_staged_retries_then_stops_when_actual_target_is_reached(self):
        weights = {"a": .2, "b": .2, "gold": .2, "bond": .4}
        action = HoldingAction(self.data, "a", staged=True)
        target = action(252, set(weights), self.state(252, weights))
        self.assertIsNotNone(action(253, set(weights), self.state(253, weights)))
        self.assertIsNone(action(254, set(weights), self.state(254, target.weights)))
        self.assertIsNone(action(255, set(weights), self.state(255, weights)))

    def test_candidate_failure_cancels_intent_and_urgently_exits_not_reenters(self):
        weights = {"a": .2, "b": .2, "gold": .2, "bond": .4}
        action = HoldingAction(self.data, "a", staged=True)
        action(252, set(weights), self.state(252, weights))
        self.data["economic_stats"][253]["votes"]["a"] = 0.
        exited = action(253, set(weights), self.state(253, weights))
        self.assertEqual(exited.weights["a"], 0.)
        self.assertIn("a", exited.risk_exits)
        self.assertIsNone(action.intent)
        self.assertIsNone(action(254, set(weights), self.state(254, weights)))

    def test_future_statistics_do_not_change_past_action(self):
        weights = {"a": .2, "b": .2, "gold": .2, "bond": .4}
        before = adjustment_target(self.data, 252, self.state(252, weights), "a")
        changed = deepcopy(self.data)
        changed["economic_stats"][259]["votes"]["a"] = 0.
        self.assertEqual(before, adjustment_target(changed, 252, self.state(252, weights), "a"))


if __name__ == "__main__":
    unittest.main()
