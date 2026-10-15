import copy
import json
import unittest
from unittest.mock import patch

from scripts.export_sector_opportunity_trial import (
    OUTPUT, current_period, latest_common_cases, project, verified_reports,
)


class SectorOpportunityTrialExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = verified_reports()
        cls.snapshot = project(cls.reports)

    def test_frozen_snapshot_reproduces_and_uses_one_common_date(self):
        self.assertEqual(json.loads(OUTPUT.read_text()), self.snapshot)
        self.assertEqual(self.snapshot["as_of"], "2026-10-13")
        self.assertEqual(self.snapshot["data_cutoff"], "2026-10-14")
        self.assertFalse(self.snapshot["investment_eligible"])
        for period in self.snapshot["periods"]:
            self.assertEqual(len(period["rows"]), 86)
            self.assertEqual(period["dates"][-1], self.snapshot["as_of"])
            self.assertEqual(len(period["dates"]), period["window"] + 1)
            self.assertEqual(period["selections"]["upward_quality"], [])
            self.assertLess(period["latest_label_end"], period["as_of"])
            for row in period["rows"]:
                self.assertEqual(len(row["curve"]), len(period["dates"]))
                self.assertEqual(row["curve"][0], 0)
                self.assertAlmostEqual(row["curve"][-1], row["return_pct"], places=7)
                self.assertTrue(0 <= row["return_percentile"] <= 100)
                self.assertTrue(0 <= row["safety_percentile"] <= 100)

    def test_future_prices_do_not_change_current_candidate_evidence(self):
        rank = self.reports["rank"]
        as_of, cases = latest_common_cases(rank)
        changed = {**rank, "input": {**rank["input"], "boards": [
            {**board, "prices": {day: value if day <= as_of else value * 10000
                                  for day, value in board["prices"].items()}}
            for board in rank["input"]["boards"]]}}
        self.assertEqual(current_period(changed, "short", cases["short"], as_of),
                         current_period(rank, "short", cases["short"], as_of))

    def test_changed_selection_and_model_parameters_are_rejected(self):
        rank = self.reports["rank"]
        as_of, cases = latest_common_cases(rank)
        changed = copy.deepcopy(cases["short"])
        changed["methods"]["maximin_top10"]["selected_codes"].reverse()
        with self.assertRaisesRegex(ValueError, "candidate selection differs"):
            current_period(rank, "short", changed, as_of)
        changed = copy.deepcopy(cases["short"])
        changed["learning"]["coefficients"][0][0] += 1
        with self.assertRaises(ValueError):
            current_period(rank, "short", changed, as_of)

    def test_account_results_include_losses_all_scenarios_and_old_coverage_gaps(self):
        expected = [115.04204593727125, 73.000961, 64.704236]
        for period, result in zip(self.snapshot["periods"], expected):
            evidence = period["evidence"]
            self.assertEqual(len(evidence["scenarios"]), 9)
            self.assertEqual(len(evidence["fresh"]), 4)
            self.assertAlmostEqual(evidence["scenarios"]["fee10_default"]["maximin_top10"]["total_return_pct"], result, places=5)
            self.assertLess(evidence["fresh"]["2026-01-05"]["maximin_top10"]["total_return_pct"], -10)
            old = next(item for item in evidence["episodes"] if item["id"] == "bear_2018")
            self.assertIsNone(old["methods"]["maximin_top10"]["value"])
        self.assertLess(self.snapshot["periods"][0]["evidence"]["scenarios"]["fee100_default"]["maximin_top10"]["total_return_pct"], 0)

    def test_failed_audit_and_changed_account_metrics_are_rejected(self):
        trial = self.reports["shadow"]["periods"]["short"]["scenarios"]["fee10_default"]["policies"]["maximin_top10"]
        with patch.dict(trial["ledger_validation"], {"status": "failed"}):
            with self.assertRaisesRegex(ValueError, "incomplete audit"):
                project(self.reports)
        with patch.dict(trial["metrics"], {"total_return_pct": 999}):
            with self.assertRaisesRegex(ValueError, "does not reproduce"):
                project(self.reports)


if __name__ == "__main__":
    unittest.main()
