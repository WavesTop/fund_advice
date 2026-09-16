"""Real, bounded manual collection for the single-process local web application."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from threading import Lock

from backend.core.config import Settings
from backend.core.errors import AppError

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
    try:
        # A subprocess bounds even an upstream library which fails to honor HTTP timeouts.
        try:
            process = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "scripts.refresh_market_data", "--target", target, "--database", database],
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
        return {"refresh": result}
    finally:
        lock.release()
