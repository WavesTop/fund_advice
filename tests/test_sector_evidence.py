import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backend.analysis.sector_evidence import (
    assess_opportunity, industry_context, load_industry_evidence, valuation_context,
)
from backend.core.errors import AppError

NOW = datetime.fromisoformat("2026-09-14T23:00:00+08:00")


def price(period="medium", state="strong"):
    return {"id": period, "status": state, "label": "偏强", "lookback_sessions": 60,
            "return_pct": 8.5, "risk": "normal", "max_drawdown_pct": -4.0, "reason": "行情已过期"}


class SectorEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.evidence = load_industry_evidence()

    def context(self, code, now=NOW):
        return industry_context(self.evidence, code, now)

    def test_verified_industry_facts_distinguish_growth_divergence_and_pressure(self):
        self.assertEqual(self.context("980017")["status"], "supportive")
        self.assertEqual(self.context("399441")["status"], "mixed")
        property_context = self.context("931775")
        self.assertEqual(property_context["status"], "pressured")
        self.assertEqual([m["change_pp"] for m in property_context["metrics"]], [0.5, -0.1])

    def test_price_strength_cannot_erase_economic_counterevidence(self):
        result = assess_opportunity(price(), self.context("931775"), valuation_context("931775", NOW))
        self.assertEqual((result["status"], result["label"]), ("conflict", "反弹与经营分歧"))
        self.assertTrue(any("-20.3%" in line for line in result["challenges"]))
        self.assertTrue(result["conditions"])

    def test_missing_price_keeps_independently_valid_economic_pressure(self):
        result = assess_opportunity(price(state="stale"), self.context("931775"), valuation_context("931775", NOW))
        self.assertEqual(result["status"], "risk")
        self.assertIn("行情已过期", result["missing"])

    def test_horizons_ask_different_questions_even_with_identical_price_signal(self):
        results = [assess_opportunity(price(p), self.context("980017"), valuation_context("980017", NOW)) for p in ("short", "medium", "long")]
        self.assertEqual([r["label"] for r in results], ["近期缺少明确催化事件", "行业增长，成分公司业绩待确认", "持续增长与买入价格尚未确认"])
        self.assertTrue(all(r["status"] == "watch" for r in results))
        self.assertEqual(len({r["conditions"][0] for r in results}), 3)

    def test_missing_industry_never_becomes_opportunity_from_prices(self):
        result = assess_opportunity(price(), self.context("unknown"), valuation_context("unknown", NOW))
        self.assertEqual(result["status"], "insufficient")

    def test_retrieval_publication_and_freshness_are_enforced(self):
        self.assertEqual(self.context("980017", NOW - timedelta(days=1))["status"], "missing")
        self.assertEqual(self.context("980017", NOW + timedelta(days=60))["status"], "stale")
        future = copy.deepcopy(self.evidence)
        future["sectors"]["980017"]["metrics"][0]["observations"][-1]["published_at"] = "2026-10-01T00:00:00+08:00"
        result = industry_context(future, "980017", NOW)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["metrics"][0]["latest"]["period"], "2026-06")

    def test_incomplete_metrics_are_not_neutral_or_supportive(self):
        incomplete = copy.deepcopy(self.evidence)
        incomplete["sectors"]["980017"]["metrics"].pop()
        self.assertEqual(industry_context(incomplete, "980017", NOW)["status"], "missing")

    def test_valuation_observation_has_no_invented_trade_date_or_ttm(self):
        valuation = valuation_context("980017", NOW)
        self.assertEqual(valuation["status"], "available")
        self.assertIsNone(valuation["as_of"])
        self.assertIsNone(valuation["pe_ttm"])
        self.assertIn("不能", valuation["summary"])
        self.assertEqual(valuation_context("980017", NOW + timedelta(days=11))["status"], "not_applicable")
        self.assertEqual(valuation_context("980017", NOW - timedelta(days=1))["status"], "missing")

    def test_invalid_evidence_is_reported_instead_of_silently_scored(self):
        bad = copy.deepcopy(self.evidence)
        bad["sectors"]["980017"]["metrics"][0]["observations"][0]["value"] = float("nan")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with patch("backend.analysis.sector_evidence.EVIDENCE_PATH", path), self.assertRaises(AppError):
                load_industry_evidence()


if __name__ == "__main__":
    unittest.main()
