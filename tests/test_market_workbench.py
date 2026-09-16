"""Fund/sector web workbench: synthetic inputs only, never a live-data/return claim."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend.api.main import create_app
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.integrations.market_refresh import refresh_market_data, TIMEOUTS
from backend.storage.catalog import import_catalog, list_catalog
from backend.storage.database import connection_scope
from backend.storage.related_market import import_related_index, record_relation_check
from backend.analysis.sector_status import sector_opportunities
from scripts.refresh_market_data import collect
from scripts.import_fund_catalog import refresh_fund_catalog

PRICE = {"date": "2026-09-14", "open": "10", "high": "12", "low": "9", "close": "11", "volume": "100"}


class MarketWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name) / "app.sqlite3")
        self.funds = [{"code": f"{i:06}", "name": f"成长测试{i}", "fund_type": "ETF"} for i in range(1, 18)]
        import_catalog(self.settings, self.funds, policy_version="fixture")

    def relation(self, index="980017", source="index_daily.sina"):
        import_related_index(self.settings, fund_code="000001", index_code=index, index_name="测试指数", rows=[PRICE],
                             source_id=source, relation_source_id="fixture.prospectus", evidence_url="https://example.test/evidence")

    def completed(self, target="catalog", status="success", returncode=0):
        return subprocess.CompletedProcess([], returncode, json.dumps({"target": target, "status": status, "message": "fixture result"}), "")

    def test_six_and_eight_card_pages_are_disjoint_and_last_page_is_partial(self):
        for size in (6, 8):
            seen = []
            for page in range(1, 1 + (17 + size - 1) // size):
                result = list_catalog(self.settings, "成长", page=page, page_size=size)
                self.assertEqual(result["total"], 17)
                self.assertLessEqual(len(result["items"]), size)
                seen.extend(item["code"] for item in result["items"])
            self.assertEqual(seen, [item["code"] for item in self.funds])
            self.assertEqual(list_catalog(self.settings, "成长", page=99, page_size=size)["items"], [])

    def test_search_metacharacters_are_literal_not_sql_wildcards(self):
        import_catalog(self.settings, [{"code": "123456", "name": "100%增长_A", "fund_type": "混合"}], policy_version="fixture")
        for query in ("%", "_", "100%"):
            self.assertEqual([item["code"] for item in list_catalog(self.settings, query)["items"]], ["123456"])
        self.assertEqual(list_catalog(self.settings, "' OR 1=1 --")["total"], 0)

    def test_cards_only_link_one_current_verified_identity(self):
        self.relation()
        first = list_catalog(self.settings, "000001", page_size=6)["items"][0]
        self.assertEqual(first["relation_status"], "linked")
        self.assertEqual(len(first["related_sectors"]), 1)
        self.assertEqual(first["related_sectors"][0]["source_id"], "index_daily.sina")
        self.assertEqual(first["related_sectors"][0]["universe_type"], "tracked_index")
        self.relation("399441")
        self.assertEqual(list_catalog(self.settings, "000001")["items"][0]["related_sectors"][0]["code"], "399441")
        record_relation_check(self.settings, "000001", outcome="failed", error="fixture offline")
        failed = list_catalog(self.settings, "000001")["items"][0]
        self.assertEqual(failed["relation_status"], "withheld")
        self.assertEqual(failed["related_sectors"], [])

    def test_ambiguous_legacy_relations_and_missing_relations_have_no_direct_link(self):
        self.relation("980017")
        self.relation("399441")
        with connection_scope(self.settings) as connection:
            connection.execute("DELETE FROM fund_relation_verification")
        first, second = list_catalog(self.settings, page_size=6)["items"][:2]
        self.assertEqual((first["relation_status"], first["related_sectors"]), ("withheld", []))
        self.assertEqual((second["relation_status"], second["related_sectors"]), ("missing", []))

    def test_page_associations_are_batched_and_limited_to_visible_funds(self):
        from backend.storage.related_market import current_associations
        with patch("backend.storage.related_market.current_associations", wraps=current_associations) as load:
            page = list_catalog(self.settings, page=2, page_size=8)
        load.assert_called_once()
        self.assertEqual(load.call_args.kwargs["fund_codes"], [item["code"] for item in page["items"]])

    def test_catalog_network_failure_preserves_previous_import(self):
        before = list_catalog(self.settings)
        with patch("scripts.import_fund_catalog.fetch_catalog", side_effect=RuntimeError("fixture offline")) as fetcher:
            with self.assertRaises(RuntimeError):
                refresh_fund_catalog(self.settings)
        fetcher.assert_called_once()
        after = list_catalog(self.settings)
        self.assertEqual((after["total"], after["updated_at"]), (before["total"], before["updated_at"]))

    def test_catalog_worker_calls_real_shared_import_and_persists_rows(self):
        fresh = [{"code": "888888", "name": "新基金", "fund_type": "混合"}]
        with patch("scripts.import_fund_catalog.fetch_catalog", return_value=fresh) as fetcher:
            result = collect(self.settings, "catalog")
        fetcher.assert_called_once()
        self.assertEqual(result["status"], "success")
        self.assertEqual(list_catalog(self.settings, "888888")["items"][0]["name"], "新基金")

    def test_sector_worker_reports_actual_partial_counts_and_unchanged_manual_evidence(self):
        fixture = {"universe": {"ranking_as_of": "2026-09-14"}, "items": [
            {"code": "BK1", "rows": [PRICE], "collection_error": None, "membership": {"status": "failed", "error": "members offline"}},
            {"code": "BK2", "rows": [PRICE], "collection_error": "prices offline", "membership": {"status": "ready"}},
        ]}
        with patch("scripts.refresh_market_data.refresh_sector_heat", return_value=fixture) as fetcher:
            result = collect(self.settings, "sectors")
        fetcher.assert_called_once_with(self.settings, industry_only=True)
        self.assertEqual((result["status"], result["price_updated"], result["price_failed"]), ("partial", 1, 1))
        self.assertEqual((result["membership_updated"], result["membership_failed"]), (1, 1))
        self.assertFalse(result["manual_evidence_refreshed"])
        fixture["items"][0]["collection_error"] = "also offline"
        with patch("scripts.refresh_market_data.refresh_sector_heat", return_value=fixture):
            self.assertEqual(collect(self.settings, "sectors")["status"], "failed")

    def test_refresh_uses_bounded_worker_not_read_only_opportunities(self):
        with patch("backend.integrations.market_refresh.subprocess.run", return_value=self.completed()) as runner:
            result = refresh_market_data(self.settings, "catalog")
        self.assertEqual(result["refresh"]["status"], "success")
        args, kwargs = runner.call_args
        self.assertEqual(args[0][:5], [sys.executable, "-X", "utf8", "-m", "scripts.refresh_market_data"])
        self.assertIn(str(self.settings.database_path.resolve()), args[0])
        self.assertEqual(kwargs["timeout"], TIMEOUTS["catalog"])
        self.assertFalse(kwargs["check"])

    def test_worker_partial_and_failure_are_not_reported_as_success(self):
        with patch("backend.integrations.market_refresh.subprocess.run", return_value=self.completed("sectors", "partial")):
            self.assertEqual(refresh_market_data(self.settings, "sectors")["refresh"]["status"], "partial")
        for completed in (self.completed(status="failed", returncode=2), self.completed(returncode=1),
                          self.completed(target="wrong"), subprocess.CompletedProcess([], 0, "not-json", "")):
            with patch("backend.integrations.market_refresh.subprocess.run", return_value=completed), self.assertRaises(AppError) as caught:
                refresh_market_data(self.settings, "catalog")
            self.assertEqual(caught.exception.status_code, 502)

    def test_timeout_and_launch_failure_release_refresh_lock(self):
        for exception, status in ((subprocess.TimeoutExpired("fixture", 90), 504), (OSError("fixture"), 503)):
            with patch("backend.integrations.market_refresh.subprocess.run", side_effect=exception), self.assertRaises(AppError) as caught:
                refresh_market_data(self.settings, "catalog")
            self.assertEqual(caught.exception.status_code, status)
        with patch("backend.integrations.market_refresh.subprocess.run", return_value=self.completed()):
            self.assertEqual(refresh_market_data(self.settings, "catalog")["refresh"]["status"], "success")

    def test_duplicate_refresh_is_409_without_a_second_worker(self):
        def during_first(*args, **kwargs):
            with self.assertRaises(AppError) as caught:
                refresh_market_data(self.settings, "catalog")
            self.assertEqual(caught.exception.status_code, 409)
            return self.completed()
        with patch("backend.integrations.market_refresh.subprocess.run", side_effect=during_first) as runner:
            refresh_market_data(self.settings, "catalog")
        runner.assert_called_once()

    def test_http_refresh_routes_and_source_exact_detail(self):
        self.relation()
        with TestClient(create_app(self.settings)) as client:
            with patch("backend.api.main.refresh_market_data", return_value={"refresh": {"status": "success"}}) as refresh:
                self.assertEqual(client.post("/api/funds/catalog/refresh").status_code, 200)
                refresh.assert_called_with(self.settings, "catalog")
                self.assertEqual(client.post("/api/sectors/refresh").status_code, 200)
                refresh.assert_called_with(self.settings, "sectors")
            response = client.get("/api/sectors/980017?source_id=index_daily.sina&universe_type=tracked_index")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["item"]["code"], "980017")
            self.assertEqual(client.get("/api/sectors/980017?source_id=wrong&universe_type=tracked_index").status_code, 404)
            self.assertEqual(client.get("/api/sectors/980017?source_id=index_daily.sina&universe_type=hot_board").status_code, 404)
            self.assertEqual(client.get("/api/sectors/980017").status_code, 422)

    def test_wrong_source_does_not_borrow_same_code_industry_or_valuation(self):
        self.relation(source="fixture.other_provider")
        result = sector_opportunities(self.settings)
        self.assertEqual(result["items"][0]["industry"]["status"], "missing")
        self.assertEqual(result["items"][0]["valuation"]["status"], "missing")
        coverage = next(item for item in result["coverage"] if item["universe_type"] == "tracked_index")
        self.assertEqual((coverage["total"], coverage["industry_current"], coverage["valuation_observations"]), (1, 0, 0))
        self.assertFalse(coverage["formal_recommendation_ready"])


if __name__ == "__main__":
    unittest.main()
