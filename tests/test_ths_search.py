"""THS page/quote identities, ETF aliases and upgrade preservation."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from asgi_client import ASGITestClient
from backend.api.main import create_app
from backend.core.config import Settings
from backend.integrations.ths_search_catalog import parse_etf_pool
from backend.storage.database import MIGRATIONS_DIR, connection_scope, migrate
from backend.storage.sector_heat import add_custom_boards, list_custom_boards, read_sector_heat
from scripts.import_sector_heat import _parse_ths_standard_sector_links, refresh_selected_ths_boards, search_ths_boards
from test_sector_heat import NOW, directory, prices, constituents


ETF_ROOT = {"data": {"total": 1, "indexes": [{"type": value} for value in
    ("etfName", "subMarket", "tradeCode", "simpleName", "code")],
    "itemList": [["沪深300ETF华泰柏瑞", "20", "510300", "华泰柏瑞沪深300ETF", "20:510300"]]}}


class ThsSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name) / "search.sqlite3")
        self.etf = parse_etf_pool(ETF_ROOT)[0]

    def test_concept_page_and_native_quote_code_return_same_identity(self):
        quote = {"1": {"cid": "309025", "platecode": "886019", "platename": "AIGC概念"}}
        html = '<a href="https://q.10jqka.com.cn/gn/detail/code/309025/">AIGC概念</a>' + \
               "<input id='gnSection' value='" + json.dumps(quote) + "'>"
        rows = _parse_ths_standard_sector_links(html)
        with patch("scripts.import_sector_heat.fetch_ths_standard_sector_directory", return_value={"rows": rows}), \
             patch("scripts.import_sector_heat.fetch_ths_etf_directory", return_value=[]), \
             patch("scripts.import_sector_heat.fetch_ths_board_identity_by_code") as probe:
            for query in ("309025", "886019", "bk_886019", "AIGC概念", "ＡＩＧＣ概念"):
                result = search_ths_boards(query)["items"][0]
                self.assertEqual(result["code"], "886019")
                self.assertEqual(result["ths_page_code"], "309025")
                self.assertEqual(result["history_source_code"], "bk_886019")
            probe.assert_not_called()

    def test_etf_short_full_name_and_market_qualified_code(self):
        with patch("scripts.import_sector_heat.fetch_ths_standard_sector_directory", return_value={"rows": directory()}), \
             patch("scripts.import_sector_heat.fetch_ths_etf_directory", return_value=[self.etf]), \
             patch("scripts.import_related_market.search_csi_indexes") as csi, \
             ASGITestClient(create_app(self.settings)) as client:
            for query in ("510300", "SH510300", "17_510300", "20:510300", "沪深300ETF华泰柏瑞", "华泰柏瑞沪深300ETF"):
                response = client.get("/api/sector-board-search", params={"q": query})
                self.assertEqual(response.status_code, 200)
                result = response.json()["items"][0]
                self.assertEqual((result["code"], result["kind"], result["history_source_code"]),
                                 ("510300", "ETF", "17_510300"))
            csi.assert_not_called()

    def test_truncated_etf_pool_is_rejected(self):
        root = {"data": {**ETF_ROOT["data"], "total": 2}}
        with self.assertRaisesRegex(ValueError, "不完整"):
            parse_etf_pool(root)

    def test_populated_database_upgrade_preserves_boards_and_accepts_etf(self):
        older = Path(self.temp.name) / "old-migrations"
        older.mkdir()
        for path in MIGRATIONS_DIR.glob("*.sql"):
            if int(path.name[:3]) <= 24:
                shutil.copyfile(path, older / path.name)
        with patch("backend.storage.database.MIGRATIONS_DIR", older):
            migrate(self.settings)
            add_custom_boards(self.settings, directory())
            with patch("scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=prices), \
                 patch("scripts.import_sector_heat.fetch_ths_visible_constituents", side_effect=constituents):
                refresh_selected_ths_boards(self.settings, directory(), now=NOW, throttle_seconds=0)
        migrate(self.settings)
        self.assertEqual({row["code"] for row in read_sector_heat(self.settings)["items"]}, {"881101", "881102"})
        add_custom_boards(self.settings, [self.etf])
        self.assertEqual({row["code"] for row in list_custom_boards(self.settings)}, {"881101", "881102", "510300"})
        with connection_scope(self.settings) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        with patch("scripts.import_sector_heat.fetch_ths_sector_daily", side_effect=prices), \
             patch("scripts.import_sector_heat.fetch_ths_visible_constituents") as stock_members:
            refresh_selected_ths_boards(self.settings, [self.etf], now=NOW, throttle_seconds=0)
        stock_members.assert_not_called()
        stored = next(row for row in read_sector_heat(self.settings)["items"] if row["code"] == "510300")
        self.assertEqual((stored["kind"], stored["history_source_code"], stored["collection_error"]),
                         ("ETF", "17_510300", None))
        self.assertEqual(stored["membership"]["status"], "missing")


if __name__ == "__main__":
    unittest.main()
