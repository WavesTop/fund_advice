"""Fund catalogue projection and import transaction."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.database import connection_scope, migrate


SOURCE_ID = "fund_catalog.eastmoney"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clean_row(row: Mapping[str, Any]) -> dict[str, str]:
    aliases = {
        "code": ("code", "fund_code", "基金代码", "基金代码（份额）"),
        "name": ("name", "fund_name", "基金简称", "基金名称"),
        "fund_type": ("fund_type", "type", "基金类型", "类型"),
    }
    result: dict[str, str] = {}
    for key, keys in aliases.items():
        value = next((row.get(k) for k in keys if row.get(k) is not None), None)
        if value is None:
            raise ValueError(f"缺少{key}")
        text = str(value).strip()
        if not text:
            raise ValueError(f"{key}不能为空")
        result[key] = text
    code = result["code"]
    if code.isdigit() and len(code) < 6:
        code = code.zfill(6)
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        raise ValueError("基金代码必须是六位数字")
    result["code"] = code
    return result


def import_catalog(settings: Settings, rows: Iterable[Mapping[str, Any]], *, policy_version: str,
                   source_id: str = SOURCE_ID) -> dict[str, Any]:
    """Validate all rows, then atomically upsert one import batch."""
    if source_id != SOURCE_ID or not isinstance(policy_version, str) or not policy_version.strip():
        raise AppError("catalog_source_not_allowed", "基金目录只能使用已核验的自动来源", 422)
    try:
        normalized = [_clean_row(row) for row in rows]
        by_code: dict[str, dict[str, str]] = {}
        for row in normalized:
            if row["code"] in by_code and by_code[row["code"]] != row:
                raise ValueError(f"基金代码重复且内容不一致: {row['code']}")
            by_code[row["code"]] = row
        normalized = list(by_code.values())
        if not normalized:
            raise ValueError("来源返回空目录")
    except (TypeError, ValueError) as exc:
        raise AppError("catalog_validation_failed", "基金目录校验失败", 422, {"reason": str(exc)}) from exc

    migrate(settings)
    with connection_scope(settings) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            cursor = connection.execute(
                "INSERT INTO fund_catalog_import_batch(source_id, policy_version, row_count, committed_at) VALUES (?, ?, ?, ?)",
                (source_id, policy_version, len(normalized), now),
            )
            batch_id = cursor.lastrowid
            for row in normalized:
                connection.execute(
                    """INSERT INTO fund_catalog_projection
                       (share_id, code, name, fund_type, source_id, first_seen_batch_id, last_seen_batch_id, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(code) DO UPDATE SET
                         share_id=excluded.share_id, name=excluded.name, fund_type=excluded.fund_type,
                         source_id=excluded.source_id, last_seen_batch_id=excluded.last_seen_batch_id,
                         updated_at=excluded.updated_at""",
                    (row["code"], row["code"], row["name"], row["fund_type"], source_id, batch_id, batch_id, now),
                )
            connection.execute("COMMIT")
        except Exception as exc:
            connection.execute("ROLLBACK")
            raise AppError("catalog_import_failed", "基金目录导入失败，旧目录已保留", 500, {"reason": str(exc)}) from exc
    return {"batch_id": batch_id, "row_count": len(normalized), "updated_at": now}


def list_catalog(settings: Settings, query: str = "", page: int = 1, page_size: int = 20) -> dict[str, Any]:
    if page < 1 or page_size < 1 or page_size > 100:
        raise AppError("invalid_pagination", "page必须大于等于1，page_size必须在1到100之间", 422)
    migrate(settings)
    q = query.strip()
    where = ""
    params: list[Any] = []
    if q:
        where = " WHERE code LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\'"
        literal = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.extend([f"%{literal}%", f"%{literal}%"])
    with connection_scope(settings) as connection:
        connection.execute("BEGIN")
        catalog_total = connection.execute("SELECT COUNT(*) FROM fund_catalog_projection").fetchone()[0]
        total = connection.execute(f"SELECT COUNT(*) FROM fund_catalog_projection{where}", params).fetchone()[0]
        offset = (page - 1) * page_size
        items = [dict(row) for row in connection.execute(
            f"SELECT share_id, code, name, fund_type, source_id FROM fund_catalog_projection{where} ORDER BY code LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        )]
        # Local import avoids catalog <-> related_market import-time recursion.
        from backend.storage.related_market import current_associations
        relations = current_associations(connection, generated_at=_now(), fund_codes=[item["code"] for item in items])
        by_fund: dict[str, list[dict]] = {}
        for relation in relations:
            by_fund.setdefault(relation["code"], []).append(relation)
        for item in items:
            known = by_fund.get(item["code"], [])
            linked = [relation for relation in known if relation["relation_status"] == "linked"]
            item["related_sectors"] = [{
                "code": relation["index_code"], "name": relation["index_name"],
                "source_id": relation["market_source_id"], "universe_type": "tracked_index",
                "relation_source_id": relation["relation_source_id"],
                "verified_at": relation["verified_at"], "evidence_url": relation["evidence_url"],
            } for relation in linked] if len(linked) == 1 else []
            item["relation_status"] = "linked" if len(linked) == 1 else "withheld" if known else "missing"
            item["relation_reason"] = ("明确跟踪关系，不代表基金已通过投资筛选。" if len(linked) == 1 else
                                       "关系待核验；进入基金详情获取或核对资料。" if known else
                                       "尚无已核验板块关联；不按基金名称推断。")
        latest = connection.execute("SELECT committed_at FROM fund_catalog_import_batch ORDER BY id DESC LIMIT 1").fetchone()
        collected = connection.execute("SELECT COUNT(DISTINCT code) FROM fund_timeseries_projection").fetchone()[0]
        latest_run = connection.execute(
            "SELECT run_id FROM fund_timeseries_collection_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        failed = 0
        if latest_run is not None:
            failed = connection.execute(
                "SELECT COUNT(*) FROM fund_timeseries_collection_item WHERE run_id = ? AND status = 'failed'",
                (latest_run[0],),
            ).fetchone()[0]
    return {"items": items, "page": page, "page_size": page_size, "total": total, "catalog_total": catalog_total,
            "updated_at": latest[0] if latest else None,
            "collection": {"collected": collected, "failed": failed, "pending": max(catalog_total - collected, 0)}}
