"""Append-only local portfolio accounts, conditions and confirmed-balance snapshots."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.database import connection_scope, migrate
from backend.storage.research import digest, number, utc_now

ACCOUNT_VERSION = "portfolio-ledger-v1"
EVENT_TYPES = {"initial_position", "buy", "sell", "cash_in", "cash_out", "dividend"}
EVENT_STATES = {"confirmed", "pending", "unknown"}
RISK_LEVELS = {"conservative", "balanced", "aggressive"}


def _text(value: object, field: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field}必须是非空且长度不超过{maximum}的字符串")
    return value.strip()


def _optional_text(value: object, field: str, *, maximum: int = 1000) -> str | None:
    if value is None:
        return None
    return _text(value, field, maximum=maximum)


def _date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}必须是日期字符串")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field}必须是有效的YYYY-MM-DD日期") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field}必须是有效的YYYY-MM-DD日期")
    return value


def _key(value: object) -> str:
    return _text(value, "Idempotency-Key", maximum=200)


def _account(connection: sqlite3.Connection, account_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM portfolio_account WHERE account_id=?", (account_id,)).fetchone()
    if row is None:
        raise AppError("portfolio_account_not_found", "持仓账户不存在", 404)
    return row


def _saved(row: sqlite3.Row, *, idempotent: bool) -> dict[str, Any]:
    result = dict(row)
    result.pop("idempotency_key", None)
    result.pop("request_hash", None)
    result["idempotent_replay"] = idempotent
    return result


def create_account(settings: Settings, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
    try:
        key = _key(idempotency_key)
        normalized = {
            "name": _text(payload.get("name"), "name"),
            "base_currency": _text(payload.get("base_currency", "CNY"), "base_currency", maximum=3),
        }
        if normalized["base_currency"] != "CNY":
            raise ValueError("首版账户仅支持CNY")
    except ValueError as exc:
        raise AppError("portfolio_input_invalid", str(exc), 422) from exc
    request_hash = digest(normalized)
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute("SELECT * FROM portfolio_account WHERE idempotency_key=?", (key,)).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise AppError("portfolio_idempotency_conflict", "幂等键已用于不同的账户请求", 409)
                connection.execute("COMMIT")
                return _saved(existing, idempotent=True)
            account_id, now = f"acct_{uuid4().hex}", utc_now()
            connection.execute(
                "INSERT INTO portfolio_account VALUES(?,?,?,?,?,?,?)",
                (account_id, normalized["name"], normalized["base_currency"], "active", now, key, request_hash),
            )
            row = connection.execute("SELECT * FROM portfolio_account WHERE account_id=?", (account_id,)).fetchone()
            connection.execute("COMMIT")
            return _saved(row, idempotent=False)
        except Exception:
            connection.execute("ROLLBACK")
            raise


def list_accounts(settings: Settings) -> list[dict[str, Any]]:
    migrate(settings)
    with connection_scope(settings) as connection:
        rows = connection.execute(
            "SELECT account_id,name,base_currency,status,opened_at FROM portfolio_account ORDER BY opened_at,account_id"
        ).fetchall()
    return [dict(row) for row in rows]


def append_condition(settings: Settings, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
    try:
        key = _key(idempotency_key)
        risk = _text(payload.get("risk_level"), "risk_level", maximum=20)
        if risk not in RISK_LEVELS:
            raise ValueError("risk_level必须为conservative、balanced或aggressive")
        maximum = number(payload.get("max_single_position_pct"), positive=True)
        if Decimal(maximum) > 100:
            raise ValueError("max_single_position_pct不能超过100")
        horizon = payload.get("investment_horizon_days")
        if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0 or horizon > 36500:
            raise ValueError("investment_horizon_days必须为1到36500的整数")
        no_holdings = payload.get("no_holdings_confirmed", False)
        if not isinstance(no_holdings, bool):
            raise ValueError("no_holdings_confirmed必须为布尔值")
        normalized = {
            "account_id": _text(payload.get("account_id"), "account_id", maximum=64),
            "effective_date": _date(payload.get("effective_date"), "effective_date"),
            "risk_level": risk,
            "max_single_position_pct": maximum,
            "investment_horizon_days": horizon,
            "channel": _text(payload.get("channel"), "channel"),
            "planned_additional_cash": (number(payload["planned_additional_cash"], nonnegative=True)
                                        if payload.get("planned_additional_cash") is not None else None),
            "no_holdings_confirmed": no_holdings,
        }
    except ValueError as exc:
        raise AppError("portfolio_input_invalid", str(exc), 422) from exc
    request_hash = digest(normalized)
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _account(connection, normalized["account_id"])
            existing = connection.execute(
                "SELECT * FROM portfolio_condition_revision WHERE account_id=? AND idempotency_key=?",
                (normalized["account_id"], key),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise AppError("portfolio_idempotency_conflict", "幂等键已用于不同的条件请求", 409)
                connection.execute("COMMIT")
                return _condition(existing, idempotent=True)
            if normalized["no_holdings_confirmed"]:
                _, existing_units = _balances(
                    _current_event_rows(connection, normalized["account_id"], normalized["effective_date"]),
                    validate=True,
                    recognition_cutoff=normalized["effective_date"],
                )
                if any(value > 0 for value in existing_units.values()):
                    raise AppError(
                        "portfolio_condition_conflict",
                        "账户在该生效日仍有已确认持仓，不能声明当前无持仓",
                        409,
                    )
            version = connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM portfolio_condition_revision WHERE account_id=?",
                (normalized["account_id"],),
            ).fetchone()[0]
            revision_id, now = f"cond_{uuid4().hex}", utc_now()
            connection.execute(
                """INSERT INTO portfolio_condition_revision
                   (revision_id,account_id,version,effective_date,risk_level,max_single_position_pct,
                    investment_horizon_days,channel,planned_additional_cash,no_holdings_confirmed,
                    recorded_at,idempotency_key,request_hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (revision_id, normalized["account_id"], version, normalized["effective_date"], risk, maximum,
                 horizon, normalized["channel"], normalized["planned_additional_cash"], int(no_holdings),
                 now, key, request_hash),
            )
            row = connection.execute(
                "SELECT * FROM portfolio_condition_revision WHERE revision_id=?", (revision_id,)
            ).fetchone()
            connection.execute("COMMIT")
            return _condition(row, idempotent=False)
        except Exception:
            connection.execute("ROLLBACK")
            raise


def _condition(row: sqlite3.Row, *, idempotent: bool | None = None) -> dict[str, Any]:
    result = dict(row)
    result.pop("idempotency_key", None)
    result.pop("request_hash", None)
    result["no_holdings_confirmed"] = bool(result["no_holdings_confirmed"])
    if idempotent is not None:
        result["idempotent_replay"] = idempotent
    return result


def list_conditions(settings: Settings, account_id: str, cutoff: str | None = None) -> dict[str, Any]:
    if cutoff is None:
        cutoff = date.today().isoformat()
    else:
        try:
            cutoff = _date(cutoff, "cutoff")
        except ValueError as exc:
            raise AppError("portfolio_input_invalid", str(exc), 422) from exc
    migrate(settings)
    with connection_scope(settings) as connection:
        _account(connection, account_id)
        rows = connection.execute(
            "SELECT * FROM portfolio_condition_revision WHERE account_id=? ORDER BY version DESC", (account_id,)
        ).fetchall()
        current = connection.execute(
            """SELECT * FROM portfolio_condition_revision
               WHERE account_id=? AND effective_date<=?
               ORDER BY effective_date DESC,version DESC LIMIT 1""",
            (account_id, cutoff),
        ).fetchone()
    items = [_condition(row) for row in rows]
    return {"account_id": account_id, "cutoff": cutoff,
            "current": _condition(current) if current else None, "items": items}


def _normalize_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    event_type = _text(payload.get("event_type"), "event_type", maximum=30)
    state = _text(payload.get("state", "confirmed"), "state", maximum=20)
    if event_type not in EVENT_TYPES:
        raise ValueError("event_type不受支持")
    if state not in EVENT_STATES:
        raise ValueError("state必须为confirmed、pending或unknown")
    fund_code = payload.get("fund_code")
    if fund_code is not None:
        fund_code = _text(fund_code, "fund_code", maximum=6)
        if len(fund_code) != 6 or not fund_code.isascii() or not fund_code.isdigit():
            raise ValueError("fund_code必须是六位数字")
    amount = number(payload["amount"], positive=True) if payload.get("amount") is not None else None
    units = number(payload["units"], positive=True) if payload.get("units") is not None else None
    confirmation_date = (_date(payload["confirmation_date"], "confirmation_date")
                         if payload.get("confirmation_date") is not None else None)
    settlement_date = (_date(payload["settlement_date"], "settlement_date")
                       if payload.get("settlement_date") is not None else None)
    business_date = _date(payload.get("business_date"), "business_date")
    fee = number(payload["confirmed_fee"], nonnegative=True) if payload.get("confirmed_fee") is not None else None
    if event_type in {"initial_position", "buy", "sell"} and fund_code is None:
        raise ValueError(f"{event_type}必须提供fund_code")
    if event_type == "initial_position" and units is None:
        raise ValueError("initial_position必须提供正数units")
    if state == "confirmed" and event_type in {"buy", "sell"} and (amount is None or units is None or fee is None):
        raise ValueError(f"已确认{event_type}必须提供amount、units和confirmed_fee（确定免费填0）")
    if state == "confirmed" and event_type in {"cash_in", "cash_out", "dividend"} and amount is None:
        raise ValueError(f"已确认{event_type}必须提供正数amount")
    if event_type == "dividend" and fund_code is None:
        raise ValueError("dividend必须提供fund_code")
    if event_type in {"cash_in", "cash_out"} and (fund_code is not None or units is not None):
        raise ValueError(f"{event_type}不能提供fund_code或units")
    if event_type == "dividend" and units is not None:
        raise ValueError("现金分红不能提供units")
    if fee is not None and event_type not in {"buy", "sell"}:
        raise ValueError("confirmed_fee只适用于buy或sell")
    if confirmation_date is not None and confirmation_date < business_date:
        raise ValueError("confirmation_date不能早于business_date")
    if settlement_date is not None and (confirmation_date is None or settlement_date < confirmation_date):
        raise ValueError("settlement_date必须在confirmation_date当天或之后")
    if state == "confirmed" and confirmation_date is None:
        raise ValueError("已确认事件必须提供confirmation_date")
    return {
        "account_id": _text(payload.get("account_id"), "account_id", maximum=64),
        "event_type": event_type,
        "fund_code": fund_code,
        "amount": amount,
        "units": units,
        "business_date": business_date,
        "confirmation_date": confirmation_date,
        "settlement_date": settlement_date,
        "channel": _text(payload.get("channel"), "channel"),
        "confirmed_fee": fee,
        "state": state,
        "source": _text(payload.get("source"), "source"),
        "note": _optional_text(payload.get("note"), "note"),
    }


def _insert_event(connection: sqlite3.Connection, normalized: Mapping[str, Any], *, key: str,
                  request_hash: str, recorded_at: str | None = None) -> sqlite3.Row:
    _account(connection, normalized["account_id"])
    if normalized["fund_code"] is not None:
        known = connection.execute(
            "SELECT 1 FROM fund_catalog_projection WHERE code=?", (normalized["fund_code"],)
        ).fetchone()
        if known is None:
            raise AppError("portfolio_fund_not_found", "基金代码不在当前真实基金目录中", 422)
    event_id, now = f"evt_{uuid4().hex}", recorded_at or utc_now()
    connection.execute(
        """INSERT INTO ledger_event
           (event_id,account_id,event_type,fund_code,amount,units,business_date,confirmation_date,
            settlement_date,channel,confirmed_fee,state,source,note,
            recorded_at,idempotency_key,request_hash)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event_id, normalized["account_id"], normalized["event_type"], normalized["fund_code"],
         normalized["amount"], normalized["units"], normalized["business_date"],
         normalized["confirmation_date"], normalized["settlement_date"], normalized["channel"],
         normalized["confirmed_fee"], normalized["state"],
         normalized["source"], normalized["note"], now, key, request_hash),
    )
    return connection.execute("SELECT * FROM ledger_event WHERE event_id=?", (event_id,)).fetchone()


def append_event(settings: Settings, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = append_event_in_transaction(
                connection,
                payload,
                idempotency_key=idempotency_key,
            )
            connection.execute("COMMIT")
            return result
        except sqlite3.IntegrityError as exc:
            connection.execute("ROLLBACK")
            raise AppError("portfolio_write_conflict", "账本写入违反完整性约束", 409, {"reason": str(exc)}) from exc
        except Exception:
            connection.execute("ROLLBACK")
            raise


def append_event_in_transaction(
    connection: sqlite3.Connection,
    payload: Mapping[str, Any],
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    """Append one ledger event inside the caller's active transaction.

    This is the shared atomic primitive for ordinary ledger writes and for a
    decision execution that must insert both the event and its audit link.
    The caller owns commit/rollback.
    """
    try:
        key, normalized = _key(idempotency_key), _normalize_event(payload)
    except ValueError as exc:
        raise AppError("portfolio_input_invalid", str(exc), 422) from exc
    request_hash = digest(normalized)
    existing = connection.execute(
        "SELECT * FROM ledger_event WHERE account_id=? AND idempotency_key=?",
        (normalized["account_id"], key),
    ).fetchone()
    if existing is not None:
        if existing["request_hash"] != request_hash:
            raise AppError("portfolio_idempotency_conflict", "幂等键已用于不同的账本请求", 409)
        return _event(existing, idempotent=True)
    row = _insert_event(connection, normalized, key=key, request_hash=request_hash)
    _assert_nonnegative(connection, normalized["account_id"])
    return _event(row, idempotent=False)


def _event(row: sqlite3.Row, *, idempotent: bool | None = None) -> dict[str, Any]:
    result = dict(row)
    result.pop("idempotency_key", None)
    result.pop("request_hash", None)
    if idempotent is not None:
        result["idempotent_replay"] = idempotent
    return result


def _current_event_rows(connection: sqlite3.Connection, account_id: str, cutoff: str | None = None) -> list[sqlite3.Row]:
    date_filter, params = (" AND e.business_date<=?", [account_id, cutoff]) if cutoff else ("", [account_id])
    return connection.execute(
        """SELECT e.* FROM ledger_event e
           LEFT JOIN ledger_event_correction c ON c.target_event_id=e.event_id
           WHERE e.account_id=? AND c.correction_id IS NULL""" + date_filter +
        " ORDER BY e.business_date,e.recorded_at,e.event_id", params,
    ).fetchall()


def _balances(rows: list[sqlite3.Row], *, validate: bool, recognition_cutoff: str | None = None,
              cash_basis: str = "confirmed") -> tuple[Decimal, dict[str, Decimal]]:
    if cash_basis not in {"confirmed", "available"}:
        raise ValueError("cash_basis必须为confirmed或available")
    changes: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"cash": Decimal(0), "positions": defaultdict(Decimal)}
    )
    for row in rows:
        if (row["state"] == "confirmed" and row["confirmation_date"] is not None
                and (recognition_cutoff is None or row["confirmation_date"] <= recognition_cutoff)):
            confirmation_date = row["confirmation_date"]
            amount = Decimal(row["amount"]) if row["amount"] is not None else Decimal(0)
            units = Decimal(row["units"]) if row["units"] is not None else Decimal(0)
            fee = Decimal(row["confirmed_fee"]) if row["confirmed_fee"] is not None else Decimal(0)
            event_type = row["event_type"]
            if event_type in {"initial_position", "buy"}:
                changes[confirmation_date]["positions"][row["fund_code"]] += units
            elif event_type == "sell":
                changes[confirmation_date]["positions"][row["fund_code"]] -= units

            if event_type in {"cash_in", "sell", "dividend"}:
                cash_date = confirmation_date if cash_basis == "confirmed" else row["settlement_date"]
                if cash_date is not None and (recognition_cutoff is None or cash_date <= recognition_cutoff):
                    changes[cash_date]["cash"] += amount - (fee if event_type == "sell" else Decimal(0))
            elif event_type == "cash_out":
                changes[confirmation_date]["cash"] -= amount
            elif event_type == "buy":
                changes[confirmation_date]["cash"] -= amount + fee

    cash, positions = Decimal(0), defaultdict(Decimal)
    for recognition_date in sorted(changes):
        cash += changes[recognition_date]["cash"]
        for code, units_delta in changes[recognition_date]["positions"].items():
            positions[code] += units_delta
        if validate and cash < 0:
            label = "可用" if cash_basis == "available" else "确认"
            raise AppError("portfolio_negative_cash", f"{recognition_date}日终{label}现金将小于零", 409)
        negative = [code for code, units in positions.items() if units < 0]
        if validate and negative:
            raise AppError("portfolio_negative_units", f"{recognition_date}日终基金{negative[0]}确认份额将小于零", 409)
    return cash, dict(positions)


def _assert_nonnegative(connection: sqlite3.Connection, account_id: str) -> None:
    rows = _current_event_rows(connection, account_id)
    _balances(rows, validate=True)
    _balances(rows, validate=True, cash_basis="available")


def correct_event(settings: Settings, target_event_id: str, payload: Mapping[str, Any], *,
                  idempotency_key: str) -> dict[str, Any]:
    try:
        key = _key(idempotency_key)
        action = _text(payload.get("action"), "action", maximum=20)
        if action not in {"reverse", "replace"}:
            raise ValueError("action必须为reverse或replace")
        reason = _text(payload.get("reason"), "reason", maximum=1000)
        replacement_raw = payload.get("replacement")
        if action == "reverse" and replacement_raw is not None:
            raise ValueError("reverse不能提供replacement")
        if action == "replace" and not isinstance(replacement_raw, Mapping):
            raise ValueError("replace必须提供replacement")
    except ValueError as exc:
        raise AppError("portfolio_input_invalid", str(exc), 422) from exc
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            target = connection.execute("SELECT * FROM ledger_event WHERE event_id=?", (target_event_id,)).fetchone()
            if target is None:
                raise AppError("portfolio_event_not_found", "账本事件不存在", 404)
            account_id = target["account_id"]
            normalized_replacement = None
            if replacement_raw is not None:
                normalized_replacement = _normalize_event({**replacement_raw, "account_id": account_id})
            request = {"target_event_id": target_event_id, "action": action, "reason": reason,
                       "replacement": normalized_replacement}
            request_hash = digest(request)
            existing = connection.execute(
                "SELECT * FROM ledger_event_correction WHERE account_id=? AND idempotency_key=?", (account_id, key)
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise AppError("portfolio_idempotency_conflict", "幂等键已用于不同的更正请求", 409)
                connection.execute("COMMIT")
                return _correction(connection, existing, idempotent=True)
            if connection.execute("SELECT 1 FROM ledger_event_correction WHERE target_event_id=?", (target_event_id,)).fetchone():
                raise AppError("portfolio_correction_conflict", "该事件已有更正；如需继续更正请引用当前替代事件", 409)
            correction_id, now = f"corr_{uuid4().hex}", utc_now()
            replacement_id = None
            if normalized_replacement is not None:
                replacement = _insert_event(
                    connection, normalized_replacement, key=f"{key}:replacement:{correction_id}",
                    request_hash=digest(normalized_replacement), recorded_at=now,
                )
                replacement_id = replacement["event_id"]
            connection.execute(
                """INSERT INTO ledger_event_correction
                   (correction_id,account_id,target_event_id,action,replacement_event_id,reason,recorded_at,
                    idempotency_key,request_hash) VALUES(?,?,?,?,?,?,?,?,?)""",
                (correction_id, account_id, target_event_id, action, replacement_id, reason, now, key, request_hash),
            )
            _assert_nonnegative(connection, account_id)
            row = connection.execute(
                "SELECT * FROM ledger_event_correction WHERE correction_id=?", (correction_id,)
            ).fetchone()
            result = _correction(connection, row, idempotent=False)
            connection.execute("COMMIT")
            return result
        except sqlite3.IntegrityError as exc:
            connection.execute("ROLLBACK")
            raise AppError("portfolio_write_conflict", "账本更正违反完整性约束", 409, {"reason": str(exc)}) from exc
        except Exception:
            connection.execute("ROLLBACK")
            raise


def _correction(connection: sqlite3.Connection, row: sqlite3.Row, *, idempotent: bool) -> dict[str, Any]:
    result = dict(row)
    result.pop("idempotency_key", None)
    result.pop("request_hash", None)
    result["idempotent_replay"] = idempotent
    result["replacement"] = (_event(connection.execute(
        "SELECT * FROM ledger_event WHERE event_id=?", (row["replacement_event_id"],)
    ).fetchone()) if row["replacement_event_id"] else None)
    return result


def list_events(settings: Settings, account_id: str) -> dict[str, Any]:
    migrate(settings)
    with connection_scope(settings) as connection:
        _account(connection, account_id)
        rows = connection.execute(
            """SELECT e.*,c.correction_id,c.action AS correction_action,c.replacement_event_id,c.reason AS correction_reason
               FROM ledger_event e LEFT JOIN ledger_event_correction c ON c.target_event_id=e.event_id
               WHERE e.account_id=? ORDER BY e.business_date,e.recorded_at,e.event_id""", (account_id,)
        ).fetchall()
    items = []
    for row in rows:
        item = {key: row[key] for key in row.keys() if key not in {
            "correction_id", "correction_action", "replacement_event_id", "correction_reason",
            "idempotency_key", "request_hash",
        }}
        item["is_current"] = row["correction_id"] is None
        item["correction"] = ({"correction_id": row["correction_id"], "action": row["correction_action"],
                               "replacement_event_id": row["replacement_event_id"], "reason": row["correction_reason"]}
                              if row["correction_id"] else None)
        items.append(item)
    return {"account_id": account_id, "items": items}


def portfolio_snapshot(settings: Settings, account_id: str, cutoff: str) -> dict[str, Any]:
    try:
        cutoff = _date(cutoff, "cutoff")
    except ValueError as exc:
        raise AppError("portfolio_input_invalid", str(exc), 422) from exc
    migrate(settings)
    with connection_scope(settings) as connection:
        account = _account(connection, account_id)
        rows = _current_event_rows(connection, account_id, cutoff)
        confirmed_cash, units = _balances(rows, validate=True, recognition_cutoff=cutoff)
        available_cash, _ = _balances(
            rows, validate=True, recognition_cutoff=cutoff, cash_basis="available",
        )
        codes = sorted(code for code, value in units.items() if value != 0)
        names = {}
        if codes:
            placeholders = ",".join("?" for _ in codes)
            names = {row["code"]: row["name"] for row in connection.execute(
                f"SELECT code,name FROM fund_catalog_projection WHERE code IN ({placeholders})", codes
            )}
        legacy_incomplete = any(row["state"] == "confirmed" and row["confirmation_date"] is None for row in rows)
        recognized = [row for row in rows if row["state"] == "confirmed"
                      and row["confirmation_date"] is not None and row["confirmation_date"] <= cutoff]
        initial_codes = {row["fund_code"] for row in recognized if row["event_type"] == "initial_position"}
        incomplete_settlement = any(row["event_type"] in {"sell", "cash_in", "cash_out", "dividend"}
                                    and row["settlement_date"] is None for row in recognized)
        pending = [_event(row) for row in rows if row["state"] == "pending"
                   or (row["state"] == "confirmed" and row["confirmation_date"] is not None
                       and row["confirmation_date"] > cutoff)
                   or (row["state"] == "confirmed" and row["confirmation_date"] is not None
                       and row["confirmation_date"] <= cutoff and row["settlement_date"] is not None
                       and row["settlement_date"] > cutoff)]
        unknown = [_event(row) for row in rows if row["state"] == "unknown"
                   or (row["state"] == "confirmed" and row["confirmation_date"] is None)]
    positions = [{"fund_code": code, "fund_name": names.get(code), "confirmed_units": number(str(units[code])),
                  "cost_history_complete": code not in initial_codes}
                 for code in codes if units[code] > 0]
    return {
        "version": ACCOUNT_VERSION,
        "account": {key: account[key] for key in ("account_id", "name", "base_currency", "status")},
        "cutoff": cutoff,
        "cutoff_semantics": "confirmed_by_date_end_current_correction_view",
        "cash": {"currency": account["base_currency"],
                 "confirmed": number(str(confirmed_cash)),
                 "available": number(str(available_cash)),
                 "pending_settlement": number(str(confirmed_cash - available_cash))},
        "positions": positions,
        "pending_events": pending,
        "unknown_events": unknown,
        "coverage": {
            "cash_and_units": "incomplete" if legacy_incomplete else "confirmed",
            "cost_history": "incomplete" if any(not item["cost_history_complete"] for item in positions) else "complete",
            "settlement_history": "incomplete" if incomplete_settlement else "complete",
            "market_valuation": "unavailable",
        },
        "generated_at": utc_now(),
    }
