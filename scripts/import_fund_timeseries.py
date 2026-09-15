#!/usr/bin/env python3
"""Import one verified fund price or NAV series into the local SQLite database."""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.config import Settings
from backend.storage.catalog import list_catalog
from backend.storage.timeseries import import_timeseries
from scripts.data_source_registry import load_registry, sources_for
from scripts.fund_nav_eastmoney import fetch_paged_nav
from scripts.probe_fund_source import ProbeError
from scripts.probe_fund_timeseries import normalize, run_adapter


def _code(value: str) -> str:
    if not isinstance(value, str) or len(value) != 6 or not value.isascii() or not value.isdigit():
        raise ValueError("基金代码必须是保留前导零的六位数字")
    return value


def _date(value: str) -> str:
    try:
        parsed = dt.datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError("日期必须是YYYYMMDD") from exc
    if parsed.strftime("%Y%m%d") != value:
        raise ValueError("日期必须是YYYYMMDD")
    return value


def series_dataset(fund: dict[str, object]) -> str | None:
    """Choose the reviewed exchange adapter only when the catalog identity says ETF/LOF."""
    identity = f"{fund.get('name', '')} {fund.get('fund_type', '')}".upper()
    # ETF linked funds are ordinary off-exchange shares with daily NAV.  Their
    # names contain "ETF", so they must be excluded before exchange matching.
    if "联接" in identity:
        return None
    if "ETF" in identity:
        return "etf-daily"
    if "LOF" in identity:
        return "lof-daily"
    return None


def _catalog_item(settings: Settings, code: str) -> dict[str, object]:
    result = list_catalog(settings, code, page=1, page_size=20)
    matches = [item for item in result["items"] if item["code"] == code]
    if not matches:
        raise ValueError("本地真实基金目录中不存在该代码")
    return matches[0]


def _automatic_sources(dataset: str, registry_path: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    registry = load_registry(registry_path) if registry_path else load_registry()
    sources = sources_for(dataset, "candidate_fact", path=registry_path) if registry_path else sources_for(dataset, "candidate_fact")
    allowed = [source for source in sources if source["automatic"] and source["adapter"] is not None]
    if not allowed:
        raise ValueError("注册表中没有可自动采集的已核验来源")
    return registry, allowed


def _fetch(dataset: str, code: str, start_date: str, end_date: str, source: dict[str, Any], timeout: float,
           before_request: Any = None) -> list[dict[str, object]]:
    if before_request is not None:
        before_request()
    payload, _ = run_adapter({
        "dataset": dataset, "code": code, "start_date": start_date, "end_date": end_date,
        "provider": source["provider"],
    }, timeout)
    return normalize(dataset, payload, start_date, end_date)["normalized_rows"]


def _import_exchange(settings: Settings, code: str, dataset: str, start_date: str, end_date: str,
                     registry_path: str | None, timeout: float, before_request: Any = None) -> dict[str, object]:
    registry, sources = _automatic_sources("exchange_daily", registry_path)
    if dataset == "lof-daily":
        sources = [source for source in sources if source["provider"] != "baostock"]
    failures = []
    for source in sources:
        try:
            rows = _fetch(dataset, code, start_date, end_date, source, timeout, before_request)
            if not rows:
                failures.append(f"{source['id']}: 空数据")
                continue
            projection_rows = [{
                "date": row["date"], "open": row["open"], "high": row["high"], "low": row["low"],
                "close": row["close"], "volume": row["volume_shares"], "amount": row["amount_cny"],
            } for row in rows]
            return import_timeseries(settings, code, "price", projection_rows,
                                     source_id=source["id"], policy_version=registry["policy_version"])
        except ProbeError as exc:
            failures.append(f"{source['id']}: {exc.code}")
            if exc.code not in {"network_error", "network_timeout", "timeout", "upstream_failure", "truncated_response"}:
                raise
    raise RuntimeError("场内日行情没有可写入的数据（" + "；".join(failures) + "）")


def _import_nav(settings: Settings, code: str, start_date: str, end_date: str,
                registry_path: str | None, timeout: float, before_request: Any = None) -> dict[str, object]:
    registry, sources = _automatic_sources("fund_nav", registry_path)
    source = sources[0]
    if source["provider"] != "eastmoney":
        raise ValueError("注册表中的基金净值自动来源不是东方财富")
    # The F10 endpoint returns both NAV values on the same source row.  It is
    # paged explicitly instead of using pingzhongdata.js, whose large payload
    # can be silently truncated for funds such as 000001.
    if before_request is None:
        rows = fetch_paged_nav(code, start_date, end_date, timeout)
    else:
        rows = fetch_paged_nav(code, start_date, end_date, timeout, before_request=before_request)
    return import_timeseries(settings, code, "nav", rows, source_id=source["id"],
                             policy_version=registry["policy_version"])


def refresh_fund_timeseries(settings: Settings, code: str, *, start_date: str | None = None,
                            end_date: str | None = None, registry_path: str | None = None,
                            timeout: float = 30.0, before_request: Any = None) -> dict[str, object]:
    """Fetch and atomically replace the reviewed real series for one catalog fund.

    The API uses this same entry point as the command line importer.  Every fetch is
    completed before ``import_timeseries`` commits, so a failed refresh leaves the
    previously persisted series untouched.
    """
    code = _code(code)
    end_date = _date(end_date or dt.date.today().strftime("%Y%m%d"))
    if start_date is None:
        end_day = dt.datetime.strptime(end_date, "%Y%m%d").date()
        start_date = (end_day - dt.timedelta(days=366 * 6)).strftime("%Y%m%d")
    start_date = _date(start_date)
    if start_date > end_date or timeout <= 0:
        raise ValueError("日期范围或超时参数无效")
    fund = _catalog_item(settings, code)
    dataset = series_dataset(fund)
    if dataset:
        return _import_exchange(settings, code, dataset, start_date, end_date, registry_path, timeout, before_request)
    return _import_nav(settings, code, start_date, end_date, registry_path, timeout, before_request)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", required=True)
    parser.add_argument("--start-date", default="20000101")
    parser.add_argument("--end-date", default=dt.date.today().strftime("%Y%m%d"))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--database", default=None)
    parser.add_argument("--registry", default=None)
    args = parser.parse_args(argv)
    try:
        code, start_date, end_date = _code(args.code), _date(args.start_date), _date(args.end_date)
        if start_date > end_date or args.timeout <= 0:
            raise ValueError("日期范围或超时参数无效")
        settings = Settings.from_env() if args.database is None else Settings(database_path=Path(args.database))
        fund = _catalog_item(settings, code)
        dataset = series_dataset(fund)
        result = (_import_exchange(settings, code, dataset, start_date, end_date, args.registry, args.timeout)
                  if dataset else _import_nav(settings, code, start_date, end_date, args.registry, args.timeout))
    except Exception as exc:
        print(f"导入失败: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
