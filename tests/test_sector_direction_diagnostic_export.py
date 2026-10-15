import gzip
import json
import unittest
from unittest.mock import patch

from scripts.export_sector_direction_diagnostic import OUTPUT, SNAPSHOT, project, verified_report


class SectorDirectionDiagnosticExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = verified_report()
        with gzip.open(cls.report["protocol"]["reference"], "rt") as stream:
            cls.reference = json.load(stream)
        cls.snapshot = json.loads(SNAPSHOT.read_text())
        cls.projected = project(cls.report, cls.snapshot, cls.reference)

    def test_export_reproduces_without_overwriting_old_selection(self):
        self.assertEqual(json.loads(OUTPUT.read_text()), self.projected)
        self.assertEqual(self.projected["as_of"], "2026-10-13")
        self.assertFalse(self.projected["investment_eligible"])
        self.assertEqual([len(value["state_codes"]) for value in self.projected["periods"].values()], [1, 9, 0])
        self.assertEqual([len(value["direction_codes"]) for value in self.projected["periods"].values()], [4, 7, 3])
        for old in self.snapshot["periods"]:
            value = self.projected["periods"][old["id"]]
            self.assertEqual(old["selections"]["upward_quality"], [])
            self.assertEqual(value["old_joint_gate_count"], 0)
            self.assertGreater(value["old_price_gate_count"], 0)
            for row in old["rows"]:
                if row["code"] in value["state_codes"]:
                    self.assertIn(row["state"], ("steady_up", "strengthening"))
        medium = self.projected["periods"]["medium"]["evidence"]
        self.assertLess(medium["primary"]["method"]["mean_selected_return_pct"], 0)
        self.assertGreater(medium["secondary"]["method"]["mean_selected_return_pct"], 0)

    def test_mixed_date_and_candidate_tampering_rejected(self):
        latest = self.report["periods"]["short"]["latest"]
        with patch.dict(latest, {"as_of": "2026-10-14"}):
            with self.assertRaisesRegex(ValueError, "mix candidate dates"):
                project(self.report, self.snapshot, self.reference)
        with patch.dict(latest["methods"], {"direction_return": []}):
            with self.assertRaisesRegex(ValueError, "reproduce its selections"):
                project(self.report, self.snapshot, self.reference)

    def test_mismatched_inputs_original_selection_and_states_rejected(self):
        with patch.dict(self.snapshot, {"data_cutoff": "2026-10-13"}):
            with self.assertRaisesRegex(ValueError, "original trial sources"):
                project(self.report, self.snapshot, self.reference)
        old = self.snapshot["periods"][0]
        with patch.dict(old["selections"], {"upward_quality": ["881131"]}):
            with self.assertRaisesRegex(ValueError, "overwrite an original"):
                project(self.report, self.snapshot, self.reference)
        with patch.dict(old["rows"][0], {"state": "forged"}):
            with self.assertRaisesRegex(ValueError, "states differ"):
                project(self.report, self.snapshot, self.reference)


if __name__ == "__main__":
    unittest.main()
