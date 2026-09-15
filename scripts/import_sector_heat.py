#!/usr/bin/env python3
"""Refresh the top 100 Eastmoney industry/concept boards for market context only."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
from threading import Lock
import time
from typing import Any, Callable
from urllib.parse import urlencode
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.config import Settings
from backend.storage.sector_heat import (
    CONSTITUENT_URL, DIRECTORY_URL, HISTORY_URL, read_sector_heat, record_sector_heat_failure,
    save_sector_heat,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
Fetcher = Callable[[str, float], dict[str, Any]]
THS_VERIFIED_ALIAS_CANDIDATES = {
    "5G概念": "5G",
    "PCB": "PCB概念",
    "新能源车": "新能源汽车",
    "央国企改革": "央企国企改革",
    "长江三角": "长三角一体化",
}
ALIAS_VISIBLE_MIN_MEMBERS = 8
ALIAS_VISIBLE_OVERLAP = Decimal("0.70")


class _ThsLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.stock_codes: set[str] = set()
        self.clid: str | None = None
        self._code: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a":
            href = values.get("href") or ""
            stock = re.search(r"(?:stockpage|basic|quote)[^>]*/(\d{6})/?", href, re.I)
            if stock:
                self.stock_codes.add(stock.group(1))
            match = re.search(r"/gn/detail/code/(\d+)/", href)
            if match:
                self._code, self._text = match.group(1), []
        if tag == "input" and values.get("id") == "clid" and re.fullmatch(r"\d+", values.get("value") or ""):
            self.clid = values["value"]

    def handle_data(self, data: str) -> None:
        if self._code:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._code:
            name = "".join(self._text).strip()
            if name:
                self.links.append((name, self._code))
            self._code, self._text = None, []


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    referer = ("https://emdatah5.eastmoney.com/dc/zjlx/block"
               if "emdatah5.eastmoney.com" in url else "https://quote.eastmoney.com/")
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Accept": "*/*", "Referer": referer,
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read(), parse_float=str, parse_int=str)
    if not isinstance(payload, dict):
        raise ValueError("来源响应不是对象")
    return payload


def fetch_text(url: str, timeout: float, referer: str = "https://q.10jqka.com.cn/") -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Referer": referer})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("gb18030", "replace")


def build_ths_history_crosswalk(members: list[dict[str, Any]], *, timeout: float = 12.0) -> dict[str, str]:
    """Return only category-consistent exact-name THS identities; never fuzzy-match boards."""
    crosswalk: dict[str, str] = {}
    try:
        import akshare as ak
        industries = {str(row[0]).strip(): str(row[1]).strip()
                      for row in ak.stock_board_industry_name_ths()[["name", "code"]].itertuples(index=False, name=None)}
        for member in members:
            if member["kind"] == "行业" and member["name"] in industries:
                crosswalk[member["code"]] = industries[member["name"]]
    except Exception:
        pass
    try:
        parser = _ThsLinkParser()
        parser.feed(fetch_text("https://q.10jqka.com.cn/gn/detail/code/307822/", timeout))
        concept_pages = dict(parser.links)
        wanted = []
        for member in members:
            if member["kind"] != "概念":
                continue
            ths_name = member["name"] if member["name"] in concept_pages else THS_VERIFIED_ALIAS_CANDIDATES.get(member["name"])
            if ths_name in concept_pages:
                wanted.append((member, ths_name))

        def resolve_concept(item: tuple[dict[str, Any], str]) -> tuple[str, str] | None:
            member, ths_name = item
            for attempt in range(3):
                try:
                    detail = _ThsLinkParser()
                    detail.feed(fetch_text(f"https://q.10jqka.com.cn/gn/detail/code/{concept_pages[ths_name]}/", timeout))
                    if detail.clid:
                        if ths_name != member["name"]:
                            eastmoney_members = {str(row["stock_code"]) for row in member.get("membership", {}).get("members", [])}
                            overlap = eastmoney_members & detail.stock_codes
                            if (len(detail.stock_codes) < ALIAS_VISIBLE_MIN_MEMBERS
                                    or Decimal(len(overlap)) / Decimal(len(detail.stock_codes)) < ALIAS_VISIBLE_OVERLAP):
                                return None
                        return member["code"], detail.clid
                except Exception:
                    if attempt < 2:
                        time.sleep(0.1 * (attempt + 1))
            return None

        with ThreadPoolExecutor(max_workers=3) as executor:
            for resolved in executor.map(resolve_concept, wanted):
                if resolved:
                    crosswalk[resolved[0]] = resolved[1]
    except Exception:
        pass
    return crosswalk


def fetch_ths_sector_daily(member: dict[str, Any], ths_code: str, *, timeout: float = 12.0) -> list[dict[str, str]]:
    payload = fetch_text(f"https://d.10jqka.com.cn/v4/line/bk_{ths_code}/01/last.js", timeout)
    left, right = payload.find("{"), payload.rfind("}")
    if left < 0 or right <= left:
        raise ValueError("同花顺日线JSONP无效")
    root = json.loads(payload[left:right + 1], parse_float=str, parse_int=str)
    expected_name = THS_VERIFIED_ALIAS_CANDIDATES.get(member["name"], member["name"])
    if str(root.get("name", "")).strip() != expected_name:
        raise ValueError("同花顺日线名称与东方财富板块严格同名校验失败")
    rows, seen = [], set()
    for value in str(root.get("data", "")).split(";"):
        fields = value.split(",")
        if len(fields) < 7 or not re.fullmatch(r"\d{8}", fields[0]):
            continue
        day = datetime.strptime(fields[0], "%Y%m%d").date().isoformat()
        if day in seen or day > member["ranking_as_of"]:
            continue
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
    local_now = datetime.now(SHANGHAI)
    if local_now.hour < 16 and rows[-1]["date"] == local_now.date().isoformat():
        rows.pop()
    if not rows:
        raise ValueError("剔除盘中未结算日线后没有可用数据")
    return rows


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


def _integer(value: object, field: str) -> int:
    text = _number(value, field)
    number = Decimal(text)
    if number != number.to_integral_value():
        raise ValueError(f"{field}必须为整数")
    return int(number)


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("rc")) != "0" or not isinstance(payload.get("data"), dict):
        raise ValueError("来源返回错误或缺少data")
    return payload["data"]


def fetch_sector_directory(fetcher: Fetcher, *, timeout: float = 12.0,
                           now: datetime | None = None,
                           source_groups: tuple[tuple[str, int], ...] = (("行业", 2), ("概念", 3))) -> list[dict[str, Any]]:
    """Read every page before any ranking; reject truncated or drifting catalogs."""
    current = now or datetime.now(timezone.utc)
    all_members: dict[str, dict[str, Any]] = {}
    source_dates: set[str] = set()
    for kind, source_type in source_groups:
        total, page, seen = None, 1, set()
        while total is None or len(seen) < total:
            params = {"pn": page, "pz": 100, "po": 1,
                      "fid": "f6", "fs": f"m:90+t:{source_type}",
                      "fields": "f12,f13,f14,f6,f124"}
            data = _data(fetcher(DIRECTORY_URL + "?" + urlencode(params), timeout))
            page_total = _integer(data.get("total"), "目录总数")
            if not 0 < page_total <= 20000 or (total is not None and page_total != total):
                raise ValueError(f"{kind}目录总数无效或采集中发生变化")
            total = page_total
            rows = data.get("diff")
            if not isinstance(rows, list) or len(rows) != min(100, total - len(seen)):
                raise ValueError(f"{kind}目录第{page}页缺失，未取得完整目录")
            for raw in rows:
                if not isinstance(raw, dict):
                    raise ValueError("板块目录行不是对象")
                code, name = raw.get("f12"), raw.get("f14")
                if not isinstance(code, str) or not re.fullmatch(r"BK\d{4,}", code) or str(raw.get("f13")) != "90":
                    raise ValueError("板块代码或市场身份不匹配")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(f"{code}板块名称缺失")
                if code in seen:
                    raise ValueError(f"{kind}目录出现重复代码{code}，分页可能漂移")
                seen.add(code)
                timestamp = _integer(raw.get("f124"), "来源更新时间")
                try:
                    updated = datetime.fromtimestamp(timestamp, SHANGHAI)
                except (ValueError, OverflowError, OSError) as exc:
                    raise ValueError("来源更新时间无效") from exc
                if timestamp <= 0 or updated > current + timedelta(minutes=5):
                    raise ValueError("来源更新时间缺失或晚于采集时间")
                source_dates.add(updated.date().isoformat())
                if len(source_dates) != 1:
                    raise ValueError("完整目录中的成交额日期不一致，不能合并排名")
                member = {"code": code, "name": name.strip(), "kind": kind,
                          "heat_value": _number(raw.get("f6"), "成交额"),
                          "heat_updated_at": updated.isoformat(), "ranking_as_of": updated.date().isoformat()}
                previous = all_members.get(code)
                if previous is not None:
                    if any(previous[key] != member[key] for key in ("name", "ranking_as_of")) or Decimal(previous["heat_value"]) != Decimal(member["heat_value"]):
                        raise ValueError(f"{code}跨分类重复记录的身份或成交额不一致")
                    continue
                all_members[code] = member
            page += 1
    return sorted(all_members.values(), key=lambda item: (-Decimal(item["heat_value"]), item["code"]))


def fetch_sector_daily(member: dict[str, Any], fetcher: Fetcher, *, timeout: float = 12.0) -> list[dict[str, str]]:
    end = date.fromisoformat(member["ranking_as_of"])
    start = end - timedelta(days=365)
    params = {"secid": "90." + member["code"], "fields1": "f1,f2,f3,f4,f5,f6",
              "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
              "klt": 101, "fqt": 0, "beg": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
              "smplmt": 10000, "lmt": 260}
    data = _data(fetcher(HISTORY_URL + "?" + urlencode(params), timeout))
    if data.get("code") != member["code"] or str(data.get("market")) != "90" or data.get("name") != member["name"]:
        raise ValueError("日线返回的板块代码、市场或名称与目录不一致")
    values = data.get("klines")
    if not isinstance(values, list) or not values:
        raise ValueError("来源未返回板块日线")
    rows, seen = [], set()
    for value in values:
        fields = value.split(",") if isinstance(value, str) else []
        if len(fields) < 7:
            raise ValueError("板块日线字段不完整")
        day = date.fromisoformat(fields[0])
        if day.isoformat() != fields[0] or not start <= day <= end or day in seen:
            raise ValueError("板块日线日期重复或超出请求范围")
        seen.add(day)
        row = {"date": fields[0]}
        for field, raw in zip(("open", "close", "high", "low", "volume", "amount"), fields[1:7]):
            row[field] = _number(raw, field, positive=field in ("open", "close", "high", "low"))
        opening, closing, high, low = (Decimal(row[key]) for key in ("open", "close", "high", "low"))
        if high < max(opening, closing) or low > min(opening, closing) or high < low:
            raise ValueError("板块日线OHLC关系无效")
        rows.append(row)
    return sorted(rows, key=lambda row: row["date"])


def fetch_sector_constituents(member: dict[str, Any], fetcher: Fetcher, *, timeout: float = 12.0) -> list[dict[str, Any]]:
    """Read the complete current membership; the source does not publish board weights."""
    total, page, seen, result = None, 1, set(), []
    while total is None or len(seen) < total:
        params = {"pn": page, "pz": 100, "po": 1,
                  "fid": "f20", "fs": f"b:{member['code']}",
                  "fields": "f12,f13,f14,f20"}
        data = _data(fetcher(CONSTITUENT_URL + "?" + urlencode(params), timeout))
        page_total = _integer(data.get("total"), "成分总数")
        if page_total <= 0 or page_total > 10000 or (total is not None and page_total != total):
            raise ValueError("成分总数无效或采集中发生变化")
        total = page_total
        rows = data.get("diff")
        if not isinstance(rows, list) or len(rows) != min(100, total - len(seen)):
            raise ValueError(f"{member['code']}成分第{page}页缺失")
        for raw in rows:
            if not isinstance(raw, dict):
                raise ValueError("成分行不是对象")
            code, name = raw.get("f12"), raw.get("f14")
            market = _integer(raw.get("f13"), "成分市场")
            if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code) or market not in (0, 1, 116):
                raise ValueError("成分证券代码或市场无效")
            if not isinstance(name, str) or not name.strip() or code in seen:
                raise ValueError("成分证券名称缺失或代码重复")
            seen.add(code)
            market_cap = _number(raw.get("f20"), "成分总市值")
            result.append({"stock_code": code, "stock_name": name.strip(), "market": market,
                           "source_order": len(result) + 1, "market_cap": market_cap})
        page += 1
    return result


class _Throttle:
    def __init__(self, fetcher: Fetcher, interval: float):
        self.fetcher, self.interval = fetcher, interval
        self.lock, self.last_start = Lock(), 0.0

    def __call__(self, url: str, timeout: float) -> dict[str, Any]:
        with self.lock:
            remaining = self.interval - (time.monotonic() - self.last_start)
            if remaining > 0:
                time.sleep(remaining)
            self.last_start = time.monotonic()
        return self.fetcher(url, timeout)


def refresh_sector_heat(settings: Settings, *, fetcher: Fetcher | None = None, timeout: float = 12.0,
                        workers: int = 3, throttle_seconds: float = 0.2,
                        progress: Callable[[str], None] | None = None,
                        now: datetime | None = None, industry_only: bool = False) -> dict[str, Any]:
    """Freeze a full top-100 ranking, collect history, and publish one snapshot.

    A failed directory refresh only records the error, preserving old rankings.
    History errors preserve existing real rows and their original fetch time.
    """
    if not 0 < timeout <= 12 or not 1 <= workers <= 3 or throttle_seconds < 0:
        raise ValueError("超时须在(0,12]秒，并发须在1–3，节流间隔不能为负")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("采集时间必须包含时区")
    attempted_at = current.isoformat(timespec="seconds")
    request = _Throttle(fetcher or fetch_json, max(0.2, throttle_seconds) if fetcher is None else throttle_seconds)
    previous_snapshot = read_sector_heat(settings)
    try:
        directory = fetch_sector_directory(request, timeout=timeout, now=current,
                                           source_groups=(("行业", 2),) if industry_only else (("行业", 2), ("概念", 3)))
        old_ranking_date = previous_snapshot["universe"]["ranking_as_of"]
        if old_ranking_date and directory[0]["ranking_as_of"] < old_ranking_date:
            raise ValueError("来源排名日期早于已保存排名，不能用旧榜覆盖新榜")
    except Exception as exc:
        record_sector_heat_failure(settings, attempted_at=attempted_at,
                                   error=f"热度榜刷新失败，保留上次快照：{type(exc).__name__}: {exc}")
        raise RuntimeError(f"热度榜刷新失败，未替换原排名：{exc}") from exc
    previous = {item["code"]: item for item in previous_snapshot["items"]}
    if industry_only and fetcher is None:
        candidates = [dict(item, heat_rank=rank) for rank, item in enumerate(directory, 1)]
        ths_crosswalk = build_ths_history_crosswalk(candidates, timeout=timeout)
        selected = [item for item in candidates if item["code"] in ths_crosswalk]
        members = selected
    else:
        members = [dict(item, heat_rank=rank) for rank, item in enumerate(directory[:100], 1)]
        ths_crosswalk = {}
    identity_candidates = [dict(member, membership=previous.get(member["code"], {}).get("membership", {}))
                           for member in members]
    if fetcher is None and not ths_crosswalk:
        ths_crosswalk = build_ths_history_crosswalk(identity_candidates, timeout=timeout)
    if progress:
        progress(f"完整目录 {len(directory)} 个板块；纳入 {len(members)} 个可核验板块，开始采集日线。")

    def collect(member: dict[str, Any]) -> dict[str, Any]:
        old = previous.get(member["code"], {})
        # A changed identity cannot inherit another board's stored history.
        if old.get("name") != member["name"]:
            old = {}
        rows, updated_at, error = old.get("rows", []), old.get("updated_at"), None
        history_source_id = old.get("history_source_id")
        history_source_code = old.get("history_source_code")
        history_identity_match = old.get("history_identity_match")
        old_membership = old.get("membership", {})
        components = old_membership.get("members", []) if old_membership.get("as_of") == member["ranking_as_of"] else []
        component_updated_at = old_membership.get("fetched_at") if components else None
        try:
            ths_code = ths_crosswalk.get(member["code"])
            fetched = (fetch_ths_sector_daily(member, ths_code, timeout=timeout)
                       if industry_only and ths_code else fetch_sector_daily(member, request, timeout=timeout))
            latest_is_previous_settlement = (industry_only and current.astimezone(SHANGHAI).hour < 16
                                             and member["ranking_as_of"] == current.astimezone(SHANGHAI).date().isoformat()
                                             and 0 < (date.fromisoformat(member["ranking_as_of"]) - date.fromisoformat(fetched[-1]["date"])).days <= 4)
            if fetched[-1]["date"] != member["ranking_as_of"] and not latest_is_previous_settlement:
                error = f"日线仅到{fetched[-1]['date']}，排名日期为{member['ranking_as_of']}"
            if industry_only or not rows or fetched[-1]["date"] >= rows[-1]["date"]:
                rows = fetched
                updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds") if now is None else attempted_at
                if industry_only and ths_code:
                    history_source_id, history_source_code = "sector_daily.ths", "bk_" + ths_code
                    history_identity_match = "category_and_exact_name"
                else:
                    history_source_id, history_source_code = "sector_daily.eastmoney", "90." + member["code"]
                    history_identity_match = "native_source_code"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            ths_code = ths_crosswalk.get(member["code"])
            if ths_code and not industry_only:
                try:
                    fetched = fetch_ths_sector_daily(member, ths_code, timeout=timeout)
                    rows = fetched
                    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds") if now is None else attempted_at
                    history_source_id, history_source_code = "sector_daily.ths", "bk_" + ths_code
                    history_identity_match = ("category_name_alias_and_constituent_overlap"
                                              if member["name"] in THS_VERIFIED_ALIAS_CANDIDATES
                                              else "category_and_exact_name")
                    error = None if fetched[-1]["date"] == member["ranking_as_of"] else f"日线仅到{fetched[-1]['date']}，排名日期为{member['ranking_as_of']}"
                except Exception as fallback_exc:
                    error += f"；同花顺严格同名备用源失败：{type(fallback_exc).__name__}: {fallback_exc}"
        if error:
            error += f"；当前保留旧行情至{rows[-1]['date']}" if rows else "；暂无可用日线"
        component_error = None
        try:
            components = fetch_sector_constituents(member, request, timeout=timeout)
            component_updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds") if now is None else attempted_at
        except Exception as exc:
            component_error = f"{type(exc).__name__}: {exc}"
            component_error += f"；当前保留{len(components)}个旧成分" if components else "；暂无可用成分"
        return {**member, "rows": rows, "updated_at": updated_at, "collection_error": error,
                "history_source_id": history_source_id, "history_source_code": history_source_code,
                "history_identity_match": history_identity_match,
                "constituents": components, "constituent_error": component_error,
                "constituent_updated_at": component_updated_at}

    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(collect, member) for member in members]
        for future in as_completed(futures):
            results.append(future.result())
            if progress and (len(results) % 10 == 0 or len(results) == len(members)):
                failures = sum(item["collection_error"] is not None for item in results)
                progress(f"日线采集 {len(results)}/{len(members)}，失败或旧行情 {failures}。")
    results.sort(key=lambda item: item["heat_rank"])
    completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds") if now is None else attempted_at
    save_sector_heat(settings, as_of=members[0]["ranking_as_of"], updated_at=completed_at,
                     catalog_count=len(directory), members=results, requested_count=len(members),
                     universe_scope="verified_industry_all" if industry_only else "hot_board_top100")
    return read_sector_heat(settings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database")
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--industry-only", action="store_true", help="只纳入能严格映射到标准日线的全部行业板块")
    args = parser.parse_args(argv)
    settings = Settings(database_path=Path(args.database)) if args.database else Settings.from_env()
    try:
        result = refresh_sector_heat(settings, timeout=args.timeout, workers=args.workers, industry_only=args.industry_only,
                                     progress=lambda message: print(message, flush=True))
    except Exception as exc:
        print(f"采集失败：{exc}", file=sys.stderr)
        return 2
    universe = result["universe"]
    print(f"排名日期 {universe['as_of']}；热门 {universe['member_count']}；"
          f"采集完成 {universe['collected_count']}；失败或旧行情 {universe['failed_count']}。", flush=True)
    return 0 if universe["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
