from copy import deepcopy
from dataclasses import replace
import math
import unittest

from backend.analysis.fund_allocation_signals import AllocationPlan, allocation_at, extend_features, plans
from scripts.research_defensive_allocations import PLANS, defensive_target, separate_peer_scores
from scripts.diagnose_trend_predictivity import rank_correlation


class AllocationSignalTests(unittest.TestCase):
    def data(self):
        codes = ("a", "b", "c", "gold")
        marks = {code: [math.exp(.0005 * i + .02 * math.sin(i / (5 + j))) for i in range(280)]
                 for j, code in enumerate(codes)}
        frames = {}
        for i in range(252, 280):
            entries = [{"code": code, "price": marks[code][i], "ma60": marks[code][i] * .98,
                        "return30_pct": 2, "scores": dict.fromkeys(("short", "medium", "long"), 70 - j),
                        "defensive_asset": code == "gold"} for j, code in enumerate(codes)]
            frames[i] = {"entries": entries, "context": {"benchmark_above_ma200": True}}
        data = {"days": list(range(280)), "marks": marks, "frames": frames}
        extend_features(data)
        return data

    def test_all_predeclared_plans_have_no_leverage_or_asset_cap_breach(self):
        data = self.data()
        self.assertEqual(len(plans()), 12)
        for plan in plans():
            result = allocation_at(data, plan, 252, set())
            self.assertLessEqual(sum(result.weights.values()), 1 + 1e-10, plan.name)
            self.assertTrue(all(0 <= v <= plan.asset_cap + 1e-10 for v in result.weights.values()), plan.name)

    def test_feature_and_targets_ignore_future_suffix(self):
        data = self.data()
        changed = deepcopy(data)
        for values in changed["marks"].values():
            values[253:] = [v * 10 for v in values[253:]]
        extend_features(changed)
        for plan in plans():
            self.assertEqual(allocation_at(data, plan, 252, {"a"}), allocation_at(changed, plan, 252, {"a"}))

    def test_cap_leaves_cash_when_only_one_asset_is_eligible(self):
        data = self.data()
        for entry in data["frames"][252]["entries"]:
            if entry["code"] != "gold":
                entry["scores"] = dict.fromkeys(("short", "medium", "long"), 20)
        result = allocation_at(data, AllocationPlan("trend", "trend"), 252, set())
        self.assertEqual(result.weights, {"gold": .35})

    def test_regime_exception_still_requires_gold_own_trend(self):
        data = self.data()
        data["frames"][252]["context"]["benchmark_above_ma200"] = False
        result = allocation_at(data, AllocationPlan("regime", "trend", benchmark_gate=True), 252, {"a"})
        self.assertEqual(result.weights, {"gold": .35})
        self.assertEqual(result.risk_exits, frozenset({"a"}))
        data["frames"][252]["entries"][-1]["return30_pct"] = -2
        self.assertEqual(allocation_at(data, AllocationPlan("regime", "trend", benchmark_gate=True), 252, set()).weights, {})

    def test_correlation_limit_rejects_duplicate_price_risk(self):
        data = self.data()
        rows = data["frames"][252]["entries"]
        rows[1]["daily60"] = list(rows[0]["daily60"])
        result = allocation_at(data, AllocationPlan("diverse", "trend", correlation_cap=.8), 252, set())
        self.assertNotIn("b", result.weights)

    def test_buy_hold_does_not_rebalance_existing_holdings(self):
        data = self.data()
        plan = AllocationPlan("hold", "hold")
        self.assertIsNone(allocation_at(data, plan, 252, {"a"}))
        self.assertEqual(sum(allocation_at(data, plan, 252, set()).weights.values()), 1)

    def test_defensive_plans_keep_risk_and_cash_budgets_distinct(self):
        data = self.data()
        data["roles"] = {"a": "sector", "b": "defensive", "c": "sector", "gold": "sector"}
        for family in PLANS:
            result = defensive_target(data, family, 252, set())
            self.assertLessEqual(sum(result.weights.values()), 1 + 1e-10)
            self.assertGreaterEqual(min(result.weights.values(), default=0), 0)
            if family != "static_40_40_20":
                self.assertLessEqual(result.weights.get("a", 0), .2)
                self.assertLessEqual(result.weights.get("c", 0), .2)
                self.assertLessEqual(result.weights.get("b", 0), .75)
        for entry in data["frames"][252]["entries"]:
            entry["scores"] = dict.fromkeys(("short", "medium", "long"), 20)
            entry["return30_pct"] = -2
            entry["returns"][126] = -1
        result = defensive_target(data, "core_trend", 252, {"a", "b", "gold"})
        self.assertEqual(result.weights, {})
        self.assertEqual(result.risk_exits, frozenset({"a", "b", "gold"}))

    def test_rank_diagnostic_handles_ties_and_direction(self):
        self.assertAlmostEqual(rank_correlation([1, 2, 3], [10, 20, 30]), 1)
        self.assertAlmostEqual(rank_correlation([1, 2, 3], [30, 20, 10]), -1)
        self.assertIsNone(rank_correlation([1, 1, 1], [30, 20, 10]))

    def test_extra_bond_does_not_change_stock_peer_scores(self):
        original = self.data()
        original["prices"] = original["marks"]
        original["roles"] = {code: "sector" for code in original["marks"]}
        expanded = deepcopy(original)
        expanded["marks"]["bond"] = [math.exp(.0001 * i) for i in range(280)]
        expanded["prices"]["bond"] = expanded["marks"]["bond"]
        expanded["roles"]["bond"] = "defensive"
        for frame in expanded["frames"].values():
            frame["entries"].append({"code": "bond", "scores": {}})
        separate_peer_scores(original)
        separate_peer_scores(expanded)
        for index in range(252, 280):
            for i, entry in enumerate(original["frames"][index]["entries"]):
                self.assertEqual(entry["scores"], expanded["frames"][index]["entries"][i]["scores"])


if __name__ == "__main__":
    unittest.main()
