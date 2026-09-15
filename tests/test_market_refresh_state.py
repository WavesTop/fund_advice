import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.api.main import create_app
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.catalog import import_catalog
from backend.storage.related_market import import_related_index, record_relation_check
from backend.storage.timeseries import import_timeseries


class FailedRefreshStateTests(unittest.TestCase):
    def test_error_payload_includes_latest_relation_state_and_preserved_prices(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(database_path=Path(directory) / "app.sqlite3")
            import_catalog(settings, [{"code": "510050", "name": "测试ETF", "fund_type": "ETF"}], policy_version="fixture")
            row = {"date": "2026-09-11", "open": "10", "high": "12", "low": "9", "close": "11", "volume": None, "amount": None}
            import_timeseries(settings, "510050", "price", [row], source_id="fixture", policy_version="v1")
            import_related_index(settings, fund_code="510050", index_code="000016", index_name="测试指数", rows=[row], source_id="fixture.index", relation_source_id="fixture.prospectus", evidence_url="https://example.test/evidence")
            app = create_app(settings)
            refresh = next(route.endpoint for route in app.routes if route.path == "/api/funds/{code}/refresh")
            def fail_relation(resolved, code):
                record_relation_check(resolved, code, outcome="failed", error="fixture unavailable")
                raise RuntimeError("fixture unavailable")
            with patch("backend.api.main.refresh_fund_timeseries", side_effect=RuntimeError("NAV unavailable")), patch("backend.api.main.refresh_related_market", side_effect=fail_relation):
                with self.assertRaises(AppError) as caught:
                    refresh("510050")
            error = caught.exception
            self.assertEqual(error.status_code, 502)
            self.assertEqual(error.body.details["refresh"]["status"], "failed")
            current = error.body.details["current"]
            self.assertEqual(current["fund"]["code"], "510050")
            self.assertEqual(current["series"]["rows"], [row])
            self.assertIsNone(current["related_market"])
            self.assertEqual(current["related_markets"][0]["relation_status"], "withheld")


if __name__ == "__main__":
    unittest.main()
