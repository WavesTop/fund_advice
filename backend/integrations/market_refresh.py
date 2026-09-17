"""Bounded local collection with durable target exclusion and fixed research capture."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from threading import Lock

from backend.core.config import Settings
from backend.core.errors import AppError
from backend.analysis.sector_status import sector_opportunities
from backend.storage.sector_heat import record_sector_heat_failure
from backend.storage.collection_runs import start_run, finish_run
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
TIMEOUTS = {"catalog": 90, "sectors": 600}
_locks: dict[tuple[str, str], Lock] = {}
_guard = Lock()


def refresh_market_data(settings: Settings, target: str) -> dict:
    if target not in TIMEOUTS:
        raise AppError("invalid_refresh_target", "采集目标无效", 422)
    database = str(settings.database_path.resolve())
    with _guard:
        lock = _locks.setdefault((database, target), Lock())
    if not lock.acquire(blocking=False):
        raise AppError("market_refresh_in_progress", "同一数据源正在联网采集中，请勿重复提交", 409)
    run_id = None
    terminal_state, terminal_result = "failed", {"message": "采集未正常完成"}
    try:
        run_id = start_run(settings, target)
        # A subprocess bounds even an upstream library which fails to honor HTTP timeouts.
        try:
            process = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "scripts.refresh_market_data", "--target", target, "--database", database, "--run-id", run_id],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUTS[target], check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AppError("market_refresh_timeout", "采集超时并已停止。请重新读取本地数据核对已提交部分；不能视为更新成功。", 504) from exc
        except OSError as exc:
            raise AppError("market_refresh_unavailable", "无法启动采集进程，请核对本地 Python 环境", 503) from exc
        try:
            result = json.loads(process.stdout)
            if (not isinstance(result, dict) or result.get("target") != target
                    or result.get("status") not in ("success", "partial", "failed")
                    or not isinstance(result.get("message"), str)):
                raise ValueError("invalid worker response")
        except (TypeError, ValueError) as exc:
            raise AppError("market_refresh_invalid", "采集进程未返回有效结果，请重新读取本地资料核对状态", 502) from exc
        if process.returncode != 0 or result["status"] == "failed":
            raise AppError("market_refresh_failed", result["message"], 502, {"refresh": result})
        if target == "sectors":
            try:
                evaluation = sector_opportunities(settings)
            except Exception as exc:
                raise AppError("market_evaluation_failed", "行情采集已完成，但评估失败；不能把本次操作认定为评估成功。",
                               500, {"refresh": result}) from exc
            # A research failure is distinct from source collection and from the legacy display.
            # No snapshot gap can silently enable an investment recommendation.
            try:
                from backend.integrations.research_inputs import capture_current_inputs
                from backend.analysis.research_pipeline import run_snapshot
                captured = capture_current_inputs(settings)
                fixed = run_snapshot(settings, captured["snapshot_id"])
                research = {"status": "recorded_unvalidated", **captured, "run_id": fixed["run_id"]}
            except Exception as exc:
                research = {"status": "failed", "reason": f"{type(exc).__name__}: {str(exc)[:1000]}"}
            terminal_state, terminal_result = result["status"], {**result, "research": research}
            return {"refresh": result, "evaluation": evaluation, "api_contract": "market-workbench-v2",
                    "collection_run_id": run_id, "research": research}
        terminal_state, terminal_result = result["status"], result
        return {"refresh": result, "collection_run_id": run_id}
    except AppError as exc:
        terminal_state = "timeout" if exc.status_code == 504 else "failed"
        terminal_result = {"code": exc.body.code, "message": exc.body.message, "details": exc.body.details}
        if run_id is not None and target == "sectors" and exc.body.code != "market_evaluation_failed":
            record_sector_heat_failure(settings, attempted_at=datetime.now(timezone.utc).isoformat(), error=exc.body.message)
        raise
    finally:
        try:
            if run_id is not None:
                finish_run(settings, run_id, terminal_state, terminal_result)
        finally:
            lock.release()
