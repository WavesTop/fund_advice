import json
import math
from pathlib import Path
import unittest

import numpy as np

from scripts.diagnose_member_core_budget import defensive_fraction


class DefensiveCoreBudgetTests(unittest.TestCase):
    def risk(self, gold_vol=0):
        return {"codes": ["stock", "gold", "bond"], "covariance": np.diag([0., gold_vol**2, 0.])}

    def test_restores_only_defensive_targets_without_risk_or_cash_constraint(self):
        base = {"stock": .2, "gold": .15, "bond": .2}
        neutral = {"stock": .2, "gold": .3, "bond": .4}
        fraction, extra = defensive_fraction(self.risk(), base, neutral, ["gold", "bond"])
        self.assertAlmostEqual(fraction, 1)
        self.assertAlmostEqual(extra, 35)

    def test_total_budget_stops_before_increasing_any_stock_target(self):
        base = {"stock": .5, "gold": .2, "bond": .2}
        neutral = {"stock": .2, "gold": .3, "bond": .4}
        fraction, extra = defensive_fraction(self.risk(), base, neutral, ["gold", "bond"])
        self.assertAlmostEqual(fraction, 1/3)
        self.assertAlmostEqual(extra, 10)

    def test_past_risk_caps_defensive_growth_and_unsafe_start_is_rejected(self):
        base = {"stock": .2, "gold": .05, "bond": .2}
        neutral = {"stock": .2, "gold": .3, "bond": .2}
        fraction, extra = defensive_fraction(self.risk(1), base, neutral, ["gold", "bond"])
        self.assertAlmostEqual(fraction, .2)
        self.assertAlmostEqual(extra, 5)
        with self.assertRaises(ValueError):
            defensive_fraction(self.risk(1), {**base, "gold": .2}, neutral, ["gold", "bond"])

    def test_missing_nonfinite_negative_or_overbudget_targets_are_rejected(self):
        base = {"stock": .2, "gold": .1, "bond": .4}
        neutral = {"stock": .2, "gold": .3, "bond": .4}
        for value in (math.nan, -1, 1):
            with self.assertRaises(ValueError):
                defensive_fraction(self.risk(), {**base, "gold": value}, neutral, ["gold", "bond"])
        with self.assertRaises(ValueError):
            defensive_fraction(self.risk(), {"stock": .2}, neutral, ["gold", "bond"])

    @unittest.skipUnless(Path("runtime/verification/member-core-budget-diagnostic-v1.json").exists(), "Frozen static diagnostic unavailable")
    def test_static_diagnostic_does_not_claim_new_returns_or_account_replays(self):
        result = json.loads(Path("runtime/verification/member-core-budget-diagnostic-v1.json").read_text())
        self.assertEqual(result["new_account_replays"], 0)
        self.assertIsNone(result["revised_return_pct"])
        self.assertEqual(set(result["cohorts"]), {"long4", "older4", "older9", "all13"})
        older = result["cohorts"]["older9"]["funded3_fixed20"]
        dates = [row["signal_date"] for row in older["rows"] if row["extra_defensive_target_pp"] >= .005]
        self.assertEqual(dates, ["2026-07-30"])


if __name__ == "__main__":
    unittest.main()
