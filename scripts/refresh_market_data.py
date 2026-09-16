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


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def collect(settings: Settings, target: str) -> dict:
    started = now()
    if target == "catalog":
        result = refresh_fund_catalog(settings)
        return {"target": target, "status": "success", "started_at": started, "completed_at": now(),
                "row_count": result["row_count"], "updated_at": result["updated_at"],
                "message": f"基金目录已联网更新，共导入 {result['row_count']} 条份额身份；净值和 K 线请在基金详情更新。"}
    if target != "sectors":
        raise ValueError("不支持的采集目标")
    result = refresh_sector_heat(settings, industry_only=True)
    items = result["items"]
    price_ok = sum(bool(item.get("rows")) and not item.get("collection_error") for item in items)
    members_ok = sum(item.get("membership", {}).get("status") == "ready" for item in items)
    status = "failed" if not price_ok else "success" if price_ok == members_ok == len(items) else "partial"
    errors = [{"code": item["code"], "message": item.get("collection_error") or item.get("membership", {}).get("error")}
              for item in items if item.get("collection_error") or item.get("membership", {}).get("error")]
    return {"target": target, "status": status, "started_at": started, "completed_at": now(),
            "ranking_as_of": result["universe"]["ranking_as_of"], "requested_count": len(items),
            "price_updated": price_ok, "price_failed": len(items) - price_ok,
            "membership_updated": members_ok, "membership_failed": len(items) - members_ok,
            "errors": errors, "manual_evidence_refreshed": False,
            "message": f"行业日线成功 {price_ok}/{len(items)}，成分成功 {members_ok}/{len(items)}。"
                       "失败项保留可核对的旧资料；行业经营和估值人工快照、已有参考指数未由本按钮更新。"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("catalog", "sectors"), required=True)
    parser.add_argument("--database", required=True)
    args = parser.parse_args(argv)
    started = now()
    try:
        # Upstream libraries may write diagnostics; stdout is reserved for this JSON contract.
        with redirect_stdout(sys.stderr):
            result = collect(Settings(database_path=Path(args.database)), args.target)
    except Exception as exc:
        result = {"target": args.target, "status": "failed", "started_at": started,
                  "completed_at": now(), "message": f"采集未完成：{type(exc).__name__}: {str(exc)[:1000]}"}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 2 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
