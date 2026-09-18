"""Bounded web-worker entry; CLI collectors remain the only upstream import paths."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from backend.core.config import Settings
from scripts.import_fund_catalog import refresh_fund_catalog
from scripts.import_sector_heat import refresh_sector_heat
from backend.integrations.sector_financials import collect_sector_fundamentals


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def collect(settings: Settings, target: str, *, progress=None) -> dict:
    started = now()
    if target == "catalog":
        result = refresh_fund_catalog(settings)
        return {"target": target, "status": "success", "started_at": started, "completed_at": now(),
                "row_count": result["row_count"], "updated_at": result["updated_at"],
                "message": f"基金目录已联网更新，共导入 {result['row_count']} 条份额身份；净值和 K 线请在基金详情更新。"}
    if target != "sectors":
        raise ValueError("不支持的采集目标")
    if progress:
        progress({"stage": "market", "state": "running"})
    result = refresh_sector_heat(settings, industry_only=True)
    if progress:
        progress({"stage": "market", "state": "completed"})
    items = result["items"]
    price_ok = sum(bool(item.get("rows")) and not item.get("collection_error") for item in items)
    members_ok = sum(item.get("membership", {}).get("status") == "ready" for item in items)
    universe = result["universe"]
    requested = universe.get("requested_count", len(items))
    catalog_count = universe.get("catalog_count", requested)
    if (not isinstance(requested, int) or isinstance(requested, bool) or requested < len(items)
            or not isinstance(catalog_count, int) or isinstance(catalog_count, bool) or catalog_count < requested):
        raise ValueError("行业采集覆盖统计不一致，不能认定更新成功")
    status = "failed" if not price_ok else "success" if price_ok == members_ok == requested and not universe.get("last_error") else "partial"
    if progress:
        progress({"stage": "financials", "state": "running"})
    financials = (collect_sector_fundamentals(settings, items, progress=progress) if progress
                  else collect_sector_fundamentals(settings, items))
    if progress:
        progress({"stage": "financials", "state": financials["status"]})
    if status == "success" and financials["status"] != "success":
        status = "partial"
    errors = [{"code": item["code"], "message": item.get("collection_error") or item.get("membership", {}).get("error")}
              for item in items if item.get("collection_error") or item.get("membership", {}).get("error")]
    return {"target": target, "status": status, "started_at": started, "completed_at": now(),
            "ranking_as_of": universe["ranking_as_of"], "requested_count": requested,
            "catalog_count": catalog_count, "identity_excluded_count": catalog_count - requested,
            "price_updated": price_ok, "price_failed": requested - price_ok,
            "membership_updated": members_ok, "membership_failed": requested - members_ok,
            "errors": errors, "manual_evidence_refreshed": False, "fundamentals": financials,
            "message": f"目录 {catalog_count} 个，纳入可核验行业 {requested} 个；日线成功 {price_ok}/{requested}，成分成功 {members_ok}/{requested}。"
                       f"成分经营与同日估值：{financials['message']}。"
                       "旧人工背景及已有参考指数未由本按钮更新；失败项不计作本轮完整研究。"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("catalog", "sectors"), required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--run-id")
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
            result = collect(settings, args.target, progress=(
                (lambda event: record_progress(settings, args.run_id, event)) if args.run_id else None))
    except Exception as exc:
        result = {"target": args.target, "status": "failed", "started_at": started,
                  "completed_at": now(), "message": f"采集未完成：{type(exc).__name__}: {str(exc)[:1000]}"}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 2 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
