#!/usr/bin/env python3
"""Fetch and validate public Tonghuashun fund NAV history.

The public fund pages load two static JavaScript assignments: one for unit NAV
and one for accumulated NAV.  Those files contain no fund identity, so this
adapter first verifies the ordinary fund profile page and then requires both
series to contain the same strictly increasing date set.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from html import unescape
import json
import math
import re
import socket
import time
from typing import Callable
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from scripts.probe_fund_source import ProbeError


BASE_URL = "https://fund.10jqka.com.cn"
PROFILE_MAX_BYTES = 2 * 1024 * 1024
SERIES_MAX_BYTES = 16 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
_ALLOWED_SCRIPT_TYPES = {"application/json", "application/javascript", "text/javascript"}
MAX_ESTABLISHMENT_GAP_DAYS = 15
MAX_LATEST_NAV_AGE_DAYS = 10
MIN_HISTORY_DENSITY_PERCENT = 40


def _code(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
        raise ProbeError("invalid_request", "基金代码必须是保留前导零的六位 ASCII 数字")
    return value


def _day(value: str, *, compact: bool = False) -> dt.date:
    if not isinstance(value, str):
        raise ProbeError("invalid_response", "同花顺净值日期不是字符串")
    try:
        parsed = dt.datetime.strptime(value, "%Y%m%d" if compact else "%Y-%m-%d").date()
    except ValueError as exc:
        raise ProbeError("invalid_response", "同花顺净值日期无效") from exc
    if parsed.strftime("%Y%m%d" if compact else "%Y-%m-%d") != value:
        raise ProbeError("invalid_response", "同花顺净值日期格式无效")
    return parsed


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise ProbeError("network_timeout", "同花顺净值采集超过总超时限制")
    return value


def _response_url(response: object, expected_url: str) -> None:
    final_url = response.geturl() if hasattr(response, "geturl") else expected_url
    expected, actual = urlsplit(expected_url), urlsplit(str(final_url))
    if actual.scheme != "https" or actual.hostname != expected.hostname or actual.path != expected.path:
        raise ProbeError("invalid_response", "同花顺净值请求发生了非预期跳转")


def _request(
    url: str,
    *,
    referer: str,
    deadline: float,
    max_bytes: int,
    opener: Callable[..., object],
    before_request: Callable[[], None] | None,
) -> tuple[bytes, dict[str, str | None]]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "Referer": referer,
            "User-Agent": USER_AGENT,
        },
    )
    for attempt in range(3):
        if before_request is not None:
            before_request()
        try:
            with opener(request, timeout=_remaining(deadline)) as response:
                _response_url(response, url)
                status = response.getcode() if hasattr(response, "getcode") else 200
                if status != 200:
                    raise ProbeError("upstream_failure", f"同花顺净值来源返回 HTTP {status}")
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise ProbeError("truncated_response", "同花顺净值响应超过安全大小限制")
                headers = getattr(response, "headers", {})
                return body, {
                    "content_type": str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower(),
                    "last_modified": str(headers.get("Last-Modified", "")).strip() or None,
                    "etag": str(headers.get("ETag", "")).strip() or None,
                }
        except ProbeError:
            raise
        except urllib.error.HTTPError as exc:
            if attempt < 2 and exc.code in {403, 429, 500, 502, 503, 504}:
                time.sleep(min(0.25 * (2 ** attempt), _remaining(deadline)))
                continue
            raise ProbeError("upstream_failure", f"同花顺净值来源返回 HTTP {exc.code}") from exc
        except (TimeoutError, socket.timeout) as exc:
            if attempt < 2:
                time.sleep(min(0.25 * (2 ** attempt), _remaining(deadline)))
                continue
            raise ProbeError("network_timeout", "同花顺净值来源请求超时") from exc
        except (urllib.error.URLError, OSError) as exc:
            if attempt < 2:
                time.sleep(min(0.25 * (2 ** attempt), _remaining(deadline)))
                continue
            raise ProbeError("network_error", "无法连接同花顺净值来源") from exc
    raise AssertionError("同花顺净值重试状态无效")


def _plain_text(fragment: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", fragment)).split())


def parse_profile(payload: bytes, code: str, expected_name: str,
                  expected_fund_type: str | None = None) -> dict[str, str]:
    """Extract the identity box and require an exact share-code/name match."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeError("invalid_response", "同花顺基金资料页不是 UTF-8") from exc
    dialog = re.search(
        r"<ul\b[^>]*class=[\"'][^\"']*\bg-dialog\b[^\"']*[\"'][^>]*>(.*?)</ul>",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if dialog is None:
        raise ProbeError("invalid_response", "同花顺基金资料页缺少身份信息区")
    values: dict[str, str] = {}
    for item in re.findall(r"<li\b[^>]*>(.*?)</li>", dialog.group(1), re.IGNORECASE | re.DOTALL):
        key = re.search(
            r"<span\b[^>]*class=[\"'][^\"']*\bkey\b[^\"']*[\"'][^>]*>(.*?)</span>",
            item,
            re.IGNORECASE | re.DOTALL,
        )
        value = re.search(
            r"<span\b[^>]*class=[\"'][^\"']*\bvalue\b[^\"']*[\"'][^>]*>(.*?)</span>",
            item,
            re.IGNORECASE | re.DOTALL,
        )
        if key is not None and value is not None:
            label, content = _plain_text(key.group(1)), _plain_text(value.group(1))
            if not label or label in values:
                raise ProbeError("invalid_response", "同花顺基金资料页身份字段重复或为空")
            values[label] = content
    required = {"基金代码", "基金简称", "成立日期", "投资类型"}
    if not required.issubset(values) or any(not values[field] for field in required):
        raise ProbeError("invalid_response", "同花顺基金资料页缺少代码、名称、成立日期或投资类型")
    if values["基金代码"] != code:
        raise ProbeError("invalid_response", "同花顺基金资料页代码与请求不一致")
    if not isinstance(expected_name, str) or not expected_name.strip() or values["基金简称"] != expected_name.strip():
        raise ProbeError("invalid_response", "同花顺基金资料页名称与本地份额身份不一致")
    investment_type = values["投资类型"].strip()
    floating_nav_money = (
        isinstance(expected_fund_type, str) and "货币" in expected_fund_type
        and "浮动净值" in expected_fund_type
    )
    if (("货币" in investment_type and not floating_nav_money)
            or investment_type.upper() == "REITS"):
        raise ProbeError("unsupported_semantics", "该产品不是普通单位净值/累计净值口径，拒绝写入净值序列")
    established = _day(values["成立日期"])
    return {"code": code, "name": values["基金简称"], "fund_type": investment_type,
            "established_on": established.isoformat()}


def parse_nav_script(payload: bytes, code: str, kind: str) -> list[tuple[dt.date, str]]:
    if kind not in {"dwjz", "ljjz"}:
        raise ValueError("kind must be dwjz or ljjz")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeError("invalid_response", "同花顺净值文件不是 UTF-8") from exc
    match = re.fullmatch(
        rf"\s*var\s+{kind}_{re.escape(code)}\s*=\s*(\[.*\])\s*;?\s*",
        text,
        re.DOTALL,
    )
    if match is None:
        raise ProbeError("invalid_response", "同花顺净值文件变量名或结构与基金代码不一致")
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ProbeError("invalid_response", "同花顺净值文件不是有效数组") from exc
    if not isinstance(raw, list) or not raw:
        raise ProbeError("empty_response", "同花顺净值文件没有历史记录")
    result: list[tuple[dt.date, str]] = []
    previous: dt.date | None = None
    for row in raw:
        if (not isinstance(row, list) or len(row) != 2
                or not all(isinstance(value, str) for value in row)):
            raise ProbeError("invalid_response", "同花顺净值记录必须是日期和小数字符串")
        day = _day(row[0], compact=True)
        if len(row[1]) > 64 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", row[1]) is None:
            raise ProbeError("invalid_response", "同花顺净值不是普通十进制小数字符串")
        try:
            value = Decimal(row[1])
        except InvalidOperation as exc:
            raise ProbeError("invalid_response", "同花顺净值不是有效小数") from exc
        if not value.is_finite() or value <= 0 or row[1].strip() != row[1]:
            raise ProbeError("invalid_response", "同花顺净值超出允许范围")
        if previous is not None and day <= previous:
            raise ProbeError("duplicate_response", "同花顺净值日期重复或未严格递增")
        previous = day
        result.append((day, row[1]))
    return result


def _validate_complete_history(identity: dict[str, str], rows: list[tuple[dt.date, str]],
                               expected_through: dt.date) -> None:
    """Reject structurally valid fragments before they can replace lifetime history."""
    established = dt.date.fromisoformat(identity["established_on"])
    first, latest = rows[0][0], rows[-1][0]
    today = dt.date.today()
    if first < established or (first - established).days > MAX_ESTABLISHMENT_GAP_DAYS:
        raise ProbeError("truncated_response", "同花顺净值没有从基金成立初期开始，拒绝覆盖完整历史")
    as_of = min(expected_through, today)
    if latest > today or (as_of - latest).days > MAX_LATEST_NAV_AGE_DAYS:
        raise ProbeError("truncated_response", "同花顺净值末日过旧或晚于当前日期，拒绝覆盖完整历史")
    span_days = (latest - first).days + 1
    if len(rows) * 100 < span_days * MIN_HISTORY_DENSITY_PERCENT:
        raise ProbeError("truncated_response", "同花顺净值历史密度不足，疑似只返回了部分记录")


def fetch_ths_fund_nav(
    code: str,
    expected_name: str,
    start_date: str,
    end_date: str,
    *,
    timeout: float,
    expected_fund_type: str | None = None,
    before_request: Callable[[], None] | None = None,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> dict[str, object]:
    """Return a date-bounded, identity-checked NAV series and transport evidence."""
    code = _code(code)
    start, end = _day(start_date), _day(end_date)
    if (start > end or not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout <= 0):
        raise ProbeError("invalid_request", "同花顺净值日期范围或超时无效")
    deadline = time.monotonic() + float(timeout)
    profile_url = f"{BASE_URL}/{code}/interduce.html"
    unit_url = f"{BASE_URL}/{code}/json/jsondwjz.json"
    accumulated_url = f"{BASE_URL}/{code}/json/jsonljjz.json"
    profile_body, profile_headers = _request(
        profile_url, referer=f"{BASE_URL}/{code}/", deadline=deadline,
        max_bytes=PROFILE_MAX_BYTES, opener=opener, before_request=before_request,
    )
    identity = parse_profile(profile_body, code, expected_name, expected_fund_type)
    unit_body, unit_headers = _request(
        unit_url, referer=f"{BASE_URL}/{code}/historynet.html", deadline=deadline,
        max_bytes=SERIES_MAX_BYTES, opener=opener, before_request=before_request,
    )
    accumulated_body, accumulated_headers = _request(
        accumulated_url, referer=f"{BASE_URL}/{code}/historynet.html", deadline=deadline,
        max_bytes=SERIES_MAX_BYTES, opener=opener, before_request=before_request,
    )
    for headers in (unit_headers, accumulated_headers):
        if headers["content_type"] not in _ALLOWED_SCRIPT_TYPES:
            raise ProbeError("invalid_response", "同花顺净值文件内容类型无效")
    unit = parse_nav_script(unit_body, code, "dwjz")
    accumulated = parse_nav_script(accumulated_body, code, "ljjz")
    if [day for day, _ in unit] != [day for day, _ in accumulated]:
        raise ProbeError("invalid_response", "同花顺单位净值与累计净值日期集合不一致")
    _validate_complete_history(identity, unit, end)
    rows = [
        {"date": day.isoformat(), "unit_nav": unit_value, "accumulated_nav": accumulated_value}
        for (day, unit_value), (_, accumulated_value) in zip(unit, accumulated)
        if start <= day <= end
    ]
    if not rows:
        raise ProbeError("empty_response", "同花顺净值在请求日期范围内没有记录")
    return {
        "rows": rows,
        "identity": identity,
        "source_evidence": {
            "profile_url": profile_url,
            "unit_nav_url": unit_url,
            "accumulated_nav_url": accumulated_url,
            "source_row_count": len(unit),
            "source_min_date": unit[0][0].isoformat(),
            "source_max_date": unit[-1][0].isoformat(),
            "profile_headers": profile_headers,
            "unit_nav_headers": unit_headers,
            "accumulated_nav_headers": accumulated_headers,
            "published_at": None,
        },
    }
