#!/usr/bin/env python3
"""Refresh Tonghuashun sector boards for market context only."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as daytime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.config import Settings
from backend.core.trading_calendar import get_calendar
from backend.integrations.http_transport import get_bytes
from backend.integrations.ths_search_catalog import fetch_ths_etf_directory, normalize_query, read_identity_cache, save_identity_cache
from backend.storage.sector_heat import (SectorHistoryIntegrityError, merge_consistent_sector_history,
                                         list_custom_boards, list_manual_index_projections_as_ths_boards,
                                         read_sector_heat, record_sector_heat_failure,
                                         remove_sector_subjects, removed_subject_keys,
                                         save_sector_heat)

SHANGHAI = ZoneInfo("Asia/Shanghai")
Fetcher = Callable[[str, float], dict[str, Any]]
COLLECTOR_VERSION = "sector-ths-only-v6-intraday-quotes"
THS_CODE_RE = r"[0-9A-Z]{6}"
THS_NUMERIC_CODE_RE = r"\d{6}"
THS_SOURCE_CODE_RE = rf"(?:bk|zs|hs|16|17|33)_{THS_CODE_RE}"
THS_CATEGORY_META = {
    "industry": {"path": "thshy", "kind": "行业", "prefix": "bk"},
    "concept": {"path": "gn", "kind": "概念", "prefix": "bk"},
    "index": {"path": "zs", "kind": "指数", "prefix": "zs"},
    "etf": {"path": "fund", "kind": "ETF", "prefix": "17"},
}
THS_PATH_CATEGORY = {value["path"]: key for key, value in THS_CATEGORY_META.items()}


class _ThsConstituentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.page_position: tuple[int, int] | None = None
        self._in_body = False
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._page_info: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "tbody":
            self._in_body = True
        elif self._in_body and tag == "tr":
            self._row = []
        elif self._row is not None and tag == "td":
            self._cell = []
        elif tag == "span" and "page_info" in (attributes.get("class") or "").split():
            self._page_info = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        if self._page_info is not None:
            self._page_info.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "tbody":
            self._in_body = False
        elif tag == "span" and self._page_info is not None:
            match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", "".join(self._page_info))
            if match:
                self.page_position = int(match.group(1)), int(match.group(2))
            self._page_info = None


def fetch_text(url: str, timeout: float, referer: str = "https://q.10jqka.com.cn/",
               headers: dict[str, str] | None = None) -> str:
    request_headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Referer": referer}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    payload = get_bytes(request, timeout=timeout)
    try:
        return payload.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return payload.decode("gb18030", "replace")


def _ths_history_source_code(prefix: str, code: str) -> str:
    prefix = str(prefix or "bk").strip().lower()
    code = str(code or "").strip().upper()
    if prefix not in ("bk", "zs", "hs", "16", "17", "33") or not re.fullmatch(THS_CODE_RE, code):
        raise ValueError("同花顺行情身份无效")
    return f"{prefix}_{code}"


def _is_direct_ths_concept_market_code(code: str) -> bool:
    return bool(re.fullmatch(r"88[456]\d{3}", str(code or "").strip()))


def _enrich_ths_identity(item: dict[str, Any], *, category: str | None = None,
                         page_code: str | None = None, market_code: str | None = None,
                         prefix: str | None = None) -> dict[str, Any]:
    """Attach Tonghuashun page identity and quote identity to a board record."""
    inferred_category = category or str(item.get("ths_category") or "").strip()
    kind = str(item.get("kind") or "").strip()
    if not inferred_category:
        if kind == "行业":
            inferred_category = "industry"
        elif kind == "指数":
            inferred_category = "index"
        elif kind == "ETF":
            inferred_category = "etf"
        else:
            inferred_category = "concept"
    if inferred_category not in THS_CATEGORY_META:
        raise ValueError("同花顺板块类别无效")
    meta = THS_CATEGORY_META[inferred_category]
    display_code = str(item.get("code") or page_code or market_code or "").strip()
    page = str(page_code or item.get("ths_page_code") or display_code).strip()
    quote_code = str(market_code or item.get("ths_market_code") or display_code).strip()
    quote_prefix = str(prefix or item.get("ths_prefix") or meta["prefix"]).strip().lower()
    display_code = display_code.upper()
    page = page.upper()
    quote_code = quote_code.upper()
    if not re.fullmatch(THS_CODE_RE, display_code) or not re.fullmatch(THS_CODE_RE, page):
        raise ValueError("同花顺板块代码无效")
    if not re.fullmatch(THS_CODE_RE, quote_code):
        quote_code = display_code
    history_source_code = _ths_history_source_code(quote_prefix, quote_code)
    return {
        **item,
        "code": display_code,
        "kind": meta["kind"],
        "source_id": "sector_daily.ths",
        "ths_category": inferred_category,
        "ths_page_code": page,
        "ths_market_code": quote_code,
        "ths_prefix": quote_prefix,
        "history_source_id": "sector_daily.ths",
        "history_source_code": history_source_code,
        "history_identity_match": "native_source_code",
    }


def _member_history_source_code(member: dict[str, Any], fallback_code: str | None = None) -> str:
    value = str(member.get("history_source_code") or "").strip().lower()
    matched_source = re.fullmatch(THS_SOURCE_CODE_RE, value, flags=re.IGNORECASE)
    if matched_source:
        prefix, code = value.split("_", 1)
        if re.fullmatch(THS_NUMERIC_CODE_RE, code) or not (
            fallback_code and re.fullmatch(THS_CODE_RE, fallback_code, flags=re.IGNORECASE)
        ):
            return value
        return _ths_history_source_code(prefix, fallback_code)
    code = str(member.get("code") or "").strip()
    if (
        not re.fullmatch(THS_NUMERIC_CODE_RE, code)
        or not re.fullmatch(THS_CODE_RE, code, flags=re.IGNORECASE)
    ) and fallback_code and re.fullmatch(
        THS_CODE_RE,
        fallback_code,
        flags=re.IGNORECASE,
    ):
        member = {**member, "code": fallback_code.upper()}
    enriched = _enrich_ths_identity(member)
    return str(enriched["history_source_code"])


@lru_cache(maxsize=1)
def _ths_cookie() -> str:
    """Generate the visitor cookie used by Tonghuashun's ordinary HTML pages."""
    from akshare.datasets import get_ths_js
    import py_mini_racer

    script = Path(get_ths_js("ths.js")).read_text(encoding="utf-8")
    runtime = py_mini_racer.MiniRacer()
    runtime.eval(script)
    value = str(runtime.call("v")).strip()
    if not value:
        raise ValueError("同花顺访问Cookie生成失败")
    return f"v={value}"


def fetch_ths_line_text_by_source(source_code: str, *, timeout: float = 12.0) -> str:
    """Fetch Tonghuashun board daily JSONP using the same visitor cookie family as board pages."""
    if not re.fullmatch(THS_SOURCE_CODE_RE, source_code, flags=re.IGNORECASE):
        raise ValueError("同花顺日线来源代码无效")
    version = "v6" if source_code.startswith(("17_", "33_")) else "v4"
    url = f"https://d.10jqka.com.cn/{version}/line/{source_code}/01/last.js"
    try:
        return fetch_text(url, timeout, headers={"Cookie": _ths_cookie()})
    except Exception:
        return fetch_text(url, timeout)


def fetch_ths_line_text(ths_code: str, *, timeout: float = 12.0) -> str:
    return fetch_ths_line_text_by_source(_ths_history_source_code("bk", ths_code), timeout=timeout)


def _number(value: object, field: str, *, positive: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{field}缺失或不是精确数字")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field}不是有效数字") from exc
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise ValueError(f"{field}必须为{'正' if positive else '非负'}有限数字")
    return format(number, "f")


def fetch_ths_today(member: dict[str, Any], source_code: str, *, current: datetime | None = None,
                    timeout: float = 12.0) -> dict[str, Any]:
    requested = current or datetime.now(SHANGHAI)
    version = "v6" if source_code.startswith(("17_", "33_")) else "v4"
    url = f"https://d.10jqka.com.cn/{version}/line/{source_code}/01/today.js?_={int(requested.timestamp())}"
    payload = fetch_text(url, timeout, headers={"Cache-Control": "no-cache"})
    left, right = payload.find("{"), payload.rfind("}")
    if left < 0 or right <= left:
        raise ValueError("同花顺当日行情JSONP无效")
    root = json.loads(payload[left:right + 1], parse_float=str, parse_int=str)
    quote = root.get(source_code)
    if not isinstance(quote, dict):
        raise ValueError("同花顺当日行情身份不一致")
    name = str(quote.get("name", "")).strip()
    if not name or (name != member["name"] and name not in member["name"] and member["name"] not in name):
        raise ValueError("同花顺当日行情名称不一致")
    local = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    observed = datetime.strptime(str(quote.get("1", "")), "%Y%m%d").date()
    if observed != local.date():
        raise ValueError(f"同花顺当日行情未更新至{local.date()}")
    quote_time = datetime.strptime(str(quote.get("dt", "")), "%H%M").time()
    quote_at = datetime.combine(observed, quote_time, tzinfo=SHANGHAI)
    if quote_at > local:
        raise ValueError("同花顺行情时间晚于刷新时间")
    row: dict[str, Any] = {"date": observed.isoformat(), "quote_as_of": quote_at.isoformat(),
                           "provisional": local.time() < daytime(16, 0) or quote_time < daytime(15, 0)}
    for field, key in (("open", "7"), ("high", "8"), ("low", "9"), ("close", "11"),
                       ("volume", "13"), ("amount", "19")):
        row[field] = _number(quote.get(key), field, positive=field in ("open", "high", "low", "close"))
    if Decimal(row["high"]) < max(Decimal(row["open"]), Decimal(row["close"])) or Decimal(row["low"]) > min(Decimal(row["open"]), Decimal(row["close"])):
        raise ValueError("同花顺当日行情OHLC关系无效")
    return row


def fetch_ths_sector_daily(member: dict[str, Any], ths_code: str, *, timeout: float = 12.0) -> list[dict[str, Any]]:
    payload = fetch_ths_line_text_by_source(_member_history_source_code(member, ths_code), timeout=timeout)
    left, right = payload.find("{"), payload.rfind("}")
    if left < 0 or right <= left:
        raise ValueError("同花顺日线JSONP无效")
    root = json.loads(payload[left:right + 1], parse_float=str, parse_int=str)
    source_name = str(root.get("name", "")).strip()
    member_name = str(member["name"]).strip()
    if source_name != member_name and source_name not in member_name and member_name not in source_name and not (
        member.get("ths_category") == "etf" and f"line_{_member_history_source_code(member, ths_code)}_01_" in payload[:left]
    ):
        raise ValueError("同花顺日线名称与行业目录不一致")
    cutoff = (
        date.fromisoformat(member["price_cutoff"])
        if member.get("price_cutoff")
        else get_calendar().latest_market_price_session(datetime.now(SHANGHAI))
    )
    rows, seen = [], set()
    for value in str(root.get("data", "")).split(";"):
        fields = value.split(",")
        if not value.strip():
            continue
        if len(fields) < 7 or not re.fullmatch(r"\d{8}", fields[0]):
            raise ValueError(f"{member['code']} 同花顺日线字段不完整或日期无效")
        day = datetime.strptime(fields[0], "%Y%m%d").date().isoformat()
        if day > cutoff.isoformat():
            continue
        if day in seen:
            raise ValueError(f"{member['code']} {day} 同花顺日线日期重复")
        seen.add(day)
        row = {"date": day}
        for field, raw in zip(("open", "high", "low", "close", "volume", "amount"), fields[1:7]):
            row[field] = _number(raw, field, positive=field in ("open", "close", "high", "low"))
        if Decimal(row["high"]) < max(Decimal(row["open"]), Decimal(row["close"])) or Decimal(row["low"]) > min(Decimal(row["open"]), Decimal(row["close"])):
            raise ValueError("同花顺板块日线OHLC关系无效")
        rows.append(row)
    if not rows:
        raise ValueError("同花顺未返回可用板块日线")
    rows.sort(key=lambda row: row["date"])
    current = datetime.fromisoformat(member["quote_requested_at"]) if member.get("quote_requested_at") else datetime.now(SHANGHAI)
    if cutoff == current.astimezone(SHANGHAI).date() and get_calendar().is_session(cutoff):
        quote = fetch_ths_today(member, _member_history_source_code(member, ths_code),
                                current=current if member.get("quote_requested_at") else None, timeout=timeout)
        rows = [row for row in rows if row["date"] != quote["date"]] + [quote]
    return rows


def _source_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:500]}"


def fetch_ths_industry_directory(*, now: datetime | None = None) -> dict[str, Any]:
    """Return industry rows together with truthful directory/summary provenance."""
    current = now or datetime.now(timezone.utc)
    cutoff = get_calendar().latest_market_price_session(current.astimezone(SHANGHAI)).isoformat()
    seed_path = ROOT / "config" / "ths-industry-boards.json"
    attempts: list[dict[str, Any]] = []
    directory_mode = "live"
    live_directory_error = None
    try:
        import akshare as ak
        names = ak.stock_board_industry_name_ths()
        name_rows = [{"name": str(row["name"]).strip(), "code": str(row["code"]).strip()} for _, row in names.iterrows()]
        if not name_rows:
            raise ValueError("同花顺在线行业目录为空")
        attempts.append({"dataset": "directory", "subject": "ths_industry",
                         "endpoint": "akshare.stock_board_industry_name_ths", "provenance": "live",
                         "status": "available", "row_count": len(name_rows), "error": None})
    except Exception as exc:
        live_directory_error = _source_error(exc)
        attempts.append({"dataset": "directory", "subject": "ths_industry",
                         "endpoint": "akshare.stock_board_industry_name_ths", "provenance": "live",
                         "status": "failed", "row_count": 0, "error": live_directory_error})
        try:
            name_rows = json.loads(seed_path.read_text(encoding="utf-8"))
            if not isinstance(name_rows, list) or not name_rows:
                raise ValueError("本地同花顺行业种子目录为空或结构无效")
            directory_mode = "seed"
            attempts.append({"dataset": "directory", "subject": "ths_industry",
                             "endpoint": "config/ths-industry-boards.json", "provenance": "seed",
                             "status": "available", "row_count": len(name_rows), "error": None})
        except Exception as fallback_exc:
            attempts.append({"dataset": "directory", "subject": "ths_industry",
                             "endpoint": "config/ths-industry-boards.json", "provenance": "seed",
                             "status": "failed", "row_count": 0, "error": _source_error(fallback_exc)})
            raise RuntimeError(
                f"同花顺行业目录读取失败：{type(exc).__name__}: {exc}；"
                f"本地种子目录也不可用：{type(fallback_exc).__name__}: {fallback_exc}"
            ) from exc
    turnover_by_name: dict[str, Decimal] = {}
    summary_error = None
    try:
        import akshare as ak
        summary = ak.stock_board_industry_summary_ths()
        if len(summary) == 0:
            raise ValueError("同花顺行业摘要为空")
        for _, row in summary.iterrows():
            name = str(row["板块"]).strip()
            try:
                turnover_by_name[name] = Decimal(str(row["总成交额"])) * Decimal("100000000")
            except (InvalidOperation, ValueError):
                turnover_by_name[name] = Decimal(0)
        attempts.append({"dataset": "industry_summary", "subject": "ths_industry",
                         "endpoint": "akshare.stock_board_industry_summary_ths", "provenance": "live",
                         "status": "available", "row_count": len(turnover_by_name), "error": None})
    except Exception as exc:
        summary_error = _source_error(exc)
        turnover_by_name = {}
        attempts.append({"dataset": "industry_summary", "subject": "ths_industry",
                         "endpoint": "akshare.stock_board_industry_summary_ths", "provenance": "live",
                         "status": "failed", "row_count": 0, "error": summary_error})
    rows = []
    for raw in name_rows:
        name, code = str(raw["name"]).strip(), str(raw["code"]).strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError(f"同花顺行业代码无效：{name} {code}")
        rows.append(_enrich_ths_identity({
            "code": code, "name": name, "kind": "行业", "source_id": "sector_daily.ths",
            "heat_value": format(turnover_by_name.get(name, Decimal(0)), "f"),
            "heat_updated_at": current.astimezone(SHANGHAI).isoformat(timespec="seconds"),
            "ranking_as_of": cutoff,
        }, category="industry", page_code=code, market_code=code, prefix="bk"))
    ranked = [dict(row, heat_rank=rank) for rank, row in enumerate(
        sorted(rows, key=lambda item: (-Decimal(item["heat_value"]), item["code"])), 1)]
    return {
        "rows": ranked,
        "provenance": {
            "directory": {
                "mode": directory_mode,
                "endpoint": ("akshare.stock_board_industry_name_ths" if directory_mode == "live"
                             else "config/ths-industry-boards.json"),
                "live_error": live_directory_error,
            },
            "summary": {
                "status": "failed" if summary_error else "available",
                "endpoint": "akshare.stock_board_industry_summary_ths",
                "error": summary_error,
            },
        },
        "source_attempts": attempts,
    }


def fetch_ths_concept_directory(*, now: datetime | None = None) -> dict[str, Any]:
    """Return Tonghuashun concept boards available for manual watch-list additions."""
    current = now or datetime.now(timezone.utc)
    cutoff = get_calendar().latest_market_price_session(current.astimezone(SHANGHAI)).isoformat()
    attempts: list[dict[str, Any]] = []
    try:
        import akshare as ak
        concepts = ak.stock_board_concept_name_ths()
        rows = [{"name": str(row["name"]).strip(), "code": str(row["code"]).strip()} for _, row in concepts.iterrows()]
        if not rows:
            raise ValueError("同花顺概念目录为空")
        attempts.append({"dataset": "directory", "subject": "ths_concept",
                         "endpoint": "akshare.stock_board_concept_name_ths", "provenance": "live",
                         "status": "available", "row_count": len(rows), "error": None})
    except Exception as exc:
        attempts.append({"dataset": "directory", "subject": "ths_concept",
                         "endpoint": "akshare.stock_board_concept_name_ths", "provenance": "live",
                         "status": "failed", "row_count": 0, "error": _source_error(exc)})
        raise RuntimeError(f"同花顺概念目录读取失败：{type(exc).__name__}: {exc}") from exc
    result = []
    for raw in rows:
        name, code = raw["name"], raw["code"]
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError(f"同花顺概念代码无效：{name} {code}")
        result.append(_enrich_ths_identity({
            "code": code, "name": name, "kind": "概念", "source_id": "sector_daily.ths",
            "heat_value": "0",
            "heat_updated_at": current.astimezone(SHANGHAI).isoformat(timespec="seconds"),
            "ranking_as_of": cutoff,
        }, category="concept", page_code=code, market_code=code, prefix="bk"))
    return {"rows": result, "source_attempts": attempts}


def _parse_ths_standard_sector_links(html: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    pattern = re.compile(
        r"q\.10jqka\.com\.cn/(gn|thshy|zs)/detail/code/([^/]+)/[^>]*>([^<]+)</a>",
        re.IGNORECASE,
    )
    for match in pattern.finditer(html):
        path, page_code, raw_name = match.groups()
        category = THS_PATH_CATEGORY[path.lower()]
        name = " ".join(unescape(raw_name).split())
        if not re.fullmatch(THS_CODE_RE, page_code, re.IGNORECASE) or not name or "\ufffd" in name:
            continue
        meta = THS_CATEGORY_META[category]
        market_code = page_code if category != "concept" or _is_direct_ths_concept_market_code(page_code) else ""
        key = (category, page_code, name)
        if key in seen:
            continue
        seen.add(key)
        rows.append(_enrich_ths_identity({
            "code": page_code,
            "name": name,
            "source_id": "sector_daily.ths",
        }, category=category, page_code=page_code, market_code=market_code or page_code,
           prefix=meta["prefix"]))
        if category == "concept" and not market_code:
            rows[-1]["ths_market_code"] = ""
            rows[-1]["history_source_code"] = ""
    parser = _ThsDirectoryInputs()
    parser.feed(html)
    native = parser.concepts
    for item in rows:
        quote = native.get(item.get("ths_page_code"))
        if quote and item["ths_category"] == "concept":
            item.update(_enrich_ths_identity(item, category="concept", page_code=quote["cid"],
                                             market_code=quote["platecode"], prefix="bk"))
            item["code"] = quote["platecode"]
            item["aliases"] = list({item["name"], quote["platename"]})
    represented = {item.get("ths_page_code") for item in rows if item["ths_category"] == "concept"}
    for page_code, quote in native.items():
        if page_code not in represented:
            item = _enrich_ths_identity({"code": quote["platecode"], "name": quote["platename"]},
                category="concept", page_code=page_code, market_code=quote["platecode"], prefix="bk")
            rows.append(item)
    return rows


class _ThsDirectoryInputs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.concepts: dict[str, dict] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "input" and values.get("id") == "gnSection":
            root = json.loads(values.get("value") or "{}")
            for quote in root.values():
                if re.fullmatch(r"\d{6}", str(quote.get("cid", ""))) and _is_direct_ths_concept_market_code(str(quote.get("platecode", ""))):
                    self.concepts[quote["cid"]] = quote


def _parse_ths_page_count(html: str) -> int:
    match = re.search(
        r"<span[^>]*class\s*=\s*['\"][^'\"]*page_info[^'\"]*['\"][^>]*>\s*\d+\s*/\s*(\d+)\s*</span>",
        html,
        re.IGNORECASE,
    )
    if match:
        return max(1, min(100, int(match.group(1))))
    pages = [int(value) for value in re.findall(r"\bpage\s*=\s*['\"](\d+)['\"]", html, re.IGNORECASE)]
    return max([1, *pages])


def _parse_ths_market_code_from_detail_html(html: str) -> str:
    for pattern in (
        r"id\s*=\s*['\"]clid['\"][^>]*value\s*=\s*['\"]([^'\"]+)['\"]",
        r"value\s*=\s*['\"]([^'\"]+)['\"][^>]*id\s*=\s*['\"]clid['\"]",
    ):
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def resolve_ths_concept_market_code(page_code: str, *, timeout: float = 12.0) -> str:
    if _is_direct_ths_concept_market_code(page_code):
        return page_code
    if not re.fullmatch(r"\d{6}", page_code):
        raise ValueError("同花顺概念页面代码无效")
    html = fetch_text(f"https://q.10jqka.com.cn/gn/detail/code/{page_code}/", timeout,
                      referer="https://q.10jqka.com.cn/gn/", headers={"Cookie": _ths_cookie()})
    market_code = _parse_ths_market_code_from_detail_html(html)
    if not _is_direct_ths_concept_market_code(market_code):
        raise ValueError("同花顺概念详情页未提供有效行情代码")
    return market_code


@lru_cache(maxsize=1)
def _fetch_ths_standard_sector_entries_for_hour(hour: int) -> tuple[dict[str, Any], ...]:
    cached = read_identity_cache("sectors")
    if cached:
        return tuple(cached)
    entries: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    sources = (
        ("industry", "https://q.10jqka.com.cn/thshy/",
         "https://q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/{page}/ajax/1/"),
        ("concept", "https://q.10jqka.com.cn/gn/",
         "https://q.10jqka.com.cn/gn/index/field/addtime/order/desc/page/{page}/ajax/1/"),
        ("index", "https://q.10jqka.com.cn/zs/",
         "https://q.10jqka.com.cn/zs/index/field/199112/order/desc/page/{page}/ajax/1/"),
    )
    for category, first_url, page_url in sources:
        meta = THS_CATEGORY_META[category]
        try:
            first = fetch_text(first_url, 12.0, referer=first_url, headers={"Cookie": _ths_cookie()})
            page_count = _parse_ths_page_count(first)
            category_entries = _parse_ths_standard_sector_links(first)
            # The category navigation and gnSection carry the complete directory.
            for page in (range(2, page_count + 1) if category == "index" else ()):
                try:
                    category_entries.extend(_parse_ths_standard_sector_links(
                        fetch_text(page_url.format(page=page).replace("ajax/1/", ""), 12.0, referer=first_url,
                                   headers={"Cookie": _ths_cookie()}),
                    ))
                except Exception:
                    continue
            entries.extend(item for item in category_entries if item.get("ths_category") == category)
            attempts.append({"dataset": "directory", "subject": f"ths_{category}",
                             "endpoint": first_url, "provenance": "live",
                             "status": "available", "row_count": len(category_entries), "error": None})
        except Exception as exc:
            attempts.append({"dataset": "directory", "subject": f"ths_{category}",
                             "endpoint": first_url, "provenance": "live",
                             "status": "failed", "row_count": 0, "error": _source_error(exc)})
            if category == "industry":
                continue
        for item in entries:
            item.setdefault("kind", meta["kind"])
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    # Funding rankings sometimes retain valid boards omitted from navigation.
    try:
        first_url = "https://data.10jqka.com.cn/funds/gnzjl/"
        first = fetch_text(first_url, 12, headers={"Cookie": _ths_cookie()})
        entries.extend(_parse_ths_standard_sector_links(first))
        for page in range(2, _parse_ths_page_count(first) + 1):
            entries.extend(_parse_ths_standard_sector_links(fetch_text(
                f"https://data.10jqka.com.cn/funds/gnzjl/field/je/order/desc/page/{page}/", 12,
                headers={"Cookie": _ths_cookie()})))
    except Exception as exc:
        attempts.append({"dataset": "directory", "subject": "ths_concept_funding", "error": _source_error(exc)})
    for item in entries:
        key = (item["ths_category"], item["ths_page_code"])
        if key in unique:
            aliases = set(unique[key].get("aliases", []) + item.get("aliases", []))
            aliases.update((unique[key]["name"], item["name"]))
            unique[key]["aliases"] = sorted(aliases)
        else:
            unique[key] = item
    pending = [key for key, item in unique.items() if item["ths_category"] == "concept" and not item.get("ths_market_code")]
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(resolve_ths_board_identity, unique[key]): key for key in pending}
        for future in as_completed(futures):
            key = futures[future]
            try:
                item = future.result()
                item["code"] = item["ths_market_code"]
                unique[key] = item
            except Exception as exc:
                unique[key]["identity_resolution_error"] = _source_error(exc)
    rows = list(unique.values())
    if rows:
        save_identity_cache("sectors", rows)
    return tuple(rows)


def _fetch_ths_standard_sector_entries_cached() -> tuple[dict[str, Any], ...]:
    return _fetch_ths_standard_sector_entries_for_hour(int(time.time() // 3600))


def fetch_ths_standard_sector_directory(*, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    cutoff = get_calendar().latest_market_price_session(current.astimezone(SHANGHAI)).isoformat()
    rows = []
    for entry in _fetch_ths_standard_sector_entries_cached():
        entry = dict(entry)
        internal_index = re.fullmatch(r"[12][AB](\d{4})", str(entry.get("ths_page_code", "")), re.IGNORECASE)
        if entry.get("ths_category") == "index" and internal_index:
            entry["aliases"] = list(dict.fromkeys([*entry.get("aliases", []), f"00{internal_index.group(1)}"]))
        rows.append({
            **entry,
            "heat_value": "0",
            "heat_updated_at": current.astimezone(SHANGHAI).isoformat(timespec="seconds"),
            "ranking_as_of": cutoff,
        })
    return {"rows": rows, "source_attempts": []}


def resolve_ths_board_identity(item: dict[str, Any], *, timeout: float = 12.0) -> dict[str, Any]:
    page_code = str(item.get("ths_page_code") or item.get("code") or "").strip()
    needs_concept_resolution = (
        str(item.get("ths_category") or "").strip() == "concept"
        and not str(item.get("ths_market_code") or "").strip()
        and not _is_direct_ths_concept_market_code(page_code)
    )
    enriched = _enrich_ths_identity(item)
    if needs_concept_resolution:
        market_code = resolve_ths_concept_market_code(enriched["ths_page_code"], timeout=timeout)
        enriched = _enrich_ths_identity(enriched, category="concept", page_code=enriched["ths_page_code"],
                                        market_code=market_code, prefix="bk")
    return enriched


def fetch_ths_board_identity_by_code(code: str, *, timeout: float = 12.0) -> dict[str, Any]:
    code = str(code or "").strip().upper()
    if not re.fullmatch(THS_CODE_RE, code):
        raise ValueError("同花顺板块代码必须为六位代码")
    for item in _fetch_ths_standard_sector_entries_cached():
        if code in (item.get("code"), item.get("ths_page_code"), item.get("ths_market_code")):
            return resolve_ths_board_identity(item, timeout=timeout)
    attempts = []
    preferred = [("industry", "bk")] if code.startswith("881") else [("index", "zs")]
    probes = [*preferred, ("concept", "bk"), ("index", "zs"), ("index", "hs"), ("index", "16")]
    for category, prefix in probes:
        source_code = _ths_history_source_code(prefix, code)
        if source_code in attempts:
            continue
        attempts.append(source_code)
        try:
            payload = fetch_ths_line_text_by_source(source_code, timeout=timeout)
            left, right = payload.find("{"), payload.rfind("}")
            if left < 0 or right <= left:
                raise ValueError("同花顺板块日线JSONP无效")
            root = json.loads(payload[left:right + 1], parse_float=str, parse_int=str)
            name = str(root.get("name", "")).strip()
            if not name:
                raise ValueError("同花顺未返回板块名称")
            return _enrich_ths_identity({"code": code, "name": name, "source_id": "sector_daily.ths"},
                                        category=category, page_code=code, market_code=code, prefix=prefix)
        except Exception:
            continue
    raise ValueError("同花顺未返回可用板块身份")


def _resolve_search_identity(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("ths_category") == "concept" and not item.get("ths_market_code"):
        try:
            resolved = resolve_ths_board_identity(item)
            return {**resolved, "code": resolved["ths_market_code"]}
        except Exception:
            return item
    return item


def search_ths_boards(query: str, *, now: datetime | None = None, limit: int = 30) -> dict[str, Any]:
    """Search Tonghuashun industry, concept and index directories by name or code."""
    term = query.strip()
    if not term:
        raise ValueError("请输入板块名称或代码")
    if len(term) > 100:
        raise ValueError("搜索词过长")
    qualified_code = re.fullmatch(r"(?:bk_|zs_|hs_|16_|17_|33_|sh|sz|20:|36:)([0-9A-Z]{6})", term, re.IGNORECASE)
    if qualified_code:
        term = qualified_code.group(1).upper()
    lowered = term.lower()
    relaxed_terms = {lowered}
    relaxed = lowered
    for noise in ("资源", "指数", "概念", "板块", "行业"):
        relaxed = relaxed.replace(noise, "")
    if len(relaxed) >= 2:
        relaxed_terms.add(relaxed)
    candidates: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        result = fetch_ths_standard_sector_directory(now=now)
        candidates.extend(result["rows"])
        attempts.extend(result.get("source_attempts", []))
    except Exception as exc:
        errors.append(str(exc))
    if not candidates:
        for loader in (fetch_ths_industry_directory, fetch_ths_concept_directory):
            try:
                result = loader(now=now)
                candidates.extend(result["rows"])
                attempts.extend(result.get("source_attempts", []))
            except Exception as exc:
                errors.append(str(exc))
    try:
        candidates.extend(fetch_ths_etf_directory())
    except Exception as exc:
        errors.append(f"同花顺ETF目录：{exc}")
        candidates.extend(read_identity_cache("etfs", max_age=None))
    if not candidates:
        candidates.extend(read_identity_cache("sectors", max_age=None))
    if not candidates and errors:
        raise RuntimeError("；".join(errors))

    def matched(item: dict[str, Any]) -> bool:
        values = [item.get(key, "") for key in ("name", "code", "ths_page_code", "ths_market_code", "history_source_code")]
        values.extend(item.get("aliases", []))
        return any(normalize_query(value) in normalize_query(str(candidate)) for value in relaxed_terms for candidate in values if candidate)

    def rank(item: dict[str, Any]) -> tuple[int, int, str]:
        exact = any(normalize_query(str(value)) == normalize_query(term) for value in (
            item["code"], item["name"], item.get("ths_page_code", ""), item.get("ths_market_code", ""),
            *item.get("aliases", [])))
        starts = item["code"].startswith(term) or any(item["name"].lower().startswith(value) for value in relaxed_terms)
        kind_order = {"行业": 0, "概念": 1, "指数": 2, "ETF": 3}.get(item["kind"], 4)
        return (0 if exact else 1 if starts else 2, kind_order, item["code"])

    seen: set[tuple[str, str]] = set()
    items = []
    for item in sorted((item for item in candidates if matched(item)), key=rank):
        key = (item["source_id"], item["code"])
        if key in seen:
            continue
        seen.add(key)
        resolved = _resolve_search_identity(item)
        keys = (
            "code", "name", "kind", "source_id", "ths_category", "ths_page_code", "ths_market_code",
            "ths_prefix", "history_source_id", "history_source_code", "history_identity_match",
            "aliases",
        )
        items.append({key: resolved[key] for key in keys if key in resolved and resolved.get(key) != ""})
        if len(items) >= limit:
            break
    if re.fullmatch(THS_CODE_RE, term, flags=re.IGNORECASE) and not any(
        term.upper() in (str(item.get(key, "")).upper() for key in ("code", "ths_page_code", "ths_market_code"))
        or normalize_query(term) in [normalize_query(alias) for alias in item.get("aliases", [])] for item in items
    ):
        try:
            item = fetch_ths_board_identity_by_code(term)
            items.insert(0, item)
            attempts.append({"dataset": "daily_identity", "subject": term,
                             "endpoint": "https://d.10jqka.com.cn/v4/line/",
                             "provenance": "live", "status": "available",
                             "row_count": 1, "error": None})
        except Exception as exc:
            attempts.append({"dataset": "daily_identity", "subject": term,
                             "endpoint": "https://d.10jqka.com.cn/v4/line/",
                             "provenance": "live", "status": "failed",
                             "row_count": 0, "error": _source_error(exc)})
    return {"items": items, "source_attempts": attempts, "errors": errors}


def _stock_market(code: str) -> int:
    return 1 if code.startswith("6") else 0


def _market_cap(value: str) -> str | None:
    cleaned = value.replace(",", "").replace(" ", "")
    if cleaned in ("", "-", "--"):
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(万|亿)?(?:元)?", cleaned)
    if not match:
        return None
    scale = {None: Decimal(1), "万": Decimal(10_000), "亿": Decimal(100_000_000)}[match.group(2)]
    return format(Decimal(match.group(1)) * scale, "f")


def _parse_ths_constituent_page(html: str, *, expected_page: int) -> tuple[int, list[dict[str, Any]]]:
    parser = _ThsConstituentParser()
    parser.feed(html)
    rows = []
    for cells in parser.rows:
        if len(cells) < 14 or not cells[0].isdigit():
            continue
        order, code, name = int(cells[0]), cells[1], cells[2].strip()
        if not re.fullmatch(r"\d{6}", code) or not name:
            raise ValueError(f"同花顺普通分页第{expected_page}页成分身份无效")
        rows.append({"stock_code": code, "stock_name": name, "market": _stock_market(code),
                     "source_order": order, "market_cap": _market_cap(cells[12])})
    # Tonghuashun omits the pager entirely for industries that fit on one page.
    if parser.page_position is None:
        if expected_page == 1 and rows:
            return 1, rows
        if "chameleon" in html.lower():
            raise ValueError(f"同花顺普通分页第{expected_page}页触发访问限制")
        raise ValueError(f"同花顺普通分页第{expected_page}页缺少页数标记")
    current_page, page_count = parser.page_position
    if current_page != expected_page or not expected_page <= page_count <= 100:
        raise ValueError(
            f"同花顺普通分页位置无效：请求第{expected_page}页，响应{current_page}/{page_count}"
        )
    return page_count, rows


def fetch_ths_visible_constituents(member: dict[str, Any], *, timeout: float = 12.0,
                                   throttle_seconds: float = 0.5) -> list[dict[str, Any]]:
    """Read every ordinary HTML page; never use the blocked ``ajax/1`` route."""
    code = str(member["code"])
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("同花顺行业代码无效")
    headers = {"Cookie": _ths_cookie()}
    rows: list[dict[str, Any]] = []
    page_count: int | None = None
    for page in range(1, 101):
        if page > 1 and throttle_seconds:
            time.sleep(throttle_seconds)
        category = str(member.get("ths_category") or ("concept" if member.get("kind") == "概念" else "industry"))
        path = THS_CATEGORY_META.get(category, THS_CATEGORY_META["industry"])["path"]
        page_code = str(member.get("ths_page_code") or code)
        url = f"https://q.10jqka.com.cn/{path}/detail/page/{page}/code/{page_code}/"
        parsed_count, page_rows = _parse_ths_constituent_page(
            fetch_text(url, timeout, headers=headers), expected_page=page,
        )
        if page_count is None:
            page_count = parsed_count
        elif parsed_count != page_count:
            raise ValueError(f"同花顺普通分页总页数在采集中变化：{page_count}→{parsed_count}")
        if not page_rows:
            raise ValueError(f"同花顺普通分页声明{page_count}页，但第{page}页为空；已取得{len(rows)}只")
        rows.extend(page_rows)
        if page == page_count:
            break
    if page_count is None or len(rows) == 0:
        raise ValueError("同花顺普通分页未返回成分")
    expected_orders = list(range(1, len(rows) + 1))
    actual_orders = [row["source_order"] for row in rows]
    if actual_orders != expected_orders:
        raise ValueError("同花顺普通分页成分序号不连续，拒绝将截断结果当作完整名单")
    codes = [row["stock_code"] for row in rows]
    if len(set(codes)) != len(codes):
        raise ValueError("同花顺普通分页包含重复证券代码")
    return rows


def _constituent_endpoint(member: dict[str, Any]) -> str:
    category = str(member.get("ths_category") or ("concept" if member.get("kind") == "概念" else "industry"))
    path = THS_CATEGORY_META.get(category, THS_CATEGORY_META["industry"])["path"]
    return f"https://q.10jqka.com.cn/{path}/detail/page/{{page}}/code/"


def collect_ths_board(member: dict[str, Any], *, previous: dict[str, Any],
                      current: datetime, attempted_at: str, timeout: float = 12.0,
                      throttle_seconds: float = 0.5,
                      now: datetime | None = None) -> dict[str, Any]:
    """Collect one Tonghuashun board without touching the rest of the snapshot."""
    member = {**member, "price_cutoff": get_calendar().latest_market_price_session(current).isoformat(),
              "quote_requested_at": current.isoformat() if now is not None else None}
    old = previous.get(member["code"], {})
    if old.get("name") != member["name"]:
        old = {}
    rows, updated_at, error = old.get("rows", []), old.get("updated_at"), None
    old_membership = old.get("membership", {})
    components = old_membership.get("members", []) if old_membership.get("as_of") == member["ranking_as_of"] else []
    component_updated_at = old_membership.get("fetched_at") if components else None
    attempts: list[dict] = []
    resolution_error = None
    try:
        member = resolve_ths_board_identity(member, timeout=timeout)
    except Exception as exc:
        resolution_error = f"{type(exc).__name__}: {exc}"
    native_source_id = "sector_daily.ths"
    native_source_code = _member_history_source_code(member, member["code"])
    quote_version = "v6" if native_source_code.startswith(("17_", "33_")) else "v4"
    native = old.get("history_source_id") == native_source_id and old.get("history_source_code") == native_source_code
    fetched: list[dict[str, Any]] = []
    try:
        if resolution_error:
            raise ValueError(f"同花顺板块身份解析失败：{resolution_error}")
        fetched = fetch_ths_sector_daily(member, member["code"], timeout=timeout)
        cutoff = date.fromisoformat(member["price_cutoff"])
        quality = get_calendar().validate_grid(
            [row["date"] for row in fetched], end=cutoff, minimum=121,
        )
        if quality["status"] != "ready":
            raise SectorHistoryIntegrityError(f"同花顺日线交易日校验失败：{quality['errors']}")
        if native and rows:
            candidate_rows, merge_details = merge_consistent_sector_history(rows, fetched)
        else:
            candidate_rows = fetched
            merge_details = {
                "previous_row_count": len(rows) if native else 0,
                "fetched_row_count": len(fetched),
                "overlap_row_count": 0,
                "preserved_row_count": 0,
                "merged_row_count": len(fetched),
            }
        merged_quality = get_calendar().validate_grid(
            [row["date"] for row in candidate_rows], end=cutoff, minimum=121,
        )
        if merged_quality["status"] != "ready":
            raise SectorHistoryIntegrityError(
                f"合并后的同花顺日线交易日校验失败：{merged_quality['errors']}"
            )
        rows = candidate_rows
        attempts.append({"dataset": "daily", "subject": member["code"],
                         "endpoint": f"https://d.10jqka.com.cn/{quote_version}/line/{native_source_code}/01/last.js",
                         "status": "available", "row_count": len(fetched),
                         "first_date": fetched[0]["date"], "last_date": fetched[-1]["date"],
                         "error": None, **merge_details})
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds") if now is None else attempted_at
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        attempts.append({"dataset": "daily", "subject": member["code"],
                         "endpoint": f"https://d.10jqka.com.cn/{quote_version}/line/{native_source_code}/01/last.js",
                         "status": "failed",
                         "row_count": len(fetched),
                         "first_date": fetched[0]["date"] if fetched else None,
                         "last_date": fetched[-1]["date"] if fetched else None,
                         "error": error})
        if native and rows and not isinstance(exc, SectorHistoryIntegrityError) and not (
            member["price_cutoff"] == current.astimezone(SHANGHAI).date().isoformat()
        ):
            quality = get_calendar().validate_grid(
                [row["date"] for row in rows],
                end=date.fromisoformat(member["price_cutoff"]), minimum=121,
            )
            if quality["status"] == "ready":
                error = None
        if error:
            error += f"；当前保留旧同花顺行情至{rows[-1]['date']}" if rows else "；暂无可用日线"
    component_error = None
    try:
        components = [] if member.get("ths_category") == "etf" else fetch_ths_visible_constituents(
            member, timeout=timeout, throttle_seconds=throttle_seconds,
        )
        attempts.append({"dataset": "constituents", "subject": member["code"],
                         "endpoint": None if member.get("ths_category") == "etf" else _constituent_endpoint(member),
                         "status": "not_collected" if member.get("ths_category") == "etf" else "available",
                         "row_count": len(components), "error": None})
        component_updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds") if now is None else attempted_at
    except Exception as exc:
        component_error = f"{type(exc).__name__}: {exc}"
        component_error += f"；当前保留{len(components)}个旧同花顺成分" if components else "；暂无可用成分"
        attempts.append({"dataset": "constituents", "subject": member["code"],
                         "endpoint": _constituent_endpoint(member),
                         "status": "failed", "row_count": 0, "error": component_error})
    return {**member, "rows": rows, "updated_at": updated_at, "collection_error": error,
            "history_source_id": native_source_id, "history_source_code": native_source_code,
            "history_identity_match": "native_source_code",
            "constituents": components, "constituent_error": component_error,
            "constituent_updated_at": component_updated_at, "source_attempts": attempts}


def _existing_member_for_save(item: dict[str, Any]) -> dict[str, Any]:
    membership = item.get("membership", {})
    return {**item, "heat_value": str(item.get("heat_value", "0")),
            "constituents": membership.get("members", []),
            "constituent_error": membership.get("error") if membership.get("status") == "failed" else None,
            "constituent_updated_at": membership.get("fetched_at")}


def refresh_selected_ths_boards(settings: Settings, boards: list[dict[str, Any]], *,
                                timeout: float = 12.0, throttle_seconds: float = 0.5,
                                now: datetime | None = None,
                                progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Refresh only selected manually-added Tonghuashun boards and merge them into the local library."""
    if not boards:
        raise ValueError("请选择至少一个板块")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("采集时间必须包含时区")
    attempted_at = current.isoformat(timespec="seconds")
    snapshot = read_sector_heat(settings)
    price_as_of = get_calendar().latest_market_price_session(current).isoformat()
    ranking_as_of = max(snapshot["universe"].get("ranking_as_of") or price_as_of, price_as_of)
    previous = {item["code"]: item for item in snapshot["items"]}
    total = len(boards)
    results = []
    for index, board in enumerate(boards, 1):
        if progress:
            progress({"stage": "market", "state": "running", "percent": 10,
                      "message": f"正在采集 {board['name']} 日线与成分。", "current": index - 1, "total": total})
        member = {**board, "heat_value": "0", "heat_updated_at": current.astimezone(SHANGHAI).isoformat(timespec="seconds"),
                  "ranking_as_of": ranking_as_of, "heat_rank": 0}
        result = collect_ths_board(member, previous=previous, current=current, attempted_at=attempted_at,
                                   timeout=timeout, throttle_seconds=throttle_seconds, now=now)
        if result.get("rows"):
            result["heat_value"] = result["rows"][-1].get("amount", result.get("heat_value", "0"))
            result["heat_updated_at"] = result["rows"][-1]["date"]
        results.append(result)
        if progress:
            progress({"stage": "market", "state": "running", "percent": 20 + round(70 * index / total),
                      "message": f"{board['name']} 采集完成，正在合并板块库。", "current": index, "total": total})
    by_code = {item["code"]: _existing_member_for_save(item) for item in snapshot["items"]}
    by_code.update({item["code"]: item for item in results})
    merged = list(by_code.values())
    merged.sort(key=lambda item: (-Decimal(str(item.get("heat_value", "0"))), item["code"]))
    for rank, item in enumerate(merged, 1):
        item["heat_rank"] = rank
    completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds") if now is None else attempted_at
    provenance = snapshot.get("directory_provenance")
    save_sector_heat(settings, as_of=ranking_as_of, updated_at=completed_at,
                     catalog_count=max(snapshot["universe"].get("catalog_count", 0), len(merged)),
                     members=merged, requested_count=len(merged),
                     universe_scope="verified_industry_all", directory_provenance=provenance)
    saved = read_sector_heat(settings)
    saved["collector_version"] = COLLECTOR_VERSION
    saved["source_attempts"] = [attempt for item in results for attempt in item["source_attempts"]]
    return saved


def refresh_sector_heat(settings: Settings, *, fetcher: Fetcher | None = None, timeout: float = 12.0,
                        workers: int = 2, throttle_seconds: float = 0.5,
                        budget_seconds: float | None = None,
                        progress: Callable[[str], None] | None = None,
                        now: datetime | None = None, industry_only: bool = False) -> dict[str, Any]:
    """Collect the Tonghuashun industry universe, daily prices and visible constituents."""
    budget = budget_seconds if budget_seconds is not None else 620
    if not 0 < timeout <= 12 or not 1 <= workers <= 3 or throttle_seconds < 0 or not 60 <= budget <= 840:
        raise ValueError("超时须在(0,12]秒，并发须在1–3，节流间隔不能为负，预算须在60–840秒")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("采集时间必须包含时区")
    attempted_at = current.isoformat(timespec="seconds")
    source_outcomes: list[dict] = []
    previous_snapshot = read_sector_heat(settings)
    try:
        directory_result = fetch_ths_industry_directory(now=current)
        if (not isinstance(directory_result, dict) or not isinstance(directory_result.get("rows"), list)
                or not isinstance(directory_result.get("provenance"), dict)
                or not isinstance(directory_result.get("source_attempts"), list)):
            raise ValueError("同花顺行业目录结果缺少来源证明")
        directory = directory_result["rows"]
        directory_provenance = directory_result["provenance"]
        source_outcomes.extend(directory_result["source_attempts"])
        if not directory:
            raise ValueError("同花顺行业目录没有可发布的行业")
        removed = removed_subject_keys(settings)
        directory = [
            item for item in directory
            if ("hot_board", item["source_id"], item["code"]) not in removed
        ]
        if not directory:
            raise ValueError("同花顺行业目录已全部被用户移除")
        old_ranking_date = previous_snapshot["universe"]["ranking_as_of"]
        if old_ranking_date and directory[0]["ranking_as_of"] < old_ranking_date:
            raise ValueError("来源排名日期早于已保存排名，不能用旧榜覆盖新榜")
        custom_boards = list_custom_boards(settings)
        legacy_index_boards = list_manual_index_projections_as_ths_boards(settings)
        if legacy_index_boards:
            remove_sector_subjects(settings, [
                {"universe_type": "tracked_index", "source_id": board["legacy_index_source_id"],
                 "code": board["code"], "name": board["name"]}
                for board in legacy_index_boards
            ])
        existing_keys = {(item["source_id"], item["code"]) for item in directory}
        resolved_extra_boards = []
        for board in [*custom_boards, *legacy_index_boards]:
            try:
                resolved_extra_boards.append(resolve_ths_board_identity(board, timeout=timeout))
            except Exception:
                resolved_extra_boards.append(board)
        for board in resolved_extra_boards:
            key = (board["source_id"], board["code"])
            if ("hot_board", board["source_id"], board["code"]) in removed:
                continue
            if key in existing_keys:
                continue
            directory.append({
                "code": board["code"],
                "name": board["name"],
                "kind": board["kind"],
                "source_id": board["source_id"],
                "ths_category": board.get("ths_category"),
                "ths_page_code": board.get("ths_page_code"),
                "ths_market_code": board.get("ths_market_code"),
                "ths_prefix": board.get("ths_prefix"),
                "history_source_id": board.get("history_source_id"),
                "history_source_code": board.get("history_source_code"),
                "history_identity_match": board.get("history_identity_match"),
                "heat_value": "0",
                "heat_updated_at": current.astimezone(SHANGHAI).isoformat(timespec="seconds"),
                "ranking_as_of": directory[0]["ranking_as_of"],
                "custom": True,
            })
            existing_keys.add(key)
    except Exception as exc:
        record_sector_heat_failure(settings, attempted_at=attempted_at,
                                   error=f"同花顺行业目录刷新失败，保留上次快照：{type(exc).__name__}: {exc}")
        raise RuntimeError(f"同花顺行业目录刷新失败，未替换原排名：{exc}") from exc
    previous = {item["code"]: item for item in previous_snapshot["items"]}
    members = [dict(item, heat_rank=rank) for rank, item in enumerate(directory, 1)]
    if progress:
        progress(f"同花顺板块/概念清单 {len(directory)} 个，开始采集日线。")

    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(collect_ths_board, member, previous=previous, current=current,
                                   attempted_at=attempted_at, timeout=timeout,
                                   throttle_seconds=throttle_seconds, now=now)
                   for member in members]
        for future in as_completed(futures):
            results.append(future.result())
            if progress and (len(results) % 10 == 0 or len(results) == len(members)):
                price_failures = sum(item["collection_error"] is not None for item in results)
                member_failures = sum(item["constituent_error"] is not None for item in results)
                progress(
                    f"同花顺板块采集 {len(results)}/{len(members)}，"
                    f"失败或旧行情 {price_failures}，成分不完整 {member_failures}。"
                )
    for item in results:
        if item.get("rows"):
            item["heat_value"] = item["rows"][-1].get("amount", item.get("heat_value", "0"))
            item["heat_updated_at"] = item["rows"][-1]["date"]
    results.sort(key=lambda item: (-Decimal(item["heat_value"]), item["code"]))
    for rank, item in enumerate(results, 1):
        item["heat_rank"] = rank
    completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds") if now is None else attempted_at
    save_sector_heat(settings, as_of=members[0]["ranking_as_of"], updated_at=completed_at,
                     catalog_count=len(directory), members=results, requested_count=len(members),
                     universe_scope="verified_industry_all", directory_provenance=directory_provenance)
    saved = read_sector_heat(settings)
    saved["collector_version"] = COLLECTOR_VERSION
    completed = get_calendar().latest_market_price_session(current)
    saved["price_cutoff"] = completed.isoformat()
    saved["price_window_coverage"] = {
        key: sum(not item.get("collection_error") and bool(item.get("rows"))
                 and get_calendar().validate_grid([row["date"] for row in item["rows"][-window - 1:]],
                     end=completed, minimum=window + 1)["status"] == "ready" for item in saved["items"])
        for key, window in (("short", 20), ("medium", 60), ("long", 120))
    }
    saved["source_attempts"] = source_outcomes + [attempt for item in results for attempt in item["source_attempts"]]
    return saved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database")
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--industry-only", action="store_true", help="兼容旧命令参数；当前始终采集同花顺完整行业目录")
    args = parser.parse_args(argv)
    settings = Settings(database_path=Path(args.database)) if args.database else Settings.from_env()
    from backend.storage.collection_runs import start_run, finish_run, record_attempt
    from backend.integrations.http_transport import set_observer
    run_id = None
    try:
        run_id = start_run(settings, "sectors")
        set_observer(lambda event: record_attempt(settings, run_id, event))
        result = refresh_sector_heat(settings, timeout=args.timeout, workers=args.workers, industry_only=args.industry_only,
                                     progress=lambda message: print(message, flush=True))
    except Exception as exc:
        if run_id is not None:
            finish_run(settings, run_id, "failed", {"reason": str(exc)})
        print(f"采集失败：{exc}", file=sys.stderr)
        return 2
    finally:
        set_observer(None)
    universe = result["universe"]
    finish_run(settings, run_id, "success" if universe["status"] == "ready" else "partial", {"universe": universe,
        "collector_version": COLLECTOR_VERSION, "source_attempts": result.get("source_attempts", []),
        "directory_provenance": result.get("directory_provenance", {})})
    print(f"排名日期 {universe['as_of']}；热门 {universe['member_count']}；"
          f"采集完成 {universe['collected_count']}；失败或旧行情 {universe['failed_count']}。", flush=True)
    return 0 if universe["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
