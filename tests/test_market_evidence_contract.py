"""The documented light screen is tested separately from investment effectiveness."""
import copy
import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.analysis.sector_evidence import (
    assess_opportunity, evidence_matches, industry_context, load_industry_evidence, valuation_context, VALUATION_PATH,
)
from backend.analysis.sector_strength import attach_strength, build_advantage_summary
from backend.core.errors import AppError

NOW = datetime.fromisoformat("2026-09-16T23:00:00+08:00")


class MarketEvidenceContractTests(unittest.TestCase):
    def setUp(self):
        self.evidence = load_industry_evidence()
        self.valuation = json.loads(VALUATION_PATH.read_text(encoding="utf-8"))

    def read_valuation(self, value):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "valuation.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with patch("backend.analysis.sector_evidence.VALUATION_PATH", path):
                return valuation_context("931775", NOW)

    def test_fresh_download_cannot_make_old_business_valuation_current(self):
        self.valuation["retrieved_at"] = NOW.isoformat()
        self.valuation["indexes"]["931775"]["as_of"] = "2026-08-01"
        result = self.read_valuation(self.valuation)
        self.assertEqual(result["status"], "not_applicable")
        self.assertEqual(result["as_of"], "2026-08-01")
        self.assertTrue(result["metrics"])

    def test_future_business_date_and_business_date_after_observation_are_rejected(self):
        for day in ("2026-09-17", "2026-09-15", "invalid"):
            self.valuation["indexes"]["931775"]["as_of"] = day
            result = self.read_valuation(self.valuation)
            self.assertEqual(result["status"], "missing")
            self.assertEqual(result["metrics"], [])
            self.assertIsNone(result["as_of"])

    def test_invalid_numbers_naive_time_empty_metrics_do_not_become_available(self):
        for number in (True, "29.01", float("nan"), float("inf")):
            value = copy.deepcopy(self.valuation)
            value["indexes"]["931775"]["metrics"][0]["value"] = number
            self.assertEqual(self.read_valuation(value)["status"], "missing")
        value = copy.deepcopy(self.valuation)
        value["retrieved_at"] = "2026-09-14T12:00:00"
        self.assertEqual(self.read_valuation(value)["status"], "missing")
        value = copy.deepcopy(self.valuation)
        value["indexes"]["931775"]["metrics"] = []
        self.assertEqual(self.read_valuation(value)["status"], "missing")
        self.assertEqual(self.read_valuation({"indexes": []})["status"], "missing")

    def test_unknown_valuation_business_date_stays_unknown_not_invented_ttm(self):
        result = valuation_context("980017", NOW)
        self.assertEqual(result["status"], "available")  # only an observed raw value
        self.assertIsNone(result["as_of"])
        self.assertIsNone(result["pe_ttm"])
        self.assertIn("口径尚未确认", result["summary"])

    def test_context_identity_requires_explicit_source_and_universe(self):
        record = self.evidence["sectors"]["980017"]
        self.assertTrue(evidence_matches(record, {"source_id": "index_daily.sina", "universe_type": "tracked_index"}))
        self.assertFalse(evidence_matches(record, {"source_id": "index_daily.sina", "universe_type": "hot_board"}))
        self.assertFalse(evidence_matches(record, {"source_id": "other", "universe_type": "tracked_index"}))
        self.assertFalse(evidence_matches({"subject": {}}, {}))
        self.assertEqual(valuation_context("980017", NOW, subject={"source_id": "other", "universe_type": "tracked_index"})["status"], "missing")

    def test_nonadjacent_and_cross_year_cumulative_periods_are_not_sustained_growth(self):
        for previous, latest in (("2026-05", "2026-07"), ("2025-12", "2026-01")):
            value = copy.deepcopy(self.evidence)
            for metric in value["sectors"]["980017"]["metrics"]:
                metric["observations"][0]["period"] = previous
                metric["observations"][1]["period"] = latest
            result = industry_context(value, "980017", NOW)
            self.assertEqual(result["status"], "missing")
            self.assertIn("相邻报告期", result["summary"])

    def test_invalid_or_duplicate_report_facts_are_rejected_before_scoring(self):
        fixtures = [[]]
        duplicate = copy.deepcopy(self.evidence)
        duplicate["sectors"]["980017"]["metrics"].append(copy.deepcopy(duplicate["sectors"]["980017"]["metrics"][0]))
        fixtures.append(duplicate)
        for field, bad in (("period", "2026-13"), ("period", "2026-12"), ("source_url", 123)):
            value = copy.deepcopy(self.evidence)
            value["sectors"]["980017"]["metrics"][0]["observations"][0][field] = bad
            fixtures.append(value)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "industry.json"
            for value in fixtures:
                path.write_text(json.dumps(value), encoding="utf-8")
                with patch("backend.analysis.sector_evidence.EVIDENCE_PATH", path), self.assertRaises(AppError) as caught:
                    load_industry_evidence()
                self.assertEqual(caught.exception.status_code, 503)

    def test_manufacturing_pressure_does_not_use_real_estate_explanation(self):
        for metric in self.evidence["sectors"]["980017"]["metrics"]:
            for row in metric["observations"]:
                row["value"] = -2
        industry = industry_context(self.evidence, "980017", NOW)
        period = {"id": "medium", "status": "stale", "reason": "行情过期"}
        result = assess_opportunity(period, industry, valuation_context("980017", NOW))
        self.assertEqual(result["status"], "risk")
        self.assertIn("收入和利润", result["summary"])
        self.assertNotIn("到位资金", result["summary"])
        self.assertIn("行情过期", result["missing"])
        self.assertTrue(any("-2%" in fact for fact in result["challenges"]))

    def ranked(self, count, target):
        items = []
        for value in range(count):
            items.append({"code": str(value), "name": str(value), "universe_type": "hot_board", "as_of": "2026-09-14",
                          "periods": [{"id": key, "return_pct": value, "status": "strong" if value == target else "weak",
                                       "reason": "fixture", "observation_start": "2026-08-17", "ma_bias_pct": 1,
                                       "risk": "normal", "max_drawdown_pct": -2} for key in ("short", "medium", "long")]})
        attach_strength(items, "2026-09-14")
        return items

    def test_rounded_75_is_not_enough_to_enter_top_quartile(self):
        items = self.ranked(1000, 749)
        strength = items[749]["periods"][0]["strength"]
        self.assertEqual(strength["percentile"], 75.0)  # 100 * 749 / 999 == 74.9749...
        self.assertFalse(strength["top_quartile"])
        self.assertEqual(build_advantage_summary(items)[0]["candidate_count"], 0)

    def test_exact_75_percentile_still_meets_documented_boundary(self):
        items = self.ranked(5, 3)
        self.assertTrue(items[3]["periods"][0]["strength"]["top_quartile"])
        self.assertEqual([item["code"] for item in build_advantage_summary(items)[0]["items"]], ["3"])


if __name__ == "__main__":
    unittest.main()
