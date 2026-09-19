"""The API must expose evidence denominators separately from investment ranking."""
from __future__ import annotations

import unittest

from backend.analysis.sector_status import DATA_SUPPORT_VERSION, summarize_data_support


def board(code: str, *, membership: str, operating: str = "not_collected") -> dict:
    members = ([
        {"stock_code": "600001", "market_cap": 100.0, "weight": None},
        {"stock_code": "000002", "market_cap": 200.0, "weight": None},
    ] if membership == "ready" else [])
    return {
        "code": code,
        "universe_type": "hot_board",
        "periods": [
            {"id": key, "strength": {"eligible": True}}
            for key in ("short", "medium", "long")
        ],
        "membership": {"status": membership, "members": members},
        "fundamentals": {
            "operating": {"status": operating},
            "valuation": {"status": "not_collected", "percentile": None},
            "catalysts": {"status": "not_collected"},
        },
    }


class SectorDataSupportTests(unittest.TestCase):
    def test_partial_membership_and_market_cap_keep_separate_denominators(self):
        result = summarize_data_support([board("881101", membership="ready"), board("881102", membership="failed")])
        datasets = {row["id"]: row for row in result["datasets"]}
        self.assertEqual(result["version"], DATA_SUPPORT_VERSION)
        self.assertEqual(datasets["sector_price_history"]["status"], "available")
        self.assertEqual(datasets["current_constituents"]["status"], "partial")
        self.assertEqual(datasets["current_constituents"]["coverage"], {
            "boards": 1, "total_boards": 2, "members": 2,
        })
        self.assertEqual(datasets["constituent_market_cap"]["status"], "available")
        self.assertEqual(datasets["official_constituent_weights"]["status"], "not_collected")
        self.assertIn("current_constituents", result["blocking_gaps"])
        self.assertFalse(result["formal_recommendation_ready"])

    def test_non_price_gaps_do_not_reduce_price_coverage(self):
        result = summarize_data_support([board("881101", membership="ready")])
        datasets = {row["id"]: row for row in result["datasets"]}
        self.assertEqual(datasets["sector_price_history"]["coverage"]["long"], 1)
        for key in ("constituent_financials", "constituent_quote_valuation", "dated_valuation",
                    "market_breadth_and_flow", "benchmark_relative_return", "total_return_adjustments",
                    "point_in_time_catalysts", "fund_exposure_and_cost"):
            self.assertEqual(datasets[key]["status"], "not_collected")

    def test_empty_pool_never_reports_available(self):
        result = summarize_data_support([])
        self.assertTrue(all(row["status"] == "not_collected" for row in result["datasets"]))


if __name__ == "__main__":
    unittest.main()
