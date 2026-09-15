import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


from scripts import probe_fund_timeseries as probe


def payload(columns, rows):
    return {"columns": columns, "dtypes": {c: "object" for c in columns}, "rows": rows, "akshare_version": "1.18.94"}


class NormalizeTests(unittest.TestCase):
    def test_real_quote_sources_preserve_units_split_and_known_disagreement(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/data_sources/fund_quote_examples.json").read_text())
        results = {}
        for case in fixture["cases"]:
            request = case["request"]
            result = probe.normalize(request["dataset"], case["adapter"], request["start_date"], request["end_date"])
            results[(case["provider"], case["code"])] = {row["date"]: row for row in result["normalized_rows"]}
        from decimal import Decimal
        for provider in ("sina", "tencent"):
            split = results[(provider, "513660")]
            self.assertEqual(Decimal(split["2026-04-20"]["close"]), Decimal("3.128"))
            self.assertEqual(Decimal(split["2026-04-21"]["close"]), Decimal("1.569"))
            self.assertEqual(Decimal(results[(provider, "160706")]["2005-10-17"]["close"]), Decimal("0.932"))
        self.assertEqual(results[("sina", "510050")]["2026-09-11"]["volume_shares"], "667541903")
        self.assertEqual(Decimal(results[("tencent", "510050")]["2026-09-11"]["volume_shares"]), Decimal("667541900"))
        # 已知的来源差异保留各自原值，不能拼接或取平均。
        self.assertEqual(results[("sina", "510050")]["2018-08-31"]["close"], "2.518")
        self.assertEqual(results[("tencent", "510050")]["2018-08-31"]["close"], "2.521")

    def test_real_nav_and_event_boundary_examples(self):
        path = Path(__file__).parent / "fixtures/data_sources/fund_timeseries_examples.json"
        fixture = json.loads(path.read_text())
        results = {}
        for case in fixture["cases"]:
            request = case["request"]
            result = probe.normalize(request["dataset"], case["adapter"], request["start_date"], request["end_date"])
            results[case["id"]] = {row["date"]: row for row in result["normalized_rows"]}
        historical = results["exchange-510050-20251031"]
        self.assertEqual(historical["2025-10-31"]["unit_nav"], "3.1567")
        latest = results["exchange-510050-20260901"]["2026-09-11"]
        self.assertEqual((latest["unit_nav"], latest["cumulative_nav"]), ("2.9817", "4.4734"))
        self.assertEqual(latest["return_fraction"], "-0.0122")
        # 拆分净值在登记日调整，不能移到次日交易除权日，也不能要求累计 >= 单位净值。
        split = results["exchange-513660-20260415"]
        self.assertEqual(split["2026-04-17"]["unit_nav"], "3.127")
        self.assertEqual(split["2026-04-20"]["unit_nav"], "1.5751")
        self.assertEqual(split["2026-04-17"]["cumulative_nav"], "1.6234")
        self.assertTrue(all(row["published_at"] is None for row in split.values()))
        self.assertEqual(set(results["calendar"]),
                         {"2026-02-13", "2026-02-24", "2026-02-25", "2026-02-26", "2026-02-27"})

    def test_nav_filters_dates_and_converts_percent_decimal(self):
        result = probe.normalize("unit-nav", payload(
            ["净值日期", "单位净值", "日增长率"],
            [["2026-01-01", 1.2, None], ["2026-01-02", "1.2345", 1.25], ["2026-01-04", 1.3, -0.5]],
        ), "20260102", "20260103")
        self.assertEqual(result["source"], {"row_count": 3, "min_date": "2026-01-01", "max_date": "2026-01-04",
                                                "missing_value_counts": {"净值日期": 0, "单位净值": 0, "日增长率": 1}})
        self.assertEqual(result["selected"]["row_count"], 1)
        self.assertEqual(result["normalized_rows"][0]["unit_nav"], "1.2345")
        self.assertEqual(result["normalized_rows"][0]["return_fraction"], "0.0125")
        self.assertIsNone(result["normalized_rows"][0]["published_at"])

    def test_dates_and_decimal_conversion_do_not_silently_change(self):
        with self.assertRaises(probe.ProbeError):
            probe.parse_date("2026-W01-1")
        from decimal import Decimal
        value = "1.123456789012345678901234567890123456789"
        self.assertEqual(probe.decimal_string(value, "日增长率", factor=Decimal("0.01")),
                         "0.01123456789012345678901234567890123456789")

    def test_cumulative_nav_and_nullable_return(self):
        result = probe.normalize("cumulative-nav", payload(
            ["净值日期", "累计净值", "日增长率"], [["2026-02-01", "2.500", None]]
        ), "20260201", "20260201")
        self.assertEqual(result["normalized_rows"][0]["cumulative_nav"], "2.500")
        self.assertIsNone(result["normalized_rows"][0]["return_fraction"])

    def test_quote_volume_ohlc_and_nullable_amount(self):
        result = probe.normalize("etf-daily", payload(
            ["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"],
            [["2026-03-01", 1.01, 1.05, 1.0, 1.04, 12.5, None]],
        ), "20260301", "20260301")
        row = result["normalized_rows"][0]
        self.assertEqual(row["volume_shares"], "1250.0")
        self.assertIsNone(row["amount_cny"])
        self.assertEqual(row["adjustment"], "none")

    def test_calendar_scope_and_empty_range(self):
        result = probe.normalize("calendar", payload(["trade_date"], [["2026-01-01"]]), "20260102", "20260103")
        self.assertEqual(result["normalized_rows"], [])
        report = probe.make_report(
            {"dataset": "calendar", "code": None, "start_date": "20260102", "end_date": "20260103"},
            payload(["trade_date"], [["2026-01-01"]]), 0.1, "2026-01-01T00:00:00Z")
        self.assertEqual(report["status"], "empty")
        self.assertFalse(report["production_ready"])

    def test_empty_dataframe_is_empty_but_partial_schema_is_drift(self):
        result = probe.normalize("unit-nav", payload([], []), "20260101", "20260102")
        self.assertEqual(result["source"]["row_count"], 0)
        with self.assertRaises(probe.ProbeError) as caught:
            probe.normalize("unit-nav", payload(["净值日期"], []), "20260101", "20260102")
        self.assertEqual(caught.exception.code, "missing_columns")

    def test_exchange_nav_has_both_values_and_sorts(self):
        result = probe.normalize("exchange-nav", payload(
            ["净值日期", "单位净值", "累计净值"],
            [["2026-01-02", 1.2, None], ["2026-01-01", 1.1, 2.1]],
        ), "20260101", "20260102")
        self.assertEqual([row["date"] for row in result["normalized_rows"]], ["2026-01-01", "2026-01-02"])
        self.assertEqual(result["normalized_rows"][0]["cumulative_nav"], "2.1")
        self.assertIsNone(result["normalized_rows"][1]["cumulative_nav"])
        self.assertEqual(result["normalization"]["units"]["unit_nav"], "unverified_currency_per_share")

    def test_malformed_duplicate_missing_and_bad_ohlc_fail(self):
        cases = [
            ("unit-nav", payload(["净值日期", "单位净值"], [["bad", 1]]), "invalid_response"),
            ("unit-nav", payload(["净值日期", "单位净值"], [["2026-01-01junk", 1]]), "invalid_response"),
            ("unit-nav", payload(["净值日期", "单位净值"], [["2026-01-01", 1], ["2026-01-01", 2]]), "duplicate_response"),
            ("unit-nav", payload(["净值日期"], [["2026-01-01"]]), "missing_columns"),
            ("lof-daily", payload(["日期", "开盘", "最高", "最低", "收盘"], [["2026-01-01", 2, 1.5, 1, 1.8]]), "invalid_response"),
        ]
        for dataset, value, code in cases:
            with self.subTest(code=code), self.assertRaises(probe.ProbeError) as caught:
                probe.normalize(dataset, value, "20260101", "20260102")
            self.assertEqual(caught.exception.code, code)


class IsolationAndCliTests(unittest.TestCase):
    @mock.patch.object(probe.subprocess, "run")
    def test_safe_upstream_error_does_not_expose_stdout(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, 'secret debug\n{"error":"upstream_failure"}\n', "private traceback")
        with self.assertRaises(probe.ProbeError) as caught:
            probe.run_adapter({"dataset": "calendar", "start_date": "20260101", "end_date": "20260102"}, 1)
        self.assertEqual(caught.exception.message, probe.SOURCE_ERRORS["upstream_failure"])
        self.assertNotIn("secret", caught.exception.message)

    @mock.patch.object(probe.subprocess, "run")
    def test_non_string_error_payload_is_rejected_safely(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, '{"error":{}}\n', "")
        with self.assertRaises(probe.ProbeError) as caught:
            probe.run_adapter({}, 1)
        self.assertEqual(caught.exception.code, "invalid_response")

    @mock.patch.object(probe.subprocess, "run", side_effect=subprocess.TimeoutExpired("child", 0.01))
    def test_hard_timeout_is_categorized(self, _run):
        with self.assertRaises(probe.ProbeError) as caught:
            probe.run_adapter({}, 0.01)
        self.assertEqual(caught.exception.code, "timeout")

    def test_cli_validation_and_no_default_network(self):
        invalid = [
            [],
            ["--dataset", "unit-nav", "--code", "１２３４５６", "--start-date", "20260101", "--end-date", "20260102"],
            ["--dataset", "unit-nav", "--code", "000001", "--start-date", "20260230", "--end-date", "20260301"],
            ["--dataset", "calendar", "--start-date", "20260102", "--end-date", "20260101"],
            ["--dataset", "calendar", "--start-date", "20260101", "--end-date", "20260102", "--timeout", "inf"],
        ]
        for argv in invalid:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                probe.parse_args(argv)
        args = probe.parse_args(["--dataset", "unit-nav", "--code", "000001", "--start-date", "20260101", "--end-date", "20260102"])
        self.assertEqual(args.code, "000001")

    def test_exact_adapter_parameters(self):
        base = {"code": "510050", "start_date": "20260101", "end_date": "20260102"}
        self.assertEqual(probe.adapter_params({"dataset": "exchange-nav", **base}),
                         {"fund": "510050", "start_date": "20260101", "end_date": "20260102"})
        self.assertEqual(probe.adapter_params({"dataset": "etf-daily", **base})["adjust"], "")
        self.assertIn('period="成立来"', probe._child_code())

    @mock.patch.object(probe, "run_adapter")
    def test_empty_has_distinct_exit_and_report(self, run_adapter):
        run_adapter.return_value = (payload(["trade_date"], [["2026-01-01"]]), 0.01)
        with tempfile.TemporaryDirectory() as directory:
            exit_code = probe.main(["--dataset", "calendar", "--start-date", "20260102", "--end-date", "20260103", "--output-dir", directory])
            reports = list(Path(directory).glob("*/report.json"))
            self.assertEqual(exit_code, 3)
            self.assertEqual(json.loads(reports[0].read_text())["status"], "empty")

    @mock.patch.object(probe, "run_adapter")
    def test_normalization_error_report_preserves_adapter(self, run_adapter):
        run_adapter.return_value = (payload(["净值日期", "单位净值"], [["bad", 1.0]]), 0.01)
        with tempfile.TemporaryDirectory() as directory:
            exit_code = probe.main(["--dataset", "unit-nav", "--code", "000001", "--start-date", "20260101",
                                    "--end-date", "20260102", "--output-dir", directory])
            report = json.loads(next(Path(directory).glob("*/report.json")).read_text())
            self.assertEqual(exit_code, 2)
            self.assertEqual(report["adapter"]["rows"], [["bad", 1.0]])


if __name__ == "__main__":
    unittest.main()
