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
from backend.storage.sector_heat import read_sector_heat, save_sector_heat
from scripts.import_sector_heat import fetch_ths_industry_directory, fetch_ths_sector_daily, refresh_sector_heat


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
