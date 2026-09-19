"""Verifier acceptance mechanics against local fixtures; no external source claim."""
from __future__ import annotations

from datetime import date
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from backend.core.trading_calendar import get_calendar
from scripts.import_sector_heat import COLLECTOR_VERSION
from scripts.verify_sector_data import (clean, export_assets, inspect_evaluation, local_base,
                                       main, verify_price_series, worker_result)


def fixtures():
    calendar = get_calendar()
    days = [day.isoformat() for day in calendar.sessions(date(2026, 1, 5), date(2026, 9, 18))][-130:]
    boards, series = [], {}
    for code in ("BK0422", "BK0428"):
        periods = [{"id": key, "status": "neutral", "return_pct": 0, "ma_bias_pct": 0,
                    "max_drawdown_pct": 0, "observation_start": days[-window-1],
                    "observation_grid": sha256(",".join(days[-window-1:]).encode()).hexdigest(),
                    "strength": {"eligible": True, "rank": 1, "sample_count": 2, "as_of": days[-1]}}
                   for key, window in (("short", 20), ("medium", 60), ("long", 120))]
        metric = {"complete": True, "current_sum": "0", "base_sum": "0", "value": None}
        context = {"usable": True, "operating": {"status": "available", "periods": [
                       {"report_date": report, "metrics": {name: dict(metric) for name in
                        ("revenue_yoy", "profit_yoy", "operating_cashflow_yoy")}}
                       for report in ("2026-06-30", "2026-03-31")]},
                   "valuation": {"status": "available", "as_of": days[-1], "covered": 1, "total": 1,
                                 "history_count": 1, "history_span_days": 0, "percentile": None},
                   "catalysts": {"status": "not_collected"}}
        item = {"code": code, "name": "合成行业", "universe_type": "hot_board", "source_id": "sector_daily.ths",
                "history_source_id": "sector_daily.ths", "history_source_code": "bk_"+code,
                "as_of": days[-1], "periods": periods, "fundamentals": context,
                "membership": {"status": "ready", "as_of": days[-1], "member_count": 1,
                               "members": [{"stock_code": "600001", "market": 1}]}}
        boards.append(item)
        series[code] = {"http_status": 200, "payload": {"code": code, "source_id": item["source_id"],
                        "history_source_id": item["history_source_id"], "history_source_code": item["history_source_code"],
                        "rows": [{"date": day, "close": 100} for day in days]}}
    evaluation = {"items": boards, "price_cutoff": days[-1], "calendar_status": "ready", "calendar_hash": calendar.hash,
                  "universe": {"catalog_count": 2, "scope": "verified_industry_all"}}
    refresh = {"target": "sectors", "status": "success", "collector_version": COLLECTOR_VERSION, "catalog_count": 2}
    return evaluation, refresh, series


class VerificationLogicTests(unittest.TestCase):
    def test_equal_orders_are_valid_but_missing_research_is_not_complete(self):
        evaluation, refresh, _ = fixtures()
        report = inspect_evaluation(evaluation, refresh)
        self.assertTrue(report["core_data_complete"])
        self.assertFalse(report["all_missing_data_resolved"])
        self.assertEqual([row["code"] for row in report["rank_orders"]["short"]],
                         [row["code"] for row in report["rank_orders"]["long"]])

    def test_http_success_payload_does_not_override_stale_prices(self):
        evaluation, refresh, _ = fixtures()
        for item in evaluation["items"]:
            for period in item["periods"]:
                period.update(status="insufficient", historical={"available": True, "return_pct": 5})
                period["strength"]["eligible"] = False
        report = inspect_evaluation(evaluation, refresh)
        self.assertFalse(report["core_data_complete"])
        self.assertEqual(report["eligible_counts"], {"short": 0, "medium": 0, "long": 0})

    def test_real_series_metrics_are_recomputed_not_trusted(self):
        evaluation, _, series = fixtures()
        self.assertTrue(verify_price_series(evaluation, series)["passed"])
        evaluation["items"][0]["periods"][0]["return_pct"] = 20
        self.assertFalse(verify_price_series(evaluation, series)["passed"])

    def test_real_series_gap_and_wrong_calendar_are_rejected(self):
        evaluation, _, series = fixtures()
        series["BK0422"]["payload"]["rows"].pop(-10)
        self.assertFalse(verify_price_series(evaluation, series)["passed"])
        evaluation["calendar_hash"] = "wrong"
        self.assertFalse(verify_price_series(evaluation, series)["passed"])

    def test_reversed_rank_and_wrong_sample_count_are_rejected(self):
        evaluation, refresh, _ = fixtures()
        a, b = evaluation["items"]
        a["periods"][0]["return_pct"] = 5
        a["periods"][0]["strength"]["rank"] = 2
        b["periods"][0]["strength"]["rank"] = 1
        b["periods"][0]["strength"]["sample_count"] = 1
        result = inspect_evaluation(evaluation, refresh)
        self.assertFalse(result["core_checks"]["ranking_integrity"])
        self.assertTrue(result["ranking_errors"])

    def test_market_scope_and_old_collector_do_not_pass(self):
        evaluation, refresh, _ = fixtures()
        refresh["collector_version"] = "old"
        evaluation["universe"]["catalog_count"] = 3
        refresh["catalog_count"] = 3
        report = inspect_evaluation(evaluation, refresh)
        self.assertFalse(report["core_checks"]["nonempty_complete_industry_universe"])
        self.assertFalse(report["core_checks"]["patched_collector_reported"])

    def test_local_url_restriction_and_redaction(self):
        self.assertEqual(local_base("http://127.0.0.1:8000/"), "http://127.0.0.1:8000")
        for value in ("https://example.com", "http://user:pw@localhost:8000", "http://localhost?token=secret"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                local_base(value)
        cleaned = clean({"password": "abc", "url": "http://localhost?token=abc", "path": str(Path.home())+"/test"})
        self.assertNotIn("abc", json.dumps(cleaned))
        self.assertEqual(cleaned["path"], "<HOME>/test")

    def test_worker_result_handles_structured_error_and_malformed_values(self):
        self.assertEqual(worker_result({"payload": {"error": {"details": {"refresh": {"status": "failed"}}}}}), {"status": "failed"})
        self.assertEqual(worker_result({"payload": {"error": {"details": {"refresh": None}}}}), {})


class LocalHTTPVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.evaluation, self.refresh, self.series = fixtures()
        self.posts = []
        self.run_id = "old"
        self.bad_hash = False
        self.refresh_error = False
        self.body = b'{"fixture":true}'
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.posts.append(self.path)
                owner.run_id = "new"
                if owner.refresh_error:
                    self.respond({"error": {"details": {"refresh": owner.refresh}}}, 502)
                else:
                    self.respond({"refresh": owner.refresh})
            def respond(self, body, status=200):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_GET(self):
                if self.path == "/health":
                    self.respond({"status": "ok"})
                elif self.path == "/api/sectors/opportunities":
                    self.respond(owner.evaluation)
                elif self.path == "/api/sectors/collection-status":
                    self.respond({"run_id": owner.run_id, "state": "partial"})
                elif self.path.startswith("/api/research/collections"):
                    self.respond({"items": [{"run_id": owner.run_id, "target": "sectors", "result": owner.refresh,
                                  "attempts": [{"url": "https://q.10jqka.com.cn/fixture", "raw_asset_id": "a" * 64}]}]})
                elif "/series?" in self.path:
                    self.respond(owner.series[self.path.split("/")[3]]["payload"])
                elif self.path.startswith("/api/research/assets/"):
                    self.send_response(200)
                    self.send_header("X-Content-SHA256", "wrong" if owner.bad_hash else sha256(owner.body).hexdigest())
                    self.send_header("Content-Length", str(len(owner.body)))
                    self.end_headers()
                    self.wfile.write(owner.body)
                else:
                    self.respond({}, 404)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_one_command_exports_http_results_rows_and_raw_without_database(self):
        output = Path(self.temp.name) / "verification.zip"
        self.assertEqual(main(["--base-url", self.base, "--output", str(output)]), 1)
        self.assertEqual(self.posts, ["/api/sectors/refresh"])
        with ZipFile(output) as archive:
            for name in ("analysis.json", "series.json", "series-checks.json", "collection.json", "raw-manifest.json", "metadata.json"):
                self.assertIn(name, archive.namelist())
            analysis = json.loads(archive.read("analysis.json"))
            self.assertTrue(analysis["core_data_complete"])
            self.assertTrue(analysis["live_refresh_verified"])
            self.assertFalse(analysis["all_missing_data_resolved"])
            self.assertTrue(any(name.startswith("raw/") for name in archive.namelist()))
            self.assertFalse(any(name.endswith((".sqlite3", ".env")) for name in archive.namelist()))

    def test_read_only_never_posts_and_cannot_claim_live_refresh(self):
        output = Path(self.temp.name) / "read.zip"
        self.assertEqual(main(["--base-url", self.base, "--read-only", "--output", str(output)]), 1)
        self.assertEqual(self.posts, [])
        with ZipFile(output) as archive:
            self.assertFalse(json.loads(archive.read("analysis.json"))["live_refresh_verified"])

    def test_failed_refresh_still_exports_error_report(self):
        self.refresh_error = True
        output = Path(self.temp.name) / "failed.zip"
        self.assertEqual(main(["--base-url", self.base, "--output", str(output)]), 1)
        with ZipFile(output) as archive:
            self.assertEqual(json.loads(archive.read("refresh.json"))["http_status"], 502)
            self.assertFalse(json.loads(archive.read("analysis.json"))["live_refresh_verified"])

    def test_raw_budget_and_hash_failure_are_visible(self):
        run = {"attempts": [{"url": "https://q.10jqka.com.cn/fixture", "raw_asset_id": "a" * 64}]}
        manifest, files = export_assets(self.base, run, {}, max_assets=0, max_bytes=100)
        self.assertEqual(files, {})
        self.assertEqual(manifest[0]["reason"], "raw_export_budget_exhausted")
        self.bad_hash = True
        manifest, files = export_assets(self.base, run, {}, max_assets=1, max_bytes=100)
        self.assertEqual(files, {})
        self.assertFalse(manifest[0]["exported"])
        self.assertIn("SHA256", manifest[0]["reason"])

    def test_bootstrap_failure_still_creates_zip(self):
        output = Path(self.temp.name) / "bootstrap.zip"
        with patch("scripts.verify_sector_data.source_metadata", return_value={}), patch(
                "scripts.verify_sector_data.subprocess.Popen", side_effect=OSError("fixture missing executable")):
            self.assertEqual(main(["--output", str(output)]), 2)
        with ZipFile(output) as archive:
            self.assertIn("fixture missing executable", json.loads(archive.read("metadata.json"))["error"])

    def test_existing_output_is_not_overwritten(self):
        output = Path(self.temp.name) / "existing.zip"
        output.write_bytes(b"previous diagnostic")
        with self.assertRaises(SystemExit):
            main(["--base-url", self.base, "--output", str(output)])
        self.assertEqual(output.read_bytes(), b"previous diagnostic")


if __name__ == "__main__":
    unittest.main()
