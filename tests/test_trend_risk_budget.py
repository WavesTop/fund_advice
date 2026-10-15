from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest

import numpy as np

from backend.analysis.fund_allocation import AllocationExecution, simulate_allocations
from backend.analysis.trend_risk_budget import budget_policies, budget_statistics, projected_volatility, risk_budget_target
from backend.core.trading_calendar import TradingCalendar
from scripts.research_local_trend_models import prepare_local
from scripts.validate_fund_allocation import validate_allocation
from tests import test_fund_allocation as allocation_fixtures
from tests import test_local_trend_models as model_fixtures


def calendar_fixture():
    path = Path(__file__).resolve().parents[1] / "config/research-exchange-calendar-2009-2026.json"
    return TradingCalendar(json.loads(path.read_text()))


class TrendRiskBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = calendar_fixture()

    def funds(self):
        return model_fixtures.LocalTrendModelTests.funds(self)

    def data(self):
        funds, codes = self.funds()
        return prepare_local(funds, codes, self.calendar)

    def test_budget_bounds_and_passive_exposures(self):
        data = self.data()
        for policy in budget_policies():
            targets, metadata = risk_budget_target(data, policy, 252, set())
            self.assertLessEqual(sum(targets.weights.values()), 1 + 1e-12)
            self.assertTrue(all(v >= 0 for v in targets.weights.values()))
            self.assertLessEqual(metadata["target_stock_budget"], .6 + 1e-12)
            self.assertLessEqual(targets.weights.get("bond", 0), .9)
            if policy.name == "static40":
                self.assertAlmostEqual(metadata["target_stock_budget"], .4)
            if policy.volatility_target:
                self.assertLessEqual(metadata["projected_volatility"], .1 + 1e-10)

    def test_volatile_risk_is_scaled_without_treating_cash_as_free_leverage(self):
        data = self.data()
        stats = budget_statistics(data, 252)
        variances = {"a": 1., "b": 1., "000217": .04, "bond": .0001}
        stats["covariance"] = np.diag([variances[c] for c in stats["codes"]])
        policy = next(p for p in budget_policies() if p.name == "breadth_vol10")
        target, meta = risk_budget_target(data, policy, 252, set(data["prices"]), stats=stats)
        self.assertGreater(meta["risky_scale"], 0)
        self.assertLess(meta["risky_scale"], 1)
        self.assertLessEqual(projected_volatility(stats, target.weights), .1 + 1e-10)
        self.assertIn("a", target.risk_exits)
        self.assertIn("000217", target.risk_exits)

    def test_future_suffix_does_not_change_risk_statistics(self):
        funds, codes = self.funds()
        original = prepare_local(funds, codes, self.calendar)
        changed = deepcopy(funds)
        asof = original["days"][252]
        for f in changed.values():
            for day, row in f["rows"].items():
                if day > asof:
                    row["unit_nav"] = str(float(row["unit_nav"]) * 20)
        altered = prepare_local(changed, codes, self.calendar)
        for policy in budget_policies():
            left, left_meta = risk_budget_target(original, policy, 252, set())
            right, right_meta = risk_budget_target(altered, policy, 252, set())
            self.assertEqual(left, right)
            self.assertEqual(left_meta, right_meta)

    def test_rejects_wrong_date_risk_cache_and_nonfinite_covariance(self):
        data = self.data()
        stats = budget_statistics(data, 252)
        stats["signal_index"] = 253
        with self.assertRaises(ValueError):
            risk_budget_target(data, budget_policies()[0], 252, set(), stats=stats)
        stats["covariance"][0][0] = float("nan")
        with self.assertRaises(ValueError):
            projected_volatility(stats, {"a": .4})


class RelativeBandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = calendar_fixture()

    def data(self):
        return allocation_fixtures.FundAllocationTests.data(self)

    def test_relative_band_makes_small_target_budget_changes_executable(self):
        data = self.data()
        target = lambda i, held: {"one": .02 if i < 10 else .04}
        execution = AllocationExecution(review_sessions=10, rebalance_band=.05)
        legacy = simulate_allocations(data, self.calendar, target, start=0, execution=execution)
        relative = simulate_allocations(data, self.calendar, target, start=0,
                                        execution=replace(execution, relative_rebalance_band=.25))
        self.assertEqual(sum(t["action"] == "BUY" for t in legacy["trades"]), 1)
        self.assertEqual(sum(t["action"] == "BUY" for t in relative["trades"]), 2)
        self.assertEqual(validate_allocation(relative, data, self.calendar)["status"], "passed")
        with self.assertRaises(ValueError):
            replace(execution, relative_rebalance_band=float("nan")).validate()


if __name__ == "__main__":
    unittest.main()
