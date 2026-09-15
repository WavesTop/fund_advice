import copy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from backend.core.config import Settings
from backend.storage.database import connection_scope
from backend.storage.sector_heat import read_sector_heat
from scripts.import_sector_heat import (
    _ThsLinkParser, fetch_sector_constituents, fetch_sector_daily, fetch_sector_directory, refresh_sector_heat,
)


NOW = datetime.fromisoformat("2026-09-14T18:00:00+08:00")
SOURCE_TIME = datetime.fromisoformat("2026-09-14T15:39:32+08:00")


def board(number, amount, *, when=SOURCE_TIME):
    return {"f12": f"BK{number:04d}", "f13": 90, "f14": f"板块{number}",
            "f6": str(amount), "f124": int(when.timestamp()), "f3": 999 if amount == 0 else -1}


class Source:
    def __init__(self, industry=None, concepts=None):
        self.groups = {
            "2": industry if industry is not None else [board(i, 100000 - i) for i in range(105)],
            "3": concepts if concepts is not None else [board(200, 1)],
        }
        self.calls = []
        self.failed_codes = set()
        self.identity_override = {}
        self.daily_end = "2026-09-14"
        self.mutate_page = None
        self.failed_constituents = set()

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        query = parse_qs(urlsplit(url).query)
        if "getZDYLBData" in url:
            if query["fs"][0].startswith("b:"):
                code = query["fs"][0].split()[0].removeprefix("b:")
                if code in self.failed_constituents:
                    raise TimeoutError("成分来源超时")
                rows = [{"f12": f"{i:06d}", "f13": i % 2, "f14": f"股票{i}", "f20": str(1000 - i)}
                        for i in range(1, 102)]
                page = int(query["pn"][0])
                return {"rc": 0, "data": {"total": len(rows),
                        "diff": copy.deepcopy(rows[(page - 1) * 100:page * 100])}}
            kind = "2" if "t:2" in query["fs"][0] else "3"
            page = int(query["pn"][0])
            rows = self.groups[kind]
            payload = {"rc": 0, "data": {"total": len(rows), "diff": copy.deepcopy(rows[(page - 1) * 100:page * 100])}}
            if self.mutate_page:
                self.mutate_page(kind, page, payload)
            return payload
        code = query["secid"][0].removeprefix("90.")
        if code in self.failed_codes:
            raise TimeoutError("来源超时")
        member = next(row for group in self.groups.values() for row in group if row["f12"] == code)
        return {"rc": 0, "data": {
            "code": self.identity_override.get("code", code),
            "name": self.identity_override.get("name", member["f14"]),
            "market": self.identity_override.get("market", 90),
            "klines": [f"{self.daily_end},100.00,101.20,102.00,99.00,200,100000.25,0,0,0,0"],
        }}


class SectorHeatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = Settings(database_path=Path(self.temporary.name) / "app.sqlite3")

    def tearDown(self):
        self.temporary.cleanup()

    def refresh(self, source, **kwargs):
        return refresh_sector_heat(self.settings, fetcher=source, throttle_seconds=0, now=NOW, **kwargs)

    def test_empty_snapshot_and_source_scope(self):
        result = read_sector_heat(self.settings)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["universe"]["status"], "empty")
        self.assertEqual(result["universe"]["source_policy"]["usage"], "market_context_only")

    def test_ths_identity_parser_requires_explicit_codes(self):
        parser = _ThsLinkParser()
        parser.feed("<a href='/gn/detail/code/301558/'>阿里巴巴概念</a><a href='https://stockpage.10jqka.com.cn/600000/'>浦发银行</a><input value='885944' id='clid'>")
        self.assertEqual(parser.links, [("阿里巴巴概念", "301558")])
        self.assertEqual(parser.clid, "885944")
        self.assertEqual(parser.stock_codes, {"600000"})

    def test_reads_all_pages_before_combined_turnover_ranking(self):
        source = Source([board(i, i) for i in range(205)], [board(300 + i, i) for i in range(101)])
        directory = fetch_sector_directory(source, now=NOW)
        self.assertEqual(len(directory), 306)
        self.assertEqual(directory[0]["code"], "BK0204")
        self.assertEqual(directory[-2]["heat_value"], "0")
        calls = [parse_qs(urlsplit(url).query) for url, _ in source.calls]
        self.assertEqual([item["pn"][0] for item in calls], ["1", "2", "3", "1", "2"])
        self.assertTrue(all(item["pz"] == ["100"] and item["fid"] == ["f6"] for item in calls))
        self.assertEqual({item["fs"][0] for item in calls}, {"m:90+t:2", "m:90+t:3"})

    def test_top100_uses_exact_turnover_and_code_ties_not_price_change(self):
        source = Source([board(i, "10000000000000000.01") for i in range(105)],
                        [board(200, "10000000000000000.02"), board(201, 0)])
        progress = []
        result = self.refresh(source, progress=progress.append)
        items = result["items"]
        self.assertEqual(len(items), 100)
        self.assertEqual([item["code"] for item in items[:3]], ["BK0200", "BK0000", "BK0001"])
        self.assertEqual(items[-1]["code"], "BK0098")
        self.assertNotIn("BK0201", [item["code"] for item in items])
        self.assertEqual([item["heat_rank"] for item in items], list(range(1, 101)))
        self.assertEqual(result["universe"]["status"], "ready")
        self.assertEqual(result["universe"]["ranking_as_of"], "2026-09-14")
        self.assertEqual(result["universe"]["collected_count"], 100)
        self.assertEqual(items[0]["membership"]["member_count"], 101)
        self.assertEqual(items[0]["membership"]["weight_basis"], "not_provided")
        self.assertEqual(len(progress), 11)
        self.assertTrue(all(timeout == 12 for _, timeout in source.calls))
        with connection_scope(self.settings) as connection:
            stored = connection.execute("SELECT heat_value FROM sector_heat_member WHERE code='BK0200'").fetchone()[0]
            self.assertEqual(stored, "10000000000000000.02")
            self.assertEqual(Decimal(stored), Decimal("10000000000000000.02"))
            identity = connection.execute(
                "SELECT name,kind,taxonomy_revision FROM sector_identity WHERE source_code='BK0200'"
            ).fetchone()
            self.assertEqual(tuple(identity), ("板块200", "概念", "2026-09-14"))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sector_identity_revision").fetchone()[0], 100)

    def test_duplicate_across_classifications_deduplicates_only_identical_identity(self):
        first = board(1, 200)
        source = Source([first], [copy.deepcopy(first), board(2, 100)])
        directory = fetch_sector_directory(source, now=NOW)
        self.assertEqual([item["code"] for item in directory], ["BK0001", "BK0002"])
        self.assertEqual(directory[0]["kind"], "行业")
        source.groups["3"][0]["f6"] = "201"
        with self.assertRaisesRegex(ValueError, "跨分类重复"):
            fetch_sector_directory(source, now=NOW)

    def test_duplicate_page_total_drift_and_short_page_are_rejected(self):
        def duplicate(kind, page, payload):
            if kind == "2" and page == 2:
                payload["data"]["diff"][0] = board(0, 100000)

        def changed_total(kind, page, payload):
            if kind == "2" and page == 2:
                payload["data"]["total"] += 1

        def short_page(kind, page, payload):
            if kind == "2" and page == 1:
                payload["data"]["diff"].pop()

        for mutate in (duplicate, changed_total, short_page):
            with self.subTest(mutate=mutate.__name__):
                source = Source()
                source.mutate_page = mutate
                with self.assertRaises(ValueError):
                    fetch_sector_directory(source, now=NOW)

    def test_ranking_failure_retains_complete_old_snapshot_and_dates(self):
        original = self.refresh(Source())
        source = Source()
        source.groups["3"][0]["f124"] = int((SOURCE_TIME - timedelta(days=1)).timestamp())
        with self.assertRaisesRegex(RuntimeError, "日期不一致"):
            self.refresh(source)
        result = read_sector_heat(self.settings)
        self.assertEqual(result["items"], original["items"])
        self.assertEqual(result["universe"]["updated_at"], original["universe"]["updated_at"])
        self.assertEqual(result["universe"]["as_of"], original["universe"]["as_of"])
        self.assertIn("保留上次快照", result["universe"]["last_error"])
        self.assertEqual(result["universe"]["status"], "partial")

    def test_first_ranking_failure_is_empty_with_error(self):
        source = Source()
        source.groups["2"][0]["f6"] = "-"
        with self.assertRaises(RuntimeError):
            self.refresh(source)
        result = read_sector_heat(self.settings)
        self.assertEqual(result["universe"]["status"], "empty")
        self.assertIsNone(result["universe"]["updated_at"])
        self.assertIsNotNone(result["universe"]["last_error"])

    def test_older_source_ranking_cannot_replace_newer_saved_ranking(self):
        original = self.refresh(Source())
        source = Source()
        for group in source.groups.values():
            for item in group:
                item["f124"] = int((SOURCE_TIME - timedelta(days=3)).timestamp())
        with self.assertRaisesRegex(RuntimeError, "旧榜覆盖新榜"):
            self.refresh(source)
        self.assertEqual(read_sector_heat(self.settings)["items"], original["items"])

    def test_failed_daily_retains_real_previous_rows_and_date(self):
        original = self.refresh(Source())
        source = Source()
        source.failed_codes = {"BK0000"}
        result = self.refresh(source)
        item = result["items"][0]
        self.assertEqual(item["rows"], original["items"][0]["rows"])
        self.assertEqual(item["updated_at"], original["items"][0]["updated_at"])
        self.assertIn("保留旧行情至2026-09-14", item["collection_error"])
        self.assertEqual(result["universe"]["collected_count"], 99)
        self.assertEqual(result["universe"]["failed_count"], 1)

    def test_first_daily_failure_does_not_fabricate_rows(self):
        source = Source()
        source.failed_codes = {"BK0000"}
        item = self.refresh(source)["items"][0]
        self.assertEqual(item["rows"], [])
        self.assertIsNone(item["updated_at"])
        self.assertIn("暂无可用日线", item["collection_error"])

    def test_stale_returned_history_is_stored_with_explicit_error(self):
        source = Source()
        source.daily_end = "2026-09-11"
        result = self.refresh(source)
        self.assertEqual(result["items"][0]["rows"][-1]["date"], "2026-09-11")
        self.assertIn("日线仅到2026-09-11", result["items"][0]["collection_error"])
        self.assertEqual(result["universe"]["failed_count"], 100)

    def test_daily_identity_fields_are_verified_and_not_guessed(self):
        for override in ({"code": "BK9999"}, {"name": "另一板块"}, {"market": 1}):
            with self.subTest(override=override):
                source = Source()
                member = fetch_sector_directory(source, now=NOW)[0]
                source.identity_override = override
                with self.assertRaisesRegex(ValueError, "与目录不一致"):
                    fetch_sector_daily(member, source)

    def test_daily_dates_and_ohlc_are_validated(self):
        member = fetch_sector_directory(Source(), now=NOW)[0]
        for lines in (
            ["2026-09-14,100,101,100,99,1,1"],
            ["2026-09-15,100,101,102,99,1,1"],
            ["2026-09-14,100,101,102,99,1,1"] * 2,
            ["2026-09-14,100,NaN,102,99,1,1"],
        ):
            with self.subTest(lines=lines):
                def fetcher(url, timeout):
                    return {"rc": 0, "data": {"code": member["code"], "market": 90, "name": member["name"], "klines": lines}}
                with self.assertRaises(ValueError):
                    fetch_sector_daily(member, fetcher)

    def test_snapshot_contract_does_not_create_fund_relations(self):
        self.refresh(Source())
        result = read_sector_heat(self.settings)
        item = result["items"][0]
        self.assertEqual(item["universe_type"], "hot_board")
        self.assertEqual(item["funds"], [])
        self.assertEqual(item["rows"][0]["amount"], "100000.25")
        self.assertEqual(item["rows"][0]["close"], "101.20")
        self.assertIsNone(item["collection_error"])
        self.assertEqual(item["membership"]["status"], "ready")
        self.assertEqual(item["membership"]["members"][0]["stock_code"], "000001")
        with connection_scope(self.settings) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM market_index_projection").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM fund_market_relation").fetchone()[0], 0)

    def test_constituents_are_complete_versioned_and_have_no_invented_weights(self):
        source = Source()
        member = fetch_sector_directory(source, now=NOW)[0]
        components = fetch_sector_constituents(member, source)
        self.assertEqual(len(components), 101)
        self.assertEqual(components[-1]["source_order"], 101)
        self.assertNotIn("weight", components[0])
        calls = [query for url, _ in source.calls
                 if (query := parse_qs(urlsplit(url).query)).get("fs", [""])[0].startswith("b:")]
        self.assertEqual([call["pn"][0] for call in calls], ["1", "2"])
        self.assertTrue(all(call["fid"] == ["f20"] for call in calls))

    def test_constituent_failure_is_visible_without_erasing_same_day_old_membership(self):
        original = self.refresh(Source())
        source = Source()
        source.failed_constituents = {original["items"][0]["code"]}
        result = self.refresh(source)
        membership = result["items"][0]["membership"]
        self.assertEqual(membership["status"], "failed")
        self.assertEqual(membership["member_count"], 101)
        self.assertIn("保留101个旧成分", membership["error"])


if __name__ == "__main__":
    unittest.main()
