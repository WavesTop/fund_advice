"""Tonghuashun ordinary-page constituent parsing and completeness checks."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.import_sector_heat import fetch_ths_visible_constituents


def page(current: int, total: int, rows: list[tuple[int, str, str, str]]) -> str:
    body = "".join(
        "<tr>" + "".join(f"<td>{value}</td>" for value in (
            order, code, name, "10", "1", "0.1", "0", "1", "1", "1",
            "1亿", "1亿", market_cap, "20",
        )) + "</tr>"
        for order, code, name, market_cap in rows
    )
    return f'<table><tbody>{body}</tbody></table><span class="page_info">{current}/{total}</span>'


class ThsConstituentPaginationTests(unittest.TestCase):
    def test_reads_every_ordinary_page_and_converts_market_cap(self):
        payloads = {
            1: page(1, 2, [(1, "600001", "甲公司", "12.34亿"), (2, "000002", "乙公司", "5600万")]),
            2: page(2, 2, [(3, "300003", "丙公司", "--")]),
        }

        def fetch(url, timeout, **kwargs):
            self.assertNotIn("ajax", url)
            self.assertIn("/detail/page/", url)
            self.assertIn("Cookie", kwargs["headers"])
            number = int(url.split("/page/")[1].split("/")[0])
            return payloads[number]

        with patch("scripts.import_sector_heat._ths_cookie", return_value="v=fixture"), patch(
            "scripts.import_sector_heat.fetch_text", side_effect=fetch,
        ):
            rows = fetch_ths_visible_constituents({"code": "881101"}, throttle_seconds=0)

        self.assertEqual([row["source_order"] for row in rows], [1, 2, 3])
        self.assertEqual([row["stock_code"] for row in rows], ["600001", "000002", "300003"])
        self.assertEqual(rows[0]["market_cap"], "1234000000.00")
        self.assertEqual(rows[1]["market_cap"], "56000000")
        self.assertIsNone(rows[2]["market_cap"])

    def test_declared_page_that_is_empty_is_rejected_as_truncated(self):
        payloads = {1: page(1, 2, [(1, "600001", "甲公司", "1亿")]), 2: page(2, 2, [])}

        def fetch(url, timeout, **kwargs):
            number = int(url.split("/page/")[1].split("/")[0])
            return payloads[number]

        with patch("scripts.import_sector_heat._ths_cookie", return_value="v=fixture"), patch(
            "scripts.import_sector_heat.fetch_text", side_effect=fetch,
        ), self.assertRaisesRegex(ValueError, "第2页为空"):
            fetch_ths_visible_constituents({"code": "881272"}, throttle_seconds=0)

    def test_single_page_without_pager_is_complete(self):
        payload = page(1, 1, [(1, "600001", "甲公司", "1亿")]).replace(
            '<span class="page_info">1/1</span>', "",
        )
        with patch("scripts.import_sector_heat._ths_cookie", return_value="v=fixture"), patch(
            "scripts.import_sector_heat.fetch_text", return_value=payload,
        ):
            rows = fetch_ths_visible_constituents({"code": "881101"}, throttle_seconds=0)
        self.assertEqual(len(rows), 1)

    def test_browser_challenge_is_reported_as_access_limit(self):
        payload = '<script src="//s.thsi.cn/js/chameleon/chameleon.js"></script>'
        with patch("scripts.import_sector_heat._ths_cookie", return_value="v=fixture"), patch(
            "scripts.import_sector_heat.fetch_text", return_value=payload,
        ), self.assertRaisesRegex(ValueError, "触发访问限制"):
            fetch_ths_visible_constituents({"code": "881101"}, throttle_seconds=0)

    def test_non_contiguous_source_order_is_rejected(self):
        payload = page(1, 1, [(1, "600001", "甲公司", "1亿"), (3, "000002", "乙公司", "2亿")])
        with patch("scripts.import_sector_heat._ths_cookie", return_value="v=fixture"), patch(
            "scripts.import_sector_heat.fetch_text", return_value=payload,
        ), self.assertRaisesRegex(ValueError, "序号不连续"):
            fetch_ths_visible_constituents({"code": "881101"}, throttle_seconds=0)

    def test_duplicate_stock_code_is_rejected(self):
        payload = page(1, 1, [(1, "600001", "甲公司", "1亿"), (2, "600001", "甲公司", "1亿")])
        with patch("scripts.import_sector_heat._ths_cookie", return_value="v=fixture"), patch(
            "scripts.import_sector_heat.fetch_text", return_value=payload,
        ), self.assertRaisesRegex(ValueError, "重复证券代码"):
            fetch_ths_visible_constituents({"code": "881101"}, throttle_seconds=0)


if __name__ == "__main__":
    unittest.main()
