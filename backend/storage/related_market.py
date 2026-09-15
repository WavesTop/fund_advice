"""Verified fund-to-index relationships and their real daily market series."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone

from backend.core.config import Settings
from backend.storage.database import connection_scope, migrate
from backend.storage.timeseries import _clean_rows


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def import_related_index(settings: Settings, *, fund_code: str, index_code: str, index_name: str,
                         rows: Iterable[Mapping[str, object]], source_id: str,
                         relation_source_id: str, evidence_url: str) -> None:
    normalized = _clean_rows("price", rows)
    migrate(settings)
    now = _now()
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
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


def get_related_market(settings: Settings, fund_code: str) -> dict[str, object] | None:
    migrate(settings)
    with connection_scope(settings) as connection:
        relation = connection.execute(
            """SELECT i.code,i.name,i.source_id,i.updated_at,r.relation_type,r.evidence_url
               FROM fund_market_relation r JOIN market_index_projection i ON i.code=r.index_code
               WHERE r.fund_code=? ORDER BY r.relation_type LIMIT 1""", (fund_code,)
        ).fetchone()
        if relation is None:
            return None
        rows = [dict(row) for row in connection.execute(
            "SELECT date,open,high,low,close,volume,amount FROM market_index_daily WHERE index_code=? ORDER BY date",
            (relation["code"],),
        )]
    return {**dict(relation), "kind": "index", "rows": rows}
