import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.export_strategy_trial import OUTPUT, REPORT, checked_metrics, interval, project, verified_report


class TrialProjectionTests(unittest.TestCase):
    def test_intervals_use_previous_close_and_reset_only_drawdown(self):
        curve = [{"date": f"2026-01-{i:02}", "equity": 100 + i} for i in range(1, 32)]
        result = interval(curve, "2026-01-05", "2026-01-31")
        self.assertAlmostEqual(result["total_return_pct"], (131 / 104 - 1) * 100)
        self.assertEqual(result["reference_date"], "2026-01-04")
        self.assertEqual(result["max_drawdown_pct"], 0)

    def test_no_overlap_is_missing_not_zero_return(self):
        curve = [{"date": "2026-01-01", "equity": 100}]
        self.assertIsNone(interval(curve, "2018-01-01", "2018-12-31"))

    def test_failed_audit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "audit"):
            checked_metrics({"ledger_validation": {"status": "failed"}})

    def test_replaced_report_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "wrong.gz"
            path.write_bytes(b"not the reviewed report")
            with self.assertRaisesRegex(ValueError, "digest"):
                verified_report(path)


@unittest.skipUnless(REPORT.exists(), "Local immutable research report is not available")
class FrozenTrialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = verified_report()

    def test_committed_snapshot_matches_all_audited_accounts(self):
        self.assertEqual(project(self.report), json.loads(OUTPUT.read_text()))

    def test_modern_results_and_bear_history_are_not_hidden(self):
        cohorts = {c["id"]: c for c in project(self.report)["cohorts"]}
        modern = cohorts["all13"]
        self.assertAlmostEqual(modern["metrics"]["fixed20"]["total_return_pct"], 41.9760262564)
        self.assertGreater(abs(modern["metrics"]["fixed20"]["max_drawdown_pct"]),
                           abs(modern["metrics"]["inverse20"]["max_drawdown_pct"]))
        self.assertIsNone(modern["episodes"][0]["policies"]["fixed20"])
        self.assertLess(modern["episodes"][-1]["policies"]["fixed20"]["total_return_pct"], 0)
        self.assertEqual(len(cohorts["long4"]["episodes"]), 9)
        self.assertEqual(modern["curve"][0][1:], [100000, 100000])

    def test_corrupt_curve_is_rejected_even_with_passed_labels(self):
        run = copy.deepcopy(self.report["cohorts"]["all13"]["full"]["fixed20"])
        run["account"]["equity_curve"][-1]["equity"] = "200000"
        with self.assertRaisesRegex(ValueError, "reproduce"):
            checked_metrics(run)


if __name__ == "__main__":
    unittest.main()
