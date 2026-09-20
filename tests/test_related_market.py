import json
import datetime as dt
import tempfile
import unittest
import urllib.error
from pathlib import Path

from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.catalog import import_catalog
from backend.storage.database import connection_scope
from backend.storage.related_market import (
    get_related_market,
    get_related_markets,
    import_related_index,
    record_relation_check,
)
from scripts.import_related_market import refresh_related_market, resolve_cni_relation, resolve_related_relation
from scripts.probe_fund_source import ProbeError


class _Response:
    def __init__(self, text):
        self.data = text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, size=-1):
        return self.data if size < 0 else self.data[:size]


def _profile(code="012970", name="鹏华国证半导体芯片ETF联接C",
             benchmark="国证半导体芯片指数收益率*95%+活期存款利率(税后)*5%",
             *, script_code=None, script_name=None):
    return f"""
    <script>
      var fundCode = '{script_code or code}',
          fundName = '{script_name or name}',
          fundType = '股票型';
    </script>
    <ul>
      <li><span class="key">基金代码</span><span class="value">{code}</span></li>
      <li><span class="key">基金简称</span><span class="value">{name}</span></li>
      <li><span class="key">业绩比较基准</span><span class="value">{benchmark}</span></li>
    </ul>
    """


def _cni(code="980017", short="国证芯片", full="国证半导体芯片指数"):
    return json.dumps({"data": {"total": 1, "rows": [{
        "indexcode": code, "indexname": short, "indexfullcname": full,
    }]}})


def _csi_list(*items, total=None, page=1, page_size=200):
    total = len(items) if total is None else total
    pages = (total + page_size - 1) // page_size
    return json.dumps({"code": 200, "success": True, "total": total,
                       "currentPage": page if total else 0, "pageSize": page_size, "size": pages,
                       "data": [
        {"indexCode": code, "indexName": short, "indexSeries": series}
        for code, short, series in items
    ]})


def _csi_info(code, full, short):
    return json.dumps({"code": 200, "success": True, "data": {
        "indexCode": code, "indexFullNameCn": full, "indexShortNameCn": short,
    }})


def _sina_rows():
    end = dt.date.today()
    day = end - dt.timedelta(days=382)
    rows = []
    while day <= end:
        if day.weekday() < 5:
            rows.append({"day": day.isoformat(), "open": "100", "high": "105", "low": "99",
                         "close": "103", "volume": "10", "amount": "20"})
        day += dt.timedelta(days=1)
    return rows


def _sina_history():
    rows = _sina_rows()
    return "var _data=(" + json.dumps(rows, separators=(",", ":")) + ");"


def _csi_history(code, name):
    return json.dumps({"code": 200, "success": True, "data": [
        {"tradeDate": row["day"].replace("-", ""), "indexCode": code,
         "indexNameCnAll": name, "open": row["open"], "high": row["high"],
         "low": row["low"], "close": row["close"], "tradingVol": row["volume"],
         "tradingValue": row["amount"]}
        for row in _sina_rows()
    ]})


class RelatedMarketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(database_path=Path(self.temp.name) / "app.sqlite3")
        import_catalog(self.settings, [{"code": "012970", "name": "鹏华国证半导体芯片ETF联接C", "fund_type": "指数型-股票"}],
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

    def test_expired_market_rows_do_not_hide_relation_or_enter_current_rows(self):
        expired = (dt.date.today() - dt.timedelta(days=22)).isoformat()
        import_related_index(
            self.settings, fund_code="012970", index_code="980017",
            index_name="国证半导体芯片指数",
            rows=[{"date": expired, "open": "100", "high": "105", "low": "99",
                   "close": "103", "volume": "10", "amount": None}],
            source_id="index_daily.sina",
            relation_source_id="fund_profile.ths+cni_index.official",
            evidence_url="https://fund.10jqka.com.cn/012970/interduce.html",
        )
        current = get_related_market(self.settings, "012970")
        assert current is not None
        self.assertEqual((current["relation_status"], current["market_status"], current["rows"]),
                         ("linked", "stale", []))
        with connection_scope(self.settings) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM market_index_daily WHERE index_code='980017'"
            ).fetchone()[0], 1)

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

    def test_ths_profile_and_official_cni_catalog_resolve_exact_relation(self):
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            if request.full_url.endswith("/011041/interduce.html"):
                return _Response(_profile(
                    "011041", "天弘国证生物医药ETF发起式联接C",
                    "国证生物医药指数收益率*95%＋银行活期存款利率(税后)*5%",
                ))
            return _Response(_cni("399441", "生物医药", "国证生物医药指数"))
        relation = resolve_cni_relation("011041", 1, opener, {})
        assert relation is not None
        self.assertEqual((relation["fund_code"], relation["fund_name"]),
                         ("011041", "天弘国证生物医药ETF发起式联接C"))
        self.assertEqual((relation["index_code"], relation["index_name"]),
                         ("399441", "国证生物医药指数"))
        self.assertEqual(relation["relation_source_id"], "fund_profile.ths+cni_index.official")
        self.assertEqual(relation["evidence_url"],
                         "https://fund.10jqka.com.cn/011041/interduce.html")
        self.assertTrue(all("eastmoney" not in url for url in calls))

    def test_ths_profile_and_official_csi_catalog_resolve_exact_relation(self):
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            if request.full_url.endswith("/008089/interduce.html"):
                return _Response(_profile(
                    "008089", "华夏中证全指房地产ETF联接C",
                    "中证全指房地产指数收益率×95%+银行活期存款利率(税后)×5%",
                ))
            if "query-index-item" in request.full_url:
                return _Response(_csi_list(("931775", "中证全指房地产", "中证系列指数")))
            if request.full_url.endswith("/931775"):
                return _Response(_csi_info("931775", "中证全指房地产指数", "中证全指房地产"))
            raise AssertionError(request.full_url)
        relation = resolve_related_relation("008089", 1, opener, {})
        assert relation is not None
        self.assertEqual((relation["index_code"], relation["index_name"], relation["provider"]),
                         ("931775", "中证全指房地产指数", "csi"))
        self.assertEqual(relation["relation_source_id"], "fund_profile.ths+csi_index.official")
        self.assertTrue(all("eastmoney" not in url for url in calls))

    def test_csi_official_short_name_and_non_brand_prefixes_resolve_exactly(self):
        cases = (
            ("000008", "嘉实中证500ETF联接A",
             "中证500指数收益率*95%+银行活期存款税后利率*5%",
             "000905", "中证小盘500指数", "中证500"),
            ("000051", "华夏沪深300ETF联接A",
             "沪深300指数收益率*95%+1%(指年收益率,评价时应按期间折算)",
             "000300", "沪深300指数", "沪深300"),
            ("001051", "华夏上证50ETF联接A",
             "上证50指数收益率*95%＋人民币活期存款税后利率*5%",
             "000016", "上证50指数", "上证50"),
            ("000950", "易方达沪深300非银ETF联接A",
             "沪深300非银行金融指数收益率*95%+活期存款利率(税后)*5%",
             "H30035", "沪深300非银行金融指数", "300非银"),
        )
        for code, name, benchmark, index_code, full, short in cases:
            with self.subTest(code=code):
                def opener(request, timeout):
                    del timeout
                    if request.full_url.endswith(f"/{code}/interduce.html"):
                        return _Response(_profile(code, name, benchmark))
                    if "query-index-item" in request.full_url:
                        search = json.loads(request.data)["searchInput"]
                        if code == "000008" and search == "中证500指数":
                            return _Response(_csi_list())
                        return _Response(_csi_list((index_code, short, "中证系列指数")))
                    if request.full_url.endswith("/" + index_code):
                        return _Response(_csi_info(index_code, full, short))
                    raise AssertionError(request.full_url)

                relation = resolve_related_relation(code, 1, opener, {})
                assert relation is not None
                self.assertEqual((relation["index_code"], relation["index_name"]),
                                 (index_code, full))

    def test_benchmark_with_leading_weight_resolves_official_full_name(self):
        def opener(request, timeout):
            del timeout
            if request.full_url.endswith("/000373/interduce.html"):
                return _Response(_profile(
                    "000373", "华安中证细分医药ETF联接A",
                    "95％×中证细分医药产业主题指数收益率+5％×商业银行税后活期存款利率",
                ))
            if "query-index-item" in request.full_url:
                return _Response(_csi_list(("000814", "细分医药", "中证系列指数")))
            if request.full_url.endswith("/000814"):
                return _Response(_csi_info(
                    "000814", "中证细分医药产业主题指数", "细分医药",
                ))
            raise AssertionError(request.full_url)

        relation = resolve_related_relation("000373", 1, opener, {})
        assert relation is not None
        self.assertEqual((relation["target"], relation["index_code"]),
                         ("中证细分医药产业主题指数", "000814"))

    def test_official_short_name_confirms_index_product_identity(self):
        import_catalog(self.settings, [{
            "code": "000373", "name": "华安中证细分医药ETF联接A", "fund_type": "指数型-股票",
        }], policy_version="v1", source_id="fund_catalog.test_fixture")

        def opener(request, timeout):
            del timeout
            if request.full_url.endswith("/000373/interduce.html"):
                return _Response(_profile(
                    "000373", "华安中证细分医药ETF联接A",
                    "95%*中证细分医药产业主题指数收益率+5%*商业银行税后活期存款利率",
                ))
            if "query-index-item" in request.full_url:
                return _Response(_csi_list(("000814", "细分医药", "中证系列指数")))
            if request.full_url.endswith("/000814"):
                return _Response(_csi_info(
                    "000814", "中证细分医药产业主题指数", "细分医药",
                ))
            if "index-perf" in request.full_url:
                return _Response(_csi_history("000814", "中证细分医药产业主题指数"))
            raise AssertionError(request.full_url)

        result = refresh_related_market(
            self.settings, "000373", timeout=1, opener=opener, catalogue_cache={},
        )
        assert result is not None
        self.assertEqual((result["code"], result["row_count"]), ("000814", len(_sina_rows())))

    def test_profile_identity_and_ambiguous_benchmark_fail_closed(self):
        with self.assertRaises(ProbeError) as caught:
            resolve_related_relation(
                "012970", 1,
                lambda request, timeout: _Response(_profile(script_code="999999")), {},
            )
        self.assertEqual(caught.exception.code, "invalid_response")
        ambiguous = _profile(
            benchmark="国证半导体芯片指数收益率*50%+国证生物医药指数收益率*50%",
        )
        self.assertIsNone(resolve_related_relation(
            "012970", 1, lambda request, timeout: _Response(ambiguous), {},
        ))
        partially_parseable = _profile(
            benchmark="国证半导体芯片指数收益率*80%+国证生物医药指数*20%",
        )
        self.assertIsNone(resolve_related_relation(
            "012970", 1, lambda request, timeout: _Response(partially_parseable), {},
        ))

    def test_incomplete_official_catalog_is_rejected(self):
        def opener(request, timeout):
            del timeout
            if request.full_url.endswith("/012970/interduce.html"):
                return _Response(_profile())
            return _Response(json.dumps({"data": {"total": 2, "rows": [{
                "indexcode": "980017", "indexname": "国证芯片",
                "indexfullcname": "国证半导体芯片指数",
            }]}}))

        with self.assertRaises(ProbeError) as caught:
            resolve_related_relation("012970", 1, opener, {})
        self.assertEqual(caught.exception.code, "truncated_response")

    def test_cni_catalog_requires_total_and_unique_complete_identities(self):
        profile = _profile()
        valid = {
            "indexcode": "980017", "indexname": "国证芯片",
            "indexfullcname": "国证半导体芯片指数",
        }
        cases = (
            ({"rows": [valid]}, "invalid_response"),
            ({"total": 2, "rows": [valid, dict(valid)]}, "invalid_response"),
            ({"total": 1, "rows": [{**valid, "indexfullcname": ""}]}, "invalid_response"),
        )
        for container, expected in cases:
            with self.subTest(container=container):
                def opener(request, timeout):
                    del timeout
                    if request.full_url.endswith("/012970/interduce.html"):
                        return _Response(profile)
                    return _Response(json.dumps({"data": container}))

                with self.assertRaises(ProbeError) as caught:
                    resolve_related_relation("012970", 1, opener, {})
                self.assertEqual(caught.exception.code, expected)

    def test_csi_catalog_requires_complete_unique_pagination_identity(self):
        profile = _profile(
            "000051", "华夏沪深300ETF联接A",
            "沪深300指数收益率*95%+银行活期存款利率*5%",
        )

        def missing_total(request, timeout):
            del timeout
            if request.full_url.endswith("/000051/interduce.html"):
                return _Response(profile)
            return _Response(json.dumps({
                "code": 200, "success": True, "currentPage": 0,
                "pageSize": 200, "size": 0, "data": [],
            }))

        with self.assertRaises(ProbeError) as caught:
            resolve_related_relation("000051", 1, missing_total, {})
        self.assertEqual(caught.exception.code, "invalid_response")

        first_page = [
            (f"{number:06d}", f"无关指数{number}", "中证系列指数")
            for number in range(1, 201)
        ]

        def repeated_page(request, timeout):
            del timeout
            if request.full_url.endswith("/000051/interduce.html"):
                return _Response(profile)
            query = json.loads(request.data)
            page = query["pager"]["pageNum"]
            if page == 1:
                return _Response(_csi_list(*first_page, total=201, page=1))
            return _Response(_csi_list(first_page[0], total=201, page=2))

        with self.assertRaises(ProbeError) as caught:
            resolve_related_relation("000051", 1, repeated_page, {})
        self.assertEqual(caught.exception.code, "invalid_response")

        def invalid_code(request, timeout):
            del timeout
            if request.full_url.endswith("/000051/interduce.html"):
                return _Response(profile)
            return _Response(_csi_list(("BAD", "沪深300", "中证系列指数")))

        with self.assertRaises(ProbeError) as caught:
            resolve_related_relation("000051", 1, invalid_code, {})
        self.assertEqual(caught.exception.code, "invalid_response")

    def test_refresh_imports_ths_cni_relation_and_sina_index_rows(self):
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            if request.full_url.endswith("/012970/interduce.html"):
                return _Response(_profile())
            if "cnindex.com.cn/index/indexList" in request.full_url:
                return _Response(_cni())
            if "quotes.sina.cn" in request.full_url:
                return _Response(_sina_history())
            raise AssertionError(request.full_url)
        result = refresh_related_market(self.settings, "012970", timeout=1, opener=opener,
                                        catalogue_cache={})
        assert result is not None
        self.assertEqual((result["code"], result["name"], result["row_count"]),
                         ("980017", "国证半导体芯片指数", len(_sina_rows())))
        self.assertEqual(result["relation_source_id"], "fund_profile.ths+cni_index.official")
        stored = get_related_market(self.settings, "012970")
        assert stored is not None
        self.assertEqual((stored["code"], stored["source_id"], len(stored["rows"])),
                         ("980017", "index_daily.sina", len(_sina_rows())))
        self.assertTrue(all("eastmoney" not in url for url in calls))

    def test_first_market_failure_keeps_verified_relation_identity(self):
        def opener(request, timeout):
            del timeout
            if request.full_url.endswith("/012970/interduce.html"):
                return _Response(_profile())
            if "cnindex.com.cn/index/indexList" in request.full_url:
                return _Response(_cni())
            if "quotes.sina.cn" in request.full_url:
                return _Response("invalid market response")
            raise AssertionError(request.full_url)

        with self.assertRaises(ProbeError) as caught:
            refresh_related_market(
                self.settings, "012970", timeout=1, opener=opener, catalogue_cache={},
            )
        self.assertEqual(caught.exception.code, "invalid_response")
        stored = get_related_market(self.settings, "012970")
        assert stored is not None
        self.assertEqual(
            (stored["code"], stored["relation_status"], stored["market_status"], stored["rows"]),
            ("980017", "linked", "failed", []),
        )
        self.assertEqual(stored["relation_source_id"],
                         "fund_profile.ths+cni_index.official")

    def test_benchmark_does_not_create_tracked_relation_for_active_fund(self):
        import_catalog(self.settings, [{"code": "009999", "name": "示例稳健成长混合C", "fund_type": "混合型-偏股"}],
                       policy_version="v1", source_id="fund_catalog.test_fixture")

        def opener(request, timeout):
            if request.full_url.endswith("/009999/interduce.html"):
                return _Response(_profile("009999", "示例稳健成长混合C",
                                          "国证半导体芯片指数收益率*60%+活期存款利率*40%"))
            if "cnindex.com.cn/index/indexList" in request.full_url:
                return _Response(_cni())
            raise AssertionError("非指数基金不应请求指数行情")

        with self.assertRaises(ProbeError) as caught:
            refresh_related_market(self.settings, "009999", timeout=1, opener=opener,
                                   catalogue_cache={})
        self.assertEqual(caught.exception.code, "identity_mismatch")
        self.assertIsNone(get_related_market(self.settings, "009999"))

    def test_low_or_double_weight_benchmark_cannot_create_tracked_relation(self):
        import_catalog(self.settings, [{
            "code": "009998", "name": "示例国证半导体芯片指数基金", "fund_type": "指数型-股票",
        }], policy_version="v1", source_id="fund_catalog.test_fixture")

        def opener(request, timeout):
            del timeout
            if request.full_url.endswith("/009998/interduce.html"):
                return _Response(_profile(
                    "009998", "示例国证半导体芯片指数基金",
                    "国证半导体芯片指数收益率*20%+活期存款利率*80%",
                ))
            if "cnindex.com.cn/index/indexList" in request.full_url:
                return _Response(_cni())
            raise AssertionError("低权重基准不应请求指数行情")

        with self.assertRaises(ProbeError) as caught:
            refresh_related_market(self.settings, "009998", timeout=1, opener=opener,
                                   catalogue_cache={})
        self.assertEqual(caught.exception.code, "identity_mismatch")
        malformed = _profile(
            "009998", "示例国证半导体芯片指数基金",
            "20%*国证半导体芯片指数收益率*80%+活期存款利率*20%",
        )
        self.assertIsNone(resolve_related_relation(
            "009998", 1, lambda request, timeout: _Response(malformed), {},
        ))

    def test_truncated_index_response_keeps_stored_rows(self):
        old = [{"date": "2026-09-18", "open": "100", "high": "105", "low": "99",
                "close": "103", "volume": "10", "amount": "20"}]
        import_related_index(
            self.settings, fund_code="012970", index_code="980017", index_name="国证半导体芯片指数",
            rows=old, source_id="index_daily.sina",
            relation_source_id="fund_profile.ths+cni_index.official",
            evidence_url="https://fund.10jqka.com.cn/012970/interduce.html",
        )

        def opener(request, timeout):
            if request.full_url.endswith("/012970/interduce.html"):
                return _Response(_profile())
            if "cnindex.com.cn/index/indexList" in request.full_url:
                return _Response(_cni())
            if "quotes.sina.cn" in request.full_url:
                return _Response('var _data=([{"day":"2026-09-18","open":"100","high":"105",'
                                 '"low":"99","close":"104","volume":"10","amount":"20"}]);')
            raise AssertionError(request.full_url)

        with self.assertRaises(ProbeError) as caught:
            refresh_related_market(self.settings, "012970", timeout=1, opener=opener,
                                   catalogue_cache={})
        self.assertEqual(caught.exception.code, "truncated_response")
        with connection_scope(self.settings) as connection:
            stored = [dict(row) for row in connection.execute(
                "SELECT date,open,high,low,close,volume,amount FROM market_index_daily WHERE index_code='980017'"
            )]
        self.assertEqual(stored, old)
        current = get_related_market(self.settings, "012970")
        assert current is not None
        self.assertEqual((current["relation_status"], current["market_status"], current["rows"]),
                         ("linked", "failed", []))

    def test_relation_transport_failure_does_not_revoke_verified_relation(self):
        old = [{"date": "2026-09-18", "open": "100", "high": "105", "low": "99",
                "close": "103", "volume": "10", "amount": "20"}]
        import_related_index(
            self.settings, fund_code="012970", index_code="980017", index_name="国证半导体芯片指数",
            rows=old, source_id="index_daily.sina",
            relation_source_id="fund_profile.ths+cni_index.official",
            evidence_url="https://fund.10jqka.com.cn/012970/interduce.html",
        )

        def unavailable(request, timeout):
            del timeout
            raise urllib.error.HTTPError(request.full_url, 403, "limited", {}, None)

        with self.assertRaises(ProbeError) as caught:
            refresh_related_market(
                self.settings, "012970", timeout=1, opener=unavailable, catalogue_cache={},
            )
        self.assertEqual(caught.exception.code, "upstream_http_error")
        current = get_related_market(self.settings, "012970")
        assert current is not None
        self.assertEqual((current["relation_status"], current["market_status"], current["rows"]),
                         ("linked", "ready", old))

    def test_candidate_refresh_preserves_manager_relation_without_network(self):
        import_related_index(
            self.settings, fund_code="012970", index_code="980017", index_name="国证半导体芯片指数",
            rows=[{"date": "2026-09-18", "open": "100", "high": "105", "low": "99",
                   "close": "103", "volume": "10", "amount": "20"}],
            source_id="index_daily.csi_official",
            relation_source_id="fund_relations.manager_documents",
            evidence_url="https://example.test/manager-document.pdf",
        )
        result = refresh_related_market(
            self.settings, "012970", timeout=1,
            opener=lambda request, timeout: (_ for _ in ()).throw(AssertionError("不得请求候选来源")),
            catalogue_cache={},
        )
        assert result is not None
        self.assertTrue(result["preserved"])
        stored = get_related_market(self.settings, "012970")
        assert stored is not None
        self.assertEqual(stored["relation_source_id"], "fund_relations.manager_documents")

    def test_storage_blocks_candidate_relation_over_manager_evidence(self):
        official = [{"date": "2026-09-18", "open": "100", "high": "105", "low": "99",
                     "close": "103", "volume": "10", "amount": "20"}]
        import_related_index(
            self.settings, fund_code="012970", index_code="980017", index_name="国证半导体芯片指数",
            rows=official, source_id="index_daily.csi_official",
            relation_source_id="fund_relations.manager_documents",
            evidence_url="https://example.test/manager-document.pdf",
        )
        with self.assertRaises(AppError) as caught:
            import_related_index(
                self.settings, fund_code="012970", index_code="399441", index_name="国证生物医药指数",
                rows=official, source_id="index_daily.sina",
                relation_source_id="fund_profile.ths+cni_index.official",
                evidence_url="https://fund.10jqka.com.cn/012970/interduce.html",
            )
        self.assertEqual(caught.exception.body.code, "relation_source_downgrade_blocked")
        stored = get_related_market(self.settings, "012970")
        assert stored is not None
        self.assertEqual((stored["code"], stored["relation_source_id"]),
                         ("980017", "fund_relations.manager_documents"))

    def test_storage_blocks_shorter_index_history(self):
        rows = [
            {"date": f"2026-09-{day:02d}", "open": "100", "high": "105", "low": "99",
             "close": "103", "volume": "10", "amount": "20"}
            for day in range(10, 19)
        ]
        import_related_index(
            self.settings, fund_code="012970", index_code="980017",
            index_name="国证半导体芯片指数", rows=rows,
            source_id="index_daily.sina",
            relation_source_id="fund_profile.ths+cni_index.official",
            evidence_url="https://fund.10jqka.com.cn/012970/interduce.html",
        )
        with self.assertRaises(AppError) as caught:
            import_related_index(
                self.settings, fund_code="012970", index_code="980017",
                index_name="国证半导体芯片指数", rows=rows[-3:],
                source_id="index_daily.sina",
                relation_source_id="fund_profile.ths+cni_index.official",
                evidence_url="https://fund.10jqka.com.cn/012970/interduce.html",
            )
        self.assertEqual(caught.exception.body.code, "index_history_regression_blocked")
        stored = get_related_market(self.settings, "012970")
        assert stored is not None
        self.assertEqual(stored["rows"], rows)
        gap = [rows[0], *rows[2:], {
            "date": "2026-09-19", "open": "100", "high": "105", "low": "99",
            "close": "103", "volume": "10", "amount": "20",
        }]
        with self.assertRaises(AppError) as caught:
            import_related_index(
                self.settings, fund_code="012970", index_code="980017",
                index_name="国证半导体芯片指数", rows=gap,
                source_id="index_daily.sina",
                relation_source_id="fund_profile.ths+cni_index.official",
                evidence_url="https://fund.10jqka.com.cn/012970/interduce.html",
            )
        self.assertEqual(caught.exception.body.code, "index_history_regression_blocked")
        self.assertEqual(get_related_market(self.settings, "012970")["rows"], rows)

        rolling = [*rows[1:], {
            "date": "2026-09-19", "open": "100", "high": "105", "low": "99",
            "close": "104", "volume": "10", "amount": "20",
        }]
        import_related_index(
            self.settings, fund_code="012970", index_code="980017",
            index_name="国证半导体芯片指数", rows=rolling,
            source_id="index_daily.sina",
            relation_source_id="fund_profile.ths+cni_index.official",
            evidence_url="https://fund.10jqka.com.cn/012970/interduce.html",
        )
        accumulated = get_related_market(self.settings, "012970")["rows"]
        self.assertEqual((len(accumulated), accumulated[0]["date"], accumulated[-1]["date"]),
                         (10, "2026-09-10", "2026-09-19"))

    def test_candidate_failure_cannot_hide_concurrent_manager_relation(self):
        official = [{"date": "2026-09-18", "open": "100", "high": "105", "low": "99",
                     "close": "103", "volume": "10", "amount": "20"}]
        inserted = False

        def opener(request, timeout):
            nonlocal inserted
            del timeout
            if request.full_url.endswith("/012970/interduce.html"):
                if not inserted:
                    import_related_index(
                        self.settings, fund_code="012970", index_code="980017",
                        index_name="国证半导体芯片指数", rows=official,
                        source_id="index_daily.csi_official",
                        relation_source_id="fund_relations.manager_documents",
                        evidence_url="https://example.test/manager-document.pdf",
                    )
                    inserted = True
                return _Response(_profile(
                    benchmark="国证半导体芯片指数收益率*50%+国证生物医药指数收益率*50%",
                ))
            raise AssertionError(request.full_url)

        result = refresh_related_market(
            self.settings, "012970", timeout=1, opener=opener, catalogue_cache={},
        )
        assert result is not None
        self.assertTrue(result["preserved"])
        stored = get_related_market(self.settings, "012970")
        assert stored is not None
        self.assertEqual(stored["relation_source_id"], "fund_relations.manager_documents")
        self.assertFalse(record_relation_check(
            self.settings, "012970", outcome="failed", error="candidate unavailable",
            attempted_source_id="fund_profile.ths+cni_index.official",
        ))
        self.assertIsNotNone(get_related_market(self.settings, "012970"))

    def test_older_candidate_failure_cannot_hide_newer_candidate_success(self):
        import_related_index(
            self.settings, fund_code="012970", index_code="980017",
            index_name="国证半导体芯片指数",
            rows=[{"date": "2026-09-18", "open": "100", "high": "105", "low": "99",
                   "close": "103", "volume": "10", "amount": "20"}],
            source_id="index_daily.sina",
            relation_source_id="fund_profile.ths+cni_index.official",
            evidence_url="https://fund.10jqka.com.cn/012970/interduce.html",
        )
        self.assertFalse(record_relation_check(
            self.settings, "012970", outcome="failed", error="older slow request failed",
            attempted_source_id="fund_profile.ths+cni_index.official",
            attempted_at="2020-01-01T00:00:00.000Z",
        ))
        stored = get_related_market(self.settings, "012970")
        assert stored is not None
        self.assertEqual((stored["code"], stored["relation_status"]), ("980017", "linked"))

    def test_refresh_identity_mismatch_withholds_existing_relation(self):
        import_catalog(self.settings, [{"code": "008089", "name": "华夏房地产ETF联接C", "fund_type": "指数型-股票"}],
                       policy_version="v1", source_id="fund_catalog.test_fixture")
        import_related_index(
            self.settings, fund_code="008089", index_code="931775", index_name="中证全指房地产指数",
            rows=[{"date": "2026-09-14", "open": "1", "high": "1", "low": "1", "close": "1", "volume": None, "amount": None}],
            source_id="index_daily.csi_official", relation_source_id="fund_disclosure.manager",
            evidence_url="https://example.test/evidence.pdf",
        )
        def opener(request, timeout):
            if request.full_url.endswith("/008089/interduce.html"):
                return _Response(_profile(
                    "008089", "不匹配的基金简称",
                    "中证全指房地产指数收益率*95%+银行活期存款利率(税后)*5%",
                ))
            if "query-index-item" in request.full_url:
                return _Response(_csi_list(("931775", "中证全指房地产", "中证系列指数")))
            return _Response(_csi_info("931775", "中证全指房地产指数", "中证全指房地产"))
        with self.assertRaises(ProbeError) as caught:
            refresh_related_market(self.settings, "008089", timeout=1, opener=opener, catalogue_cache={})
        self.assertEqual(caught.exception.code, "identity_mismatch")
        stored = get_related_market(self.settings, "008089")
        self.assertIsNone(stored)


if __name__ == "__main__":
    unittest.main()
