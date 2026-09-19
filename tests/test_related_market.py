import json
import tempfile
import unittest
from pathlib import Path

from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.catalog import import_catalog
from backend.storage.database import connection_scope
from backend.storage.related_market import get_related_market, get_related_markets, import_related_index
from scripts.import_related_market import refresh_related_market, resolve_cni_relation, resolve_related_relation
from scripts.probe_fund_source import ProbeError


class _Response:
    def __init__(self, text):
        self.data = text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return self.data


class RelatedMarketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(database_path=Path(self.temp.name) / "app.sqlite3")
        import_catalog(self.settings, [{"code": "012970", "name": "芯片ETF联接C", "fund_type": "指数型-股票"}],
                       policy_version="v1", source_id="fund_catalog.test_fixture")

    def tearDown(self):
        self.temp.cleanup()

    def test_import_and_read_verified_index(self):
        import_related_index(
            self.settings, fund_code="012970", index_code="980017", index_name="国证半导体芯片指数",
            rows=[{"date": "2026-09-11", "open": "100", "high": "105", "low": "99", "close": "103", "volume": "10", "amount": None}],
            source_id="index_daily.sina", relation_source_id="fund_disclosure.cninfo",
            evidence_url="https://example.test/evidence.pdf",
        )
        result = get_related_market(self.settings, "012970")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["code"], "980017")
        self.assertEqual(result["kind"], "index")
        self.assertEqual(result["rows"][0]["close"], "103")

    def test_unknown_relation_is_hidden(self):
        self.assertIsNone(get_related_market(self.settings, "012970"))

    def test_retired_sources_cannot_replace_relation_and_legacy_rows_are_audit_only(self):
        row = {"date": "2026-09-11", "open": "100", "high": "105", "low": "99",
               "close": "103", "volume": "10", "amount": None}
        import_related_index(
            self.settings, fund_code="012970", index_code="980017", index_name="国证半导体芯片指数",
            rows=[row], source_id="index_daily.sina", relation_source_id="fund_disclosure.cninfo",
            evidence_url="https://example.test/evidence.pdf",
        )
        cases = (
            {"source_id": "index_daily.eastmoney"},
            {"relation_source_id": "fund_profile.eastmoney"},
            {"relation_source_id": "东方财富.基金资料"},
            {"evidence_url": "https://fund.eastmoney.com/012970.html"},
            {"evidence_url": "https://emdata.example/fund/012970"},
            {"evidence_url": "https://push2.example/api/quote"},
            {"evidence_url": "https://datacenter-web.example/api/data"},
            {"evidence_url": "https://东财.example/012970"},
        )
        for change in cases:
            arguments = {
                "fund_code": "012970", "index_code": "980017", "index_name": "伪造替换指数",
                "rows": [{**row, "close": "104"}], "source_id": "index_daily.sina",
                "relation_source_id": "fund_disclosure.cninfo",
                "evidence_url": "https://example.test/evidence.pdf", **change,
            }
            with self.subTest(change=change), self.assertRaises(AppError) as caught:
                import_related_index(self.settings, **arguments)
            self.assertEqual(caught.exception.body.code, "relation_source_not_allowed")
        current = get_related_market(self.settings, "012970")
        assert current is not None
        self.assertEqual((current["source_id"], current["relation_source_id"], current["rows"]),
                         ("index_daily.sina", "fund_disclosure.cninfo", [row]))
        with connection_scope(self.settings) as connection:
            connection.execute(
                "UPDATE market_index_projection SET source_id='index_daily.eastmoney' WHERE code='980017'"
            )
            connection.execute(
                "UPDATE fund_market_relation SET source_id='fund_profile.eastmoney', "
                "evidence_url='https://fund.eastmoney.com/legacy' WHERE fund_code='012970'"
            )
        self.assertIsNone(get_related_market(self.settings, "012970"))
        legacy = get_related_markets(self.settings, "012970")
        self.assertEqual(len(legacy), 1)
        self.assertEqual((legacy[0]["source_id"], legacy[0]["relation_source_id"]),
                         ("index_daily.eastmoney", "fund_profile.eastmoney"))
        self.assertEqual(legacy[0]["relation_status"], "withheld")
        self.assertEqual(legacy[0]["rows"], [])
        self.assertIsNone(legacy[0]["evidence_url"])
        self.assertIn("已退役历史来源", legacy[0]["relation_reason"])
        with connection_scope(self.settings) as connection:
            stored = connection.execute(
                "SELECT source_id,evidence_url FROM fund_market_relation WHERE fund_code='012970'"
            ).fetchone()
        self.assertEqual((stored["source_id"], stored["evidence_url"]),
                         ("fund_profile.eastmoney", "https://fund.eastmoney.com/legacy"))

    def test_relation_discovery_retires_eastmoney_before_network(self):
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            raise AssertionError("network must not run")
        with self.assertRaises(ProbeError) as caught:
            resolve_cni_relation("011041", 1, opener)
        self.assertEqual(caught.exception.code, "source_retired")
        self.assertEqual(calls, [])

    def test_related_relation_discovery_is_retired_before_network(self):
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            raise AssertionError("network must not run")
        with self.assertRaises(ProbeError) as caught:
            resolve_related_relation("008089", 1, opener, {})
        self.assertEqual(caught.exception.code, "source_retired")
        self.assertEqual(calls, [])

    def test_refresh_withholds_existing_relation_and_records_retired_failure(self):
        import_catalog(self.settings, [{"code": "008089", "name": "华夏房地产ETF联接C", "fund_type": "指数型-股票"}],
                       policy_version="v1", source_id="fund_catalog.test_fixture")
        import_related_index(
            self.settings, fund_code="008089", index_code="931775", index_name="中证全指房地产指数",
            rows=[{"date": "2026-09-14", "open": "1", "high": "1", "low": "1", "close": "1", "volume": None, "amount": None}],
            source_id="index_daily.csi_official", relation_source_id="fund_disclosure.manager",
            evidence_url="https://example.test/evidence.pdf",
        )
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            raise AssertionError("network must not run")
        with self.assertRaises(ProbeError) as caught:
            refresh_related_market(self.settings, "008089", timeout=1, opener=opener, catalogue_cache={})
        self.assertEqual(caught.exception.code, "source_retired")
        self.assertEqual(calls, [])
        stored = get_related_market(self.settings, "008089")
        self.assertIsNone(stored)


if __name__ == "__main__":
    unittest.main()
