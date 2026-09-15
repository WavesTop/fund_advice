import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from backend.core.config import Settings
from backend.storage.catalog import import_catalog
from backend.storage.timeseries import get_timeseries
from scripts import fund_nav_eastmoney as eastmoney
from scripts.import_fund_timeseries import _import_nav
from scripts.probe_fund_source import ProbeError


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


def _page(rows, *, total, index, size, code=None):
    data = {"LSJZList": rows}
    if code is not None:
        data["FundCode"] = code
    return json.dumps({"Data": data, "ErrCode": 0, "TotalCount": total,
                       "PageSize": size, "PageIndex": index}).encode()


def _row(day, unit="1.2300", accumulated="2.3400"):
    return {"FSRQ": day, "DWJZ": unit, "LJJZ": accumulated}


class EastmoneyPagedNavTests(unittest.TestCase):
    def test_fetches_all_pages_filters_locally_and_preserves_source_precision(self):
        pages = {
            1: _page([_row("2026-01-03", "1.2300", "2.3400"), _row("2026-01-02", "1.2000", "2.3000")],
                     total=3, index=1, size=2, code="000001"),
            2: _page([_row("2026-01-01", "1.1000", "2.2000")], total=3, index=2, size=2, code="000001"),
        }
        requested = []

        def opener(request, *, timeout):
            query = parse_qs(urlparse(request.full_url).query)
            requested.append(query)
            return _Response(pages[int(query["pageIndex"][0])])

        rows = eastmoney.fetch_paged_nav("000001", "20260102", "20260103", 1, opener=opener)
        self.assertEqual([row["date"] for row in rows], ["2026-01-02", "2026-01-03"])
        self.assertEqual(rows[1]["unit_nav"], "1.2300")
        self.assertEqual(rows[1]["accumulated_nav"], "2.3400")
        self.assertEqual([item["fundCode"] for item in requested], [["000001"], ["000001"]])
        self.assertEqual([item["startDate"] for item in requested], [["2026-01-02"], ["2026-01-02"]])

    def test_long_history_requests_every_reported_page(self):
        total, page_size = 6_005, 200
        calls = []

        def opener(request, *, timeout):
            page = int(parse_qs(urlparse(request.full_url).query)["pageIndex"][0])
            calls.append(page)
            count = page_size if page < 31 else 5
            first = dt.date(2000, 1, 1) + dt.timedelta(days=(page - 1) * page_size)
            rows = [_row((first + dt.timedelta(days=index)).isoformat()) for index in range(count)]
            return _Response(_page(rows, total=total, index=page, size=page_size))

        rows = eastmoney.fetch_paged_nav("000001", "20000101", "20260914", 1, opener=opener)
        self.assertEqual(len(rows), total)
        self.assertEqual(calls, list(range(1, 32)))

    def test_rejects_duplicate_dates_across_pages(self):
        pages = {
            1: _page([_row("2026-01-03"), _row("2026-01-02")], total=3, index=1, size=2),
            2: _page([_row("2026-01-02")], total=3, index=2, size=2),
        }

        def opener(request, *, timeout):
            return _Response(pages[int(parse_qs(urlparse(request.full_url).query)["pageIndex"][0])])

        with self.assertRaises(ProbeError) as caught:
            eastmoney.fetch_paged_nav("000001", "20260101", "20260103", 1, opener=opener)
        self.assertEqual(caught.exception.code, "duplicate_response")

    def test_rejects_empty_declared_page_and_identity_mismatch(self):
        def empty_page(request, *, timeout):
            return _Response(_page([], total=3, index=1, size=2))

        with self.assertRaises(ProbeError) as caught:
            eastmoney.fetch_paged_nav("000001", "20260101", "20260103", 1, opener=empty_page)
        self.assertEqual(caught.exception.code, "empty_response")

        def wrong_identity(request, *, timeout):
            return _Response(_page([_row("2026-01-01")], total=1, index=1, size=1, code="000002"))

        with self.assertRaises(ProbeError) as caught:
            eastmoney.fetch_paged_nav("000001", "20260101", "20260103", 1, opener=wrong_identity)
        self.assertEqual(caught.exception.code, "invalid_response")

    def test_classifies_http_network_and_json_failures(self):
        cases = (
            (lambda *_args, **_kwargs: (_ for _ in ()).throw(HTTPError("x", 503, "", {}, None)), "upstream_http_error"),
            (lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")), "network_error"),
            (lambda *_args, **_kwargs: _Response(b"not json"), "invalid_response"),
        )
        for opener, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(ProbeError) as caught:
                eastmoney.fetch_paged_nav("000001", "20260101", "20260103", 1, opener=opener)
            self.assertEqual(caught.exception.code, expected)


class NavImporterIntegrationTests(unittest.TestCase):
    def test_importer_uses_single_paged_source_row_for_both_nav_values(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(database_path=Path(directory) / "app.sqlite3")
            import_catalog(settings, [{"code": "000001", "name": "平安成长", "fund_type": "混合型"}], policy_version="test")
            rows = [{"date": "2026-01-02", "unit_nav": "1.2300", "accumulated_nav": "2.3400"}]
            with mock.patch("scripts.import_fund_timeseries.fetch_paged_nav", return_value=rows) as fetch:
                result = _import_nav(settings, "000001", "20260101", "20260103", None, 1)
            self.assertEqual(fetch.call_args.args, ("000001", "20260101", "20260103", 1))
            self.assertEqual(result["row_count"], 1)
            self.assertEqual(get_timeseries(settings, "000001")["rows"], rows)


if __name__ == "__main__":
    unittest.main()
