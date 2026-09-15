"""Paged Eastmoney open-fund historical NAV adapter.

The older AKShare adapter reads ``pingzhongdata.js``.  That response can be
truncated for funds with a long history, so this module uses Eastmoney's
pageable F10 NAV endpoint and validates every page before it is imported.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import socket
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.probe_fund_source import ProbeError


ENDPOINT = "https://api.fund.eastmoney.com/f10/lsjz"
PAGE_SIZE = 200
MAX_PAGES = 1_000
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://fundf10.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (compatible; fund-advice-local-import/1.0)",
}


def _code(value: str) -> str:
    if not isinstance(value, str) or len(value) != 6 or not value.isascii() or not value.isdigit():
        raise ProbeError("invalid_request", "基金代码必须是保留前导零的六位 ASCII 数字")
    return value


def _date(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ProbeError("invalid_response", f"东方财富历史净值 {field} 不是日期")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ProbeError("invalid_response", f"东方财富历史净值 {field} 不是有效日期") from exc
    if parsed.isoformat() != value:
        raise ProbeError("invalid_response", f"东方财富历史净值 {field} 不是 YYYY-MM-DD 日期")
    return value


def _request_date(value: str) -> str:
    if not isinstance(value, str) or len(value) != 8 or not value.isascii() or not value.isdigit():
        raise ProbeError("invalid_request", "日期必须是 YYYYMMDD")
    try:
        return dt.datetime.strptime(value, "%Y%m%d").date().isoformat()
    except ValueError as exc:
        raise ProbeError("invalid_request", "日期必须是有效 YYYYMMDD 日期") from exc


def _decimal(value: object, field: str, *, required: bool) -> str | None:
    if value is None or value == "":
        if required:
            raise ProbeError("invalid_response", f"东方财富历史净值 {field} 为空")
        return None
    # json.loads below retains numeric tokens as strings.  Rejecting any other
    # type here makes it impossible to silently round through a Python float.
    if not isinstance(value, str) or not value.strip():
        raise ProbeError("invalid_response", f"东方财富历史净值 {field} 不是精确数字字符串")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ProbeError("invalid_response", f"东方财富历史净值 {field} 不是有效数字") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ProbeError("invalid_response", f"东方财富历史净值 {field} 超出允许范围")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ProbeError("invalid_response", f"东方财富历史净值 {field} 无效")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ProbeError("invalid_response", f"东方财富历史净值 {field} 无效") from exc
    if result <= 0 or str(result) != str(value):
        raise ProbeError("invalid_response", f"东方财富历史净值 {field} 无效")
    return result


def _unwrap_json(body: bytes) -> dict[str, Any]:
    try:
        text = body.decode("utf-8-sig").strip()
    except UnicodeError as exc:
        raise ProbeError("invalid_response", "东方财富历史净值响应编码无法解析") from exc
    if text.endswith(")") and "(" in text:
        text = text[text.index("(") + 1:-1].strip()
    try:
        # parse_float/parse_int preserve a value such as 1.2300 exactly when an
        # upstream deployment emits it as a JSON number instead of a string.
        payload = json.loads(text, parse_float=str, parse_int=str)
    except (TypeError, ValueError) as exc:
        raise ProbeError("invalid_response", "东方财富历史净值响应不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise ProbeError("invalid_response", "东方财富历史净值响应结构无效")
    return payload


def _identity(payload: dict[str, Any], data: dict[str, Any], code: str) -> None:
    """Verify an identity field when the endpoint supplies one.

    The live F10 endpoint currently omits a code in its normal response.  Its
    identity contract is therefore the six-digit request plus the catalog
    verification performed by the importer.  If an endpoint variant includes
    an identity field, accepting a mismatch would be unsafe.
    """
    values = [container[key] for container in (payload, data)
              for key in ("FundCode", "fundCode", "FCODE", "code") if key in container]
    if any(not isinstance(value, str) or value != code for value in values):
        raise ProbeError("invalid_response", "东方财富历史净值响应基金代码与请求不匹配")


def _parse_page(body: bytes, code: str, expected_page: int) -> tuple[list[dict[str, str | None]], int, int]:
    payload = _unwrap_json(body)
    if payload.get("ErrCode") not in (0, "0", None):
        raise ProbeError("upstream_failure", "东方财富历史净值接口返回失败状态")
    data = payload.get("Data")
    if not isinstance(data, dict):
        raise ProbeError("invalid_response", "东方财富历史净值响应缺少数据对象")
    _identity(payload, data, code)
    total = _positive_int(payload.get("TotalCount"), "TotalCount")
    page_size = _positive_int(payload.get("PageSize"), "PageSize")
    page_index = _positive_int(payload.get("PageIndex"), "PageIndex")
    if page_index != expected_page:
        raise ProbeError("invalid_response", "东方财富历史净值响应页码与请求不一致")
    rows = data.get("LSJZList")
    if not isinstance(rows, list):
        raise ProbeError("invalid_response", "东方财富历史净值响应缺少净值列表")
    total_pages = math.ceil(total / page_size)
    if expected_page > total_pages:
        raise ProbeError("invalid_response", "东方财富历史净值响应页码超过总页数")
    expected_count = page_size if expected_page < total_pages else total - page_size * (total_pages - 1)
    if len(rows) != expected_count:
        raise ProbeError("empty_response" if not rows else "invalid_response", "东方财富历史净值分页行数不完整")
    result: list[dict[str, str | None]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise ProbeError("invalid_response", "东方财富历史净值行结构无效")
        if set(("FSRQ", "DWJZ", "LJJZ")) - raw.keys():
            raise ProbeError("missing_columns", "东方财富历史净值行缺少日期、单位净值或累计净值字段")
        result.append({
            "date": _date(raw["FSRQ"], "FSRQ"),
            "unit_nav": _decimal(raw["DWJZ"], "DWJZ", required=True),
            "accumulated_nav": _decimal(raw["LJJZ"], "LJJZ", required=False),
        })
    return result, total_pages, total


def _fetch_page(code: str, start_date: str, end_date: str, page: int, page_size: int, timeout: float,
                opener: Callable[..., Any]) -> bytes:
    query = urlencode({"fundCode": code, "pageIndex": page, "pageSize": page_size,
                       "startDate": start_date, "endDate": end_date})
    request = Request(f"{ENDPOINT}?{query}", headers=_HEADERS)
    try:
        with opener(request, timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        raise ProbeError("upstream_http_error", "东方财富历史净值接口返回 HTTP 错误") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ProbeError("network_timeout", "东方财富历史净值请求超时") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise ProbeError("network_timeout", "东方财富历史净值请求超时") from exc
        raise ProbeError("network_error", "无法连接东方财富历史净值接口") from exc
    except OSError as exc:
        raise ProbeError("network_error", "无法连接东方财富历史净值接口") from exc


def fetch_paged_nav(code: str, start_date: str, end_date: str, timeout: float, *,
                    opener: Callable[..., Any] = urlopen, page_size: int = PAGE_SIZE,
                    max_pages: int = MAX_PAGES, before_request: Callable[[], None] | None = None) -> list[dict[str, str | None]]:
    """Fetch every reported page and locally filter the requested date range."""
    code = _code(code)
    start, end = _request_date(start_date), _request_date(end_date)
    if start > end:
        raise ProbeError("invalid_request", "start_date 不能晚于 end_date")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ProbeError("invalid_request", "timeout 必须为正数")
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0:
        raise ProbeError("invalid_request", "page_size 必须为正整数")
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages <= 0:
        raise ProbeError("invalid_request", "max_pages 必须为正整数")

    all_rows: list[dict[str, str | None]] = []
    seen_dates: set[str] = set()
    total_pages: int | None = None
    page = 1
    while total_pages is None or page <= total_pages:
        if page > max_pages:
            raise ProbeError("invalid_response", "东方财富历史净值分页超过安全上限")
        if before_request is not None:
            before_request()
        body = _fetch_page(code, start, end, page, page_size, float(timeout), opener)
        rows, reported_pages, _ = _parse_page(body, code, page)
        if total_pages is None:
            total_pages = reported_pages
            if total_pages > max_pages:
                raise ProbeError("invalid_response", "东方财富历史净值总页数超过安全上限")
        elif reported_pages != total_pages:
            raise ProbeError("invalid_response", "东方财富历史净值分页总页数在请求中发生变化")
        for row in rows:
            if row["date"] in seen_dates:
                raise ProbeError("duplicate_response", "东方财富历史净值响应包含重复日期")
            seen_dates.add(str(row["date"]))
            if start <= row["date"] <= end:
                all_rows.append(row)
        page += 1
    if not all_rows:
        raise ProbeError("empty_response", "东方财富历史净值在请求日期范围内没有数据")
    return sorted(all_rows, key=lambda row: str(row["date"]))
