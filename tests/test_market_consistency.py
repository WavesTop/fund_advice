import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.api.main import create_app
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.catalog import import_catalog
from backend.storage.database import connection_scope
from backend.storage.timeseries import _clean_rows, import_timeseries, get_timeseries
from backend.storage.related_market import import_related_index, get_related_market, get_related_markets, record_relation_check
from backend.storage.sector_heat import save_sector_heat, read_sector_heat, record_sector_heat_failure
from backend.storage.market_series import market_series
from backend.analysis.research_view import fund_associations
from backend.analysis.sector_strength import attach_strength

PRICE = {"date": "2026-09-11", "open": "10", "high": "12", "low": "9", "close": "11", "volume": "0", "amount": None}


class MarketConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name) / "app.sqlite3")
        import_catalog(self.settings, [{"code": "510050", "name": "测试ETF", "fund_type": "ETF"}], policy_version="fixture")

    def members(self, count):
        return [{"code": f"BK{index:04}", "name": f"测试行业{index}", "kind": "行业", "heat_rank": index + 1,
                 "heat_value": "100", "heat_updated_at": "2026-09-11T08:00:00Z", "updated_at": "2026-09-11T08:01:00Z",
                 "collection_error": None, "rows": [dict(PRICE)]} for index in range(count)]

    def save(self, count, scope="verified_industry_all"):
        save_sector_heat(self.settings, as_of="2026-09-11", updated_at="2026-09-11T08:01:00Z",
                         catalog_count=count, members=self.members(count), requested_count=count, universe_scope=scope)

    def relation(self, code):
        import_related_index(self.settings, fund_code="510050", index_code=code, index_name=f"测试指数{code}", rows=[dict(PRICE)],
                             source_id="fixture.index", relation_source_id="fixture.prospectus", evidence_url="https://example.test/prospectus")

    def test_snapshot_target_changes_and_all_industry_exceeds_100(self):
        self.save(100, "hot_board_top100")
        self.save(3)
        self.assertEqual(read_sector_heat(self.settings)["universe"]["requested_count"], 3)
        self.assertEqual(read_sector_heat(self.settings)["universe"]["status"], "ready")
        self.save(101)
        self.assertEqual(read_sector_heat(self.settings)["universe"]["member_count"], 101)
        with self.assertRaises(ValueError):
            self.save(101, "hot_board_top100")

    def test_initial_failure_does_not_pin_target_to_100(self):
        record_sector_heat_failure(self.settings, attempted_at="2026-09-11T08:00:00Z", error="fixture")
        self.save(3)
        self.assertEqual(read_sector_heat(self.settings)["universe"]["status"], "ready")

    def test_invalid_prices_and_nav_rejected_without_replacing_old_rows(self):
        import_timeseries(self.settings, "510050", "price", [dict(PRICE)], source_id="fixture", policy_version="v1")
        for change in ({"high": "9"}, {"low": "12"}, {"open": "0"}, {"volume": "-1"}, {"close": "NaN"}):
            with self.subTest(change=change), self.assertRaises(AppError):
                import_timeseries(self.settings, "510050", "price", [{**PRICE, **change}], source_id="fixture", policy_version="v1")
            self.assertEqual(get_timeseries(self.settings, "510050")["rows"], [PRICE])
        for value in ("-1", "0", "Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _clean_rows("nav", [{"date": "2026-09-11", "unit_nav": value}])

    def test_explicit_series_kind_does_not_hide_nav(self):
        import_timeseries(self.settings, "510050", "price", [dict(PRICE)], source_id="fixture", policy_version="v1")
        import_timeseries(self.settings, "510050", "nav", [{"date": "2026-09-11", "unit_nav": "1.25"}], source_id="fixture", policy_version="v1")
        self.assertEqual(get_timeseries(self.settings, "510050")["kind"], "price")
        self.assertEqual(get_timeseries(self.settings, "510050", "nav")["rows"][0]["unit_nav"], "1.25")
        with self.assertRaises(AppError):
            get_timeseries(self.settings, "510050", "invalid")

    def test_new_relation_supersedes_old_and_failure_preserves_audit(self):
        self.relation("000001")
        self.relation("000002")
        self.assertEqual(get_related_market(self.settings, "510050")["code"], "000002")
        relations = get_related_markets(self.settings, "510050")
        self.assertEqual([row["relation_status"] for row in relations], ["superseded", "linked"])
        record_relation_check(self.settings, "510050", outcome="failed", error="network fixture")
        self.assertIsNone(get_related_market(self.settings, "510050"))
        with connection_scope(self.settings) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM fund_market_relation").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM fund_relation_verification").fetchone()[0], 3)
        self.relation("000002")
        self.assertEqual(get_related_market(self.settings, "510050")["code"], "000002")

    def test_legacy_ambiguous_relations_are_withheld(self):
        self.relation("000001"); self.relation("000002")
        with connection_scope(self.settings) as connection:
            connection.execute("DELETE FROM fund_relation_verification")
        self.assertIsNone(get_related_market(self.settings, "510050"))
        self.assertTrue(all(row["relation_status"] == "withheld" for row in get_related_markets(self.settings, "510050")))

    def test_research_does_not_reenable_withheld_relation(self):
        item = {"universe_type": "tracked_index", "funds": [{"code": "510050", "name": "测试", "relation_type": "tracked_index",
                "relation_source_id": "fixture", "evidence_url": "https://example.test/evidence", "verified_at": "2026-09-11T08:00:00Z",
                "relation_status": "withheld", "relation_reason": "本轮核验失败"}]}
        self.assertEqual(fund_associations(item, generated_at="2026-09-15T08:00:00Z")[0]["status"], "withheld")

    def test_refresh_reports_partial_commit(self):
        app = create_app(self.settings)
        endpoint = next(route.endpoint for route in app.routes if route.path == "/api/funds/{code}/refresh")
        def success(settings, code):
            return import_timeseries(settings, code, "price", [dict(PRICE)], source_id="fixture", policy_version="v1")
        with patch("backend.api.main.refresh_fund_timeseries", side_effect=success), patch("backend.api.main.refresh_related_market", side_effect=RuntimeError("source fixture")):
            result = endpoint("510050")
        self.assertEqual(result["refresh"]["status"], "partial")
        self.assertEqual(result["refresh"]["stages"]["fund_series"]["status"], "updated")
        self.assertEqual(result["series"]["rows"], [PRICE])
        with patch("backend.api.main.refresh_fund_timeseries", side_effect=RuntimeError("fund failure")), patch("backend.api.main.refresh_related_market", side_effect=RuntimeError("index failure")):
            with self.assertRaises(AppError) as caught:
                endpoint("510050")
            self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(get_timeseries(self.settings, "510050")["rows"], [PRICE])

    def test_source_exact_market_query_and_range(self):
        self.save(1)
        result = market_series(self.settings, "BK0000", source_id="sector_daily.eastmoney", universe_type="hot_board")
        self.assertEqual(result["rows"], [PRICE])
        with self.assertRaises(AppError) as caught:
            market_series(self.settings, "BK0000", source_id="wrong-source", universe_type="hot_board")
        self.assertEqual(caught.exception.status_code, 404)
        with self.assertRaises(AppError):
            market_series(self.settings, "BK0000", source_id="sector_daily.eastmoney", universe_type="hot_board", start="2026-10-01", end="2026-09-01")

    def test_universes_choose_independent_comparison_dates(self):
        def item(code, universe, day):
            return {"code": code, "universe_type": universe, "as_of": day, "periods": [
                {"id": key, "status": "strong", "return_pct": 2, "reason": "", "observation_start": "2026-08-01"}
                for key in ("short", "medium", "long")]}
        items = [item("a", "hot_board", "2026-09-11"), item("b", "tracked_index", "2026-09-14")]
        attach_strength(items, {"hot_board": "2026-09-11", "tracked_index": "2026-09-14"})
        self.assertTrue(all(period["strength"]["eligible"] for item in items for period in item["periods"]))


if __name__ == "__main__":
    unittest.main()
