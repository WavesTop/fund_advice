"""Immutable strategy recommendations, user decisions and validation runs."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, localcontext
import json
import sqlite3

from backend.core.config import Settings
from backend.core.errors import AppError
from backend.core.trading_calendar import CalendarUnavailable, SHANGHAI, get_calendar
from backend.storage.database import connection_scope, migrate
from backend.storage.portfolio import append_event_in_transaction
from backend.storage.research import canonical, digest, number, text, utc_now


def _json(value: str) -> object:
    return json.loads(value)


def save_recommendation(settings: Settings, *, account_id: str, strategy_version: str,
                        input_cutoff: str, data_as_of: str, status: str,
                        inputs: dict, output: dict) -> dict:
    if status not in ("actionable", "partial", "blocked"):
        raise ValueError("建议状态无效")
    input_hash, output_hash = digest(inputs), digest(output)
    # The migration's natural key is account + strategy + inputs.  Keep the
    # identifier on that same key so a deterministic replay cannot be ignored
    # under one id and then looked up under a different, nonexistent id.
    recommendation_id = digest([account_id, strategy_version, input_hash])
    created_at = utc_now()
    migrate(settings)
    with connection_scope(settings) as connection:
        try:
            existing = connection.execute(
                """SELECT * FROM strategy_recommendation_run
                   WHERE account_id=? AND strategy_version=? AND input_hash=?""",
                (account_id, strategy_version, input_hash),
            ).fetchone()
            if existing is not None:
                if (existing["input_cutoff"] != input_cutoff or existing["data_as_of"] != data_as_of
                        or existing["status"] != status or existing["output_hash"] != output_hash):
                    raise AppError(
                        "strategy_recommendation_nondeterministic",
                        "相同策略输入产生了不同建议，已拒绝覆盖原记录",
                        409,
                    )
                return _recommendation(existing)
            connection.execute(
                """INSERT INTO strategy_recommendation_run
                   (recommendation_id,account_id,strategy_version,input_cutoff,data_as_of,
                    created_at,status,input_hash,input_json,output_hash,output_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (recommendation_id, account_id, strategy_version, input_cutoff, data_as_of,
                 created_at, status, input_hash, canonical(inputs), output_hash, canonical(output)),
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError("strategy_recommendation_save_failed", "策略建议保存失败", 500,
                           {"reason": str(exc)}) from exc
    return get_recommendation(settings, recommendation_id)


def _recommendation(row) -> dict:
    value = dict(row)
    value["inputs"] = _json(value.pop("input_json"))
    value["output"] = _json(value.pop("output_json"))
    return value


def get_recommendation(settings: Settings, recommendation_id: str) -> dict:
    migrate(settings)
    with connection_scope(settings) as connection:
        row = connection.execute("SELECT * FROM strategy_recommendation_run WHERE recommendation_id=?",
                                 (recommendation_id,)).fetchone()
    if row is None:
        raise AppError("strategy_recommendation_not_found", "策略建议不存在", 404)
    return _recommendation(row)


def latest_recommendation(settings: Settings, account_id: str) -> dict | None:
    migrate(settings)
    with connection_scope(settings) as connection:
        row = connection.execute(
            "SELECT * FROM strategy_recommendation_run WHERE account_id=? ORDER BY created_at DESC LIMIT 1",
            (account_id,),
        ).fetchone()
    return _recommendation(row) if row else None


def list_recommendations(settings: Settings, account_id: str, limit: int = 50) -> list[dict]:
    if not 1 <= limit <= 100:
        raise ValueError("建议数量须为1至100")
    migrate(settings)
    with connection_scope(settings) as connection:
        rows = connection.execute(
            "SELECT * FROM strategy_recommendation_run WHERE account_id=? ORDER BY created_at DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
    return [_recommendation(row) for row in rows]


def save_decision(settings: Settings, *, recommendation_id: str, account_id: str,
                  fund_code: str | None, proposed_action: str, choice: str,
                  planned_amount: str | None, reason: str, idempotency_key: str,
                  supersedes_decision_id: str | None = None,
                  current_input_hash: str | None = None) -> dict:
    if proposed_action not in ("BUY", "ADD", "HOLD", "REDUCE", "EXIT", "WAIT"):
        raise AppError("strategy_decision_invalid", "建议动作无效", 422)
    if choice not in ("accepted", "partially_accepted", "rejected", "deferred"):
        raise AppError("strategy_decision_invalid", "用户决定无效", 422)
    if (fund_code is not None
            and (len(fund_code) != 6 or not fund_code.isascii() or not fund_code.isdigit())):
        raise AppError("strategy_decision_invalid", "基金决定必须使用六位代码；账户级暂缓决定使用null", 422)
    try:
        reason = text(reason, "reason", maximum=1000)
        idempotency_key = text(idempotency_key, "Idempotency-Key", maximum=200)
        planned_amount = number(planned_amount, nonnegative=True) if planned_amount is not None else None
    except ValueError as exc:
        raise AppError("strategy_decision_invalid", str(exc), 422) from exc
    if choice == "partially_accepted" and planned_amount is None:
        raise AppError("strategy_decision_invalid", "部分采纳必须填写计划金额", 422)
    request = {"recommendation_id": recommendation_id, "account_id": account_id,
               "fund_code": fund_code, "proposed_action": proposed_action, "choice": choice,
               "planned_amount": planned_amount, "reason": reason,
               "supersedes_decision_id": supersedes_decision_id}
    request_hash = digest(request)
    decision_id = digest([account_id, idempotency_key])
    migrate(settings)
    with connection_scope(settings) as connection:
        recommendation = connection.execute(
            "SELECT * FROM strategy_recommendation_run WHERE recommendation_id=?",
            (recommendation_id,),
        ).fetchone()
        if recommendation is None:
            raise AppError("strategy_recommendation_not_found", "策略建议不存在", 404)
        if recommendation["account_id"] != account_id:
            raise AppError("strategy_decision_account_mismatch", "决定账户与策略建议账户不一致", 409)
        recommended_items = _json(recommendation["output_json"]).get("items", [])
        recommended = next((item for item in recommended_items if item.get("fund_code") == fund_code), None)
        if recommended is None or recommended.get("action") != proposed_action:
            raise AppError("strategy_decision_action_mismatch", "决定与该次策略建议中的基金动作不一致", 409)
        recommended_amount = recommended.get("proposed_amount")
        if choice in ("rejected", "deferred") and planned_amount is not None:
            raise AppError("strategy_decision_amount_invalid", "拒绝或延后决定不能填写计划金额", 422)
        if proposed_action in ("HOLD", "WAIT") and planned_amount is not None:
            raise AppError("strategy_decision_amount_invalid", "无需交易的决定不能填写计划金额", 422)
        if choice == "accepted" and proposed_action in ("BUY", "ADD", "REDUCE", "EXIT"):
            if planned_amount is None:
                raise AppError("strategy_decision_amount_invalid", "完整采纳交易必须保留建议金额", 422)
            if recommended_amount is None or Decimal(planned_amount) != Decimal(str(recommended_amount)):
                raise AppError("strategy_decision_amount_mismatch", "完整采纳的计划金额必须等于建议金额", 409)
        if choice == "partially_accepted":
            if (recommended_amount is None or Decimal(str(planned_amount)) <= 0
                    or Decimal(str(planned_amount)) >= Decimal(str(recommended_amount))):
                raise AppError("strategy_decision_amount_invalid", "部分采纳金额必须大于0且小于建议金额", 422)
        if supersedes_decision_id is not None:
            superseded = connection.execute(
                "SELECT account_id,fund_code FROM strategy_decision WHERE decision_id=?",
                (supersedes_decision_id,),
            ).fetchone()
            if (superseded is None or superseded["account_id"] != account_id
                    or superseded["fund_code"] != fund_code):
                raise AppError("strategy_decision_supersedes_invalid", "被替代决定不存在或不属于同一账户基金", 409)
        existing = connection.execute(
            "SELECT * FROM strategy_decision WHERE account_id=? AND idempotency_key=?",
            (account_id, idempotency_key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise AppError("strategy_idempotency_conflict", "相同请求标识对应不同决定内容", 409)
            return _decision(existing)
        if current_input_hash is not None and recommendation["input_hash"] != current_input_hash:
            raise AppError(
                "strategy_decision_stale",
                "建议使用的账本、约束、行情日期或策略输入已经变化，请重新生成后再记录新决定",
                409,
                {
                    "recommendation_id": recommendation_id,
                    "stored_input_hash": recommendation["input_hash"],
                    "current_input_hash": current_input_hash,
                },
            )
        try:
            connection.execute(
                """INSERT INTO strategy_decision
                   (decision_id,recommendation_id,account_id,fund_code,proposed_action,choice,
                    planned_amount,reason,created_at,supersedes_decision_id,idempotency_key,request_hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (decision_id, recommendation_id, account_id, fund_code, proposed_action, choice,
                 planned_amount, reason, utc_now(), supersedes_decision_id, idempotency_key, request_hash),
            )
        except Exception as exc:
            raise AppError("strategy_decision_invalid", "决定记录未保存", 422,
                           {"reason": str(exc)}) from exc
        return _decision(connection.execute("SELECT * FROM strategy_decision WHERE decision_id=?",
                                            (decision_id,)).fetchone())


def _decision(row) -> dict:
    value = dict(row)
    value.pop("idempotency_key", None)
    value.pop("request_hash", None)
    return value


def _execution_summary_inner(decision: dict, links: list[dict]) -> dict:
    active_actual = [
        link for link in links
        if link["role"] == "actual_execution" and not link["event"]["corrected"]
    ]
    invalidated_actual = [
        link for link in links
        if link["role"] == "actual_execution" and link["event"]["corrected"]
    ]
    planned_links = [link for link in links if link["role"] == "planned_execution"]

    def decimal_total(field: str) -> Decimal | None:
        values = [Decimal(str(link["event"][field])) for link in active_actual
                  if link["event"].get(field) is not None]
        return sum(values, Decimal(0)) if values else None

    actual_amount = decimal_total("amount")
    actual_units = decimal_total("units")
    if decision["proposed_action"] in ("HOLD", "WAIT") or decision["choice"] in ("rejected", "deferred"):
        status = "not_required"
    elif active_actual:
        if decision["proposed_action"] in ("REDUCE", "EXIT"):
            planned_target = (Decimal(str(decision["planned_units"]))
                              if decision.get("planned_units") is not None else None)
            actual = actual_units
        else:
            planned_target = (Decimal(str(decision["planned_amount"]))
                              if decision.get("planned_amount") is not None else None)
            actual = actual_amount
        tolerance = Decimal("0.000001") if decision["proposed_action"] in ("REDUCE", "EXIT") else Decimal("0.01")
        if planned_target is not None and (actual is None or actual + tolerance < planned_target):
            status = "partially_executed"
        elif planned_target is not None and actual is not None and actual > planned_target + tolerance:
            status = "over_executed"
        else:
            status = "executed"
    elif invalidated_actual:
        status = "execution_invalidated"
    elif planned_links:
        status = "planned"
    else:
        status = "awaiting_execution"

    def rendered(value: Decimal | None) -> str | None:
        if value is None:
            return None
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    return {
        "status": status,
        "actual_amount": rendered(actual_amount),
        "actual_units": rendered(actual_units),
        "actual_fee": rendered(decimal_total("confirmed_fee")),
        "actual_event_count": len(active_actual),
        "planned_event_count": len(planned_links),
        "invalidated_actual_event_count": len(invalidated_actual),
    }


def _execution_summary(decision: dict, links: list[dict]) -> dict:
    with localcontext() as context:
        context.prec = 40
        return _execution_summary_inner(decision, links)


def list_decisions(settings: Settings, account_id: str, limit: int = 100) -> list[dict]:
    if not 1 <= limit <= 200:
        raise ValueError("决定数量须为1至200")
    migrate(settings)
    with connection_scope(settings) as connection:
        decisions = [_decision(row) for row in connection.execute(
            "SELECT * FROM strategy_decision WHERE account_id=? ORDER BY created_at DESC,rowid DESC LIMIT ?",
            (account_id, limit),
        )]
        if not decisions:
            return []
        recommendation_ids = sorted({str(item["recommendation_id"]) for item in decisions})
        recommendation_placeholders = ",".join("?" for _ in recommendation_ids)
        recommendation_outputs = {
            str(row["recommendation_id"]): _json(str(row["output_json"]))
            for row in connection.execute(
                f"""SELECT recommendation_id,output_json FROM strategy_recommendation_run
                    WHERE recommendation_id IN ({recommendation_placeholders})""",
                tuple(recommendation_ids),
            )
        }
        placeholders = ",".join("?" for _ in decisions)
        rows = connection.execute(
            f"""SELECT l.link_id,l.decision_id,l.event_id,l.role,l.created_at AS linked_at,
                       e.event_type,e.fund_code,e.amount,e.units,e.confirmed_fee,
                       e.business_date,e.confirmation_date,e.settlement_date,e.state,
                       c.correction_id
                FROM decision_trade_link l
                JOIN ledger_event e ON e.event_id=l.event_id
                LEFT JOIN ledger_event_correction c ON c.target_event_id=e.event_id
                WHERE l.decision_id IN ({placeholders})
                ORDER BY l.created_at,l.link_id""",
            tuple(item["decision_id"] for item in decisions),
        ).fetchall()
        by_decision: dict[str, list[dict]] = {item["decision_id"]: [] for item in decisions}
        for row in rows:
            by_decision[str(row["decision_id"])].append({
                "link_id": str(row["link_id"]),
                "event_id": str(row["event_id"]),
                "role": str(row["role"]),
                "linked_at": str(row["linked_at"]),
                "event": {
                    "event_type": str(row["event_type"]),
                    "fund_code": row["fund_code"],
                    "amount": row["amount"],
                    "units": row["units"],
                    "confirmed_fee": row["confirmed_fee"],
                    "business_date": str(row["business_date"]),
                    "confirmation_date": row["confirmation_date"],
                    "settlement_date": row["settlement_date"],
                    "state": str(row["state"]),
                    "corrected": row["correction_id"] is not None,
                },
            })
        for decision in decisions:
            recommendation = recommendation_outputs.get(str(decision["recommendation_id"]), {})
            recommended = next(
                (item for item in recommendation.get("items", [])
                 if item.get("fund_code") == decision["fund_code"]
                 and item.get("action") == decision["proposed_action"]),
                None,
            )
            planned_units = None
            if recommended is not None and recommended.get("proposed_units") is not None:
                with localcontext() as context:
                    context.prec = 40
                    planned_units_value = Decimal(str(recommended["proposed_units"]))
                    if (decision["choice"] == "partially_accepted"
                            and decision["planned_amount"] is not None
                            and recommended.get("proposed_amount") is not None
                            and Decimal(str(recommended["proposed_amount"])) > 0):
                        planned_units_value *= (
                            Decimal(str(decision["planned_amount"]))
                            / Decimal(str(recommended["proposed_amount"]))
                        )
                    planned_units = format(planned_units_value, "f")
            decision["planned_units"] = planned_units
            links = by_decision[decision["decision_id"]]
            decision["trade_links"] = links
            decision["execution"] = _execution_summary(decision, links)
        return decisions


def _local_business_date(timestamp_value: str, field: str) -> date:
    try:
        parsed = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AppError("strategy_trade_link_time_invalid", f"{field}时间戳无效", 409) from exc
    if parsed.tzinfo is None:
        raise AppError("strategy_trade_link_time_invalid", f"{field}时间戳缺少时区", 409)
    return parsed.astimezone(SHANGHAI).date()


def _link_decision_trade_in_transaction(
    connection: sqlite3.Connection,
    *,
    decision_id: str,
    event_id: str,
    role: str,
) -> dict:
    if role not in ("planned_execution", "actual_execution", "independent_reference"):
        raise AppError("strategy_trade_link_invalid", "成交关联角色无效", 422)
    link_id = digest([decision_id, event_id, role])
    existing = connection.execute(
        "SELECT * FROM decision_trade_link WHERE link_id=?",
        (link_id,),
    ).fetchone()
    if existing is not None:
        return dict(existing)
    ownership = connection.execute(
        """SELECT d.account_id AS decision_account,e.account_id AS event_account,
                  d.fund_code AS decision_fund,d.proposed_action,d.choice,
                  d.created_at AS decision_created_at,
                  e.fund_code AS event_fund,e.event_type,e.state,e.business_date,
                  e.recorded_at AS event_recorded_at,
                  r.data_as_of,r.created_at AS recommendation_created_at,
                  r.input_json,r.output_json,c.correction_id
           FROM strategy_decision d
           JOIN strategy_recommendation_run r ON r.recommendation_id=d.recommendation_id
           JOIN ledger_event e ON e.event_id=?
           LEFT JOIN ledger_event_correction c ON c.target_event_id=e.event_id
           WHERE d.decision_id=?""",
        (event_id, decision_id),
    ).fetchone()
    if ownership is None:
        raise AppError("strategy_trade_link_not_found", "决定或账本事件不存在", 404)
    if ownership["decision_account"] != ownership["event_account"]:
        raise AppError("strategy_trade_link_account_mismatch", "决定与账本事件不属于同一账户", 409)
    if ownership["decision_fund"] is None:
        raise AppError("strategy_trade_link_account_level", "账户级暂缓决定不能关联基金成交", 409)
    if (ownership["event_type"] not in ("buy", "sell")
            or ownership["decision_fund"] != ownership["event_fund"]):
        raise AppError("strategy_trade_link_fund_mismatch", "只能关联同一基金的买入或卖出账本事件", 409)
    expected_event = ({"BUY": "buy", "ADD": "buy", "REDUCE": "sell", "EXIT": "sell"}
                      .get(ownership["proposed_action"]))
    if role != "independent_reference" and ownership["event_type"] != expected_event:
        raise AppError("strategy_trade_link_action_mismatch", "建议动作与账本买卖方向不一致", 409)
    if role != "independent_reference" and ownership["choice"] not in ("accepted", "partially_accepted"):
        raise AppError("strategy_trade_link_decision_not_accepted", "未采纳的决定不能关联计划或实际执行", 409)
    if role != "independent_reference":
        try:
            business_date = date.fromisoformat(str(ownership["business_date"]))
            output = _json(str(ownership["output_json"]))
            inputs = _json(str(ownership["input_json"]))
            execution_policy = output.get("execution_policy", {})
            earliest_text = execution_policy.get("earliest_execution_session")
            latest_text = execution_policy.get("latest_execution_session", earliest_text)
            earliest = date.fromisoformat(earliest_text) if isinstance(earliest_text, str) else None
            latest = date.fromisoformat(latest_text) if isinstance(latest_text, str) else None
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AppError(
                "strategy_trade_link_window_unavailable",
                "建议缺少可核验的执行交易日窗口，不能关联执行",
                409,
            ) from exc
        if earliest is None or latest is None or latest < earliest:
            raise AppError(
                "strategy_trade_link_window_unavailable",
                "建议缺少可核验的执行交易日窗口，不能关联执行",
                409,
            )
        if business_date < earliest:
            raise AppError(
                "strategy_trade_link_time_invalid",
                "执行日期早于建议规定的最早可执行交易日",
                409,
                {"earliest_execution_session": earliest_text},
            )
        if business_date > latest:
            raise AppError(
                "strategy_trade_link_expired",
                "执行日期已超过建议的最晚有效交易日，请重新生成建议",
                409,
                {
                    "latest_execution_session": latest_text,
                    "business_date": business_date.isoformat(),
                },
            )
        decision_date = _local_business_date(str(ownership["decision_created_at"]), "决定")
        if business_date < decision_date:
            raise AppError("strategy_trade_link_time_invalid", "执行业务日不能早于决定记录日", 409)
        if str(ownership["event_recorded_at"]) < str(ownership["decision_created_at"]):
            raise AppError("strategy_trade_link_time_invalid", "成交记录必须在决定记录完成后产生", 409)
        calendar = get_calendar()
        stored_calendar_hash = inputs.get("calendar_hash")
        if isinstance(stored_calendar_hash, str) and stored_calendar_hash != calendar.hash:
            raise AppError("strategy_trade_link_calendar_changed", "建议使用的交易日历已变化，请重新生成建议", 409)
        try:
            is_session = calendar.is_session(business_date)
        except CalendarUnavailable as exc:
            raise AppError("strategy_trade_link_calendar_unavailable", str(exc), 409) from exc
        if not is_session:
            raise AppError("strategy_trade_link_non_session", "执行业务日不是已核验交易日", 409)
        if ownership["correction_id"] is not None:
            raise AppError("strategy_trade_link_corrected_event", "已更正的账本事件不能作为计划或实际执行", 409)
    if role == "planned_execution" and ownership["state"] != "pending":
        raise AppError("strategy_trade_link_planned_state_invalid", "计划执行只能关联待确认账本事件", 409)
    if role == "actual_execution":
        if ownership["state"] != "confirmed":
            raise AppError("strategy_trade_link_unconfirmed_event", "实际执行只能关联已确认账本事件", 409)
        existing_actual = connection.execute(
            """SELECT decision_id FROM decision_trade_link
               WHERE event_id=? AND role='actual_execution' AND decision_id<>? LIMIT 1""",
            (event_id, decision_id),
        ).fetchone()
        if existing_actual is not None:
            raise AppError("strategy_trade_link_event_reused", "该实际成交已关联另一项决定", 409)
    try:
        connection.execute(
            "INSERT INTO decision_trade_link VALUES(?,?,?,?,?)",
            (link_id, decision_id, event_id, role, utc_now()),
        )
    except sqlite3.IntegrityError as exc:
        raise AppError("strategy_trade_link_invalid", "决定与交易关联失败", 422,
                       {"reason": str(exc)}) from exc
    return dict(connection.execute(
        "SELECT * FROM decision_trade_link WHERE link_id=?",
        (link_id,),
    ).fetchone())


def link_decision_trade(settings: Settings, *, decision_id: str, event_id: str, role: str) -> dict:
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = _link_decision_trade_in_transaction(
                connection,
                decision_id=decision_id,
                event_id=event_id,
                role=role,
            )
            connection.execute("COMMIT")
            return result
        except Exception:
            connection.execute("ROLLBACK")
            raise


def append_decision_execution(
    settings: Settings,
    *,
    decision_id: str,
    payload: dict,
    role: str,
    idempotency_key: str,
) -> dict:
    """Atomically append a ledger event and link it to an accepted decision."""
    if role not in ("planned_execution", "actual_execution"):
        raise AppError("strategy_trade_link_invalid", "决定执行只支持计划或实际成交", 422)
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            decision = connection.execute(
                "SELECT account_id FROM strategy_decision WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if decision is None:
                raise AppError("strategy_decision_not_found", "策略决定不存在", 404)
            event = append_event_in_transaction(
                connection,
                {**payload, "account_id": str(decision["account_id"])},
                idempotency_key=idempotency_key,
            )
            link = _link_decision_trade_in_transaction(
                connection,
                decision_id=decision_id,
                event_id=str(event["event_id"]),
                role=role,
            )
            connection.execute("COMMIT")
            return {"event": event, "link": link}
        except sqlite3.IntegrityError as exc:
            connection.execute("ROLLBACK")
            raise AppError("strategy_execution_write_conflict", "成交与决定关联违反完整性约束", 409,
                           {"reason": str(exc)}) from exc
        except Exception:
            connection.execute("ROLLBACK")
            raise


def save_backtest(settings: Settings, *, strategy_version: str, start_date: str, end_date: str,
                  calendar_basis: str, protocol: dict, inputs: dict, output: dict,
                  status: str = "completed") -> dict:
    if status not in ("completed", "insufficient", "failed"):
        raise ValueError("回测状态无效")
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("回测日期无效") from exc
    if start > end or start.isoformat() != start_date or end.isoformat() != end_date:
        raise ValueError("回测日期无效")
    stored_output = {key: value for key, value in output.items() if key != "generated_at"}
    protocol_hash, input_hash, output_hash = digest(protocol), digest(inputs), digest(stored_output)
    run_id = digest([strategy_version, protocol_hash, input_hash])
    migrate(settings)
    with connection_scope(settings) as connection:
        existing = connection.execute("SELECT * FROM strategy_backtest_run WHERE run_id=?", (run_id,)).fetchone()
        if existing is not None:
            if existing["output_hash"] != output_hash or existing["status"] != status:
                raise AppError("strategy_backtest_nondeterministic",
                               "相同历史验证输入产生了不同结果，已拒绝覆盖原记录", 409)
            return _backtest(existing)
        try:
            connection.execute(
                """INSERT INTO strategy_backtest_run
                   (run_id,strategy_version,created_at,start_date,end_date,calendar_basis,
                    protocol_hash,protocol_json,input_hash,input_json,output_hash,output_json,status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, strategy_version, utc_now(), start_date, end_date, calendar_basis,
                 protocol_hash, canonical(protocol), input_hash, canonical(inputs), output_hash,
                 canonical(stored_output), status),
            )
        except sqlite3.IntegrityError as exc:
            raise AppError("strategy_backtest_save_failed", "策略历史验证保存失败", 409,
                           {"reason": str(exc)}) from exc
    return get_backtest(settings, run_id)


def _backtest(row) -> dict:
    value = dict(row)
    for field in ("protocol_json", "input_json", "output_json"):
        value[field.removesuffix("_json")] = _json(value.pop(field))
    return value


def get_backtest(settings: Settings, run_id: str) -> dict:
    migrate(settings)
    with connection_scope(settings) as connection:
        row = connection.execute("SELECT * FROM strategy_backtest_run WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise AppError("strategy_backtest_not_found", "策略历史验证不存在", 404)
    return _backtest(row)


def latest_backtest(settings: Settings) -> dict | None:
    migrate(settings)
    with connection_scope(settings) as connection:
        row = connection.execute(
            "SELECT * FROM strategy_backtest_run ORDER BY created_at DESC,rowid DESC LIMIT 1"
        ).fetchone()
    return _backtest(row) if row else None


def list_backtests(settings: Settings, limit: int = 20) -> list[dict]:
    if not 1 <= limit <= 100:
        raise ValueError("历史验证数量须为1至100")
    migrate(settings)
    with connection_scope(settings) as connection:
        rows = connection.execute(
            "SELECT * FROM strategy_backtest_run ORDER BY created_at DESC,rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_backtest(row) for row in rows]
