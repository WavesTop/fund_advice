"""Tonghuashun-only sector refresh and storage contract tests."""
from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from asgi_client import ASGITestClient
from backend.api.main import create_app
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.core.trading_calendar import get_calendar
from backend.storage.database import connection_scope
from backend.storage.related_market import upsert_index_projection
from backend.storage.sector_heat import add_custom_boards, read_sector_heat, remove_sector_subjects, save_sector_heat, merge_consistent_sector_history
from scripts.import_sector_heat import (
    fetch_ths_industry_directory,
    fetch_ths_sector_daily,
    fetch_ths_today,
    _parse_ths_market_code_from_detail_html,
    _parse_ths_standard_sector_links,
    refresh_sector_heat,
    refresh_selected_ths_boards,
    search_ths_boards,
)


NOW = datetime.fromisoformat("2026-09-18T18:00:00+08:00")
DAYS = [value.isoformat() for value in get_calendar().sessions(date(2026, 1, 1), NOW.date())][-130:]
LONG_DAYS = [value.isoformat() for value in get_calendar().sessions(date(2025, 1, 2), NOW.date())]


def directory() -> list[dict]:
    return [
        {"code": "881101", "name": "合成行业甲", "kind": "行业", "source_id": "sector_daily.ths",
         "heat_value": "200", "heat_updated_at": NOW.isoformat(), "ranking_as_of": DAYS[-1], "heat_rank": 1},
        {"code": "881102", "name": "合成行业乙", "kind": "行业", "source_id": "sector_daily.ths",
         "heat_value": "100", "heat_updated_at": NOW.isoformat(), "ranking_as_of": DAYS[-1], "heat_rank": 2},
    ]


def directory_result() -> dict:
    return {
        "rows": directory(),
        "provenance": {
            "directory": {"mode": "live", "endpoint": "akshare.stock_board_industry_name_ths",
                          "live_error": None},
            "summary": {"status": "available", "endpoint": "akshare.stock_board_industry_summary_ths",
                        "error": None},
        },
        "source_attempts": [
            {"dataset": "directory", "subject": "ths_industry",
             "endpoint": "akshare.stock_board_industry_name_ths", "provenance": "live",
             "status": "available", "row_count": 2, "error": None},
            {"dataset": "industry_summary", "subject": "ths_industry",
             "endpoint": "akshare.stock_board_industry_summary_ths", "provenance": "live",
             "status": "available", "row_count": 2, "error": None},
        ],
    }


class Frame:
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def iterrows(self):
        return iter(enumerate(self.rows))


def prices(member, code, *, timeout=12):
    return [{"date": day, "open": "100", "high": "102", "low": "99", "close": "101",
             "volume": "1000", "amount": "100000"} for day in DAYS]


def long_prices(member, code, *, timeout=12):
    return [{"date": day, "open": "100", "high": "102", "low": "99", "close": "101",
             "volume": "1000", "amount": "100000"} for day in LONG_DAYS]


def constituents(member, *, timeout=12, throttle_seconds=.5):
    suffix = int(member["code"][-1])
    return [{"stock_code": f"60000{suffix}", "stock_name": member["name"] + "公司", "market": 1,
             "source_order": 1, "market_cap": "100000000"}]


class SectorHeatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name) / "app.sqlite3")
        etfs = patch("scripts.import_sector_heat.fetch_ths_etf_directory", return_value=[])
        etfs.start()
        self.addCleanup(etfs.stop)

    def refresh(self, **kwargs):
        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", return_value=directory_result()), patch(
            "scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=prices,
        ), patch("scripts.import_sector_heat.fetch_ths_visible_constituents", side_effect=constituents):
            return refresh_sector_heat(
                self.settings, now=NOW, workers=1, throttle_seconds=0, budget_seconds=60, **kwargs,
            )

    def test_empty_snapshot_has_market_context_scope(self):
        result = read_sector_heat(self.settings)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["universe"]["status"], "empty")
        self.assertEqual(result["universe"]["source_policy"]["provider"], "同花顺")

    def test_refresh_publishes_native_prices_and_current_membership(self):
        result = self.refresh()
        self.assertEqual(result["universe"]["requested_count"], 2)
        self.assertEqual(result["universe"]["status"], "ready")
        self.assertEqual(result["price_window_coverage"], {"short": 2, "medium": 2, "long": 2})
        self.assertTrue(all(item["source_id"] == "sector_daily.ths" for item in result["items"]))
        self.assertTrue(all(item["history_relation"] == "native_source" for item in result["items"]))
        self.assertTrue(all(item["membership"]["status"] == "ready" for item in result["items"]))
        self.assertEqual(sum(item["membership"]["member_count"] for item in result["items"]), 2)
        self.assertEqual(result["directory_provenance"]["directory"]["mode"], "live")

    def test_removed_initial_board_stays_removed_after_refresh(self):
        self.refresh()
        removed = remove_sector_subjects(self.settings, [{
            "universe_type": "hot_board", "source_id": "sector_daily.ths",
            "code": "881101", "name": "合成行业甲",
        }], removed_at=NOW.isoformat())
        self.assertEqual([item["code"] for item in removed], ["881101"])
        self.assertEqual([item["code"] for item in read_sector_heat(self.settings)["items"]], ["881102"])
        result = self.refresh()
        self.assertEqual([item["code"] for item in result["items"]], ["881102"])
        self.assertEqual(result["universe"]["requested_count"], 1)

    def test_custom_board_joins_normal_refresh_and_series(self):
        add_custom_boards(self.settings, [{
            "code": "886001", "name": "合成概念", "kind": "概念", "source_id": "sector_daily.ths",
        }], added_at=NOW.isoformat())
        result = self.refresh()
        stored = {item["code"]: item for item in result["items"]}
        self.assertIn("886001", stored)
        self.assertEqual(stored["886001"]["kind"], "概念")
        self.assertEqual(stored["886001"]["history_relation"], "native_source")
        response = ASGITestClient(create_app(self.settings))
        with response as client:
            series = client.get("/api/sectors/886001/series", params={
                "source_id": "sector_daily.ths", "universe_type": "hot_board",
            })
            self.assertEqual(series.status_code, 200)
            self.assertEqual(series.json()["as_of"], DAYS[-1])

    def test_manual_reference_index_is_retried_as_ths_board_on_refresh(self):
        upsert_index_projection(
            self.settings, index_code="931248", index_name="油气资源",
            source_id="index_daily.csi_official",
        )
        calls = []

        def tracked_prices(member, code, *, timeout=12):
            calls.append(code)
            return prices(member, code, timeout=timeout)

        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", return_value=directory_result()), patch(
            "scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=tracked_prices,
        ), patch("scripts.import_sector_heat.fetch_ths_visible_constituents", side_effect=constituents):
            result = refresh_sector_heat(
                self.settings, now=NOW, workers=1, throttle_seconds=0, budget_seconds=60,
            )
        self.assertIn("931248", calls)
        stored = {item["code"]: item for item in result["items"]}
        self.assertEqual((stored["931248"]["source_id"], stored["931248"]["kind"]),
                         ("sector_daily.ths", "指数"))
        self.assertEqual(stored["931248"]["history_source_code"], "zs_931248")
        visible = read_sector_heat(self.settings)
        self.assertTrue(any(item["code"] == "931248" for item in visible["items"]))

    def test_selected_custom_refresh_only_fetches_added_board(self):
        self.refresh()
        calls = []

        def tracked_prices(member, code, *, timeout=12):
            calls.append(code)
            return prices(member, code, timeout=timeout)

        with patch("scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=tracked_prices), patch(
            "scripts.import_sector_heat.fetch_ths_visible_constituents", side_effect=constituents,
        ):
            result = refresh_selected_ths_boards(self.settings, [{
                "code": "886001", "name": "合成概念", "kind": "概念", "source_id": "sector_daily.ths",
            }], now=NOW, throttle_seconds=0)
        self.assertEqual(calls, ["886001"])
        stored = {item["code"]: item for item in result["items"]}
        self.assertIn("881101", stored)
        self.assertIn("886001", stored)
        self.assertEqual(stored["886001"]["rows"][-1]["date"], DAYS[-1])

    def test_post_close_refresh_accepts_same_day_ths_daily_before_strict_calendar_cutoff(self):
        current = NOW.replace(hour=15, minute=1)
        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", return_value=directory_result()), patch(
            "scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=prices,
        ), patch("scripts.import_sector_heat.fetch_ths_visible_constituents", side_effect=constituents):
            result = refresh_sector_heat(
                self.settings, now=current, workers=1, throttle_seconds=0, budget_seconds=60,
            )
        self.assertEqual(result["price_cutoff"], DAYS[-1])
        self.assertTrue(all(item["rows"][-1]["date"] == DAYS[-1] for item in result["items"]))
        self.assertTrue(all(item["collection_error"] is None for item in result["items"]))

    def test_search_ths_boards_matches_industry_and_concept(self):
        standard = {
            "rows": [
                *directory_result()["rows"],
                {"code": "886001", "name": "合成概念", "kind": "概念",
                 "source_id": "sector_daily.ths", "heat_value": "0",
                 "heat_updated_at": NOW.isoformat(), "ranking_as_of": DAYS[-1]},
                {"code": "881107", "name": "油气开采及服务", "kind": "行业",
                 "source_id": "sector_daily.ths", "ths_category": "industry",
                 "ths_page_code": "881107", "ths_market_code": "881107", "ths_prefix": "bk",
                 "history_source_id": "sector_daily.ths", "history_source_code": "bk_881107",
                 "history_identity_match": "native_source_code",
                 "heat_value": "0", "heat_updated_at": NOW.isoformat(), "ranking_as_of": DAYS[-1]},
            ],
            "source_attempts": [],
        }
        concept = {
            "rows": [{"code": "886001", "name": "合成概念", "kind": "概念",
                      "source_id": "sector_daily.ths", "heat_value": "0",
                      "heat_updated_at": NOW.isoformat(), "ranking_as_of": DAYS[-1]}],
            "source_attempts": [],
        }
        with patch("scripts.import_sector_heat.fetch_ths_standard_sector_directory",
                   return_value=standard), patch(
            "scripts.import_sector_heat.fetch_ths_industry_directory", return_value=directory_result()), patch(
            "scripts.import_sector_heat.fetch_ths_concept_directory", return_value=concept,
        ):
            by_name = search_ths_boards("合成")
            self.assertEqual({item["code"] for item in by_name["items"]}, {"881101", "881102", "886001"})
            by_code = search_ths_boards("886001")
            self.assertEqual(by_code["items"][0]["kind"], "概念")
            relaxed = search_ths_boards("油气资源")
            self.assertEqual(relaxed["items"][0]["code"], "881107")

    def test_exact_board_code_falls_back_to_ths_daily_identity(self):
        concept = {"rows": [], "source_attempts": []}
        payload = 'callback({"name":"合成代码板块","data":"20260918,1,2,1,2,10,20"});'.encode("gb18030")
        with patch("scripts.import_sector_heat.fetch_ths_standard_sector_directory",
                   return_value={"rows": [], "source_attempts": []}), patch(
            "scripts.import_sector_heat.fetch_ths_industry_directory", return_value=directory_result()), patch(
            "scripts.import_sector_heat.fetch_ths_concept_directory", return_value=concept,
        ), patch("scripts.import_sector_heat._fetch_ths_standard_sector_entries_cached", return_value=()), \
             patch("scripts.import_sector_heat.get_bytes", return_value=payload):
            result = search_ths_boards("881153")
        self.assertEqual(result["items"][0]["code"], "881153")
        self.assertEqual(result["items"][0]["name"], "合成代码板块")
        self.assertEqual(result["items"][0]["kind"], "行业")
        self.assertEqual(result["items"][0]["history_source_code"], "bk_881153")

    def test_ths_standard_directory_parses_index_and_concept_identity(self):
        html = """
        <a href="https://q.10jqka.com.cn/zs/detail/code/931248/">油气资源</a>
        <a href="https://q.10jqka.com.cn/gn/detail/code/301558/">合成概念</a>
        """
        rows = _parse_ths_standard_sector_links(html)
        by_code = {item["code"]: item for item in rows}
        self.assertEqual(by_code["931248"]["kind"], "指数")
        self.assertEqual(by_code["931248"]["history_source_code"], "zs_931248")
        self.assertEqual(by_code["301558"]["kind"], "概念")
        self.assertEqual(by_code["301558"]["ths_market_code"], "")
        self.assertEqual(_parse_ths_market_code_from_detail_html(
            '<input id="clid" value="886001">'
        ), "886001")

    def test_fetch_sector_daily_uses_saved_ths_source_code(self):
        member = {
            "code": "931248", "name": "油气资源", "kind": "指数", "source_id": "sector_daily.ths",
            "ranking_as_of": DAYS[-1], "history_source_code": "zs_931248",
            "price_cutoff": DAYS[-1],
            "history_source_id": "sector_daily.ths", "ths_category": "index",
            "ths_page_code": "931248", "ths_market_code": "931248", "ths_prefix": "zs",
        }
        payload = 'callback({"name":"油气资源","data":"20260918,1,2,1,2,10,20"});'.encode("gb18030")
        with patch("scripts.import_sector_heat.get_bytes", return_value=payload) as get:
            rows = fetch_ths_sector_daily(member, member["code"])
        self.assertEqual(rows[-1]["date"], "2026-09-18")
        url = get.call_args[0][0].full_url
        self.assertIn("/zs_931248/01/last.js", url)

    def test_short_source_window_preserves_consistent_long_native_history(self):
        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", return_value=directory_result()), patch(
            "scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=long_prices,
        ), patch("scripts.import_sector_heat.fetch_ths_visible_constituents", side_effect=constituents):
            refresh_sector_heat(self.settings, now=NOW, workers=1, throttle_seconds=0, budget_seconds=60)
        result = self.refresh()
        self.assertTrue(all(len(item["rows"]) == len(LONG_DAYS) for item in result["items"]))
        self.assertTrue(all(item["rows"][0]["date"] == LONG_DAYS[0] for item in result["items"]))
        daily = [item for item in result["source_attempts"] if item["dataset"] == "daily"]
        self.assertTrue(all(item["status"] == "available" for item in daily))
        self.assertTrue(all(item["overlap_row_count"] == len(DAYS) for item in daily))
        self.assertTrue(all(item["preserved_row_count"] == len(LONG_DAYS) - len(DAYS) for item in daily))
        self.assertTrue(all(item["merged_row_count"] == len(LONG_DAYS) for item in daily))

    def test_overlap_conflict_keeps_old_history_and_marks_refresh_failed(self):
        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", return_value=directory_result()), patch(
            "scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=long_prices,
        ), patch("scripts.import_sector_heat.fetch_ths_visible_constituents", side_effect=constituents):
            refresh_sector_heat(self.settings, now=NOW, workers=1, throttle_seconds=0, budget_seconds=60)
        before = read_sector_heat(self.settings)

        def conflicting(member, code, *, timeout=12):
            rows = prices(member, code, timeout=timeout)
            rows[0] = {**rows[0], "close": "100.5"}
            return rows

        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", return_value=directory_result()), patch(
            "scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=conflicting,
        ), patch("scripts.import_sector_heat.fetch_ths_visible_constituents", side_effect=constituents):
            result = refresh_sector_heat(
                self.settings, now=NOW, workers=1, throttle_seconds=0, budget_seconds=60,
            )
        self.assertEqual(result["universe"]["status"], "partial")
        self.assertTrue(all(item["rows"] == original["rows"]
                            for item, original in zip(result["items"], before["items"])))
        self.assertTrue(all("重叠值冲突" in item["collection_error"] for item in result["items"]))
        daily = [item for item in result["source_attempts"] if item["dataset"] == "daily"]
        self.assertTrue(all(item["status"] == "failed" and "重叠值冲突" in item["error"] for item in daily))

    def test_storage_rejects_direct_same_identity_history_shrink(self):
        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", return_value=directory_result()), patch(
            "scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=long_prices,
        ), patch("scripts.import_sector_heat.fetch_ths_visible_constituents", side_effect=constituents):
            refresh_sector_heat(self.settings, now=NOW, workers=1, throttle_seconds=0, budget_seconds=60)
        stored = read_sector_heat(self.settings)
        shortened = {**stored["items"][0], "rows": stored["items"][0]["rows"][-130:]}
        with self.assertRaisesRegex(ValueError, "拒绝缩短历史"):
            save_sector_heat(
                self.settings, as_of=stored["universe"]["as_of"], updated_at=NOW.isoformat(),
                catalog_count=1, members=[shortened], requested_count=1,
                universe_scope="verified_industry_all",
            )
        after = read_sector_heat(self.settings)
        self.assertEqual(after["items"][0]["rows"], stored["items"][0]["rows"])
        with self.assertRaisesRegex(ValueError, "本次来源历史为空"):
            save_sector_heat(
                self.settings, as_of=stored["universe"]["as_of"], updated_at=NOW.isoformat(),
                catalog_count=1, members=[{**stored["items"][0], "rows": []}], requested_count=1,
                universe_scope="verified_industry_all",
            )
        self.assertEqual(read_sector_heat(self.settings)["items"][0]["rows"], stored["items"][0]["rows"])

    def test_repeated_empty_history_failure_does_not_roll_back_other_boards(self):
        attempt = 0

        def partially_available(member, code, *, timeout=12):
            if code == "881101":
                raise TimeoutError("synthetic repeated unavailable history")
            rows = prices(member, code, timeout=timeout)
            return rows[:-1] if attempt == 0 else rows

        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", return_value=directory_result()), patch(
            "scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=partially_available,
        ), patch("scripts.import_sector_heat.fetch_ths_visible_constituents", side_effect=constituents):
            for attempt in range(2):
                result = refresh_sector_heat(
                    self.settings, now=NOW, workers=1, throttle_seconds=0, budget_seconds=60,
                )
                self.assertEqual(result["universe"]["status"], "partial")
        stored = {item["code"]: item for item in read_sector_heat(self.settings)["items"]}
        self.assertEqual(stored["881101"]["rows"], [])
        self.assertIn("synthetic repeated", stored["881101"]["collection_error"])
        self.assertEqual(stored["881102"]["rows"][-1]["date"], DAYS[-1])
        self.assertIsNone(stored["881102"]["collection_error"])

    def test_retired_sector_source_cannot_replace_snapshot_but_legacy_rows_remain_readable(self):
        original = self.refresh()
        stored = read_sector_heat(self.settings)
        item = dict(stored["items"][0])
        for change in (
            {"source_id": "sector_daily.eastmoney"},
            {"source_id": "东方财富.行业板块"},
            {"history_source_id": "sector_daily.east-money"},
        ):
            rejected = {**item, **change}
            with self.subTest(change=change), self.assertRaises(AppError) as caught:
                save_sector_heat(
                    self.settings, as_of=stored["universe"]["as_of"], updated_at=NOW.isoformat(),
                    catalog_count=1, members=[rejected], requested_count=1,
                    universe_scope="verified_industry_all",
                )
            self.assertEqual(caught.exception.body.code, "sector_source_not_allowed")
        self.assertEqual(read_sector_heat(self.settings)["universe"]["member_count"],
                         original["universe"]["member_count"])
        with connection_scope(self.settings) as connection:
            connection.execute(
                "UPDATE sector_heat_member SET source_id='sector_daily.eastmoney', "
                "history_source_id='sector_daily.eastmoney' WHERE code=?",
                (item["code"],),
            )
        legacy = read_sector_heat(self.settings)
        legacy_item = next(row for row in legacy["items"] if row["code"] == item["code"])
        self.assertEqual((legacy_item["source_id"], legacy_item["history_source_id"]),
                         ("sector_daily.eastmoney", "sector_daily.eastmoney"))

    def test_constituent_failure_is_visible_and_does_not_remove_board(self):
        def failed(member, **kwargs):
            if member["code"] == "881102":
                raise ValueError("普通分页第6页触发访问限制")
            return constituents(member, **kwargs)
        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", return_value=directory_result()), patch(
            "scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=prices,
        ), patch("scripts.import_sector_heat.fetch_ths_visible_constituents", side_effect=failed):
            result = refresh_sector_heat(self.settings, now=NOW, workers=1, throttle_seconds=0, budget_seconds=60)
        self.assertEqual(len(result["items"]), 2)
        failed_item = next(item for item in result["items"] if item["code"] == "881102")
        self.assertEqual(failed_item["membership"]["status"], "failed")
        self.assertIn("第6页", failed_item["membership"]["error"])

    def test_daily_jsonp_validates_identity_and_cutoff(self):
        member = {**directory()[0], "price_cutoff": DAYS[-1]}
        values = ["20260918,100,102,99,101,1000,100000", "20260919,-,-,-,-,-,-"]
        payload = "callback(" + json.dumps({"name": member["name"], "data": ";".join(values)}) + ")"
        with patch("scripts.import_sector_heat.fetch_text", return_value=payload):
            rows = fetch_ths_sector_daily(member, member["code"])
        self.assertEqual([row["date"] for row in rows], ["2026-09-18"])
        bad = "callback(" + json.dumps({"name": "错误行业", "data": values[0]}) + ")"
        with patch("scripts.import_sector_heat.fetch_text", return_value=bad), self.assertRaisesRegex(ValueError, "名称"):
            fetch_ths_sector_daily(member, member["code"])

    def test_intraday_quote_replaces_same_day_bar_without_duplicating_date(self):
        current = NOW.replace(hour=10, minute=31)
        member = {**directory()[0], "price_cutoff": DAYS[-1], "quote_requested_at": current.isoformat()}
        history = 'callback(' + json.dumps({"name": member["name"], "data":
            "20260917,100,102,99,101,1000,100000;20260918,101,103,100,102,1100,110000"}) + ')'
        quote = 'callback(' + json.dumps({"bk_881101": {"name": member["name"], "1": "20260918",
            "7": "101", "8": "105", "9": "100", "11": "104", "13": "1500", "19": "150000", "dt": "1030"}}) + ')'
        with patch("scripts.import_sector_heat.fetch_ths_line_text_by_source", return_value=history), \
             patch("scripts.import_sector_heat.fetch_text", return_value=quote):
            rows = fetch_ths_sector_daily(member, member["code"])
        self.assertEqual([row["date"] for row in rows], ["2026-09-17", "2026-09-18"])
        self.assertEqual(rows[-1]["close"], "104")
        self.assertTrue(rows[-1]["provisional"])
        self.assertEqual(rows[-1]["quote_as_of"], "2026-09-18T10:30:00+08:00")

    def test_stale_quote_is_rejected(self):
        quote = 'callback(' + json.dumps({"bk_881101": {"name": directory()[0]["name"], "1": "20260917"}}) + ')'
        with patch("scripts.import_sector_heat.fetch_text", return_value=quote), \
             self.assertRaisesRegex(ValueError, "未更新"):
            fetch_ths_today(directory()[0], "bk_881101", current=NOW.replace(hour=10))

    def test_repeated_intraday_refresh_and_next_day_finalization_preserve_history(self):
        old = prices(directory()[0], "881101")
        old[-1] = {**old[-1], "provisional": True, "quote_as_of": "2026-09-18T10:00:00+08:00"}
        newer = [*old[:-1], {**old[-1], "close": "101", "quote_as_of": "2026-09-18T10:30:00+08:00"}]
        merged, _ = merge_consistent_sector_history(old, newer)
        self.assertEqual(merged[-1]["close"], "101")
        final = [*newer[:-1], {key: value for key, value in newer[-1].items() if key not in ("provisional", "quote_as_of")}]
        final[-1]["close"] = "102"
        merged, _ = merge_consistent_sector_history(merged, final)
        self.assertEqual(merged[-1]["close"], "102")
        self.assertNotIn("provisional", merged[-1])
        with self.assertRaisesRegex(ValueError, "时间早于"):
            merge_consistent_sector_history(newer, old)

    def test_intraday_refresh_updates_stored_same_day_price(self):
        current = NOW.replace(hour=10, minute=31)
        def intraday_prices(member, code, *, timeout=12):
            rows = prices(member, code, timeout=timeout)
            rows[-1] = {**rows[-1], "close": value[0], "high": "110", "provisional": True,
                        "quote_as_of": current.isoformat()}
            return rows
        value = ["101"]
        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", return_value=directory_result()), \
             patch("scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=intraday_prices), \
             patch("scripts.import_sector_heat.fetch_ths_visible_constituents", side_effect=constituents):
            refresh_sector_heat(self.settings, now=current, workers=1, throttle_seconds=0, budget_seconds=60)
            value[0] = "105"
            result = refresh_sector_heat(self.settings, now=current, workers=1, throttle_seconds=0, budget_seconds=60)
        self.assertTrue(all(item["rows"][-1]["close"] == "105" for item in result["items"]))
        self.assertTrue(all(item["collection_error"] is None for item in result["items"]))

    def test_failed_intraday_refresh_keeps_last_quote_and_reports_failure(self):
        current = NOW.replace(hour=10, minute=31)
        self.refresh()
        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", return_value=directory_result()), \
             patch("scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=TimeoutError("实时行情不可用")), \
             patch("scripts.import_sector_heat.fetch_ths_visible_constituents", side_effect=constituents):
            result = refresh_sector_heat(self.settings, now=current, workers=1, throttle_seconds=0, budget_seconds=60)
        self.assertTrue(all(item["rows"][-1]["close"] == "101" for item in result["items"]))
        self.assertTrue(all("实时行情不可用" in item["collection_error"] for item in result["items"]))

    def test_market_cutoff_during_lunch_and_on_nontrading_day(self):
        calendar = get_calendar()
        self.assertEqual(calendar.latest_market_price_session(NOW.replace(hour=12)), NOW.date())
        self.assertEqual(calendar.latest_market_price_session(NOW.replace(day=19, hour=10)), NOW.date())

    def test_directory_failure_keeps_previous_snapshot(self):
        original = self.refresh()
        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", side_effect=TimeoutError("offline")):
            with self.assertRaisesRegex(RuntimeError, "未替换"):
                refresh_sector_heat(self.settings, now=NOW, workers=1, throttle_seconds=0, budget_seconds=60)
        saved = read_sector_heat(self.settings)
        self.assertEqual([item["code"] for item in saved["items"]], [item["code"] for item in original["items"]])
        self.assertIsNotNone(saved["universe"]["last_error"])

    def test_directory_fallback_records_failed_live_attempt_and_seed_provenance(self):
        adapter = SimpleNamespace(
            stock_board_industry_name_ths=lambda: (_ for _ in ()).throw(TimeoutError("offline")),
            stock_board_industry_summary_ths=lambda: Frame([{"板块": "合成行业甲", "总成交额": "2"}]),
        )
        with tempfile.TemporaryDirectory() as directory_path:
            root = Path(directory_path)
            (root / "config").mkdir()
            (root / "config" / "ths-industry-boards.json").write_text(
                json.dumps([{"name": "合成行业甲", "code": "881101"}], ensure_ascii=False), encoding="utf-8",
            )
            with patch.dict("sys.modules", {"akshare": adapter}), patch("scripts.import_sector_heat.ROOT", root):
                result = fetch_ths_industry_directory(now=NOW)
        self.assertEqual(result["provenance"]["directory"]["mode"], "seed")
        self.assertIn("TimeoutError: offline", result["provenance"]["directory"]["live_error"])
        self.assertEqual([(item["provenance"], item["status"]) for item in result["source_attempts"][:2]],
                         [("live", "failed"), ("seed", "available")])
        self.assertEqual(result["provenance"]["summary"]["status"], "available")

    def test_summary_failure_is_explicit_without_hiding_live_directory(self):
        adapter = SimpleNamespace(
            stock_board_industry_name_ths=lambda: Frame([{"name": "合成行业甲", "code": "881101"}]),
            stock_board_industry_summary_ths=lambda: (_ for _ in ()).throw(RuntimeError("summary blocked")),
        )
        with patch.dict("sys.modules", {"akshare": adapter}):
            result = fetch_ths_industry_directory(now=NOW)
        self.assertEqual(result["provenance"]["directory"]["mode"], "live")
        self.assertEqual(result["provenance"]["summary"]["status"], "failed")
        self.assertIn("RuntimeError: summary blocked", result["provenance"]["summary"]["error"])
        summary_attempt = next(item for item in result["source_attempts"] if item["dataset"] == "industry_summary")
        self.assertEqual(summary_attempt["status"], "failed")
        self.assertEqual(result["rows"][0]["heat_value"], "0")

    def test_seed_directory_never_reports_a_fully_live_refresh(self):
        fallback = directory_result()
        fallback["provenance"]["directory"] = {
            "mode": "seed", "endpoint": "config/ths-industry-boards.json",
            "live_error": "TimeoutError: offline",
        }
        fallback["source_attempts"] = [
            {"dataset": "directory", "subject": "ths_industry",
             "endpoint": "akshare.stock_board_industry_name_ths", "provenance": "live",
             "status": "failed", "row_count": 0, "error": "TimeoutError: offline"},
            {"dataset": "directory", "subject": "ths_industry",
             "endpoint": "config/ths-industry-boards.json", "provenance": "seed",
             "status": "available", "row_count": 2, "error": None},
            fallback["source_attempts"][1],
        ]
        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", return_value=fallback), patch(
            "scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=prices,
        ), patch("scripts.import_sector_heat.fetch_ths_visible_constituents", side_effect=constituents):
            result = refresh_sector_heat(
                self.settings, now=NOW, workers=1, throttle_seconds=0, budget_seconds=60,
            )
        self.assertEqual(result["universe"]["status"], "partial")
        self.assertEqual(result["directory_provenance"]["directory"]["mode"], "seed")
        self.assertEqual(result["source_attempts"][0]["status"], "failed")
        self.assertEqual(result["source_attempts"][1]["provenance"], "seed")
        persisted = read_sector_heat(self.settings)
        self.assertEqual(persisted["directory_provenance"], fallback["provenance"])
        self.assertEqual(persisted["universe"]["directory_provenance"], fallback["provenance"])
        self.assertEqual(persisted["universe"]["status"], "partial")
        self.assertIn("完整性待核验", persisted["universe"]["label"])
        self.assertNotIn("完整读取", persisted["universe"]["source_policy"]["classification"])
        with ASGITestClient(create_app(self.settings)) as client:
            response = client.get("/api/sectors/opportunities")
        self.assertEqual(response.status_code, 200)
        api_universe = response.json()["universe"]
        self.assertEqual(api_universe["status"], "partial")
        self.assertEqual(api_universe["directory_provenance"], fallback["provenance"])
        self.assertIn("完整性待核验", api_universe["label"])
        self.assertIsNone(api_universe["last_error"])
        self.refresh()
        recovered = read_sector_heat(self.settings)
        self.assertEqual(recovered["universe"]["status"], "ready")
        self.assertEqual(recovered["directory_provenance"]["directory"]["mode"], "live")


if __name__ == "__main__":
    unittest.main()
