import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from backend.analysis.sector_status import analyze_index, sector_opportunities
from backend.api.main import create_app
from backend.core.config import Settings
from backend.storage.catalog import import_catalog
from backend.storage.database import migrate
from backend.storage.related_market import import_related_index


TODAY = date(2026, 9, 14)


def history(prices, end=TODAY):
    days = []
    day = end
    while len(days) < len(prices):
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    return [{"date": day, "close": str(price)} for day, price in zip(reversed(days), prices)]


class SectorStatusTests(unittest.TestCase):
    def analyze(self, prices):
        return analyze_index(history(prices), as_of=TODAY)["periods"]

    def test_rising_falling_and_flat_paths_in_all_periods(self):
        for prices, state in [(range(100, 221), "strong"), (range(221, 100, -1), "weak"), ([100] * 121, "neutral")]:
            with self.subTest(state=state):
                self.assertEqual([p["status"] for p in self.analyze(prices)], [state] * 3)

    def test_independent_metrics_and_risk_do_not_overwrite_direction(self):
        # Last 20 sessions recover after a 25% fall; final price +10%, mean 92.5.
        short = self.analyze([100, 120, 90] + [90] * 17 + [110])[0]
        self.assertEqual(short["status"], "strong")
        self.assertEqual(short["return_pct"], 10)
        self.assertAlmostEqual(short["ma_bias_pct"], 18.92, places=2)  # mean = 92.5
        self.assertEqual(short["max_drawdown_pct"], -25)
        self.assertEqual(short["risk"], "elevated")

    def test_mixed_trend_is_neutral_and_periods_can_disagree(self):
        prices = [200] * 100 + list(range(100, 121))
        self.assertEqual([p["status"] for p in self.analyze(prices)], ["strong", "weak", "weak"])
        short = self.analyze([100] + [120] * 19 + [110])[0]
        self.assertEqual(short["status"], "neutral")
        self.assertEqual(short["label"], "上涨后回落")
        rebound = self.analyze([120] + [90] * 19 + [110])[0]
        self.assertEqual(rebound["label"], "下跌后修复")

    def test_exact_threshold_does_not_claim_strength(self):
        self.assertEqual(self.analyze([100] * 20 + [101])[0]["status"], "neutral")
        self.assertEqual(self.analyze([100] * 20 + [99])[0]["status"], "neutral")

    def test_sample_requirements_are_per_period(self):
        self.assertEqual([p["status"] for p in self.analyze(range(100, 121))], ["strong", "insufficient", "insufficient"])
        self.assertTrue(all(p["return_pct"] is None for p in self.analyze([])))
        self.assertEqual(self.analyze([100] * 20)[0]["status"], "insufficient")

    def test_stale_rows_never_produce_current_direction(self):
        result = analyze_index(history(range(100, 221), end=TODAY - timedelta(days=20)), as_of=TODAY)
        self.assertTrue(all(p["status"] == "stale" and p["return_pct"] is None for p in result["periods"]))

    def test_future_rows_are_excluded_and_order_is_irrelevant(self):
        rows = history(range(100, 221))
        expected = analyze_index(rows, as_of=TODAY)
        rows.append({"date": "2026-09-15", "close": "1"})
        self.assertEqual(analyze_index(list(reversed(rows)), as_of=TODAY), expected)

    def test_invalid_values_dates_and_gaps_remain_unknown(self):
        for bad in [None, "NaN", "Infinity", "0", "-1", "not-a-price"]:
            with self.subTest(bad=bad):
                self.assertTrue(all(p["status"] == "insufficient" for p in self.analyze([100] * 120 + [bad])))
        for extra in [{"date": "invalid", "close": "100"}, {"date": TODAY.isoformat(), "close": "100"}]:
            result = analyze_index(history([100] * 121) + [extra], as_of=TODAY)
            self.assertTrue(all(p["status"] == "insufficient" for p in result["periods"]))
        rows = history([100] * 121)
        for row in rows[:-1]:
            row["date"] = (date.fromisoformat(row["date"]) - timedelta(days=20)).isoformat()
        self.assertTrue(all(p["status"] == "insufficient" for p in analyze_index(rows, as_of=TODAY)["periods"]))

    def test_service_reads_real_indexes_and_route_without_external_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(database_path=Path(directory) / "app.sqlite3")
            migrate(settings)
            self.assertEqual(sector_opportunities(settings)["items"], [])
            import_catalog(settings, [{"code": "012970", "name": "芯片ETF联接C", "fund_type": "指数型-股票"}], policy_version="v1")
            rows = [{**r, "open": r["close"], "high": r["close"], "low": r["close"]} for r in history(range(100, 221))]
            import_related_index(settings, fund_code="012970", index_code="980017", index_name="国证半导体芯片指数",
                                 rows=rows, source_id="index_daily.sina", relation_source_id="official",
                                 evidence_url="https://example.test/evidence")
            endpoint = next(r.endpoint for r in create_app(settings).routes if r.path == "/api/sectors/opportunities")
            response = endpoint()
            self.assertEqual(response["method_version"], "sector-horizons-v1")
            self.assertEqual(len(response["items"]), 1)
            item = response["items"][0]
            self.assertEqual((item["code"], item["source_id"], item["observation_count"]), ("980017", "index_daily.sina", 121))
            self.assertEqual(item["funds"][0]["code"], "012970")
            self.assertEqual(item["funds"][0]["name"], "芯片ETF联接C")
            self.assertEqual(item["funds"][0]["relation_type"], "tracked_index")
            self.assertEqual(item["funds"][0]["relation_source_id"], "official")
            self.assertEqual(item["funds"][0]["evidence_url"], "https://example.test/evidence")
            self.assertEqual(len(item["periods"]), 3)


if __name__ == "__main__":
    unittest.main()
