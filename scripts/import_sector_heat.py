#!/usr/bin/env python3
"""Refresh Tonghuashun sector boards for market context only."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
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
from backend.storage.sector_heat import (SectorHistoryIntegrityError, merge_consistent_sector_history,
                                         read_sector_heat, record_sector_heat_failure, save_sector_heat)

SHANGHAI = ZoneInfo("Asia/Shanghai")
Fetcher = Callable[[str, float], dict[str, Any]]
COLLECTOR_VERSION = "sector-ths-only-v5-history-integrity"


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
    return get_bytes(request, timeout=timeout).decode("gb18030", "strict")


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


def fetch_ths_sector_daily(member: dict[str, Any], ths_code: str, *, timeout: float = 12.0) -> list[dict[str, str]]:
    payload = fetch_text(f"https://d.10jqka.com.cn/v4/line/bk_{ths_code}/01/last.js", timeout)
    left, right = payload.find("{"), payload.rfind("}")
    if left < 0 or right <= left:
        raise ValueError("同花顺日线JSONP无效")
    root = json.loads(payload[left:right + 1], parse_float=str, parse_int=str)
    if str(root.get("name", "")).strip() != member["name"]:
        raise ValueError("同花顺日线名称与行业目录不一致")
    cutoff = min(date.fromisoformat(member["ranking_as_of"]),
                 date.fromisoformat(member["price_cutoff"]) if member.get("price_cutoff")
                 else get_calendar().latest_completed(datetime.now(SHANGHAI)))
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
    return rows


def _source_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:500]}"


def fetch_ths_industry_directory(*, now: datetime | None = None) -> dict[str, Any]:
    """Return industry rows together with truthful directory/summary provenance."""
    current = now or datetime.now(timezone.utc)
    cutoff = get_calendar().latest_completed(current.astimezone(SHANGHAI)).isoformat()
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
        rows.append({"code": code, "name": name, "kind": "行业", "source_id": "sector_daily.ths",
                     "heat_value": format(turnover_by_name.get(name, Decimal(0)), "f"),
                     "heat_updated_at": current.astimezone(SHANGHAI).isoformat(timespec="seconds"),
                     "ranking_as_of": cutoff})
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
        url = f"https://q.10jqka.com.cn/thshy/detail/page/{page}/code/{code}/"
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
        old_ranking_date = previous_snapshot["universe"]["ranking_as_of"]
        if old_ranking_date and directory[0]["ranking_as_of"] < old_ranking_date:
            raise ValueError("来源排名日期早于已保存排名，不能用旧榜覆盖新榜")
    except Exception as exc:
        record_sector_heat_failure(settings, attempted_at=attempted_at,
                                   error=f"同花顺行业目录刷新失败，保留上次快照：{type(exc).__name__}: {exc}")
        raise RuntimeError(f"同花顺行业目录刷新失败，未替换原排名：{exc}") from exc
    previous = {item["code"]: item for item in previous_snapshot["items"]}
    members = [dict(item, heat_rank=rank) for rank, item in enumerate(directory, 1)]
    if progress:
        progress(f"同花顺行业目录 {len(directory)} 个板块，开始采集日线。")

    def collect(member: dict[str, Any]) -> dict[str, Any]:
        member = {**member, "price_cutoff": get_calendar().latest_completed(current).isoformat()}
        old = previous.get(member["code"], {})
        if old.get("name") != member["name"]:
            old = {}
        rows, updated_at, error = old.get("rows", []), old.get("updated_at"), None
        old_membership = old.get("membership", {})
        components = old_membership.get("members", []) if old_membership.get("as_of") == member["ranking_as_of"] else []
        component_updated_at = old_membership.get("fetched_at") if components else None
        attempts: list[dict] = []
        native_source_id, native_source_code = "sector_daily.ths", "bk_" + member["code"]
        native = old.get("history_source_id") == native_source_id and old.get("history_source_code") == native_source_code
        fetched: list[dict[str, Any]] = []
        try:
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
                             "endpoint": "https://d.10jqka.com.cn/v4/line/",
                             "status": "available", "row_count": len(fetched),
                             "first_date": fetched[0]["date"], "last_date": fetched[-1]["date"],
                             "error": None, **merge_details})
            updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds") if now is None else attempted_at
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            attempts.append({"dataset": "daily", "subject": member["code"],
                             "endpoint": "https://d.10jqka.com.cn/v4/line/", "status": "failed",
                             "row_count": len(fetched),
                             "first_date": fetched[0]["date"] if fetched else None,
                             "last_date": fetched[-1]["date"] if fetched else None,
                             "error": error})
            if native and rows and not isinstance(exc, SectorHistoryIntegrityError):
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
            components = fetch_ths_visible_constituents(
                member, timeout=timeout, throttle_seconds=throttle_seconds,
            )
            attempts.append({"dataset": "constituents", "subject": member["code"],
                             "endpoint": "https://q.10jqka.com.cn/thshy/detail/page/{page}/code/",
                             "status": "available", "row_count": len(components), "error": None})
            component_updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds") if now is None else attempted_at
        except Exception as exc:
            component_error = f"{type(exc).__name__}: {exc}"
            component_error += f"；当前保留{len(components)}个旧同花顺成分" if components else "；暂无可用成分"
            attempts.append({"dataset": "constituents", "subject": member["code"],
                             "endpoint": "https://q.10jqka.com.cn/thshy/detail/page/{page}/code/",
                             "status": "failed", "row_count": 0, "error": component_error})
        return {**member, "rows": rows, "updated_at": updated_at, "collection_error": error,
                "history_source_id": native_source_id, "history_source_code": native_source_code,
                "history_identity_match": "native_source_code",
                "constituents": components, "constituent_error": component_error,
                "constituent_updated_at": component_updated_at, "source_attempts": attempts}

    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(collect, member) for member in members]
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
    completed = get_calendar().latest_completed(current)
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
