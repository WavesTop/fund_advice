#!/usr/bin/env python3
"""Resolve fund-to-index relations through THS profiles and official catalogues."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
import math
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, MutableMapping
from urllib.parse import urlsplit

from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.related_market import (
    get_related_markets,
    import_related_index,
    import_related_relation,
    record_index_market_failure,
    record_relation_check,
)
from backend.storage.timeseries import get_fund
from scripts.data_source_registry import load_registry, replacement_preserves_quality, sources_for
from scripts.probe_fund_source import ProbeError


THS_PROFILE = "https://fund.10jqka.com.cn/{code}/interduce.html"
CNI_URL = "https://www.cnindex.com.cn/index/indexList?channelCode=-1&rows=2000&pageNum=1"
CSI_LIST = "https://www.csindex.com.cn/csindex-home/index-list/query-index-item"
CSI_INFO = "https://www.csindex.com.cn/csindex-home/indexInfo/index-basic-info/"
CSI_PERF = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
PROFILE_MAX_BYTES = 2 * 1024 * 1024
DATA_MAX_BYTES = 16 * 1024 * 1024
CSI_PAGE_SIZE = 200
CSI_MAX_CANDIDATES = 600
MIN_INDEX_HISTORY_ROWS = 240
MIN_INDEX_HISTORY_DAYS = 330
MIN_INDEX_DENSITY_PERCENT = 40
MAX_INDEX_LATEST_AGE_DAYS = 21
MIN_TRACKED_BENCHMARK_WEIGHT_PERCENT = Decimal("80")
_TRANSIENT_HTTP = {403, 429, 500, 502, 503, 504}


def _referer(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith("10jqka.com.cn"):
        return "https://fund.10jqka.com.cn/"
    if host.endswith("cnindex.com.cn"):
        return "https://www.cnindex.com.cn/"
    if host.endswith("csindex.com.cn"):
        return "https://www.csindex.com.cn/"
    if host.endswith("sina.cn"):
        return "https://quotes.sina.cn/"
    return "https://fund.10jqka.com.cn/"


def _deadline(timeout: float) -> float:
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout <= 0):
        raise ProbeError("invalid_request", "基金关系采集超时参数无效")
    return time.monotonic() + float(timeout)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProbeError("network_timeout", "基金关系采集超过总超时限制")
    return remaining


def _read(url: str, deadline: float, opener: Callable[..., object], body: object | None = None,
          *, max_bytes: int = DATA_MAX_BYTES) -> str:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"User-Agent": "Mozilla/5.0", "Referer": _referer(url)}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    expected = urlsplit(url)
    for attempt in range(2):
        try:
            with opener(request, timeout=_remaining(deadline)) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                actual = urlsplit(str(final_url))
                if (actual.scheme != "https" or actual.hostname != expected.hostname
                        or actual.path != expected.path):
                    raise ProbeError("invalid_response", "基金关系请求发生了非预期跳转")
                status = response.getcode() if hasattr(response, "getcode") else 200
                if status != 200:
                    if attempt == 0 and status in _TRANSIENT_HTTP:
                        time.sleep(min(0.25, _remaining(deadline)))
                        continue
                    raise ProbeError("upstream_http_error", f"基金关系资料来源返回 HTTP {status}")
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise ProbeError("truncated_response", "基金关系资料响应超过安全大小限制")
                return payload.decode("utf-8")
        except ProbeError:
            raise
        except urllib.error.HTTPError as exc:
            if attempt == 0 and exc.code in _TRANSIENT_HTTP:
                time.sleep(min(0.25, _remaining(deadline)))
                continue
            raise ProbeError("upstream_http_error", f"基金关系资料来源返回 HTTP {exc.code}") from exc
        except (TimeoutError, socket.timeout) as exc:
            if attempt == 0:
                time.sleep(min(0.25, _remaining(deadline)))
                continue
            raise ProbeError("network_timeout", "基金关系资料来源请求超时") from exc
        except (urllib.error.URLError, OSError) as exc:
            if attempt == 0:
                time.sleep(min(0.25, _remaining(deadline)))
                continue
            raise ProbeError("network_error", "基金关系资料来源暂时不可用") from exc
        except UnicodeError as exc:
            raise ProbeError("invalid_response", "基金关系响应编码无效") from exc
    raise AssertionError("基金关系重试状态无效")


def _payload(text: str, message: str) -> dict[str, Any]:
    try:
        value = json.loads(text, parse_float=str, parse_int=str)
    except json.JSONDecodeError as exc:
        raise ProbeError("invalid_response", message) from exc
    if not isinstance(value, dict):
        raise ProbeError("invalid_response", message)
    return value


def _normal(value: str) -> str:
    return re.sub(r"[（）()\s]", "", value)


class _ProfileFields(HTMLParser):
    """Collect labelled spans without trusting unrelated page text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._span_kind: str | None = None
        self._text: list[str] = []
        self.tokens: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "span" or self._span_kind is not None:
            return
        classes = set(dict(attrs).get("class", "").split())
        kind = "key" if "key" in classes else "value" if "value" in classes else None
        if kind:
            self._span_kind, self._text = kind, []

    def handle_data(self, data: str) -> None:
        if self._span_kind is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self._span_kind is not None:
            self.tokens.append((self._span_kind, "".join(self._text).strip()))
            self._span_kind, self._text = None, []

    def labelled(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        pending: str | None = None
        for kind, value in self.tokens:
            if kind == "key":
                pending = value
            elif pending is not None:
                result.setdefault(pending, []).append(value)
                pending = None
        return result


def _one(fields: dict[str, list[str]], label: str) -> str:
    values = [value for value in fields.get(label, []) if value]
    if len(values) != 1:
        raise ProbeError("invalid_response", f"同花顺基金资料缺少唯一{label}")
    return values[0]


def _target_from_benchmark(benchmark: str) -> tuple[str, str] | None:
    components = re.split(r"[+＋]", benchmark)
    indexes: list[tuple[str, str]] = []
    for component in components:
        match = re.fullmatch(
            r"\s*(?:(\d+(?:\.\d+)?)\s*[%％]\s*[*×xX]\s*)?"
            r"(.+?指数)\s*收益率\s*(?:[*×xX]\s*(\d+(?:\.\d+)?)\s*[%％])?\s*",
            component,
        )
        if match:
            leading, target, trailing = match.groups()
            if leading is not None and trailing is not None:
                return None
            raw_weight = leading or trailing or "100"
            try:
                weight = Decimal(raw_weight)
            except InvalidOperation:
                return None
            if not weight.is_finite() or weight <= 0 or weight > 100:
                return None
            indexes.append((target.strip(), format(weight.normalize(), "f")))
        elif "指数" in component or re.search(r"\bindex\b", component, re.I):
            # An unparsed index component means the benchmark is not proven to
            # contain exactly one index.  Cash/deposit components may remain.
            return None
    return indexes[0] if len(indexes) == 1 else None


def _profile(fund_code: str, deadline: float, opener: Callable[..., object]) -> dict[str, str] | None:
    if not isinstance(fund_code, str) or not re.fullmatch(r"\d{6}", fund_code):
        raise ProbeError("invalid_request", "基金代码必须是六位数字")
    url = THS_PROFILE.format(code=fund_code)
    text = _read(url, deadline, opener, max_bytes=PROFILE_MAX_BYTES)
    script_code = re.findall(r"\bfundCode\s*=\s*['\"]([^'\"]+)['\"]", text)
    script_name = re.findall(r"\bfundName\s*=\s*['\"]([^'\"]+)['\"]", text)
    if len(script_code) != 1 or len(script_name) != 1:
        raise ProbeError("invalid_response", "同花顺基金资料缺少唯一代码或简称身份")
    parser = _ProfileFields()
    parser.feed(text)
    fields = parser.labelled()
    code = _one(fields, "基金代码")
    name = _one(fields, "基金简称")
    benchmark = _one(fields, "业绩比较基准")
    if code != fund_code or script_code[0] != fund_code or _normal(script_name[0]) != _normal(name):
        raise ProbeError("invalid_response", "同花顺基金资料代码或简称身份不匹配")
    parsed_benchmark = _target_from_benchmark(benchmark)
    if parsed_benchmark is None:
        return None
    target, weight = parsed_benchmark
    return {"fund_code": code, "fund_name": name, "benchmark": benchmark,
            "target": target, "benchmark_weight_percent": weight, "evidence_url": url}


def _store(cache: MutableMapping[str, object] | None) -> MutableMapping[str, object]:
    return {} if cache is None else cache


def _cni(profile: dict[str, str], deadline: float, opener: Callable[..., object],
         cache: MutableMapping[str, object]) -> dict[str, str] | None:
    container = cache.get("cni")
    if container is None:
        data = _payload(_read(CNI_URL, deadline, opener), "国证指数目录响应格式无效")
        try:
            container = data["data"]
        except (KeyError, TypeError) as exc:
            raise ProbeError("invalid_response", "国证指数目录响应格式无效") from exc
    if not isinstance(container, dict):
        raise ProbeError("invalid_response", "国证指数目录缓存格式无效")
    rows = container.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ProbeError("invalid_response", "国证指数目录响应格式无效")
    total_value = container.get("total")
    if (not isinstance(total_value, (str, int)) or isinstance(total_value, bool)
            or not str(total_value).isdigit()):
        raise ProbeError("invalid_response", "国证指数目录总数无效")
    if int(total_value) != len(rows):
        raise ProbeError("truncated_response", "国证指数目录分页不完整")
    codes: list[str] = []
    for row in rows:
        code = row.get("indexcode")
        short_name = row.get("indexname")
        full_name = row.get("indexfullcname")
        if (not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9]{6,8}", code)
                or not isinstance(short_name, str) or not short_name.strip()
                or not isinstance(full_name, str) or not full_name.strip()):
            raise ProbeError("invalid_response", "国证指数目录身份字段无效")
        codes.append(code)
    if len(codes) != len(set(codes)):
        raise ProbeError("invalid_response", "国证指数目录代码重复")
    cache["cni"] = container
    target = profile["target"]
    matches = [row for row in rows if any(
        _normal(str(row.get(key) or "")) == _normal(target)
        for key in ("indexname", "indexfullcname")
    )]
    if len(matches) != 1:
        return None
    matched = matches[0]
    code = str(matched.get("indexcode") or "")
    if not re.fullmatch(r"(?:399|980)\d{3}", code):
        raise ProbeError("invalid_response", "国证指数目录返回无效指数代码")
    official_name = str(matched.get("indexfullcname") or matched.get("indexname") or "").strip()
    aliases = {
        _normal(str(matched.get(key) or ""))
        for key in ("indexname", "indexfullcname")
        if str(matched.get(key) or "").strip()
    }
    if not official_name or _normal(target) not in aliases:
        raise ProbeError("invalid_response", "国证指数目录名称与基金业绩基准不匹配")
    short_name = str(matched.get("indexname") or official_name).strip()
    return {**profile, "index_code": code, "index_name": official_name,
            "index_short_name": short_name,
            "symbol": f"sina:sz{code}", "provider": "sina",
            "relation_source_id": "fund_profile.ths+cni_index.official"}


def resolve_cni_relation(fund_code: str, timeout: float,
                         opener: Callable[..., object] = urllib.request.urlopen,
                         catalogue_cache: MutableMapping[str, object] | None = None) -> dict[str, str] | None:
    deadline = _deadline(timeout)
    profile = _profile(fund_code, deadline, opener)
    return None if profile is None else _cni(profile, deadline, opener, _store(catalogue_cache))


def _csi_candidates(search_input: str, deadline: float, opener: Callable[..., object],
                    cache: MutableMapping[str, object]) -> list[dict[str, Any]]:
    key = "csi-list:" + _normal(search_input)
    cached = cache.get(key)
    if cached is not None:
        if not isinstance(cached, list) or any(not isinstance(row, dict) for row in cached):
            raise ProbeError("invalid_response", "中证指数目录缓存格式无效")
        return cached
    candidates: list[dict[str, Any]] = []
    expected_total: int | None = None
    page = 1
    while True:
        query = {"sorter": {"sortField": "null", "sortOrder": None},
                 "pager": {"pageNum": page, "pageSize": CSI_PAGE_SIZE},
                 "searchInput": search_input,
                 "indexFilter": {"indexSeries": None, "indexClassify": None,
                                 "marketCoverage": None, "hotSpot": None,
                                 "currency": None, "region": None}}
        answer = _payload(_read(CSI_LIST, deadline, opener, query), "中证指数目录响应格式无效")
        rows = answer.get("data")
        if str(answer.get("code")) != "200" or answer.get("success") is not True or not isinstance(rows, list):
            raise ProbeError("invalid_response", "中证指数目录响应格式无效")
        if any(not isinstance(row, dict) for row in rows):
            raise ProbeError("invalid_response", "中证指数目录候选格式无效")
        total_value = answer.get("total")
        if isinstance(total_value, (str, int)) and not isinstance(total_value, bool) and str(total_value).isdigit():
            total = int(total_value)
        else:
            raise ProbeError("invalid_response", "中证指数目录总数无效")
        metadata: dict[str, int] = {}
        for field in ("currentPage", "pageSize", "size"):
            value = answer.get(field)
            if (not isinstance(value, (str, int)) or isinstance(value, bool)
                    or not str(value).isdigit()):
                raise ProbeError("invalid_response", "中证指数目录分页身份无效")
            metadata[field] = int(value)
        expected_pages = (total + CSI_PAGE_SIZE - 1) // CSI_PAGE_SIZE
        expected_page = page if total else 0
        expected_rows = min(CSI_PAGE_SIZE, max(0, total - (page - 1) * CSI_PAGE_SIZE))
        if (metadata["currentPage"] != expected_page
                or metadata["pageSize"] != CSI_PAGE_SIZE
                or metadata["size"] != expected_pages
                or len(rows) != expected_rows):
            raise ProbeError("invalid_response", "中证指数目录分页身份或数量无效")
        if total > CSI_MAX_CANDIDATES:
            raise ProbeError("truncated_response", "中证指数目录候选过多，拒绝不完整匹配")
        if expected_total is None:
            expected_total = total
        elif expected_total != total:
            raise ProbeError("invalid_response", "中证指数目录分页总数发生变化")
        candidates.extend(rows)
        if len(candidates) >= total:
            if len(candidates) != total:
                raise ProbeError("invalid_response", "中证指数目录分页数量无效")
            break
        if not rows:
            raise ProbeError("invalid_response", "中证指数目录分页不完整")
        page += 1
    codes = [row.get("indexCode") for row in candidates]
    if (any(not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9]{6}", code) for code in codes)
            or len(codes) != len(set(codes))):
        raise ProbeError("invalid_response", "中证指数目录候选代码无效或重复")
    cache[key] = candidates
    return candidates


def _csi(profile: dict[str, str], deadline: float, opener: Callable[..., object],
         cache: MutableMapping[str, object]) -> dict[str, str] | None:
    target = profile["target"]
    stem = target.removesuffix("指数")
    candidates_by_code: dict[str, dict[str, Any]] = {}
    for search_input in dict.fromkeys((target, stem)):
        candidates = _csi_candidates(search_input, deadline, opener, cache)
        for candidate in candidates:
            code = str(candidate["indexCode"])
            short = candidate.get("indexName")
            if not isinstance(short, str) or not short.strip():
                raise ProbeError("invalid_response", "中证指数目录候选简称无效")
            # A broad stem search can return hundreds of fuzzy candidates.  Only
            # retain the official short-name identity before loading details.
            if search_input != target and _normal(short) != _normal(stem):
                continue
            candidates_by_code[code] = candidate
    matches: dict[str, dict[str, Any]] = {}
    for code, candidate in candidates_by_code.items():
        info_key = "csi-info:" + code
        info = cache.get(info_key)
        if info is None:
            answer = _payload(_read(CSI_INFO + code, deadline, opener), "中证指数基础资料响应格式无效")
            info = answer.get("data")
            if str(answer.get("code")) != "200" or answer.get("success") is not True or not isinstance(info, dict):
                raise ProbeError("invalid_response", "中证指数基础资料响应格式无效")
            cache[info_key] = info
        if not isinstance(info, dict):
            raise ProbeError("invalid_response", "中证指数基础资料身份字段无效")
        full_name = info.get("indexFullNameCn")
        short_name = info.get("indexShortNameCn")
        if (info.get("indexCode") != code or not isinstance(full_name, str) or not full_name.strip()
                or not isinstance(short_name, str) or not short_name.strip()
                or _normal(short_name) != _normal(str(candidate["indexName"]))):
            raise ProbeError("invalid_response", "中证指数基础资料身份字段无效")
        aliases = {_normal(full_name), _normal(short_name)}
        if not short_name.endswith("指数"):
            aliases.add(_normal(short_name + "指数"))
        if _normal(target) in aliases:
            matches[code] = info
    if len(matches) != 1:
        return None
    info = next(iter(matches.values()))
    return {**profile, "index_code": info["indexCode"], "index_name": info["indexFullNameCn"],
            "index_short_name": info["indexShortNameCn"],
            "symbol": "csi:" + info["indexCode"], "provider": "csi",
            "relation_source_id": "fund_profile.ths+csi_index.official"}


def _resolve_related_relation(fund_code: str, deadline: float, opener: Callable[..., object],
                              catalogue_cache: MutableMapping[str, object] | None) -> dict[str, str] | None:
    profile = _profile(fund_code, deadline, opener)
    if profile is None:
        return None
    cache = _store(catalogue_cache)
    target = profile["target"]
    if target.startswith("国证"):
        return _cni(profile, deadline, opener, cache)
    if target.startswith(("中证", "沪深", "上证")):
        return _csi(profile, deadline, opener, cache)
    relation = _cni(profile, deadline, opener, cache)
    return relation if relation is not None else _csi(profile, deadline, opener, cache)


def resolve_related_relation(fund_code: str, timeout: float,
                             opener: Callable[..., object] = urllib.request.urlopen,
                             catalogue_cache: MutableMapping[str, object] | None = None) -> dict[str, str] | None:
    return _resolve_related_relation(fund_code, _deadline(timeout), opener, catalogue_cache)


def _fetch(relation: dict[str, str], deadline: float,
           opener: Callable[..., object]) -> tuple[list[dict[str, object]], str]:
    if relation["provider"] == "csi" and relation["symbol"].startswith("csi:"):
        code = relation["symbol"][4:]
        end, start = dt.date.today(), dt.date.today() - dt.timedelta(days=366 * 6)
        url = f"{CSI_PERF}?indexCode={code}&startDate={start:%Y%m%d}&endDate={end:%Y%m%d}"
        answer = _payload(_read(url, deadline, opener), "中证指数行情响应格式无效")
        data = answer.get("data")
        if (str(answer.get("code")) != "200" or answer.get("success") is not True
                or not isinstance(data, list) or not data):
            raise ProbeError("empty_result", "中证指数暂无可展示行情")
        rows = []
        for row in data:
            date = row.get("tradeDate") if isinstance(row, dict) else None
            if (not isinstance(row, dict) or row.get("indexCode") != code
                    or _normal(str(row.get("indexNameCnAll") or "")) != _normal(relation["index_name"])
                    or not isinstance(date, str) or not re.fullmatch(r"\d{8}", date)):
                raise ProbeError("invalid_response", "中证指数行情身份或日期无效")
            rows.append({"date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
                         "open": row.get("open"), "high": row.get("high"),
                         "low": row.get("low"), "close": row.get("close"),
                         "volume": row.get("tradingVol"), "amount": row.get("tradingValue")})
        return rows, "index_daily.csi_official"
    if relation["provider"] != "sina" or not relation["symbol"].startswith("sina:sz"):
        raise ProbeError("invalid_response", "关联指数行情提供方无效")
    symbol = relation["symbol"].removeprefix("sina:")
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_data=/"
           "CN_MarketDataService.getKLineData?symbol=" + symbol + "&scale=240&ma=no&datalen=1023")
    text = _read(url, deadline, opener)
    match = re.search(r"var\s+_data=\((\[.*\])\)\s*;?\s*$", text, re.S)
    if not match:
        raise ProbeError("invalid_response", "关联指数行情响应格式无效")
    try:
        payload = json.loads(match.group(1), parse_float=str, parse_int=str)
    except json.JSONDecodeError as exc:
        raise ProbeError("invalid_response", "关联指数行情响应格式无效") from exc
    if not isinstance(payload, list) or not payload:
        raise ProbeError("empty_result", "关联指数暂无可展示行情")
    if any(not isinstance(row, dict) for row in payload):
        raise ProbeError("invalid_response", "关联指数行情行格式无效")
    return [{"date": row.get("day"), "open": row.get("open"), "high": row.get("high"),
             "low": row.get("low"), "close": row.get("close"),
             "volume": row.get("volume"), "amount": row.get("amount")} for row in payload], "index_daily.sina"


def _validate_index_history(rows: list[dict[str, object]]) -> None:
    """Require a recent, dense history before replacing a shared index series."""
    dates: list[dt.date] = []
    for row in rows:
        value = row.get("date") if isinstance(row, dict) else None
        try:
            day = dt.date.fromisoformat(value) if isinstance(value, str) else None
        except ValueError as exc:
            raise ProbeError("invalid_response", "关联指数行情日期无效") from exc
        if day is None or day.isoformat() != value:
            raise ProbeError("invalid_response", "关联指数行情日期无效")
        dates.append(day)
    if len(set(dates)) != len(dates):
        raise ProbeError("invalid_response", "关联指数行情日期重复")
    dates.sort()
    today = dt.date.today()
    if (len(dates) < MIN_INDEX_HISTORY_ROWS
            or (dates[-1] - dates[0]).days < MIN_INDEX_HISTORY_DAYS
            or len(dates) * 100 < ((dates[-1] - dates[0]).days + 1) * MIN_INDEX_DENSITY_PERCENT
            or dates[-1] > today or (today - dates[-1]).days > MAX_INDEX_LATEST_AGE_DAYS):
        raise ProbeError("truncated_response", "关联指数行情覆盖不足或末日过旧，旧数据已保留")


def _tracked_index_identity(fund: dict[str, object], relation: dict[str, str]) -> bool:
    name = _normal(str(fund.get("name") or ""))
    fund_type = _normal(str(fund.get("fund_type") or ""))
    indexed_product = "指数" in fund_type or ("ETF" in name.upper() and "联接" in name)
    identity_names = {
        _normal(relation.get(field, "")).removesuffix("指数")
        for field in ("target", "index_name")
    }
    short_name = _normal(relation.get("index_short_name", "")).removesuffix("指数")
    if len(short_name) >= 4:
        identity_names.add(short_name)
    try:
        benchmark_weight = Decimal(relation["benchmark_weight_percent"])
    except (InvalidOperation, KeyError):
        return False
    return (indexed_product and benchmark_weight >= MIN_TRACKED_BENCHMARK_WEIGHT_PERCENT
            and any(identity and identity in name for identity in identity_names))


def _automatic_source_ids(registry: dict[str, Any], dataset: str, use: str | None,
                          registry_path: str | None) -> set[str]:
    sources = (sources_for(dataset, use, path=registry_path)
               if registry_path else sources_for(dataset, use))
    return {source["id"] for source in sources if source["automatic"] and source["adapter"] is not None}


def _preserved_relation(relation: dict[str, object], message: str) -> dict[str, object]:
    return {
        "code": relation["code"], "name": relation["name"],
        "row_count": len(relation.get("rows") or []), "source_id": relation["source_id"],
        "relation_source_id": relation["relation_source_id"],
        "evidence_url": relation.get("evidence_url"), "preserved": True, "message": message,
    }


def _protected_primary_relation(settings: Settings, fund_code: str, registry: dict[str, Any],
                                candidate_source_ids: set[str]) -> dict[str, object] | None:
    for current in get_related_markets(settings, fund_code):
        if all(not replacement_preserves_quality(
                registry, current.get("relation_source_id"), candidate, dataset="fund_relations")
               for candidate in candidate_source_ids):
            return current
    return None


def refresh_related_market(settings: Settings, fund_code: str, *, timeout: float = 30.0,
                           opener: Callable[..., object] = urllib.request.urlopen,
                           catalogue_cache: MutableMapping[str, object] | None = None,
                           registry_path: str | None = None) -> dict[str, object] | None:
    fund = get_fund(settings, fund_code)
    registry = load_registry(registry_path) if registry_path else load_registry()
    relation_sources = _automatic_source_ids(registry, "fund_relations", "candidate_fact", registry_path)
    market_sources = _automatic_source_ids(registry, "index_daily", None, registry_path)
    if not relation_sources or not market_sources:
        raise ValueError("注册表中没有可自动采集的基金关系或指数行情来源")
    existing = get_related_markets(settings, fund_code)
    protected = _protected_primary_relation(settings, fund_code, registry, relation_sources)
    if protected is not None:
        return _preserved_relation(protected, "已保留管理人正式关系证据，未用同花顺候选关系覆盖。")
    attempted_source_id = sorted(relation_sources)[0]
    deadline = _deadline(timeout)
    relation_attempted_at = dt.datetime.now(dt.timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    relation_resolution_completed = False
    market_attempted_at: str | None = None
    market_index_code: str | None = None
    market_source_id: str | None = None
    relation_persisted = False
    try:
        relation = _resolve_related_relation(fund_code, deadline, opener, catalogue_cache)
        relation_resolution_completed = True
        if relation is None:
            recorded = record_relation_check(
                settings, fund_code, outcome="unresolved",
                error="同花顺业绩基准未能唯一匹配国证或中证官方指数目录，旧关系待核验。",
                attempted_source_id=attempted_source_id,
                attempted_at=relation_attempted_at,
            )
            if not recorded:
                protected = _protected_primary_relation(settings, fund_code, registry, relation_sources)
                if protected is not None:
                    return _preserved_relation(
                        protected, "并发写入了管理人正式关系证据，未记录候选关系失败。",
                    )
            return None
        if relation["fund_code"] != fund["code"] or _normal(relation["fund_name"]) != _normal(str(fund["name"])):
            raise ProbeError("identity_mismatch", "同花顺基金资料与本地基金身份不一致")
        if not _tracked_index_identity(fund, relation):
            raise ProbeError("identity_mismatch", "业绩比较基准不能证明该基金是对应指数产品")
        if relation["relation_source_id"] not in relation_sources:
            raise ProbeError("source_not_allowed", "基金关系来源未获注册表自动采集授权")
        source = "index_daily.csi_official" if relation["provider"] == "csi" else "index_daily.sina"
        if source not in market_sources:
            raise ProbeError("source_not_allowed", "指数行情来源未获注册表自动采集授权")
        same_index = [item for item in existing if item["code"] == relation["index_code"]]
        for current in same_index:
            if (not replacement_preserves_quality(
                    registry, current.get("relation_source_id"), relation["relation_source_id"],
                    dataset="fund_relations")
                    or not replacement_preserves_quality(
                        registry, current.get("source_id"), source, dataset="index_daily")):
                return _preserved_relation(current, "已保留更高等级的正式关系或指数行情来源。")
        market_attempted_at = dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        market_index_code = relation["index_code"]
        market_source_id = source
        import_related_relation(
            settings, fund_code=fund_code, index_code=relation["index_code"],
            index_name=relation["index_name"], market_source_id=source,
            relation_source_id=relation["relation_source_id"],
            evidence_url=relation["evidence_url"],
        )
        relation_persisted = True
        rows, fetched_source = _fetch(relation, deadline, opener)
        if fetched_source != source:
            raise ProbeError("invalid_response", "指数行情来源身份与预期不一致")
        _validate_index_history(rows)
        import_related_index(
            settings, fund_code=fund_code, index_code=relation["index_code"],
            index_name=relation["index_name"], rows=rows, source_id=source,
            relation_source_id=relation["relation_source_id"],
            evidence_url=relation["evidence_url"],
        )
    except Exception as exc:
        if isinstance(exc, AppError) and exc.body.code in {
                "relation_source_downgrade_blocked", "index_source_downgrade_blocked"}:
            protected = _protected_primary_relation(settings, fund_code, registry, relation_sources)
            if protected is not None:
                return _preserved_relation(
                    protected, "并发写入了更高等级的正式证据，未用候选刷新覆盖。",
                )
            raise
        if (relation_persisted and market_attempted_at is not None
                and market_index_code is not None and market_source_id is not None):
            record_index_market_failure(
                settings, market_index_code, attempted_at=market_attempted_at,
                source_id=market_source_id, error=f"{type(exc).__name__}: {exc}",
            )
            raise
        if not relation_resolution_completed and existing:
            # A transport error or malformed/incomplete upstream response is no
            # new relationship evidence.  Report this refresh as failed while
            # keeping the last successfully verified relationship visible.
            raise
        recorded = record_relation_check(
            settings, fund_code, outcome="failed", error=f"{type(exc).__name__}: {exc}",
            attempted_source_id=attempted_source_id,
            attempted_at=relation_attempted_at,
        )
        if not recorded:
            protected = _protected_primary_relation(settings, fund_code, registry, relation_sources)
            if protected is not None:
                return _preserved_relation(
                    protected, "并发写入了管理人正式关系证据，未记录候选刷新失败。",
                )
        raise
    return {"code": relation["index_code"], "name": relation["index_name"],
            "row_count": len(rows), "source_id": source,
            "relation_source_id": relation["relation_source_id"],
            "evidence_url": relation["evidence_url"]}
