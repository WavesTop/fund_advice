"""Verified fund-to-index relationships and their real daily market series."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone

from backend.analysis.strategy_readiness import is_primary_evidence
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.core.source_admission import (
    SourceAdmissionError,
    contains_retired_source_reference,
    reject_retired_source_reference,
    writable_source_id,
)
from backend.core.relation_policy import relation_problems, timestamp
from backend.storage.database import connection_scope, migrate
from backend.storage.timeseries import _clean_rows, get_fund


MAX_MARKET_DATA_AGE_DAYS = 21


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def upsert_index_projection(settings: Settings, *, index_code: str, index_name: str,
                            source_id: str, updated_at: str | None = None) -> None:
    """Persist a standalone index identity before its independent market fetch completes."""
    try:
        source_id = writable_source_id(source_id, field="source_id")
    except SourceAdmissionError as exc:
        raise AppError("index_source_not_allowed", "指数行情来源不允许写入", 422,
                       {"reason": str(exc)}) from exc
    if not all(isinstance(value, str) and value.strip()
               for value in (index_code, index_name, source_id)):
        raise ValueError("指数身份无效")
    now = updated_at or _now()
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.execute(
            """INSERT INTO market_index_projection(code, name, source_id, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(code) DO UPDATE SET name=excluded.name, source_id=excluded.source_id,
               updated_at=excluded.updated_at""",
            (index_code, index_name, source_id, now),
        )


def import_related_relation(settings: Settings, *, fund_code: str, index_code: str,
                            index_name: str, market_source_id: str,
                            relation_source_id: str, evidence_url: str) -> None:
    """Persist verified relation identity before attempting the independent market fetch."""
    try:
        market_source_id = writable_source_id(market_source_id, field="market_source_id")
        relation_source_id = writable_source_id(relation_source_id, field="relation_source_id")
        reject_retired_source_reference(evidence_url, field="evidence_url")
    except SourceAdmissionError as exc:
        raise AppError("relation_source_not_allowed", "基金关联来源不允许写入", 422,
                       {"reason": str(exc)}) from exc
    now = _now()
    problems = relation_problems({
        "relation_type": "tracked_index", "relation_source_id": relation_source_id,
        "evidence_url": evidence_url, "verified_at": now,
    }, generated_at=now)
    if problems or not all(isinstance(value, str) and value.strip()
                           for value in (index_code, index_name, market_source_id)):
        raise ValueError("关联指数身份或来源无效：" + "；".join(problems))
    get_fund(settings, fund_code)
    migrate(settings)
    with connection_scope(settings) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_relations = connection.execute(
                "SELECT source_id FROM fund_market_relation "
                "WHERE fund_code=? AND relation_type='tracked_index'",
                (fund_code,),
            ).fetchall()
            if (any(is_primary_evidence("fund_relations", row["source_id"])
                    for row in current_relations)
                    and not is_primary_evidence("fund_relations", relation_source_id)):
                raise AppError(
                    "relation_source_downgrade_blocked", "已保留管理人正式基金关系证据", 409,
                    {"attempted_source_id": relation_source_id},
                )
            current_market = connection.execute(
                "SELECT source_id FROM market_index_projection WHERE code=?", (index_code,),
            ).fetchone()
            if (current_market is not None
                    and is_primary_evidence("index_daily", current_market["source_id"])
                    and not is_primary_evidence("index_daily", market_source_id)):
                raise AppError(
                    "index_source_downgrade_blocked", "已保留更高等级的官方指数行情", 409,
                    {"current_source_id": current_market["source_id"],
                     "attempted_source_id": market_source_id},
                )
            if current_market is None:
                connection.execute(
                    "INSERT INTO market_index_projection(code,name,source_id,updated_at) "
                    "VALUES (?,?,?,?)",
                    (index_code, index_name, market_source_id, now),
                )
            else:
                # Existing source_id/updated_at describe the last successful market
                # import.  A relation-only refresh must not relabel old rows.
                connection.execute(
                    "UPDATE market_index_projection SET name=? WHERE code=?",
                    (index_name, index_code),
                )
            connection.execute(
                """INSERT INTO fund_market_relation(
                       fund_code,index_code,relation_type,source_id,evidence_url,verified_at
                   ) VALUES (?,?,'tracked_index',?,?,?)
                   ON CONFLICT(fund_code,index_code,relation_type) DO UPDATE SET
                   source_id=excluded.source_id,evidence_url=excluded.evidence_url,
                   verified_at=excluded.verified_at""",
                (fund_code, index_code, relation_source_id, evidence_url, now),
            )
            connection.execute(
                "INSERT INTO fund_relation_verification(fund_code,checked_at,outcome,index_code) "
                "VALUES (?,?,'verified',?)",
                (fund_code, now, index_code),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


def import_related_index(settings: Settings, *, fund_code: str, index_code: str, index_name: str,
                         rows: Iterable[Mapping[str, object]], source_id: str,
                         relation_source_id: str, evidence_url: str) -> None:
    try:
        source_id = writable_source_id(source_id, field="source_id")
        relation_source_id = writable_source_id(relation_source_id, field="relation_source_id")
        reject_retired_source_reference(evidence_url, field="evidence_url")
    except SourceAdmissionError as exc:
        raise AppError("relation_source_not_allowed", "基金关联来源不允许写入", 422,
                       {"reason": str(exc)}) from exc
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
            current_relations = connection.execute(
                "SELECT source_id FROM fund_market_relation WHERE fund_code=? AND relation_type='tracked_index'",
                (fund_code,),
            ).fetchall()
            if (any(is_primary_evidence("fund_relations", row["source_id"])
                    for row in current_relations)
                    and not is_primary_evidence("fund_relations", relation_source_id)):
                raise AppError(
                    "relation_source_downgrade_blocked",
                    "已保留管理人正式基金关系证据",
                    409,
                    {"attempted_source_id": relation_source_id},
                )
            current_market = connection.execute(
                "SELECT source_id FROM market_index_projection WHERE code=?", (index_code,)
            ).fetchone()
            same_market_source = bool(
                current_market is not None and current_market["source_id"] == source_id
            )
            if (current_market is not None
                    and is_primary_evidence("index_daily", current_market["source_id"])
                    and not is_primary_evidence("index_daily", source_id)):
                raise AppError(
                    "index_source_downgrade_blocked",
                    "已保留更高等级的官方指数行情",
                    409,
                    {"current_source_id": current_market["source_id"], "attempted_source_id": source_id},
                )
            current_history = connection.execute(
                "SELECT COUNT(*) AS row_count,MIN(date) AS first_date,MAX(date) AS last_date "
                "FROM market_index_daily WHERE index_code=?",
                (index_code,),
            ).fetchone()
            if current_history is not None and current_history["row_count"]:
                incoming_dates = {str(row["date"]) for row in normalized}
                retained_old_dates = {
                    row["date"] for row in connection.execute(
                        "SELECT date FROM market_index_daily "
                        "WHERE index_code=? AND date BETWEEN ? AND ?",
                        (index_code, normalized[0]["date"], normalized[-1]["date"]),
                    )
                }
                old_span = (date.fromisoformat(current_history["last_date"])
                            - date.fromisoformat(current_history["first_date"])).days
                new_span = (date.fromisoformat(str(normalized[-1]["date"]))
                            - date.fromisoformat(str(normalized[0]["date"]))).days
                rolling_extension = (
                    same_market_source
                    and str(normalized[0]["date"]) > current_history["first_date"]
                    and str(normalized[-1]["date"]) > current_history["last_date"]
                )
                if ((len(normalized) < current_history["row_count"] and not rolling_extension)
                        or str(normalized[-1]["date"]) < current_history["last_date"]
                        or (new_span + 31 < old_span and not rolling_extension)
                        or not retained_old_dates.issubset(incoming_dates)):
                    raise AppError(
                        "index_history_regression_blocked",
                        "本次指数行情覆盖小于本地历史，旧数据已保留",
                        409,
                        {
                            "current_row_count": current_history["row_count"],
                            "attempted_row_count": len(normalized),
                            "current_first_date": current_history["first_date"],
                            "attempted_first_date": normalized[0]["date"],
                            "current_last_date": current_history["last_date"],
                            "attempted_last_date": normalized[-1]["date"],
                        },
                    )
            connection.execute(
                """INSERT INTO market_index_projection(code, name, source_id, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET name=excluded.name, source_id=excluded.source_id,
                   updated_at=excluded.updated_at""",
                (index_code, index_name, source_id, now),
            )
            if not same_market_source:
                connection.execute("DELETE FROM market_index_daily WHERE index_code = ?", (index_code,))
            connection.executemany(
                """INSERT INTO market_index_daily(index_code,date,open,high,low,close,volume,amount)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(index_code,date) DO UPDATE SET
                   open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                   volume=excluded.volume,amount=excluded.amount""",
                [(index_code, row["date"], row["open"], row["high"], row["low"], row["close"], row["volume"], row["amount"])
                 for row in normalized],
            )
            connection.execute(
                """INSERT INTO index_market_refresh_state(index_code,attempted_at,outcome,source_id,error)
                   VALUES (?,?,'updated',?,NULL)
                   ON CONFLICT(index_code) DO UPDATE SET attempted_at=excluded.attempted_at,
                   outcome='updated',source_id=excluded.source_id,error=NULL""",
                (index_code, now, source_id),
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


def import_standalone_index(settings: Settings, *, index_code: str, index_name: str,
                            rows: Iterable[Mapping[str, object]], source_id: str) -> None:
    """Persist a manually watched index without requiring a fund relationship."""
    try:
        source_id = writable_source_id(source_id, field="source_id")
    except SourceAdmissionError as exc:
        raise AppError("index_source_not_allowed", "指数行情来源不允许写入", 422,
                       {"reason": str(exc)}) from exc
    normalized = _clean_rows("price", rows)
    now = _now()
    if not all(isinstance(value, str) and value.strip() for value in (index_code, index_name, source_id)):
        raise ValueError("指数身份或来源无效")
    migrate(settings)
    with connection_scope(settings) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO market_index_projection(code, name, source_id, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET name=excluded.name, source_id=excluded.source_id,
                   updated_at=excluded.updated_at""",
                (index_code, index_name, source_id, now),
            )
            connection.execute("DELETE FROM market_index_daily WHERE index_code = ?", (index_code,))
            connection.executemany(
                """INSERT INTO market_index_daily(index_code,date,open,high,low,close,volume,amount)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(index_code,date) DO UPDATE SET
                   open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                   volume=excluded.volume,amount=excluded.amount""",
                [(index_code, row["date"], row["open"], row["high"], row["low"], row["close"],
                  row["volume"], row["amount"]) for row in normalized],
            )
            connection.execute(
                """INSERT INTO index_market_refresh_state(index_code,attempted_at,outcome,source_id,error)
                   VALUES (?,?,'updated',?,NULL)
                   ON CONFLICT(index_code) DO UPDATE SET attempted_at=excluded.attempted_at,
                   outcome='updated',source_id=excluded.source_id,error=NULL""",
                (index_code, now, source_id),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


def record_index_market_failure(settings: Settings, index_code: str, *, attempted_at: str,
                                source_id: str, error: str) -> bool:
    """Record a failed market refresh without changing fund-to-index evidence."""
    checked = timestamp(attempted_at)
    if checked is None or not all(isinstance(value, str) and value.strip()
                                  for value in (index_code, source_id, error)):
        raise ValueError("指数行情失败状态参数无效")
    migrate(settings)
    with connection_scope(settings) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM market_index_projection WHERE code=?", (index_code,)
            ).fetchone()
            if exists is None:
                connection.execute("ROLLBACK")
                return False
            connection.execute(
                """INSERT INTO index_market_refresh_state(index_code,attempted_at,outcome,source_id,error)
                   VALUES (?,?,'failed',?,?)
                   ON CONFLICT(index_code) DO UPDATE SET attempted_at=excluded.attempted_at,
                   outcome='failed',source_id=excluded.source_id,error=excluded.error
                   WHERE excluded.attempted_at >= index_market_refresh_state.attempted_at""",
                (index_code, attempted_at, source_id, error[:1000]),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    return True


def market_data_state(item: Mapping[str, object], *, generated_at: str) -> dict[str, object]:
    """Classify current index rows independently from the relationship evidence."""
    generated = timestamp(generated_at)
    if generated is None:
        raise ValueError("generated_at must include a timezone")
    attempted = timestamp(item.get("market_refresh_attempted_at"))
    if item.get("market_refresh_attempted_at") is not None and (
            attempted is None or attempted > generated):
        return {"market_status": "invalid", "market_reason": "指数行情刷新时间无效或晚于本次读取。"}
    if item.get("market_refresh_outcome") == "failed":
        return {"market_status": "failed", "market_reason": "本次指数行情更新失败，旧行情不参与当前信号。"}
    value = item.get("market_data_as_of")
    try:
        latest = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        latest = None
    if latest is None or latest.isoformat() != value:
        return {"market_status": "missing", "market_reason": "已核验关系尚无可用指数日线。"}
    if latest > generated.date():
        return {"market_status": "invalid", "market_reason": "指数行情日期晚于本次读取。"}
    if (generated.date() - latest).days > MAX_MARKET_DATA_AGE_DAYS:
        return {"market_status": "stale", "market_reason": "指数行情已过期，旧行情不参与当前信号。"}
    return {"market_status": "ready", "market_reason": ""}


def record_relation_check(settings: Settings, fund_code: str, *, outcome: str, error: str,
                          attempted_source_id: str | None = None,
                          attempted_at: str | None = None) -> bool:
    """Record a candidate check unless a concurrent primary relation must win."""
    if outcome not in ("unresolved", "failed"):
        raise ValueError("关系检查状态无效")
    checked_at = attempted_at or _now()
    checked = timestamp(checked_at)
    if checked is None:
        raise ValueError("关系检查开始时间无效")
    checked_at = checked.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    get_fund(settings, fund_code)
    with connection_scope(settings) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute(
                "SELECT checked_at FROM fund_relation_verification WHERE fund_code=? "
                "ORDER BY checked_at DESC,id DESC LIMIT 1",
                (fund_code,),
            ).fetchone()
            if latest is not None:
                latest_checked = timestamp(latest["checked_at"])
                if latest_checked is None or latest_checked > checked:
                    connection.execute("ROLLBACK")
                    return False
            if attempted_source_id is not None and not is_primary_evidence(
                    "fund_relations", attempted_source_id):
                current_sources = connection.execute(
                    "SELECT source_id FROM fund_market_relation "
                    "WHERE fund_code=? AND relation_type='tracked_index'",
                    (fund_code,),
                ).fetchall()
                if any(is_primary_evidence("fund_relations", row["source_id"])
                       for row in current_sources):
                    connection.execute("ROLLBACK")
                    return False
            connection.execute(
                "INSERT INTO fund_relation_verification(fund_code,checked_at,outcome,error) VALUES (?,?,?,?)",
                (fund_code, checked_at, outcome, error[:1000]),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    return True


def current_associations(connection, *, generated_at: str, fund_codes: list[str] | None = None) -> list[dict[str, object]]:
    if fund_codes == []:
        return []
    where = " WHERE f.code IN (" + ",".join("?" for _ in fund_codes) + ")" if fund_codes else ""
    rows = [dict(row) for row in connection.execute(
        """SELECT f.code,f.name,f.source_id AS catalog_source_id,
                  r.index_code,r.relation_type,r.source_id AS relation_source_id,
                  r.evidence_url,r.verified_at,i.name AS index_name,i.source_id AS market_source_id,
                  i.updated_at AS market_updated_at,v.outcome,v.index_code AS checked_index_code,
                  v.checked_at,v.error AS check_error,
                  m.outcome AS market_refresh_outcome,m.attempted_at AS market_refresh_attempted_at,
                  m.source_id AS market_refresh_source_id,
                  (SELECT MAX(d.date) FROM market_index_daily d WHERE d.index_code=i.code) AS market_data_as_of
           FROM fund_market_relation r JOIN fund_catalog_projection f ON f.code=r.fund_code
           JOIN market_index_projection i ON i.code=r.index_code
           LEFT JOIN index_market_refresh_state m ON m.index_code=i.code
           LEFT JOIN fund_relation_verification v ON v.id=(
               SELECT id FROM fund_relation_verification WHERE fund_code=r.fund_code
               ORDER BY checked_at DESC,id DESC LIMIT 1)
        """ + where + " ORDER BY f.code,r.index_code,r.relation_type", fund_codes or [])]
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
        row.update(market_data_state(row, generated_at=generated_at))
        if any(contains_retired_source_reference(row.get(field))
               for field in ("relation_source_id", "evidence_url", "market_source_id")):
            # Preserve the original URL in SQLite for audit, but never expose a
            # retired provider as a current outbound link.
            row["evidence_url"] = None
        row["effective_from"] = row["effective_to"] = None
    return rows


def get_related_markets(settings: Settings, fund_code: str) -> list[dict[str, object]]:
    get_fund(settings, fund_code)
    with connection_scope(settings) as connection:
        connection.execute("BEGIN")
        relations = current_associations(connection, generated_at=_now(), fund_codes=[fund_code])
        result = []
        for relation in relations:
            rows = []
            if relation["relation_status"] == "linked" and relation["market_status"] == "ready":
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
