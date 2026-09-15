import datetime as dt
import json
import sys
import types
import unittest
from unittest import mock

import requests

from scripts import fund_quote_sources as source


class Response:
    def __init__(self, payload, status_code=200, assignment=True):
        body = json.dumps(payload, ensure_ascii=False)
        self.text = f"kline_day={body};" if assignment else body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("private upstream detail")


def row(day="2025-01-02", open_="1.100", close="1.200", high="1.300",
        low="1.000", volume="12.50", turnover="0.8", amount="1.2345"):
    return [day, open_, close, high, low, volume, "unused", turnover, amount]


def payload(symbol="sh510050", rows=None, *, day_key="day", identity_code="510050",
            identity_name="ETF sample", identity_kind="ETF"):
    node = {day_key: [] if rows is None else rows}
    if identity_code is not None:
        identity = [""] * 62
        identity[1], identity[2], identity[61] = identity_name, identity_code, identity_kind
        node["qt"] = {symbol: identity}
    return {"code": 0, "msg": "", "data": {symbol: node}}


class FixedDate(dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 13)


class TencentSourceTests(unittest.TestCase):
    @mock.patch.object(source.requests, "get")
    def test_exact_year_request_raw_strings_and_decimal_amount(self, get):
        precise = "1.23456789012345678901234567890123456789"
        get.return_value = Response(payload(rows=[
            row("2025-12-31", amount=precise), row("2024-12-31", amount="9")
        ]))

        result = source.fetch_tencent_daily("510050", "20250101", "20251231")

        get.assert_called_once_with(source.TENCENT_URL, params={
            "_var": "kline_day2025",
            "param": "sh510050,day,2025-01-01,2025-12-31,640,",
        }, timeout=12)
        self.assertEqual(result["columns"], ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"])
        self.assertEqual(result["rows"], [[
            "2025-12-31", "1.100", "1.200", "1.300", "1.000", "12.50",
            "12345.67890123456789012345678901234567890000",
        ]])
        self.assertEqual(result["dtypes"]["成交量"], "object")
        evidence = result["source_evidence"]
        self.assertEqual((evidence["artifact_kind"], evidence["provider"], evidence["symbol"]),
                         ("source_response", "tencent", "sh510050"))
        window = evidence["windows"][0]
        self.assertEqual(window["actual_returned_bounds"],
                         {"min_date": "2024-12-31", "max_date": "2025-12-31"})
        self.assertEqual(window["response_subset"]["day"][0]["amount"], precise)
        self.assertEqual(
            (window["response_subset"]["identity_code"],
             window["response_subset"]["identity_name"],
             window["response_subset"]["identity_kind"]),
            ("510050", "ETF sample", "ETF"),
        )
        self.assertNotIn("unused", window["response_subset"]["day"][0])

    @mock.patch.object(source.requests, "get")
    def test_full_25_year_range_stays_yearly_and_does_not_fake_coverage(self, get):
        get.side_effect = lambda _url, **_kwargs: Response(payload(rows=[row("2024-06-03")]))

        result = source.fetch_tencent_daily("510050", "20000101", "20241231")

        self.assertEqual(get.call_count, 25)
        self.assertEqual(result["rows"], [
            ["2024-06-03", "1.100", "1.200", "1.300", "1.000", "12.50", "12345.0000"]
        ])
        windows = result["source_evidence"]["windows"]
        self.assertTrue(all(item["status"] == "empty" for item in windows[:-1]))
        self.assertEqual(windows[-1]["status"], "window_selected")
        self.assertEqual(windows[-1]["returned_row_count"], 1)
        self.assertEqual(windows[0]["requested_bounds"]["start_date"], "2000-01-01")
        self.assertEqual(windows[-1]["requested_bounds"]["end_date"], "2024-12-31")

    @mock.patch.object(source.requests, "get")
    def test_duplicate_identical_is_accepted_but_conflict_fails(self, get):
        same = row()
        get.return_value = Response(payload(rows=[same, same.copy()]))
        result = source.fetch_tencent_daily("510050", "20250101", "20251231")
        self.assertEqual(len(result["rows"]), 1)

        get.return_value = Response(payload(rows=[row(), row(close="9.9")]))
        with self.assertRaises(source.ProbeError) as caught:
            source.fetch_tencent_daily("510050", "20250101", "20251231")
        self.assertEqual(caught.exception.code, "duplicate_response")

    @mock.patch.object(source.requests, "get")
    def test_identity_foreign_symbol_and_adjusted_only_are_rejected(self, get):
        invalid = [
            payload(rows=[row()], identity_code="159001"),
            payload(rows=[row()], identity_code=None),
            payload(rows=[row()], identity_kind="股票"),
            {"code": 0, "data": {"sz159001": {"day": [row()]}}},
            payload(rows=[row()], day_key="qfqday"),
            payload(rows=[row()], day_key="hfqday"),
        ]
        for value in invalid:
            get.return_value = Response(value)
            with self.subTest(value=value), self.assertRaises(source.ProbeError) as caught:
                source.fetch_tencent_daily("510050", "20250101", "20251231")
            self.assertEqual(caught.exception.code, "invalid_response")

    @mock.patch.object(source.requests, "get")
    def test_expected_kind_must_match_verified_identity(self, get):
        get.return_value = Response(payload(rows=[row()], identity_kind="ETF"))
        source.fetch_tencent_daily("510050", "20250101", "20251231", expected_kind="ETF")
        with self.assertRaises(source.ProbeError) as caught:
            source.fetch_tencent_daily("510050", "20250101", "20251231", expected_kind="LOF")
        self.assertEqual(caught.exception.code, "invalid_response")

        with self.assertRaises(source.ProbeError) as caught:
            source.fetch_tencent_daily("510050", "20250101", "20251231", expected_kind="stock")
        self.assertEqual(caught.exception.code, "invalid_request")

    @mock.patch.object(source.requests, "get")
    def test_empty_day_is_valid_empty_result(self, get):
        get.return_value = Response(payload("sz159001", rows=[], identity_code="159001"), assignment=False)
        result = source.fetch_tencent_daily("159001", "20250101", "20251231")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["source_evidence"]["windows"][0]["status"], "empty")
        self.assertEqual(result["source_evidence"]["symbol"], "sz159001")

    @mock.patch.object(source.requests, "get")
    def test_missing_volume_and_amount_remain_missing(self, get):
        get.return_value = Response(payload(rows=[row(volume=None, amount=None)]))
        result = source.fetch_tencent_daily("510050", "20250101", "20251231")
        self.assertEqual(result["rows"][0][-2:], [None, None])

    @mock.patch.object(source.requests, "get")
    def test_bad_dates_and_numbers_are_rejected(self, get):
        invalid_rows = [
            row("2025-1-02"), row("2025-02-30"), row(open_="NaN"), row(close="Infinity"),
            row(high="0"), row(low="-1"), row(volume="-0.1"), row(amount="-1"),
            row(open_=None), row(open_={"private": "object"}), ["2025-01-02"] * 8,
        ]
        for invalid in invalid_rows:
            get.return_value = Response(payload(rows=[invalid]))
            with self.subTest(invalid=invalid), self.assertRaises(source.ProbeError) as caught:
                source.fetch_tencent_daily("510050", "20250101", "20251231")
            self.assertEqual(caught.exception.code, "invalid_response")

    def test_code_and_date_validation_happens_before_http(self):
        cases = [
            ("１２３４５６", "20250101", "20251231", "invalid_request"),
            ("000001", "20250101", "20251231", "unsupported_symbol"),
            ("510050", "20250230", "20251231", "invalid_request"),
            ("510050", "20251231", "20250101", "invalid_request"),
        ]
        with mock.patch.object(source.requests, "get") as get:
            for code, start, end, error in cases:
                with self.subTest(code=code), self.assertRaises(source.ProbeError) as caught:
                    source.fetch_tencent_daily(code, start, end)
                self.assertEqual(caught.exception.code, error)
            get.assert_not_called()

    @mock.patch.object(source.dt, "date", FixedDate)
    @mock.patch.object(source.requests, "get")
    def test_today_caps_current_window_and_future_only_makes_no_request(self, get):
        get.return_value = Response(payload(rows=[row("2026-09-13")]))
        result = source.fetch_tencent_daily("510050", "20260101", "20271231")
        self.assertEqual(get.call_count, 1)
        window = result["source_evidence"]["windows"][0]
        self.assertEqual(window["requested_bounds"]["end_date"], "2026-09-13")
        self.assertIn("2026-12-31", window["params"]["param"])

        get.reset_mock()
        result = source.fetch_tencent_daily("510050", "20270101", "20271231")
        self.assertEqual(result["rows"], [])
        get.assert_not_called()

    @mock.patch.object(source.requests, "get")
    def test_640_row_cap_rejects_overlapping_incomplete_window(self, get):
        rows = [row("2025-06-01") for _ in range(640)]
        get.return_value = Response(payload(rows=rows))
        with self.assertRaises(source.ProbeError) as caught:
            source.fetch_tencent_daily("510050", "20250101", "20251231")
        self.assertEqual(caught.exception.code, "truncated_response")

    @mock.patch.object(source.requests, "get")
    def test_http_errors_bubble_without_embedding_exception_text(self, get):
        get.return_value = Response({}, status_code=503)
        with self.assertRaises(requests.HTTPError):
            source.fetch_tencent_daily("510050", "20250101", "20251231")


class BaoStockSourceTests(unittest.TestCase):
    class Result:
        def __init__(self, fields, rows, error_code="0"):
            self.fields, self.rows, self.error_code, self.error_msg = fields, iter(rows), error_code, "safe"
            self.current = None

        def next(self):
            try:
                self.current = next(self.rows)
                return True
            except StopIteration:
                return False

        def get_row_data(self):
            return self.current

    def fake_module(self, basic_rows=None, daily_rows=None):
        module = types.SimpleNamespace()
        module.login = mock.Mock(return_value=types.SimpleNamespace(error_code="0", error_msg="success"))
        module.logout = mock.Mock()
        module.query_stock_basic = mock.Mock(return_value=self.Result(
            ["code", "code_name", "ipoDate", "outDate", "type", "status"],
            basic_rows if basic_rows is not None else [["sh.510050", "华夏上证50ETF", "2026-09-01", "", "5", "1"]],
        ))
        module.query_history_k_data_plus = mock.Mock(return_value=self.Result(
            ["date", "code", "open", "high", "low", "close", "volume", "amount", "adjustflag", "tradestatus"],
            daily_rows if daily_rows is not None else [[
                "2026-09-11", "sh.510050", "3.0060", "3.0070", "2.9560", "2.9810",
                "667541903", "1984173121.0000", "3", "1",
            ]],
        ))
        return module

    def test_etf_identity_unadjusted_rows_and_units(self):
        module = self.fake_module()
        with mock.patch.dict(sys.modules, {"baostock": module}):
            result = source.fetch_baostock_daily("510050", "20260901", "20260911", expected_kind="ETF")
        self.assertEqual(result["rows"][0][-2:], ["667541903", "1984173121.0000"])
        self.assertEqual(result["quote_units"], {"volume": "shares", "amount": "CNY"})
        self.assertEqual(result["source_evidence"]["identity"]["kind"], "ETF")
        module.query_history_k_data_plus.assert_called_once_with(
            "sh.510050",
            "date,code,open,high,low,close,volume,amount,adjustflag,tradestatus",
            start_date="2026-09-01", end_date="2026-09-11", frequency="d", adjustflag="3",
        )
        module.logout.assert_called_once()

    def test_lof_and_mismatched_identity_are_rejected(self):
        with self.assertRaises(source.ProbeError) as caught:
            source.fetch_baostock_daily("160706", "20260901", "20260911", expected_kind="LOF")
        self.assertEqual(caught.exception.code, "unsupported_symbol")

        module = self.fake_module(basic_rows=[])
        with mock.patch.dict(sys.modules, {"baostock": module}), self.assertRaises(source.ProbeError) as caught:
            source.fetch_baostock_daily("510050", "20260901", "20260911", expected_kind="ETF")
        self.assertEqual(caught.exception.code, "invalid_response")
        module.logout.assert_called_once()

    def test_partial_history_is_rejected_instead_of_claiming_complete_coverage(self):
        module = self.fake_module(basic_rows=[
            ["sh.510050", "华夏上证50ETF", "2005-02-23", "", "5", "1"]
        ])
        with mock.patch.dict(sys.modules, {"baostock": module}), self.assertRaises(source.ProbeError) as caught:
            source.fetch_baostock_daily("510050", "20050101", "20260911", expected_kind="ETF")
        self.assertEqual(caught.exception.code, "truncated_response")


if __name__ == "__main__":
    unittest.main()
