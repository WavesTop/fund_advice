"""Verified fund-to-index relationships and their real daily market series."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone

from backend.core.config import Settings
from backend.storage.database import connection_scope, migrate
from backend.storage.timeseries import _clean_rows, get_fund
from backend.core.relation_policy import relation_problems, timestamp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def import_related_index(settings: Settings, *, fund_code: str, index_code: str, index_name: str,
                         rows: Iterable[Mapping[str, object]], source_id: str,
                         relation_source_id: str, evidence_url: str) -> None:
    normalized = _clean_rows("price", rows)
    now = _now()
    problems = relation_problems({"relation_type": "tracked_index", "relation_source_id": relation_source_id,
                                  "evidence_url": evidence_url, "verified_at": now}, generated_at=now)
    if problems or not all(isinstance(value, str) and value.strip() for value in (index_code, index_name, source_id)):
        raise ValueError("关联指数身份或来源无效：" + "；".join(problems))
    get_fund(settings, fund_code)
    migrate(settings)
    with connection_scope(settings) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO market_index_projection(code, name, source_id, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET name=excluded.name, source_id=excluded.source_id,
                   updated_at=excluded.updated_at""",
                (index_code, index_name, source_id, now),
            )
            connection.execute("DELETE FROM market_index_daily WHERE index_code = ?", (index_code,))
            connection.executemany(
                "INSERT INTO market_index_daily(index_code,date,open,high,low,close,volume,amount) VALUES (?,?,?,?,?,?,?,?)",
                [(index_code, row["date"], row["open"], row["high"], row["low"], row["close"], row["volume"], row["amount"])
                 for row in normalized],
            )
            connection.execute(
                """INSERT INTO fund_market_relation(fund_code,index_code,relation_type,source_id,evidence_url,verified_at)
                   VALUES (?,?,'tracked_index',?,?,?)
                   ON CONFLICT(fund_code,index_code,relation_type) DO UPDATE SET
                   source_id=excluded.source_id,evidence_url=excluded.evidence_url,verified_at=excluded.verified_at""",
                (fund_code, index_code, relation_source_id, evidence_url, now),
            )
            connection.execute(
                "INSERT INTO fund_relation_verification(fund_code,checked_at,outcome,index_code) VALUES (?,?,'verified',?)",
                (fund_code, now, index_code),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


def record_relation_check(settings: Settings, fund_code: str, *, outcome: str, error: str) -> None:
    if outcome not in ("unresolved", "failed"):
        raise ValueError("关系检查状态无效")
    get_fund(settings, fund_code)
    with connection_scope(settings) as connection:
        connection.execute(
            "INSERT INTO fund_relation_verification(fund_code,checked_at,outcome,error) VALUES (?,?,?,?)",
            (fund_code, _now(), outcome, error[:1000]),
        )


def current_associations(connection, *, generated_at: str) -> list[dict[str, object]]:
    rows = [dict(row) for row in connection.execute(
        """SELECT f.code,f.name,r.index_code,r.relation_type,r.source_id AS relation_source_id,
                  r.evidence_url,r.verified_at,i.name AS index_name,i.source_id AS market_source_id,
                  i.updated_at AS market_updated_at,v.outcome,v.index_code AS checked_index_code,
                  v.checked_at,v.error AS check_error
           FROM fund_market_relation r JOIN fund_catalog_projection f ON f.code=r.fund_code
           JOIN market_index_projection i ON i.code=r.index_code
           LEFT JOIN fund_relation_verification v ON v.id=(
               SELECT MAX(id) FROM fund_relation_verification WHERE fund_code=r.fund_code)
           ORDER BY f.code,r.index_code,r.relation_type""")]
    now = timestamp(generated_at)
    if now is None:
        raise ValueError("generated_at must include a timezone")
    for row in rows:
        reason = ""
        checked = timestamp(row["checked_at"])
        if row["outcome"] is not None and (checked is None or checked > now):
            reason = "最近关系检查时间无效或晚于本次读取，暂不开放。"
        elif row["outcome"] in ("unresolved", "failed"):
            reason = "最近一次关系核验未完成；保留旧证据，不作为当前已核验关联。"
        elif row["outcome"] == "verified" and row["checked_index_code"] != row["index_code"]:
            row["relation_status"] = "superseded"
            reason = "后续核验已记录其他跟踪指数；旧证据保留，不回填业务生效日。"
        elif row["outcome"] is None and sum(
                other["code"] == row["code"] and other["relation_type"] == "tracked_index" for other in rows) > 1:
            reason = "存在多条历史跟踪关系且缺少本次核验，不能默认选择其中一条。"
        if reason:
            row.setdefault("relation_status", "withheld")
            row["relation_reason"] = reason
        problems = relation_problems(row, generated_at=generated_at)
        if problems:
            row.setdefault("relation_status", "withheld")
        else:
            row["relation_status"] = "linked"
        row["limitations"] = problems
        row["relation_reason"] = "；".join(problems)
        row["effective_from"] = row["effective_to"] = None
    return rows


def get_related_markets(settings: Settings, fund_code: str) -> list[dict[str, object]]:
    get_fund(settings, fund_code)
    with connection_scope(settings) as connection:
        connection.execute("BEGIN")
        relations = [row for row in current_associations(connection, generated_at=_now()) if row["code"] == fund_code]
        result = []
        for relation in relations:
            rows = []
            if relation["relation_status"] == "linked":
                rows = [dict(row) for row in connection.execute(
                    "SELECT date,open,high,low,close,volume,amount FROM market_index_daily WHERE index_code=? ORDER BY date",
                    (relation["index_code"],),
                )]
            result.append({**relation, "code": relation["index_code"], "name": relation["index_name"],
                           "source_id": relation["market_source_id"], "updated_at": relation["market_updated_at"],
                           "kind": "index", "rows": rows})
        return result


def get_related_market(settings: Settings, fund_code: str) -> dict[str, object] | None:
    linked = [row for row in get_related_markets(settings, fund_code) if row["relation_status"] == "linked"]
    return linked[0] if len(linked) == 1 else None
