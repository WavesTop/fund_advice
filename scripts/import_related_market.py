#!/usr/bin/env python3
"""Resolve and import real tracked-index series from official index catalogues."""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable, MutableMapping

from backend.core.config import Settings
from backend.storage.related_market import import_related_index
from scripts.probe_fund_source import ProbeError

CNI_URL = "https://www.cnindex.com.cn/index/indexList?channelCode=-1&rows=2000&pageNum=1"
CSI_LIST = "https://www.csindex.com.cn/csindex-home/index-list/query-index-item"
CSI_INFO = "https://www.csindex.com.cn/csindex-home/indexInfo/index-basic-info/"
CSI_PERF = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
_CACHE: dict[str, object] = {}


def _read(url: str, timeout: float, opener: Callable[..., object], body: object | None = None) -> str:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with opener(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise ProbeError("upstream_http_error", "关联指数资料来源返回 HTTP 错误") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProbeError("network_error", "关联指数资料来源暂时不可用") from exc
    except UnicodeError as exc:
        raise ProbeError("invalid_response", "关联指数响应编码无效") from exc


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


def _profile(fund_code: str, timeout: float, opener: Callable[..., object]) -> tuple[str, str] | None:
    if not isinstance(fund_code, str) or not re.fullmatch(r"\d{6}", fund_code):
        raise ProbeError("invalid_request", "基金代码必须是六位数字")
    url = f"https://fund.eastmoney.com/{fund_code}.html"
    match = re.search(r"跟踪标的：</a>\s*([^|<]+)|跟踪标的：\s*([^|<]+)", _read(url, timeout, opener))
    if not match:
        return None
    target = (match.group(1) or match.group(2) or "").strip()
    if not target:
        raise ProbeError("invalid_response", "基金资料中的跟踪标的为空")
    return target, url


def _store(cache: MutableMapping[str, object] | None) -> MutableMapping[str, object]:
    return _CACHE if cache is None else cache


def _cni(target: str, profile_url: str, timeout: float, opener: Callable[..., object], cache: MutableMapping[str, object]) -> dict[str, str] | None:
    rows = cache.get("cni")
    if rows is None:
        data = _payload(_read(CNI_URL, timeout, opener), "国证指数目录响应格式无效")
        try:
            rows = data["data"]["rows"]
        except (KeyError, TypeError) as exc:
            raise ProbeError("invalid_response", "国证指数目录响应格式无效") from exc
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ProbeError("invalid_response", "国证指数目录响应格式无效")
        cache["cni"] = rows
    matches = [row for row in rows if any(_normal(str(row.get(key) or "")) == _normal(target)
                                          for key in ("indexname", "indexfullcname"))]
    if len(matches) != 1:
        return None
    code = str(matches[0].get("indexcode") or "")
    if not re.fullmatch(r"(?:399|980)\d{3}", code):
        return None
    return {"index_code": code, "index_name": target, "symbol": f"sina:sz{code}", "provider": "sina",
            "relation_source_id": "fund_profile.eastmoney+cni_index.official", "evidence_url": profile_url}


def resolve_cni_relation(fund_code: str, timeout: float, opener: Callable[..., object] = urllib.request.urlopen,
                         catalogue_cache: MutableMapping[str, object] | None = None) -> dict[str, str] | None:
    profile = _profile(fund_code, timeout, opener)
    return None if profile is None else _cni(*profile, timeout, opener, _store(catalogue_cache))


def _csi(target: str, profile_url: str, timeout: float, opener: Callable[..., object], cache: MutableMapping[str, object]) -> dict[str, str] | None:
    key = "csi-list:" + _normal(target)
    candidates = cache.get(key)
    if candidates is None:
        query = {"sorter": {"sortField": "null", "sortOrder": None}, "pager": {"pageNum": 1, "pageSize": 20},
                 "searchInput": target, "indexFilter": {"indexSeries": None, "indexClassify": None,
                 "marketCoverage": None, "hotSpot": None, "currency": None, "region": None}}
        answer = _payload(_read(CSI_LIST, timeout, opener, query), "中证指数目录响应格式无效")
        candidates = answer.get("data")
        if str(answer.get("code")) != "200" or answer.get("success") is not True or not isinstance(candidates, list):
            raise ProbeError("invalid_response", "中证指数目录响应格式无效")
        cache[key] = candidates
    matches = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("indexCode"), str) or not re.fullmatch(r"\d{6}", candidate["indexCode"]):
            raise ProbeError("invalid_response", "中证指数目录返回无效指数代码")
        code = candidate["indexCode"]
        info_key = "csi-info:" + code
        info = cache.get(info_key)
        if info is None:
            answer = _payload(_read(CSI_INFO + code, timeout, opener), "中证指数基础资料响应格式无效")
            info = answer.get("data")
            if str(answer.get("code")) != "200" or answer.get("success") is not True or not isinstance(info, dict):
                raise ProbeError("invalid_response", "中证指数基础资料响应格式无效")
            cache[info_key] = info
        if info.get("indexCode") != code or not isinstance(info.get("indexFullNameCn"), str):
            raise ProbeError("invalid_response", "中证指数基础资料身份字段无效")
        if _normal(info["indexFullNameCn"]) == _normal(target):
            matches.append(info)
    if len(matches) != 1:
        return None
    info = matches[0]
    return {"index_code": info["indexCode"], "index_name": info["indexFullNameCn"],
            "symbol": "csi:" + info["indexCode"], "provider": "csi",
            "relation_source_id": "fund_profile.eastmoney+csi_index.official", "evidence_url": profile_url}


def resolve_related_relation(fund_code: str, timeout: float, opener: Callable[..., object] = urllib.request.urlopen,
                             catalogue_cache: MutableMapping[str, object] | None = None) -> dict[str, str] | None:
    profile = _profile(fund_code, timeout, opener)
    if profile is None:
        return None
    target, url = profile
    cache = _store(catalogue_cache)
    if target.startswith("国证"):
        return _cni(target, url, timeout, opener, cache)
    if target.startswith("中证"):
        return _csi(target, url, timeout, opener, cache)
    return _cni(target, url, timeout, opener, cache) or _csi(target, url, timeout, opener, cache)


def _fetch(relation: dict[str, str], timeout: float, opener: Callable[..., object]) -> tuple[list[dict[str, object]], str]:
    if relation["provider"] == "csi" and relation["symbol"].startswith("csi:"):
        code = relation["symbol"][4:]
        end, start = dt.date.today(), dt.date.today() - dt.timedelta(days=366 * 6)
        url = f"{CSI_PERF}?indexCode={code}&startDate={start:%Y%m%d}&endDate={end:%Y%m%d}"
        answer = _payload(_read(url, timeout, opener), "中证指数行情响应格式无效")
        data = answer.get("data")
        if str(answer.get("code")) != "200" or answer.get("success") is not True or not isinstance(data, list) or not data:
            raise ProbeError("empty_result", "中证指数暂无可展示行情")
        rows = []
        for row in data:
            date = row.get("tradeDate") if isinstance(row, dict) else None
            if not isinstance(row, dict) or row.get("indexCode") != code or _normal(str(row.get("indexNameCnAll") or "")) != _normal(relation["index_name"]) or not isinstance(date, str) or not re.fullmatch(r"\d{8}", date):
                raise ProbeError("invalid_response", "中证指数行情身份或日期无效")
            rows.append({"date": f"{date[:4]}-{date[4:6]}-{date[6:]}", "open": row.get("open"), "high": row.get("high"),
                         "low": row.get("low"), "close": row.get("close"), "volume": row.get("tradingVol"), "amount": row.get("tradingValue")})
        return rows, "index_daily.csi_official"
    symbol = relation["symbol"].removeprefix("sina:")
    url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_data=/CN_MarketDataService.getKLineData?symbol=" + symbol + "&scale=240&ma=no&datalen=1023"
    text = _read(url, timeout, opener)
    match = re.search(r"var\s+_data=\((\[.*\])\)\s*;?\s*$", text, re.S)
    if not match:
        raise ProbeError("invalid_response", "关联指数行情响应格式无效")
    payload = json.loads(match.group(1), parse_float=str, parse_int=str)
    if not isinstance(payload, list) or not payload:
        raise ProbeError("empty_result", "关联指数暂无可展示行情")
    return [{"date": x.get("day"), "open": x.get("open"), "high": x.get("high"), "low": x.get("low"), "close": x.get("close"), "volume": x.get("volume"), "amount": x.get("amount")} for x in payload if isinstance(x, dict)], "index_daily.sina"


def refresh_related_market(settings: Settings, fund_code: str, *, timeout: float = 30.0,
                           opener: Callable[..., object] = urllib.request.urlopen,
                           catalogue_cache: MutableMapping[str, object] | None = None) -> dict[str, object] | None:
    relation = resolve_related_relation(fund_code, timeout, opener, catalogue_cache)
    if relation is None:
        return None
    rows, source = _fetch(relation, timeout, opener)
    import_related_index(settings, fund_code=fund_code, index_code=relation["index_code"], index_name=relation["index_name"], rows=rows,
                         source_id=source, relation_source_id=relation["relation_source_id"], evidence_url=relation["evidence_url"])
    return {"code": relation["index_code"], "row_count": len(rows), "source_id": source}
