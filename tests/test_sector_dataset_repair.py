"""Deterministic pagination, budget and storage tests; fixture payloads are not live evidence."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import tempfile
from threading import Lock
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from backend.core.config import Settings
from backend.integrations.sector_dataset_runner import run_datasets
from backend.integrations.sector_financials import collect_sector_fundamentals, fetch_table
from backend.storage.database import connection_scope, migrate

NOW = datetime.fromisoformat("2026-09-18T18:00:00+08:00")


def dataset():
    return {"status": "available", "rows": {"600001": {}}, "excluded": {}, "sources": [], "source_count": 1, "error": None}


def board():
    return {"source_id": "sector_daily.eastmoney", "code": "BK0422", "kind": "行业", "name": "合成行业",
            "membership": {"status": "ready", "as_of": "2026-09-18", "member_count": 1,
                           "members": [{"stock_code": "600001", "stock_name": "合成股票", "market": 1}]}}


class FiscalTransport:
    def __init__(self, failed=None):
        self.failed, self.calls = failed, []

    def __call__(self, url, timeout):
        q = parse_qs(urlsplit(url).query)
        name = q["reportName"][0]
        business = re.search(r"(?:REPORT_DATE|TRADE_DATE)='(.*?)'", q["filter"][0]).group(1)
        self.calls.append((name, business))
        if self.failed and name == self.failed:
            return b'{"success":false,"code":500,"message":"fixture error"}'
        row = {"SECURITY_CODE": "600001"}
        if name == "RPT_VALUEANALYSIS_DET":
            row.update(TRADE_DATE=business, PE_TTM="20")
        else:
            row.update(REPORT_DATE=business, NOTICE_DATE=business[:8] + "30 00:00:00")
            # Use actual later publication days within the fixed test clock.
            row["NOTICE_DATE"] = business[:4] + ("-08-01 00:00:00" if "06-30" in business else "-04-20 00:00:00")
            if name == "RPT_DMSK_FN_INCOME":
                row.update(TOTAL_OPERATE_INCOME="100", PARENT_NETPROFIT="10")
            else:
                row["NETCASH_OPERATE"] = "5"
        return json.dumps({"success": True, "result": {"count": 1, "pages": 1, "data": [row]}}).encode()


class DatasetRunnerTests(unittest.TestCase):
    def test_each_queued_job_gets_its_own_budget(self):
        instant = [0.0]
        available = []
        def load(kind, day, deadline):
            available.append(deadline - instant[0])
            instant[0] += 10
            return dataset()
        keys = [("income", str(i)) for i in range(9)]
        result, diagnostics = run_datasets(keys, load, workers=1, budget_seconds=180, clock=lambda: instant[0])
        self.assertEqual(available, [20] * 9)
        self.assertEqual(list(result), keys)
        self.assertEqual(len(diagnostics), 9)

    def test_one_exception_does_not_prevent_other_jobs(self):
        def load(kind, day, deadline):
            if kind == "bad":
                error = TimeoutError("fixture stalled")
                error.sources = [{"url": "https://data.eastmoney.com/fixture", "asset_id": "a" * 64}]
                error.received_row_count = 10
                raise error
            return dataset()
        result, diagnostics = run_datasets([("bad", "x"), ("good", "x"), ("good", "y")], load)
        self.assertEqual(result[("bad", "x")]["status"], "failed")
        self.assertEqual(result[("good", "y")]["status"], "available")
        self.assertEqual(diagnostics[0]["source_count"], 10)
        self.assertEqual(len(diagnostics[0]["sources"]), 1)

    def test_concurrency_is_bounded_and_result_order_is_stable(self):
        active, peak, lock = [0], [0], Lock()
        def load(kind, day, deadline):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.005)
            with lock:
                active[0] -= 1
            return dataset()
        keys = [("income", str(i)) for i in range(9)]
        _, events = run_datasets(keys, load)
        self.assertLessEqual(peak[0], 2)
        self.assertEqual([event["business_date"] for event in events], [str(i) for i in range(9)])


class FinancialRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name) / "app.sqlite3")
        migrate(self.settings)

    def test_all_nine_datasets_are_collected_once_and_stored(self):
        source = FiscalTransport()
        result = collect_sector_fundamentals(self.settings, [board()], now=NOW, fetcher=source)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(source.calls), 9)
        self.assertEqual(len(set(source.calls)), 9)
        self.assertEqual(result["datasets"][0]["dataset"], "valuation")
        with connection_scope(self.settings) as conn:
            saved = json.loads(conn.execute("SELECT payload_json FROM sector_fundamental_observation ORDER BY id DESC LIMIT 1").fetchone()[0])
            self.assertEqual(saved["operating"]["status"], "available")
            self.assertEqual(saved["valuation"]["as_of"], "2026-09-18")
            self.assertEqual(len(saved["datasets"]), 9)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM raw_asset").fetchone()[0], 9)

    def test_bad_table_keeps_raw_evidence_and_other_tables(self):
        source = FiscalTransport(failed="RPT_DMSK_FN_CASHFLOW")
        result = collect_sector_fundamentals(self.settings, [board()], now=NOW, fetcher=source)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(source.calls), 9)
        failures = [event for event in result["datasets"] if event["state"] == "failed"]
        self.assertEqual(len(failures), 4)
        self.assertTrue(all(event["sources"] for event in failures))
        self.assertEqual(result["datasets"][0]["state"], "available")

    def test_no_current_membership_reports_not_requested_instead_of_success(self):
        item = board()
        item["membership"]["status"] = "failed"
        source = FiscalTransport()
        result = collect_sector_fundamentals(self.settings, [item], now=NOW, fetcher=source)
        self.assertEqual(source.calls, [])
        self.assertEqual(len(result["datasets"]), 9)
        self.assertTrue(all(event["state"] == "not_requested" for event in result["datasets"]))

    def test_empty_success_response_is_missing_not_available(self):
        empty = lambda *args: b'{"success":true,"result":{"count":0,"pages":0,"data":[]}}'
        result = fetch_table(self.settings, "valuation", "2026-09-18", NOW, fetcher=empty, deadline=10, clock=lambda: 0)
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["rows"], {})
        self.assertEqual(len(result["sources"]), 1)

    def test_parser_failure_retains_asset_and_original_exception_type(self):
        def source(*args):
            return b'{"success":true,"result":{"count":2,"pages":1,"data":[]}}'
        with self.assertRaises(ValueError) as caught:
            fetch_table(self.settings, "valuation", "2026-09-18", NOW, fetcher=source, deadline=10, clock=lambda: 0)
        self.assertEqual(len(caught.exception.sources), 1)
        with connection_scope(self.settings) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM raw_asset").fetchone()[0], 1)

    def test_expired_budget_preserves_timeout_and_makes_no_request(self):
        calls = []
        with self.assertRaises(TimeoutError):
            fetch_table(self.settings, "valuation", "2026-09-18", NOW,
                        fetcher=lambda *args: calls.append(args), deadline=1, clock=lambda: 2)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
