"""Cross-process refresh exclusion and durable outcomes, not a background queue."""
from __future__ import annotations
import json
import os
import sqlite3
from uuid import uuid4
from hashlib import sha256
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.database import connection_scope, migrate
from backend.storage.research import canonical, utc_now, digest


def start_run(settings: Settings, target: str) -> str:
    if target not in ("catalog", "sectors"):
        raise ValueError("无效采集目标")
    migrate(settings)
    run_id = uuid4().hex
    try:
        with connection_scope(settings) as connection:
            connection.execute("INSERT INTO collection_run VALUES(?,?,?,NULL,'running',?,NULL)",
                               (run_id, target, utc_now(), os.getpid()))
    except sqlite3.IntegrityError as exc:
        raise AppError("market_refresh_in_progress", "已有该目标的采集任务。中断任务须确认旧服务及子进程均停止后人工解除。", 409) from exc
    return run_id


def finish_run(settings: Settings, run_id: str, state: str, result: dict) -> None:
    if state not in ("success", "partial", "failed", "timeout", "interrupted"):
        raise ValueError("无效采集终态")
    with connection_scope(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT result_json FROM collection_run WHERE run_id=? AND state='running'", (run_id,)).fetchone()
        previous = json.loads(row["result_json"] or "{}") if row else {}
        result = dict(result)
        if previous.get("progress"):
            result["last_progress"] = previous["progress"]
        count = connection.execute("UPDATE collection_run SET state=?,finished_at=?,result_json=? WHERE run_id=? AND state='running'",
                                   (state, utc_now(), canonical(result), run_id)).rowcount
        connection.execute("COMMIT")
    if count != 1:
        raise AppError("collection_already_finished", "采集任务不存在或已经结束", 409)


def record_attempt(settings: Settings, run_id: str, event: dict) -> None:
    with connection_scope(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            now, asset_id = utc_now(), None
            if event.get("body") is not None:
                body_hash = sha256(event["body"]).hexdigest()
                media = event.get("media_type", "application/octet-stream")
                asset_id = digest([event["url"], media, body_hash])
                connection.execute("INSERT OR IGNORE INTO raw_asset VALUES(?,?,?,?,?,?)",
                                   (asset_id, event["url"], media, event["body"], body_hash, now))
            connection.execute("INSERT INTO collection_attempt(run_id,occurred_at,attempt,url,http_status,error,raw_asset_id) VALUES(?,?,?,?,?,?,?)",
                               (run_id, now, event["attempt"], event["url"], event.get("status"), event.get("error"), asset_id))
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


def runs(settings: Settings, limit: int = 20) -> list[dict]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("数量须在1至100")
    migrate(settings)
    with connection_scope(settings) as connection:
        result = [dict(row) for row in connection.execute("SELECT * FROM collection_run ORDER BY rowid DESC LIMIT ?", (limit,))]
        for row in result:
            row["result"] = json.loads(row.pop("result_json") or "null")
            row["attempts"] = [dict(event) for event in connection.execute("SELECT occurred_at,attempt,url,http_status,error,raw_asset_id FROM collection_attempt WHERE run_id=? ORDER BY id", (row["run_id"],))]
    return result


def record_progress(settings: Settings, run_id: str, event: dict) -> None:
    """Update only an active run; finish_run still owns the immutable terminal transition."""
    with connection_scope(settings) as connection:
        connection.execute(
            "UPDATE collection_run SET result_json=? WHERE run_id=? AND state='running'",
            (canonical({"progress": event, "updated_at": utc_now()}), run_id),
        )


def latest_sector_status(settings: Settings, *, run_id: str | None = None) -> dict:
    migrate(settings)
    with connection_scope(settings) as connection:
        row = connection.execute(
            "SELECT * FROM collection_run WHERE target='sectors' AND (? IS NULL OR run_id=?) ORDER BY rowid DESC LIMIT 1",
            (run_id, run_id),
        ).fetchone()
        result = dict(row) if row else {"state": "not_started", "run_id": None}
        if row:
            result["result"] = json.loads(result.pop("result_json") or "null")
            result["attempt_count"] = connection.execute(
                "SELECT COUNT(*) FROM collection_attempt WHERE run_id=?", (row["run_id"],)
            ).fetchone()[0]
    result["database_id"] = sha256(str(settings.database_path.resolve()).encode()).hexdigest()[:16]
    return result
