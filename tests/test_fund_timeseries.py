import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.api.main import create_app
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.catalog import import_catalog
from backend.storage.database import connection_scope
from backend.storage.timeseries import get_fund, get_timeseries, import_timeseries
from scripts.import_fund_timeseries import (
    main as import_main,
    refresh_fund_timeseries,
    series_dataset,
    unsupported_nav_reason,
)
from scripts.probe_fund_source import ProbeError


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
        second = [first[0], {"date": "2026-09-12", "open": "3", "high": "3", "low": "2", "close": "2", "volume": "0", "amount": "0"}]
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
        self.assertIsNone(series_dataset({
            "code": "000673", "name": "融通四季添利债券(LOF)C", "fund_type": "债券型-混合一级",
        }))
        self.assertIsNone(series_dataset({
            "code": "023226", "name": "示例ETF-FOF", "fund_type": "FOF-稳健型",
        }))
        self.assertEqual(series_dataset({
            "code": "160001", "name": "示例混合(LOF)", "fund_type": "混合型",
        }), "lof-daily")
        self.assertEqual(series_dataset({
            "code": "160706", "name": "嘉实沪深300ETF联接A", "fund_type": "指数型-股票",
        }), "lof-daily")
        self.assertEqual(series_dataset({
            "code": "501018", "name": "南方原油A", "fund_type": "QDII-商品",
        }), "lof-daily")
        self.assertIsNone(unsupported_nav_reason({
            "code": "006283", "name": "华安德国(DAX)ETF联接(QDII)A", "fund_type": "QDII-REITs",
        }))
        self.assertIsNotNone(unsupported_nav_reason({
            "code": "508000", "name": "华安张江产业园REIT", "fund_type": "Reits",
        }))
        self.assertIsNone(unsupported_nav_reason({
            "code": "007708", "name": "中银瑞福浮动净值型货币A", "fund_type": "货币型-浮动净值",
        }))
        self.assertIsNotNone(unsupported_nav_reason({
            "code": "000009", "name": "易方达天天理财货币A", "fund_type": "货币型-普通货币",
        }))

    def test_retired_source_cannot_replace_or_publish_legacy_rows(self):
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
        self.assertEqual((legacy["kind"], legacy["source_id"], legacy["rows"]), (None, None, []))
        self.assertEqual(legacy["availability_status"], "retired_source_withheld")

        # Retired rows remain in SQLite for audit, but cannot block a smaller
        # replacement from a current admitted source.
        replacement = [{"date": "2026-09-12", "unit_nav": "1.2600", "accumulated_nav": "2.5100"}]
        import_timeseries(self.settings, "005911", "nav", replacement,
                          source_id="fund_nav.ths", policy_version="current")
        current = get_timeseries(self.settings, "005911", "nav")
        self.assertEqual((current["source_id"], current["rows"]), ("fund_nav.ths", replacement))

    def test_candidate_source_cannot_replace_manager_nav(self):
        official = [{"date": "2026-09-18", "unit_nav": "1.25", "accumulated_nav": "1.25"}]
        import_timeseries(self.settings, "005911", "nav", official,
                          source_id="fund_nav.manager_documents", policy_version="official")
        with connection_scope(self.settings) as connection:
            before_batches = connection.execute(
                "SELECT COUNT(*) FROM fund_timeseries_import_batch WHERE code='005911' AND kind='nav'"
            ).fetchone()[0]
        with self.assertRaises(AppError) as caught:
            import_timeseries(
                self.settings, "005911", "nav",
                [{"date": "2026-09-18", "unit_nav": "9", "accumulated_nav": "9"}],
                source_id="fund_nav.ths", policy_version="candidate",
            )
        self.assertEqual(caught.exception.body.code, "timeseries_source_downgrade_blocked")
        current = get_timeseries(self.settings, "005911", "nav")
        self.assertEqual((current["source_id"], current["rows"]),
                         ("fund_nav.manager_documents", official))
        with connection_scope(self.settings) as connection:
            after_batches = connection.execute(
                "SELECT COUNT(*) FROM fund_timeseries_import_batch WHERE code='005911' AND kind='nav'"
            ).fetchone()[0]
        self.assertEqual(after_batches, before_batches)

    def test_shorter_nav_cannot_replace_history_inside_storage_transaction(self):
        complete = [
            {"date": "2026-09-17", "unit_nav": "1.24", "accumulated_nav": "1.24"},
            {"date": "2026-09-18", "unit_nav": "1.25", "accumulated_nav": "1.25"},
        ]
        import_timeseries(self.settings, "005911", "nav", complete,
                          source_id="fund_nav.test_fixture", policy_version="first")
        with self.assertRaises(AppError) as caught:
            import_timeseries(
                self.settings, "005911", "nav", [complete[-1]],
                source_id="fund_nav.test_fixture", policy_version="fragment",
            )
        self.assertEqual(caught.exception.body.code, "timeseries_history_regression_blocked")
        current = get_timeseries(self.settings, "005911", "nav")
        self.assertEqual((current["policy_version"], current["rows"]), ("first", complete))

    def test_nav_refresh_cannot_drop_an_old_middle_date(self):
        complete = [
            {"date": "2026-09-16", "unit_nav": "1.23", "accumulated_nav": "1.23"},
            {"date": "2026-09-17", "unit_nav": "1.24", "accumulated_nav": "1.24"},
            {"date": "2026-09-18", "unit_nav": "1.25", "accumulated_nav": "1.25"},
        ]
        import_timeseries(self.settings, "005911", "nav", complete,
                          source_id="fund_nav.test_fixture", policy_version="first")
        replacement = [complete[0], complete[-1], {
            "date": "2026-09-19", "unit_nav": "1.26", "accumulated_nav": "1.26",
        }]
        with self.assertRaises(AppError) as caught:
            import_timeseries(self.settings, "005911", "nav", replacement,
                              source_id="fund_nav.test_fixture", policy_version="gap")
        self.assertEqual(caught.exception.body.code, "timeseries_history_regression_blocked")
        self.assertEqual(get_timeseries(self.settings, "005911", "nav")["rows"], complete)

    def test_price_refresh_cannot_truncate_history_and_reuses_earliest_date(self):
        complete = [
            {"date": "2010-01-04", "open": "1", "high": "1", "low": "1", "close": "1",
             "volume": "10", "amount": "10"},
            {"date": "2026-09-17", "open": "2", "high": "2", "low": "2", "close": "2",
             "volume": "20", "amount": "20"},
            {"date": "2026-09-18", "open": "3", "high": "3", "low": "3", "close": "3",
             "volume": "30", "amount": "30"},
        ]
        import_timeseries(self.settings, "510050", "price", complete,
                          source_id="exchange_daily.sina", policy_version="first")
        with self.assertRaises(AppError) as caught:
            import_timeseries(self.settings, "510050", "price", complete[-2:],
                              source_id="exchange_daily.sina", policy_version="fragment")
        self.assertEqual(caught.exception.body.code, "timeseries_history_regression_blocked")
        self.assertEqual(get_timeseries(self.settings, "510050", "price")["rows"], complete)

        with patch("scripts.import_fund_timeseries._import_exchange", return_value={"row_count": 3}) as refresh:
            refresh_fund_timeseries(self.settings, "510050", end_date="20260918")
        self.assertEqual(refresh.call_args.args[3:5], ("20100104", "20260918"))

    def test_exchange_candidate_falls_back_to_nav_only_when_directory_identity_is_absent(self):
        import_catalog(self.settings, [
            {"code": "160724", "name": "嘉实沪深300ETF联接C", "fund_type": "指数型-股票"},
            {"code": "160606", "name": "鹏华货币A", "fund_type": "货币型-普通货币"},
        ], policy_version="catalog-policy", source_id="fund_catalog.test_fixture")
        nav_result = {"kind": "nav", "source_id": "fund_nav.ths", "row_count": 10}
        identity_missing = ProbeError("exchange_identity_not_found", "不在当前LOF目录")
        with patch("scripts.import_fund_timeseries._import_exchange", side_effect=identity_missing), patch(
                "scripts.import_fund_timeseries._import_nav", return_value=nav_result) as nav:
            self.assertEqual(refresh_fund_timeseries(self.settings, "160724"), nav_result)
        nav.assert_called_once()

        with patch("scripts.import_fund_timeseries._import_exchange", side_effect=identity_missing), patch(
                "scripts.import_fund_timeseries._import_nav") as nav:
            with self.assertRaises(ValueError):
                refresh_fund_timeseries(self.settings, "160606")
        nav.assert_not_called()

        malformed = ProbeError("invalid_response", "已确认场内身份后的行情结构异常")
        with patch("scripts.import_fund_timeseries._import_exchange", side_effect=malformed), patch(
                "scripts.import_fund_timeseries._import_nav") as nav:
            with self.assertRaises(ProbeError):
                refresh_fund_timeseries(self.settings, "160724")
        nav.assert_not_called()

    def test_cli_uses_the_same_refresh_path_as_the_api(self):
        result = {"kind": "price", "source_id": "exchange_daily.sina", "row_count": 3}
        with patch("scripts.import_fund_timeseries.refresh_fund_timeseries", return_value=result) as refresh, \
                contextlib.redirect_stdout(io.StringIO()):
            exit_code = import_main([
                "--code", "510050", "--start-date", "20260101", "--end-date", "20260918",
                "--database", str(self.settings.database_path), "--timeout", "12",
            ])
        self.assertEqual(exit_code, 0)
        refresh.assert_called_once_with(
            self.settings.__class__(database_path=self.settings.database_path),
            "510050", start_date="20260101", end_date="20260918", registry_path=None, timeout=12.0,
        )

    def test_http_routes_are_registered(self):
        app = create_app(self.settings)
        paths = {route.path for route in app.routes}
        self.assertTrue({"/api/funds/{code}", "/api/funds/{code}/series"}.issubset(paths))


if __name__ == "__main__":
    unittest.main()
