import json
import tempfile
import unittest
from pathlib import Path

from backend.core.config import Settings
from backend.storage.catalog import import_catalog
from backend.storage.related_market import get_related_market, import_related_index
from scripts.import_related_market import refresh_related_market, resolve_cni_relation, resolve_related_relation


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
        import_catalog(self.settings, [{"code": "012970", "name": "芯片ETF联接C", "fund_type": "指数型-股票"}], policy_version="v1")

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

    def test_resolves_price_index_without_confusing_total_return_variant(self):
        profile = '跟踪标的：</a>国证生物医药指数 | <a href="x">年化跟踪误差'
        catalogue = json.dumps({"data": {"rows": [
            {"indexcode": "399441", "indexname": "生物医药", "indexfullcname": "国证生物医药指数"},
            {"indexcode": "CN2441", "indexname": "生物医药R", "indexfullcname": "国证生物医药全收益指数"},
        ]}})

        def opener(request, timeout):
            return _Response(profile if "eastmoney" in request.full_url else catalogue)

        result = resolve_cni_relation("011041", 1, opener)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["index_code"], "399441")

    def test_resolves_008089_against_csi_exact_full_name_and_caches_directory(self):
        profile = '跟踪标的：</a>中证全指房地产指数 | <a href="x">'
        directory = {"code": "200", "success": True, "data": [{"indexCode": "931775", "indexName": "房地产"}]}
        info = {"code": "200", "success": True, "data": {"indexCode": "931775", "indexFullNameCn": "中证全指房地产指数"}}
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if "eastmoney" in request.full_url:
                return _Response(profile)
            if "query-index-item" in request.full_url:
                return _Response(json.dumps(directory))
            return _Response(json.dumps(info))

        cache = {}
        first = resolve_related_relation("008089", 1, opener, cache)
        second = resolve_related_relation("008089", 1, opener, cache)
        self.assertEqual(first, second)
        assert first is not None
        self.assertEqual((first["index_code"], first["index_name"], first["symbol"]),
                         ("931775", "中证全指房地产指数", "csi:931775"))
        self.assertEqual(sum("query-index-item" in url for url in calls), 1)

    def test_csi_market_rows_are_stored_as_official_real_series(self):
        import_catalog(self.settings, [{"code": "008089", "name": "华夏房地产ETF联接C", "fund_type": "指数型-股票"}], policy_version="v1")
        profile = '跟踪标的：</a>中证全指房地产指数 |'
        directory = {"code": "200", "success": True, "data": [{"indexCode": "931775"}]}
        info = {"code": "200", "success": True, "data": {"indexCode": "931775", "indexFullNameCn": "中证全指房地产指数"}}
        quote = {"code": "200", "success": True, "data": [{"tradeDate": "20260914", "indexCode": "931775", "indexNameCnAll": "中证全指房地产指数", "open": "2345.16", "high": "2359.06", "low": "2331.26", "close": "2355.13", "tradingVol": "2033458194", "tradingValue": "87.62"}]}

        def opener(request, timeout):
            if "eastmoney" in request.full_url:
                return _Response(profile)
            if "query-index-item" in request.full_url:
                return _Response(json.dumps(directory))
            if "index-basic-info" in request.full_url:
                return _Response(json.dumps(info))
            return _Response(json.dumps(quote))

        result = refresh_related_market(self.settings, "008089", timeout=1, opener=opener, catalogue_cache={})
        self.assertEqual(result, {"code": "931775", "row_count": 1, "source_id": "index_daily.csi_official"})
        stored = get_related_market(self.settings, "008089")
        assert stored is not None
        self.assertEqual((stored["code"], stored["source_id"], stored["rows"][0]["close"]), ("931775", "index_daily.csi_official", "2355.13"))


if __name__ == "__main__":
    unittest.main()
