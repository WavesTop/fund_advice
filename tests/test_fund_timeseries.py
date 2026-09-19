import tempfile
import unittest
from pathlib import Path

from backend.api.main import create_app
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.catalog import import_catalog
from backend.storage.database import connection_scope
from backend.storage.timeseries import get_fund, get_timeseries, import_timeseries
from scripts.import_fund_timeseries import series_dataset


class FundTimeseriesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(database_path=Path(self.tmp.name) / "app.sqlite3")
        import_catalog(self.settings, [
            {"code": "510050", "name": "上证50ETF", "fund_type": "指数型-股票"},
            {"code": "005911", "name": "科技精选", "fund_type": "混合型"},
        ], policy_version="catalog-policy", source_id="fund_catalog.test_fixture")

    def tearDown(self):
        self.tmp.cleanup()

    def test_identity_and_empty_series_contract(self):
        self.assertEqual(get_fund(self.settings, "510050")["name"], "上证50ETF")
        self.assertEqual(get_timeseries(self.settings, "005911"), {
            "kind": None, "source_id": None, "policy_version": None, "updated_at": None, "rows": []
        })
        with self.assertRaises(AppError) as caught:
            get_fund(self.settings, "999999")
        self.assertEqual(caught.exception.body.code, "fund_not_found")
        self.assertEqual(caught.exception.status_code, 404)

    def test_price_import_preserves_decimal_strings_and_replaces_atomically(self):
        first = [{"date": "2026-09-11", "open": "2.9000", "high": "3.1", "low": "2.8", "close": "3.0000",
                  "volume": "667541903", "amount": None}]
        result = import_timeseries(self.settings, "510050", "price", first,
                                   source_id="exchange_daily.sina", policy_version="d0-test")
        self.assertEqual(result["row_count"], 1)
        stored = get_timeseries(self.settings, "510050")
        self.assertEqual(stored["kind"], "price")
        self.assertEqual(stored["source_id"], "exchange_daily.sina")
        self.assertEqual(stored["rows"][0], first[0])
        with self.assertRaises(AppError):
            import_timeseries(self.settings, "510050", "price", [
                {"date": "2026-09-12", "open": None, "high": "3.1", "low": "2.8", "close": "3.0", "volume": None, "amount": None}
            ], source_id="exchange_daily.sina", policy_version="d0-test")
        self.assertEqual(get_timeseries(self.settings, "510050")["rows"], first)
        second = [{"date": "2026-09-12", "open": "3", "high": "3", "low": "2", "close": "2", "volume": "0", "amount": "0"}]
        import_timeseries(self.settings, "510050", "price", second, source_id="exchange_daily.tencent", policy_version="d0-test")
        self.assertEqual(get_timeseries(self.settings, "510050")["rows"], second)

    def test_nav_import_and_series_kind_selection(self):
        rows = [
            {"date": "2026-09-10", "unit_nav": "1.2345", "accumulated_nav": None},
            {"date": "2026-09-11", "unit_nav": "1.2500", "accumulated_nav": "2.5000"},
        ]
        import_timeseries(self.settings, "005911", "nav", rows, source_id="fund_nav.test_fixture", policy_version="d0-test")
        stored = get_timeseries(self.settings, "005911")
        self.assertEqual(stored["kind"], "nav")
        self.assertEqual(stored["rows"], rows)
        self.assertEqual(series_dataset(get_fund(self.settings, "510050")), "etf-daily")
        self.assertIsNone(series_dataset(get_fund(self.settings, "005911")))
        self.assertIsNone(series_dataset({"name": "沪深300ETF联接A", "fund_type": "指数型-股票"}))

    def test_retired_source_cannot_replace_series_but_legacy_rows_remain_readable(self):
        rows = [{"date": "2026-09-11", "unit_nav": "1.2500", "accumulated_nav": "2.5000"}]
        import_timeseries(self.settings, "005911", "nav", rows,
                          source_id="fund_nav.test_fixture", policy_version="d0-test")
        for source_id in (
            "fund_nav.eastmoney", "FUND_NAV.EAST-MONEY", "东方财富.基金净值",
            "东财.基金净值", "fund_nav.emdata", "fund_nav.push2his",
        ):
            with self.subTest(source_id=source_id), self.assertRaises(AppError) as caught:
                import_timeseries(self.settings, "005911", "nav", [
                    {"date": "2026-09-12", "unit_nav": "9", "accumulated_nav": "9"},
                ], source_id=source_id, policy_version="d0-test")
            self.assertEqual(caught.exception.body.code, "timeseries_source_not_allowed")
        current = get_timeseries(self.settings, "005911", "nav")
        self.assertEqual((current["source_id"], current["rows"]), ("fund_nav.test_fixture", rows))
        with connection_scope(self.settings) as connection:
            connection.execute(
                "UPDATE fund_timeseries_projection SET source_id='fund_nav.eastmoney' WHERE code='005911' AND kind='nav'"
            )
        legacy = get_timeseries(self.settings, "005911", "nav")
        self.assertEqual((legacy["source_id"], legacy["rows"]), ("fund_nav.eastmoney", rows))

    def test_http_routes_are_registered(self):
        app = create_app(self.settings)
        paths = {route.path for route in app.routes}
        self.assertTrue({"/api/funds/{code}", "/api/funds/{code}/series"}.issubset(paths))


if __name__ == "__main__":
    unittest.main()
