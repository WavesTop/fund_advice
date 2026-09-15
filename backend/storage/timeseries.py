"""Persistent projections for verified fund prices and net asset values."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.database import connection_scope, migrate


_PRICE_FIELDS = ("open", "high", "low", "close", "volume", "amount")
_NAV_FIELDS = ("unit_nav", "accumulated_nav")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _code(value: str) -> str:
    if not isinstance(value, str) or len(value) != 6 or not value.isascii() or not value.isdigit():
        raise ValueError("基金代码必须是六位数字")
    return value


def _date(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("date必须是YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date必须是YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("date必须是YYYY-MM-DD")
    return value


def _decimal(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}必须是精确小数字符串或null")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field}不是有效数字") from exc
    if not number.is_finite():
        raise ValueError(f"{field}不是有限数字")
    return value


def _clean_rows(kind: str, rows: Iterable[Mapping[str, object]]) -> list[dict[str, str | None]]:
    if kind not in ("price", "nav"):
        raise ValueError("kind必须是price或nav")
    fields = _PRICE_FIELDS if kind == "price" else _NAV_FIELDS
    result: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("时序行必须是对象")
        item = {"date": _date(raw.get("date"))}
        if item["date"] in seen:
            raise ValueError(f"日期重复: {item['date']}")
        seen.add(item["date"])
        for field in fields:
            item[field] = _decimal(raw.get(field), field)
        if kind == "price" and any(item[field] is None for field in ("open", "high", "low", "close")):
            raise ValueError("价格行的OHLC不能为空")
        if kind == "nav" and item["unit_nav"] is None and item["accumulated_nav"] is None:
            raise ValueError("净值行至少需要单位净值或累计净值")
        result.append(item)
    if not result:
        raise ValueError("来源未返回可存储的时序数据")
    return sorted(result, key=lambda item: str(item["date"]))


def import_timeseries(settings: Settings, code: str, kind: str, rows: Iterable[Mapping[str, object]], *,
                      source_id: str, policy_version: str) -> dict[str, Any]:
    """Validate a complete import before atomically replacing a single series."""
    try:
        code = _code(code)
        if not isinstance(source_id, str) or not source_id.strip() or not isinstance(policy_version, str) or not policy_version.strip():
            raise ValueError("来源和策略版本不能为空")
        normalized = _clean_rows(kind, rows)
    except (TypeError, ValueError) as exc:
        raise AppError("timeseries_validation_failed", "基金时序数据校验失败", 422, {"reason": str(exc)}) from exc

    migrate(settings)
    with connection_scope(settings) as connection:
        exists = connection.execute("SELECT 1 FROM fund_catalog_projection WHERE code = ?", (code,)).fetchone()
        if exists is None:
            raise AppError("fund_not_found", "未找到该基金", 404)
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            batch_id = connection.execute(
                "INSERT INTO fund_timeseries_import_batch(code, kind, source_id, policy_version, row_count, committed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (code, kind, source_id, policy_version, len(normalized), now),
            ).lastrowid
            connection.execute("DELETE FROM fund_timeseries_projection WHERE code = ? AND kind = ?", (code, kind))
            connection.execute(
                "INSERT INTO fund_timeseries_projection(code, kind, source_id, policy_version, batch_id, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (code, kind, source_id, policy_version, batch_id, now),
            )
            if kind == "price":
                connection.executemany(
                    "INSERT INTO fund_price_daily(code, date, open, high, low, close, volume, amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(code, row["date"], row["open"], row["high"], row["low"], row["close"], row["volume"], row["amount"]) for row in normalized],
                )
            else:
                connection.executemany(
                    "INSERT INTO fund_nav_daily(code, date, unit_nav, accumulated_nav) VALUES (?, ?, ?, ?)",
                    [(code, row["date"], row["unit_nav"], row["accumulated_nav"]) for row in normalized],
                )
            connection.execute("COMMIT")
        except Exception as exc:
            connection.execute("ROLLBACK")
            raise AppError("timeseries_import_failed", "基金时序导入失败，旧数据已保留", 500, {"reason": str(exc)}) from exc
    return {"code": code, "kind": kind, "source_id": source_id, "policy_version": policy_version,
            "row_count": len(normalized), "updated_at": now}


def get_fund(settings: Settings, code: str) -> dict[str, object]:
    try:
        code = _code(code)
    except ValueError as exc:
        raise AppError("fund_not_found", "未找到该基金", 404) from exc
    migrate(settings)
    with connection_scope(settings) as connection:
        row = connection.execute(
            "SELECT share_id, code, name, fund_type, source_id, updated_at FROM fund_catalog_projection WHERE code = ?", (code,)
        ).fetchone()
    if row is None:
        raise AppError("fund_not_found", "未找到该基金", 404)
    return dict(row)


def get_timeseries(settings: Settings, code: str) -> dict[str, object]:
    get_fund(settings, code)
    with connection_scope(settings) as connection:
        projection = connection.execute(
            "SELECT kind, source_id, policy_version, updated_at FROM fund_timeseries_projection WHERE code = ? ORDER BY CASE kind WHEN 'price' THEN 0 ELSE 1 END LIMIT 1", (code,)
        ).fetchone()
        if projection is None:
            return {"kind": None, "source_id": None, "policy_version": None, "updated_at": None, "rows": []}
        info = dict(projection)
        if info["kind"] == "price":
            rows = [dict(row) for row in connection.execute(
                "SELECT date, open, high, low, close, volume, amount FROM fund_price_daily WHERE code = ? ORDER BY date", (code,)
            )]
        else:
            rows = [dict(row) for row in connection.execute(
                "SELECT date, unit_nav, accumulated_nav FROM fund_nav_daily WHERE code = ? ORDER BY date", (code,)
            )]
    return {**info, "rows": rows}
