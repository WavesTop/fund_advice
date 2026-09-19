"""Tonghuashun-only sector refresh and storage contract tests."""
from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.core.config import Settings
from backend.core.trading_calendar import get_calendar
from backend.storage.sector_heat import read_sector_heat
from scripts.import_sector_heat import fetch_ths_sector_daily, refresh_sector_heat


NOW = datetime.fromisoformat("2026-09-18T18:00:00+08:00")
DAYS = [value.isoformat() for value in get_calendar().sessions(date(2026, 1, 1), NOW.date())][-130:]


def directory() -> list[dict]:
    return [
        {"code": "881101", "name": "合成行业甲", "kind": "行业", "source_id": "sector_daily.ths",
         "heat_value": "200", "heat_updated_at": NOW.isoformat(), "ranking_as_of": DAYS[-1], "heat_rank": 1},
        {"code": "881102", "name": "合成行业乙", "kind": "行业", "source_id": "sector_daily.ths",
         "heat_value": "100", "heat_updated_at": NOW.isoformat(), "ranking_as_of": DAYS[-1], "heat_rank": 2},
    ]


def prices(member, code, *, timeout=12):
    return [{"date": day, "open": "100", "high": "102", "low": "99", "close": "101",
             "volume": "1000", "amount": "100000"} for day in DAYS]


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
        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", return_value=directory()), patch(
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

    def test_constituent_failure_is_visible_and_does_not_remove_board(self):
        def failed(member, **kwargs):
            if member["code"] == "881102":
                raise ValueError("普通分页第6页触发访问限制")
            return constituents(member, **kwargs)
        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", return_value=directory()), patch(
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


if __name__ == "__main__":
    unittest.main()
