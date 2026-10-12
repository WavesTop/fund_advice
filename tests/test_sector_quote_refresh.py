"""Fast prices must stay fresh without relabeling old membership or history."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from asgi_client import ASGITestClient
from backend.api.main import create_app
from backend.core.config import Settings
from backend.storage.sector_heat import read_sector_heat
from backend.storage.database import connection_scope
from scripts.import_sector_heat import refresh_sector_heat, refresh_sector_quotes
from scripts.refresh_market_data import collect
from test_sector_heat import NOW, DAYS, directory_result, prices, constituents


class SectorQuoteRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name) / "quotes.sqlite3")
        old_directory = deepcopy(directory_result())
        for item in old_directory["rows"]:
            item["ranking_as_of"] = DAYS[-2]
        with patch("scripts.import_sector_heat.fetch_ths_industry_directory", return_value=old_directory), \
             patch("scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=lambda member, code, **kwargs: prices(member, code)[:-1]), \
             patch("scripts.import_sector_heat.fetch_ths_visible_constituents", side_effect=constituents):
            refresh_sector_heat(self.settings, now=NOW.replace(day=17), workers=1, throttle_seconds=0, budget_seconds=60)

    def quote(self, member, code, **kwargs):
        return {**prices(member, code)[-1], "provisional": True, "quote_as_of": "2026-09-18T10:30:00+08:00"}

    def test_complete_history_fetches_only_quote_and_preserves_membership_date(self):
        before = read_sector_heat(self.settings)
        with patch("scripts.import_sector_heat.fetch_ths_today", side_effect=self.quote) as quotes, \
             patch("scripts.import_sector_heat.fetch_ths_sector_daily") as daily, \
             patch("scripts.import_sector_heat.fetch_ths_visible_constituents") as members, \
             patch("scripts.import_sector_heat.fetch_ths_industry_directory") as catalog:
            result = refresh_sector_quotes(self.settings, now=NOW.replace(hour=10, minute=31))
        self.assertEqual(quotes.call_count, 2)
        daily.assert_not_called()
        members.assert_not_called()
        catalog.assert_not_called()
        self.assertEqual(result["price_cutoff"], DAYS[-1])
        for item, old in zip(result["items"], before["items"]):
            self.assertEqual(item["rows"][:-1], old["rows"])
            self.assertEqual(item["membership"], old["membership"])
            self.assertEqual(item["membership"]["as_of"], DAYS[-2])
            self.assertIsNone(item["collection_error"])

    def test_failed_quote_preserves_old_prices_and_timestamp_and_reports_failure(self):
        before = read_sector_heat(self.settings)
        with patch("scripts.import_sector_heat.fetch_ths_today", side_effect=TimeoutError("fixture quote offline")):
            after = refresh_sector_quotes(self.settings, now=NOW.replace(hour=10))
        for item, old in zip(after["items"], before["items"]):
            self.assertEqual(item["rows"], old["rows"])
            self.assertEqual(item["updated_at"], old["updated_at"])
            self.assertIn("quote offline", item["collection_error"])

    def test_missing_history_uses_daily_collection_instead_of_reusing_gap(self):
        before = read_sector_heat(self.settings)
        for item in before["items"]:
            del item["rows"][5]
        with connection_scope(self.settings) as connection:
            for item in before["items"]:
                connection.execute("UPDATE sector_heat_member SET rows_json=? WHERE code=?",
                                   (json.dumps(item["rows"]), item["code"]))
        with patch("scripts.import_sector_heat.fetch_ths_today") as quote, \
             patch("scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=prices) as daily:
            after = refresh_sector_quotes(self.settings, now=NOW.replace(hour=10))
        quote.assert_not_called()
        self.assertEqual(daily.call_count, 2)
        self.assertTrue(all(len(item["rows"]) == len(DAYS) for item in after["items"]))

    def test_next_session_confirms_previous_provisional_close_before_live_quote(self):
        with patch("scripts.import_sector_heat.fetch_ths_today", side_effect=self.quote):
            refresh_sector_quotes(self.settings, now=NOW.replace(hour=10, minute=31))
        def next_prices(member, code, **kwargs):
            rows = prices(member, code)
            rows[-1] = {**rows[-1], "high": "104", "close": "103"}
            return [*rows, {**rows[-1], "date": "2026-09-21", "provisional": True}]
        with patch("scripts.import_sector_heat.fetch_ths_today") as quote, \
             patch("scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=next_prices) as daily:
            result = refresh_sector_quotes(self.settings, now=NOW.replace(day=21, hour=10))
        self.assertEqual(daily.call_count, 2)
        quote.assert_not_called()
        self.assertTrue(all(item["rows"][-2]["close"] == "103" and not item["rows"][-2].get("provisional")
                            for item in result["items"]))

    def test_fast_worker_skips_financials_and_does_not_claim_membership_refresh(self):
        with patch("scripts.refresh_market_data.refresh_sector_quotes", return_value=read_sector_heat(self.settings)), \
             patch("scripts.refresh_market_data.refresh_existing_indexes", return_value={"items": []}), \
             patch("scripts.refresh_market_data.collect_sector_fundamentals") as financials:
            result = collect(self.settings, "sectors", mode="quotes")
        financials.assert_not_called()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["membership_updated"], 0)
        self.assertEqual(result["fundamentals"]["status"], "not_requested")

    def test_api_modes_are_explicit_and_legacy_route_keeps_full_refresh(self):
        with ASGITestClient(create_app(self.settings)) as client, \
             patch("backend.api.main.refresh_market_data", return_value={"refresh": {"status": "success"}}) as refresh:
            self.assertEqual(client.post("/api/sectors/refresh", json_body={"mode": "quotes"}).status_code, 200)
            refresh.assert_called_with(self.settings, "sectors", mode="quotes")
            self.assertEqual(client.post("/api/sectors/refresh").status_code, 200)
            refresh.assert_called_with(self.settings, "sectors")
            self.assertEqual(client.post("/api/sectors/refresh", json_body={"mode": "unknown"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
