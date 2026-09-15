import tempfile
import unittest
from pathlib import Path

from backend.core.config import Settings
from backend.storage.catalog import import_catalog, list_catalog
from backend.storage.database import connection_scope


class FundCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(database_path=Path(self.tmp.name) / "app.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_search_pagination_and_leading_zero(self):
        import_catalog(self.settings, [
            {"基金代码": "110022", "基金简称": "精选成长", "基金类型": "混合型"},
            {"code": "001", "name": "短债", "fund_type": "债券型"},
        ], policy_version="test")
        result = list_catalog(self.settings, "001", page_size=1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["catalog_total"], 2)
        self.assertEqual(result["items"][0]["code"], "000001")
        self.assertEqual(result["items"][0]["share_id"], "000001")

    def test_invalid_batch_keeps_previous_projection(self):
        import_catalog(self.settings, [{"code": "510050", "name": "上证50", "fund_type": "ETF"}], policy_version="test")
        with self.assertRaises(Exception):
            import_catalog(self.settings, [{"code": "bad", "name": "坏数据", "fund_type": "ETF"}], policy_version="test")
        with connection_scope(self.settings) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM fund_catalog_projection").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM fund_catalog_import_batch").fetchone()[0], 1)

    def test_repeated_import_is_idempotent_and_empty_source_keeps_old_rows(self):
        rows = [{"code": "005911", "name": "科技精选", "fund_type": "股票型"}]
        import_catalog(self.settings, rows, policy_version="test")
        import_catalog(self.settings, rows, policy_version="test")
        with self.assertRaises(Exception):
            import_catalog(self.settings, [], policy_version="test")
        with connection_scope(self.settings) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM fund_catalog_projection").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM fund_catalog_import_batch").fetchone()[0], 2)

    def test_pagination_boundary(self):
        with self.assertRaises(Exception):
            list_catalog(self.settings, page=0)
        with self.assertRaises(Exception):
            list_catalog(self.settings, page_size=101)


if __name__ == "__main__":
    unittest.main()
