import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import probe_fund_timeseries as probe


def frame(columns, rows, provider=None):
    result = {
        "columns": columns,
        "dtypes": {column: "object" for column in columns},
        "rows": rows,
        "akshare_version": "1.18.94",
    }
    if provider is not None:
        result["provider"] = provider
    return result


SINA_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]
SINA_ROWS = [["2026-01-02", "2.10", "2.20", "2.00", "2.15", "667541903", "1984173121"]]
TENCENT_COLUMNS = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]
TENCENT_ROWS = [["2026-01-02", "2.10", "2.15", "2.20", "2.00", "6675419.00", "1984173100.00"]]


class ProviderSelectionTests(unittest.TestCase):
    def test_calendar_keeps_its_sina_calendar_interface(self):
        payload = frame(["trade_date"], [["2026-01-02"]], "sina")
        with tempfile.TemporaryDirectory() as output_dir, contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(probe, "run_adapter", return_value=(payload, 0.01)):
            status = probe.main(["--dataset", "calendar", "--start-date", "20260101",
                                 "--end-date", "20260103", "--output-dir", output_dir])
            report = json.loads(next(Path(output_dir).glob("*/report.json")).read_text())
        self.assertEqual(status, 0)
        self.assertEqual(report["provider"], "sina")
        self.assertEqual(report["interface"], "akshare.tool_trade_date_hist_sina")
        self.assertEqual(report["params"], {})

    def run_main(self, adapter, *extra):
        with tempfile.TemporaryDirectory() as output_dir:
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(probe, "run_adapter", side_effect=adapter) as run, \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = probe.main([
                    "--dataset", "etf-daily", "--code", "510050",
                    "--start-date", "20260101", "--end-date", "20260131",
                    "--output-dir", output_dir, *extra,
                ])
            reports = list(Path(output_dir).glob("*/report.json"))
            self.assertEqual(len(reports), 1)
            return exit_code, json.loads(reports[0].read_text()), run, stdout.getvalue(), stderr.getvalue()

    def test_auto_uses_sina_then_retries_tencent_for_retryable_failures(self):
        for error_code in ("network_error", "network_timeout", "upstream_failure"):
            with self.subTest(error_code=error_code):
                providers = []
                def adapter(request, _timeout, code=error_code):
                    providers.append(request["provider"])
                    if request["provider"] == "sina":
                        raise probe.ProbeError(code, probe.SOURCE_ERRORS[code])
                    return frame(TENCENT_COLUMNS, TENCENT_ROWS, "tencent"), 0.01

                exit_code, report, run, _, _ = self.run_main(adapter)
                self.assertEqual(exit_code, 0)
                self.assertEqual(providers, ["sina", "tencent"])
                self.assertEqual((report["requested_provider"], report["provider"]), ("auto", "tencent"))
                self.assertEqual([attempt["provider"] for attempt in report["attempts"]], ["sina", "tencent"])
                self.assertEqual(report["attempts"][0]["error"]["code"], error_code)
                self.assertEqual(report["attempts"][1]["status"], "success")

    def test_auto_can_reach_baostock_after_two_retryable_failures(self):
        providers = []

        def adapter(request, _timeout):
            providers.append(request["provider"])
            if request["provider"] != "baostock":
                raise probe.ProbeError("network_error", probe.SOURCE_ERRORS["network_error"])
            payload = frame(TENCENT_COLUMNS, TENCENT_ROWS, "baostock")
            payload["quote_units"] = {"volume": "shares", "amount": "CNY"}
            return payload, 0.01

        exit_code, report, _, _, _ = self.run_main(adapter)
        self.assertEqual(exit_code, 0)
        self.assertEqual(providers, ["sina", "tencent", "baostock"])
        self.assertEqual(report["provider"], "baostock")
        self.assertEqual(report["normalized_rows"][0]["volume_shares"], "6675419.00")

    def test_auto_does_not_fallback_after_invalid_ohlc_or_schema(self):
        bad_frames = [
            frame(SINA_COLUMNS, [["2026-01-02", 2.1, 2.0, 2.2, 2.15, 1, 2]], "sina"),
            frame(["date", "open", "high", "close", "volume", "amount"], [["2026-01-02", 2.1, 2.2, 2.15, 1, 2]], "sina"),
        ]
        for bad in bad_frames:
            with self.subTest(columns=bad["columns"]):
                exit_code, report, run, _, _ = self.run_main(lambda _request, _timeout, bad=bad: (bad, 0.01))
                self.assertEqual(exit_code, 2)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(report["provider"], "sina")
                self.assertEqual(report["requested_provider"], "auto")
                self.assertIn(report["error"]["code"], {"invalid_response", "missing_columns"})

    def test_auto_falls_through_empty_results_to_find_complete_data(self):
        empty = frame(SINA_COLUMNS, [], "sina")
        def adapter(request, _timeout):
            if request["provider"] == "sina":
                return empty, 0.01
            return frame(TENCENT_COLUMNS, TENCENT_ROWS, "tencent"), 0.01
        exit_code, report, run, _, _ = self.run_main(adapter)
        self.assertEqual(exit_code, 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(report["status"], "success")
        self.assertEqual(report["attempts"], [
            {"provider": "sina", "status": "empty", "error": None},
            {"provider": "tencent", "status": "success", "error": None},
        ])

    def test_auto_retries_truncated_source(self):
        providers = []
        def adapter(request, _timeout):
            providers.append(request["provider"])
            if request["provider"] != "eastmoney":
                raise probe.ProbeError("truncated_response", probe.SOURCE_ERRORS["truncated_response"])
            return frame(TENCENT_COLUMNS, TENCENT_ROWS, "eastmoney"), 0.01
        exit_code, report, _, _, _ = self.run_main(adapter)
        self.assertEqual(exit_code, 0)
        self.assertEqual(providers, ["sina", "tencent", "baostock", "eastmoney"])
        self.assertEqual(report["provider"], "eastmoney")

    def test_explicit_provider_makes_one_attempt(self):
        for provider, columns, rows in (("sina", SINA_COLUMNS, SINA_ROWS), ("tencent", TENCENT_COLUMNS, TENCENT_ROWS)):
            with self.subTest(provider=provider):
                exit_code, report, run, _, _ = self.run_main(
                    lambda _request, _timeout, columns=columns, rows=rows, provider=provider:
                    (frame(columns, rows, provider), 0.01), "--provider", provider)
                self.assertEqual(exit_code, 0)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(report["requested_provider"], provider)
                self.assertEqual(report["provider"], provider)

    def test_all_retryable_providers_fail_with_nonzero_exit_and_safe_output(self):
        def adapter(request, _timeout):
            raise probe.ProbeError("network_error", probe.SOURCE_ERRORS["network_error"])

        exit_code, report, run, stdout, stderr = self.run_main(adapter)
        self.assertEqual(exit_code, 2)
        self.assertEqual(run.call_count, 4)
        self.assertEqual([attempt["provider"] for attempt in report["attempts"]],
                         ["sina", "tencent", "baostock", "eastmoney"])
        self.assertEqual(report["error"]["code"], "network_error")
        self.assertNotIn("secret", stdout + stderr)


class QuoteNormalizationTests(unittest.TestCase):
    def test_sina_identity_requires_unique_matching_category_and_code(self):
        import pandas as pd
        etfs = pd.DataFrame([{"代码": "sh510050", "名称": "上证50ETF华夏"}])
        identity = probe.sina_identity(etfs, "sh510050", "etf-daily")
        self.assertEqual((identity["symbol"], identity["kind"]), ("sh510050", "ETF"))
        with self.assertRaises(probe.ProbeError):
            probe.sina_identity(etfs, "sz160706", "etf-daily")
        with self.assertRaises(probe.ProbeError):
            probe.sina_identity(pd.concat([etfs, etfs]), "sh510050", "etf-daily")
        with self.assertRaises(probe.ProbeError):
            probe.sina_identity(pd.DataFrame([{"code": "sh510050"}]), "sh510050", "etf-daily")

    def test_sina_english_ohlcv_keeps_source_shares_and_cny_amount(self):
        result = probe.normalize("etf-daily", frame(SINA_COLUMNS, SINA_ROWS, "sina"), "20260101", "20260131")
        row = result["normalized_rows"][0]
        self.assertEqual((row["volume_shares"], row["amount_cny"]), ("667541903", "1984173121"))
        self.assertEqual(result["normalization"]["mapping"]["date"], "date")
        self.assertEqual(result["normalization"]["units"]["volume_shares"], "shares (source shares)")

    def test_tencent_chinese_ohlcv_converts_lots_only_and_keeps_amount(self):
        result = probe.normalize("etf-daily", frame(TENCENT_COLUMNS, TENCENT_ROWS, "tencent"), "20260101", "20260131")
        row = result["normalized_rows"][0]
        self.assertEqual((row["volume_shares"], row["amount_cny"]), ("667541900.00", "1984173100.00"))
        self.assertEqual(result["normalization"]["mapping"]["volume_shares"], "成交量")
        self.assertEqual(result["normalization"]["units"]["volume_shares"], "shares (source hands x 100)")

    def test_baostock_declared_share_units_are_not_multiplied(self):
        payload = frame(TENCENT_COLUMNS, [[
            "2026-01-02", "2.10", "2.15", "2.20", "2.00", "667541903", "1984173121"
        ]], "baostock")
        payload["quote_units"] = {"volume": "shares", "amount": "CNY"}
        result = probe.normalize("etf-daily", payload, "20260101", "20260131")
        row = result["normalized_rows"][0]
        self.assertEqual((row["volume_shares"], row["amount_cny"]), ("667541903", "1984173121"))
        self.assertEqual(result["normalization"]["units"]["volume_shares"], "shares (source shares)")

    def test_nullable_quote_fields_remain_null_and_nav_does_not_create_bars(self):
        sina = frame(SINA_COLUMNS, [["2026-01-02", 2.1, 2.2, 2.0, 2.15, None, None]], "sina")
        result = probe.normalize("etf-daily", sina, "20260101", "20260131")
        self.assertEqual(result["normalized_rows"][0]["volume_shares"], None)
        self.assertIsNone(result["normalized_rows"][0]["amount_cny"])
        nav = probe.normalize("unit-nav", frame(["净值日期", "单位净值"], [["2026-01-02", 1.2]]), "20260101", "20260131")
        self.assertEqual(nav["normalized_rows"], [{"date": "2026-01-02", "unit_nav": "1.2", "published_at": None}])

    def test_source_and_selected_bounds_are_reported_for_full_and_selected_history(self):
        data = frame(SINA_COLUMNS, [
            ["2025-12-31", 2.1, 2.2, 2.0, 2.15, 1, 2],
            ["2026-01-02", 2.1, 2.2, 2.0, 2.15, 3, 4],
            ["2026-02-01", 2.1, 2.2, 2.0, 2.15, 5, 6],
        ], "sina")
        result = probe.normalize("etf-daily", data, "20260101", "20260131")
        self.assertEqual((result["source"]["min_date"], result["source"]["max_date"]), ("2025-12-31", "2026-02-01"))
        self.assertEqual((result["selected"]["min_date"], result["selected"]["max_date"]), ("2026-01-02", "2026-01-02"))
        request = {"dataset": "etf-daily", "code": "510050", "start_date": "20260101", "end_date": "20260131", "provider": "sina"}
        self.assertEqual(probe.source_interface(request), "akshare.fund_etf_hist_sina")
        self.assertEqual(probe.adapter_params(request), {"symbol": "sh510050"})
        self.assertEqual(probe.adapter_params({**request, "provider": "tencent"}), {
            "code": "510050", "start_date": "20260101", "end_date": "20260131", "adjust": "", "expected_kind": "ETF"
        })

    def test_sina_provider_rejects_nav_dataset_and_unsupported_market_code(self):
        for argv in (
            ["--dataset", "unit-nav", "--code", "510050", "--provider", "sina", "--start-date", "20260101", "--end-date", "20260102"],
            ["--dataset", "etf-daily", "--code", "000001", "--provider", "sina", "--start-date", "20260101", "--end-date", "20260102"],
        ):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                probe.parse_args(argv)


if __name__ == "__main__":
    unittest.main()
