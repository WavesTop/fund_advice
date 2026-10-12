"""Bounded web-worker entry; CLI collectors remain the only upstream import paths."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

from backend.core.config import Settings
from scripts.import_fund_catalog import refresh_fund_catalog
from scripts.import_sector_heat import refresh_sector_heat, refresh_sector_quotes
from scripts.import_related_market import refresh_existing_indexes
from backend.integrations.sector_financials import collect_sector_fundamentals
from scripts.import_sector_heat import COLLECTOR_VERSION


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def progress_event(stage: str, state: str, percent: int, message: str, **extra) -> dict:
    return {"stage": stage, "state": state, "percent": max(0, min(100, percent)), "message": message, **extra}


def collect(settings: Settings, target: str, *, progress=None, mode: str = "full") -> dict:
    if mode not in ("full", "quotes"):
        raise ValueError("刷新模式无效")
    started = now()
    if target == "catalog":
        result = refresh_fund_catalog(settings)
        return {"target": target, "status": "success", "started_at": started, "completed_at": now(),
                "row_count": result["row_count"], "updated_at": result["updated_at"],
                "message": f"基金目录已联网更新，共导入 {result['row_count']} 条份额身份；净值和 K 线请在基金详情更新。"}
    if target != "sectors":
        raise ValueError("不支持的采集目标")
    if progress:
        progress(progress_event("market", "running", 5, "正在读取板块库清单。"))

    def market_progress(message: object) -> None:
        if not progress:
            return
        text = str(message)
        match = re.search(r"采集\s+(\d+)/(\d+)", text)
        if match:
            done, total = int(match.group(1)), int(match.group(2))
            percent = 10 + round(50 * done / max(total, 1))
            progress(progress_event("market", "running", percent, text, current=done, total=total))
        else:
            progress(progress_event("market", "running", 8, text))

    result = (refresh_sector_quotes(settings, progress=market_progress if progress else None) if mode == "quotes"
              else refresh_sector_heat(settings, industry_only=True, progress=market_progress if progress else None))
    if progress:
        progress(progress_event("market", "running", 62, "同花顺板块/概念采集完成，正在刷新参考指数。"))

    def index_progress(event: dict) -> None:
        if not progress:
            return
        current = int(event.get("current") or 0)
        total = int(event.get("total") or 0)
        percent = 64 + round(10 * current / max(total, 1)) if total else 64
        progress(progress_event("market", "running", percent, str(event.get("message") or "正在刷新参考指数。"),
                                current=current, total=total))

    index_result = refresh_existing_indexes(settings, progress=index_progress if progress else None)
    if progress:
        progress(progress_event("market", "completed", 76, "板块库行情刷新完成，正在整理证据。"))
    items = result["items"]
    index_items = index_result.get("items", [])
    price_ok = sum(bool(item.get("rows")) and not item.get("collection_error") for item in items)
    members_ok = sum(item.get("membership", {}).get("status") == "ready" for item in items)
    index_ok = sum(item.get("status") == "success" for item in index_items)
    index_failed = len(index_items) - index_ok
    universe = result["universe"]
    requested = universe.get("requested_count", len(items))
    catalog_count = universe.get("catalog_count", requested)
    if (not isinstance(requested, int) or isinstance(requested, bool) or requested < len(items)
            or not isinstance(catalog_count, int) or isinstance(catalog_count, bool) or catalog_count < requested):
        raise ValueError("行业采集覆盖统计不一致，不能认定更新成功")
    raw_directory_provenance = result.get("directory_provenance")
    directory_provenance = raw_directory_provenance if isinstance(raw_directory_provenance, dict) else {}
    raw_directory = directory_provenance.get("directory")
    directory_mode = raw_directory.get("mode") if isinstance(raw_directory, dict) else None
    status = "failed" if not price_ok else "success" if (
        price_ok == members_ok == requested and not universe.get("last_error") and directory_mode == "live"
    ) else "partial"
    if mode == "quotes":
        status = "failed" if not price_ok else "success" if price_ok == requested else "partial"
    if status == "success" and index_failed:
        status = "partial"
    if progress and mode == "full":
        progress(progress_event("financials", "running", 82, "正在核对成分经营与估值资料。"))
    financials = ({"status": "not_requested", "message": "行情快速刷新保留原有经营、估值和成分资料。"} if mode == "quotes"
                  else collect_sector_fundamentals(settings, items, progress=progress) if progress
                  else collect_sector_fundamentals(settings, items))
    if progress:
        progress(progress_event("financials", financials["status"], 95, financials["message"]))
    if mode == "full" and status == "success" and financials["status"] != "success":
        status = "partial"
    window_coverage = result.get("price_window_coverage")
    if status == "success" and window_coverage is not None and any(
            window_coverage.get(key) != requested for key in ("short", "medium", "long")):
        status = "partial"
    errors = [{"code": item["code"], "message": item.get("collection_error") or item.get("membership", {}).get("error")}
              for item in items if item.get("collection_error") or (mode == "full" and item.get("membership", {}).get("error"))]
    errors.extend({"code": str(item.get("code")), "message": str(item.get("error"))}
                  for item in index_items if item.get("status") == "failed")
    return {"target": target, "mode": mode, "status": status, "started_at": started, "completed_at": now(),
            "ranking_as_of": universe["ranking_as_of"], "requested_count": requested,
            "catalog_count": catalog_count, "identity_excluded_count": catalog_count - requested,
            "collector_version": COLLECTOR_VERSION,
            "source_attempts": result.get("source_attempts", []) + index_result.get("source_attempts", []),
            "directory_provenance": directory_provenance,
            "price_cutoff": result.get("price_cutoff"), "price_window_coverage": window_coverage,
            "price_updated": price_ok, "price_failed": requested - price_ok,
            "index_updated": index_ok, "index_failed": index_failed,
            "membership_updated": members_ok if mode == "full" else 0,
            "membership_failed": requested - members_ok if mode == "full" else 0,
            "errors": errors, "manual_evidence_refreshed": bool(index_items),
            "reference_index_refreshed": bool(index_items), "fundamentals": financials,
            "message": (f"最新行情成功 {price_ok}/{requested}，参考指数成功 {index_ok}/{len(index_items)}；经营、估值和成分资料保留原有更新时间。" if mode == "quotes" else
                       f"目录 {catalog_count} 个，纳入行业 {requested} 个（失败项不剔除）；日线成功 {price_ok}/{requested}，成分成功 {members_ok}/{requested}。"
                       f"参考指数成功 {index_ok}/{len(index_items)}。"
                       f"成分经营与同日估值：{financials['message']}。"
                       "本次以当前板块库清单为准；失败项不计作本轮完整研究。")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("catalog", "sectors"), required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--mode", choices=("full", "quotes"), default="full")
    args = parser.parse_args(argv)
    settings = Settings(database_path=Path(args.database))
    if args.run_id:
        from backend.integrations.http_transport import set_observer
        from backend.storage.collection_runs import record_attempt, record_progress
        set_observer(lambda event: record_attempt(settings, args.run_id, event))
    started = now()
    try:
        # Upstream libraries may write diagnostics; stdout is reserved for this JSON contract.
        with redirect_stdout(sys.stderr):
            result = collect(settings, args.target, mode=args.mode, progress=(
                (lambda event: record_progress(settings, args.run_id, event)) if args.run_id else None))
    except Exception as exc:
        result = {"target": args.target, "status": "failed", "started_at": started,
                  "completed_at": now(), "message": f"采集未完成：{type(exc).__name__}: {str(exc)[:1000]}"}
    result["collector_version"] = COLLECTOR_VERSION
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 2 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
