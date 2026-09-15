import contextlib
import io
import tempfile
import unittest
from unittest import mock

from scripts import probe_fund_disclosure as probe


def payload(columns, rows):
    return {"columns": columns, "dtypes": {column: "object" for column in columns}, "rows": rows,
            "akshare_version": "1.18.94"}


class DisclosureNormalizationTests(unittest.TestCase):
    def test_holdings_convert_percent_wan_shares_and_wan_cny(self):
        result = probe.normalize({"dataset": "holdings", "code": "005911", "year": "2025"}, payload(
            ["序号", "股票代码", "股票名称", "占净值比例", "持股数", "持仓市值", "季度"],
            [[1, "601127", "赛力斯", "9.41", "386.79", "48693.03", "2025年1季度股票投资明细"]]))
        row = result["normalized_rows"][0]
        self.assertEqual((row["weight_fraction"], row["shares"], row["market_value_cny"]),
                         ("0.0941", "3867900.00", "486930300.00"))
        self.assertEqual((row["report_period_end"], row["scope"], row["published_at"]),
                         ("2025-03-31", "source_table_scope_unverified", None))
        self.assertEqual(row["security_namespace"], "source_unverified")

    def test_half_year_scope_stays_unverified(self):
        result = probe.normalize({"dataset": "holdings", "code": "005911", "year": "2025"}, payload(
            ["股票代码", "股票名称", "占净值比例", "持股数", "持仓市值", "季度"],
            [["601127", "赛力斯", 1, 2, 3, "2025年2季度股票投资明细"]]))
        self.assertEqual(result["normalized_rows"][0]["scope"], "source_table_scope_unverified")
        self.assertEqual(result["periods"], [{"report_period_end": "2025-06-30", "row_count": 1,
                                              "cap_hit": False, "completeness": "unknown"}])

    def test_holdings_reject_duplicate_security_in_same_period(self):
        columns = ["股票代码", "股票名称", "占净值比例", "持股数", "持仓市值", "季度"]
        row = ["601127", "赛力斯", 1, 2, 3, "2025年1季度股票投资明细"]
        with self.assertRaises(probe.ProbeError):
            probe.normalize({"dataset": "holdings", "code": "005911", "year": "2025"}, payload(columns, [row, row]))

    def test_holdings_reject_silent_year_fallback_and_preserve_foreign_code(self):
        columns = ["股票代码", "股票名称", "占净值比例", "持股数", "持仓市值", "季度"]
        with self.assertRaises(probe.ProbeError):
            probe.normalize({"dataset": "holdings", "code": "005911", "year": "2099"}, payload(
                columns, [["601127", "赛力斯", 1, 2, 3, "2026年1季度股票投资明细"]]))
        result = probe.normalize({"dataset": "holdings", "code": "050025", "year": "2025"}, payload(
            columns, [["00700", "腾讯控股", 1, 2, 3, "2025年2季度股票投资明细"]]))
        self.assertEqual(result["normalized_rows"][0]["security_code"], "00700")
        self.assertEqual(result["fund_identity_status"], "unverified_by_holdings_source")

    def test_dividend_index_is_metadata_not_cash_event(self):
        result = probe.normalize({"dataset": "dividend-announcements", "code": "510050"}, payload(
            ["基金代码", "公告标题", "基金名称", "公告日期", "报告ID"],
            [["510050", "收益分配公告", "华夏上证50ETF", "2025-12-09", "AN1"]]))
        self.assertEqual(result["normalized_rows"][0]["published_on"], "2025-12-09")
        self.assertNotIn("cash_per_unit", result["normalized_rows"][0])

    def test_structured_dividend_stays_candidate_until_announcement_check(self):
        result = probe.normalize({"dataset": "dividend-events", "code": "510050", "year": "2025"}, payload(
            ["基金代码", "基金简称", "权益登记日", "除息日期", "分红", "分红发放日"],
            [["510050", "上证50ETF华夏", "2025-12-16", "2025-12-17", "0.08", "2025-12-22"]]))
        row = result["normalized_rows"][0]
        self.assertEqual(row["candidate_cash_per_unit_cny"], "0.08")
        self.assertIsNone(row["published_at"])

    def test_split_direction_and_ex_date_are_not_invented(self):
        result = probe.normalize({"dataset": "split-events", "code": "515050", "year": "2026"}, payload(
            ["基金代码", "基金简称", "拆分折算日", "拆分类型", "拆分折算"],
            [["515050", "通信ETF华夏", "2026-05-12", "份额分拆", "3"]]))
        row = result["normalized_rows"][0]
        self.assertEqual(row["candidate_post_units_per_pre_unit"], "3")
        self.assertIsNone(row["ex_date"])

    def test_cumulative_dividend_derives_candidates_without_claiming_events(self):
        result = probe.normalize({"dataset": "dividend-cumulative", "code": "510050"}, payload(
            ["日期", "累计分红"], [["2024-12-02", "0.717"], ["2025-12-15", "0.797"]]))
        self.assertEqual([row["candidate_cash_per_unit"] for row in result["normalized_rows"]], [None, "0.080"])

    def test_cumulative_dividend_sorts_dates_and_rejects_duplicates_or_null(self):
        result = probe.normalize({"dataset": "dividend-cumulative", "code": "510050"}, payload(
            ["日期", "累计分红"], [["2025-12-15", "0.797"], ["2024-12-02", "0.717"]]))
        self.assertEqual([row["source_date"] for row in result["normalized_rows"]], ["2024-12-02", "2025-12-15"])
        for rows in ([["2024-12-02", "0.717"], ["2024-12-02", "0.717"]], [["2024-12-02", None]]):
            with self.subTest(rows=rows), self.assertRaises(probe.ProbeError):
                probe.normalize({"dataset": "dividend-cumulative", "code": "510050"}, payload(
                    ["日期", "累计分红"], rows))

    def test_events_require_requested_year_and_positive_value(self):
        dividend_columns = ["基金代码", "基金简称", "权益登记日", "除息日期", "分红", "分红发放日"]
        split_columns = ["基金代码", "基金简称", "拆分折算日", "拆分类型", "拆分折算"]
        invalid = [
            ({"dataset": "dividend-events", "code": "510050", "year": "2025"}, dividend_columns,
             ["510050", "ETF", "2024-12-16", "2024-12-17", "0.08", "2024-12-22"]),
            ({"dataset": "dividend-events", "code": "510050", "year": "2025"}, dividend_columns,
             ["510050", "ETF", "2025-12-16", "2025-12-17", None, "2025-12-22"]),
            ({"dataset": "dividend-events", "code": "510050", "year": "2025"}, dividend_columns,
             ["510050", "ETF", "2025-12-18", "2025-12-17", "0.08", "2025-12-22"]),
            ({"dataset": "split-events", "code": "515050", "year": "2026"}, split_columns,
             ["515050", "ETF", "2025-05-12", "份额分拆", "3"]),
            ({"dataset": "split-events", "code": "515050", "year": "2026"}, split_columns,
             ["515050", "ETF", "2026-05-12", "份额分拆", "0"]),
        ]
        for request, columns, row in invalid:
            with self.subTest(request=request, row=row), self.assertRaises(probe.ProbeError):
                probe.normalize(request, payload(columns, [row]))

    def test_event_indexes_and_events_reject_duplicates(self):
        cases = [
            ({"dataset": "dividend-announcements", "code": "510050"},
             ["基金代码", "公告标题", "基金名称", "公告日期", "报告ID"],
             ["510050", "公告", "ETF", "2025-12-10", "AN1"]),
            ({"dataset": "dividend-events", "code": "510050", "year": "2025"},
             ["基金代码", "基金简称", "权益登记日", "除息日期", "分红", "分红发放日"],
             ["510050", "ETF", "2025-12-16", "2025-12-17", "0.08", "2025-12-22"]),
            ({"dataset": "split-events", "code": "515050", "year": "2026"},
             ["基金代码", "基金简称", "拆分折算日", "拆分类型", "拆分折算"],
             ["515050", "ETF", "2026-05-12", "份额分拆", "3"]),
        ]
        for request, columns, row in cases:
            with self.subTest(request=request), self.assertRaises(probe.ProbeError):
                probe.normalize(request, payload(columns, [row, row]))

    def test_profile_requires_exact_code(self):
        with self.assertRaises(probe.ProbeError):
            probe.normalize({"dataset": "fund-profile", "code": "000051"}, payload(
                ["字段", "值"], [["基金代码", "005658"]]))

    def test_dates_require_exact_iso_format(self):
        for value in ("20251216", "2025-W01-1"):
            with self.subTest(value=value), self.assertRaises(probe.ProbeError):
                probe._date(value, "日期")

    def test_cli_requires_dataset_specific_arguments(self):
        cases = [
            ["--dataset", "holdings", "--code", "005911"],
            ["--dataset", "fee", "--code", "009314"],
            ["--dataset", "fund-profile", "--code", "000051", "--year", "2025"],
            ["--dataset", "split-events", "--code", "515050", "--year", "２０２６"],
            ["--dataset", "dividend-cumulative", "--code", "000001"],
        ]
        for argv in cases:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                probe.parse_args(argv)

    def test_main_writes_bounded_report(self):
        source = payload(["字段", "值"], [["基金代码", "000051"], ["基金简称", "华夏沪深300ETF联接A"]])
        with tempfile.TemporaryDirectory() as target, contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(probe, "run_adapter", return_value=(source, 0.01)):
            status = probe.main(["--dataset", "fund-profile", "--code", "000051", "--output-dir", target])
        self.assertEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
