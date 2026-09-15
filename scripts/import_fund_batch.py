#!/usr/bin/env python3
"""Resumable, deliberately rate-limited batch import for the local fund catalogue.

The command never starts a collection by itself: a stable ``--run-id`` is
required.  Reusing that id resumes pending (or interrupted) items.  Failed
items are retained for review and require ``--retry-failed`` to be retried.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.config import Settings
from backend.storage.database import connection_scope, migrate
from scripts.import_fund_timeseries import _date, refresh_fund_timeseries
from scripts.probe_fund_source import ProbeError


MAX_WORKERS = 4


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _run_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise ValueError("run-id 必须是 1 到 120 个字符")
    return value.strip()


class RequestLimiter:
    """One process-wide request gate shared by all worker threads."""

    def __init__(self, interval_seconds: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if not isinstance(interval_seconds, (int, float)) or isinstance(interval_seconds, bool) or interval_seconds < 0:
            raise ValueError("request-interval 必须为大于等于 0 的秒数")
        self.interval_seconds = float(interval_seconds)
        self._clock, self._sleep = clock, sleep
        self._next_at = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            scheduled = max(now, self._next_at)
            self._next_at = scheduled + self.interval_seconds
        if scheduled > now:
            self._sleep(scheduled - now)


def _scope(registry_path: str | None) -> str:
    return registry_path or ""


def ensure_run(settings: Settings, run_id: str, start_date: str, end_date: str, registry_path: str | None) -> dict[str, int]:
    """Create a run once, verify its scope on resume, and add new catalogue rows."""
    run_id, start_date, end_date = _run_id(run_id), _date(start_date), _date(end_date)
    if start_date > end_date:
        raise ValueError("start-date 不能晚于 end-date")
    registry = _scope(registry_path)
    migrate(settings)
    with connection_scope(settings) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT start_date, end_date, registry_path FROM fund_timeseries_collection_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO fund_timeseries_collection_run(run_id, start_date, end_date, registry_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, start_date, end_date, registry, _now(), _now()),
                )
            elif tuple(existing) != (start_date, end_date, registry):
                raise ValueError("同一 run-id 的日期范围或注册表不能改变；请新建 run-id")
            connection.execute(
                """INSERT OR IGNORE INTO fund_timeseries_collection_item(run_id, code, status, updated_at)
                   SELECT ?, code, 'pending', ? FROM fund_catalog_projection""", (run_id, _now())
            )
            connection.execute("UPDATE fund_timeseries_collection_run SET updated_at = ? WHERE run_id = ?", (_now(), run_id))
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    return status_summary(settings, run_id)


def status_summary(settings: Settings, run_id: str) -> dict[str, int]:
    run_id = _run_id(run_id)
    migrate(settings)
    with connection_scope(settings) as connection:
        if connection.execute("SELECT 1 FROM fund_timeseries_collection_run WHERE run_id = ?", (run_id,)).fetchone() is None:
            raise ValueError("未找到该 run-id")
        rows = connection.execute(
            "SELECT status, COUNT(*) AS total FROM fund_timeseries_collection_item WHERE run_id = ? GROUP BY status", (run_id,)
        ).fetchall()
    counts = {"pending": 0, "running": 0, "succeeded": 0, "failed": 0}
    counts.update({row["status"]: row["total"] for row in rows})
    return {"run_id": run_id, "total": sum(counts.values()), **counts}


def failed_items(settings: Settings, run_id: str, limit: int = 20) -> list[dict[str, object]]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("失败记录展示数量必须在 1 到 1000 之间")
    with connection_scope(settings) as connection:
        return [dict(row) for row in connection.execute(
            """SELECT code, attempts, last_error_code, last_error_message, updated_at
               FROM fund_timeseries_collection_item WHERE run_id = ? AND status = 'failed'
               ORDER BY updated_at DESC, code LIMIT ?""", (_run_id(run_id), limit)
        )]


def claim_items(settings: Settings, run_id: str, limit: int, retry_failed: bool) -> list[str]:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit 必须为正整数")
    statuses = ("pending", "running", "failed") if retry_failed else ("pending", "running")
    placeholders = ", ".join("?" for _ in statuses)
    with connection_scope(settings) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            codes = [row[0] for row in connection.execute(
                f"SELECT code FROM fund_timeseries_collection_item WHERE run_id = ? AND status IN ({placeholders}) ORDER BY code LIMIT ?",
                (run_id, *statuses, limit),
            )]
            if codes:
                code_placeholders = ", ".join("?" for _ in codes)
                connection.execute(
                    f"""UPDATE fund_timeseries_collection_item
                        SET status = 'running', attempts = attempts + 1, started_at = ?, updated_at = ?,
                            last_error_code = NULL, last_error_message = NULL
                        WHERE run_id = ? AND code IN ({code_placeholders})""",
                    (_now(), _now(), run_id, *codes),
                )
            connection.execute("COMMIT")
            return codes
        except Exception:
            connection.execute("ROLLBACK")
            raise


def _record_result(settings: Settings, run_id: str, code: str, error: tuple[str, str] | None) -> None:
    status = "succeeded" if error is None else "failed"
    details = (None, None) if error is None else error
    with connection_scope(settings) as connection:
        connection.execute(
            """UPDATE fund_timeseries_collection_item
               SET status = ?, last_error_code = ?, last_error_message = ?, completed_at = ?, updated_at = ?
               WHERE run_id = ? AND code = ?""",
            (status, *details, _now(), _now(), run_id, code),
        )


def _safe_failure(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ProbeError):
        return exc.code, exc.message
    return "local_import_failure", "本地时序数据校验或写入失败"


def run_batch(settings: Settings, run_id: str, *, start_date: str, end_date: str, registry_path: str | None,
              timeout: float, limit: int, workers: int = 1, request_interval: float = 0.5,
              retry_failed: bool = False, refresher: Callable[..., dict[str, object]] = refresh_fund_timeseries,
              limiter: RequestLimiter | None = None) -> dict[str, object]:
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"workers 必须在 1 到 {MAX_WORKERS} 之间")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("timeout 必须为正数")
    ensure_run(settings, run_id, start_date, end_date, registry_path)
    codes = claim_items(settings, _run_id(run_id), limit, retry_failed)
    gate = limiter or RequestLimiter(request_interval)

    def refresh(code: str) -> tuple[str, tuple[str, str] | None]:
        try:
            refresher(settings, code, start_date=start_date, end_date=end_date, registry_path=registry_path,
                      timeout=float(timeout), before_request=gate.wait)
            return code, None
        except Exception as exc:
            return code, _safe_failure(exc)

    if workers == 1:
        results = (refresh(code) for code in codes)
    else:
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fund-import")
        futures = [executor.submit(refresh, code) for code in codes]
        results = (future.result() for future in as_completed(futures))
    completed = {"succeeded": 0, "failed": 0}
    try:
        for code, error in results:
            _record_result(settings, run_id, code, error)
            completed["failed" if error else "succeeded"] += 1
    finally:
        if workers > 1:
            executor.shutdown(wait=True, cancel_futures=False)
    return {"processed": len(codes), **completed, "summary": status_summary(settings, run_id),
            "failures": failed_items(settings, run_id)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="固定名称；重复执行将断点续传")
    parser.add_argument("--start-date", default="20000101")
    parser.add_argument("--end-date", default=dt.date.today().strftime("%Y%m%d"))
    parser.add_argument("--limit", type=int, default=10, help="本次最多处理数量，默认仅试跑 10 只")
    parser.add_argument("--workers", type=int, default=1, help="并发数，最多 4；默认单线程")
    parser.add_argument("--request-interval", type=float, default=0.5, help="同一进程网络请求之间最小间隔（秒）")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retry-failed", action="store_true", help="重试已失败项；默认只续跑待处理或中断项")
    parser.add_argument("--status", action="store_true", help="只展示已有任务状态和最近失败项")
    parser.add_argument("--database", default=None)
    parser.add_argument("--registry", default=None)
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env() if args.database is None else Settings(database_path=Path(args.database))
        if args.status:
            result: dict[str, object] = {"summary": status_summary(settings, args.run_id),
                                         "failures": failed_items(settings, args.run_id)}
        else:
            result = run_batch(settings, args.run_id, start_date=_date(args.start_date), end_date=_date(args.end_date),
                               registry_path=args.registry, timeout=args.timeout, limit=args.limit, workers=args.workers,
                               request_interval=args.request_interval, retry_failed=args.retry_failed)
    except Exception as exc:
        print(f"批量导入失败: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
