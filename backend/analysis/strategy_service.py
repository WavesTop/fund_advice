"""Formal portfolio recommendations built only from stored, attributable data."""
from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from backend.analysis.strategy import StrategyPolicy, evaluate_strategy, policy_document
from backend.analysis.strategy_portfolio import VERSION as UNIVERSE_VERSION, eligible_fund_universe
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.core.trading_calendar import CalendarUnavailable, get_calendar
from backend.storage.database import connection_scope, migrate
from backend.storage.portfolio import portfolio_snapshot
from backend.storage.research import digest
from backend.storage.strategy_runs import save_recommendation
from backend.storage.timeseries import get_timeseries


SERVICE_VERSION = "portfolio-strategy-service-v4"

ACTION_LABELS = {
    "BUY": "买入",
    "ADD": "加仓",
    "HOLD": "持有",
    "REDUCE": "减仓",
    "EXIT": "清仓",
    "WAIT": "等待",
}

BLOCKER_REASONS = {
    "eligible_universe_empty": "当前没有关系和真实序列均通过核验的基金，暂不交易。",
    "strategy_source_not_execution_grade": "当前策略序列还没有取得正式交易用途资格，本次只输出研究级建议。",
    "strategy_not_investment_validated": "历史时点、总收益、产品执行规则或样本外验证尚未全部通过，当前策略不能生成真实资金操作。",
    "portfolio_condition_missing": "尚未确认风险等级、单只上限和投资期限，暂不交易。",
    "confirmed_assets_not_positive": "账户没有可用于计算金额的已确认资产，暂不交易。",
    "portfolio_valuation_incomplete": "存在无法以真实序列估值的持仓，组合权重不完整，暂不交易。",
    "portfolio_valuation_stale": "至少一项当前持仓缺少应有交易日的价格或净值，不能用过期市值计算交易金额。",
    "pending_ledger_events_present": "账户仍有待确认事件，持仓或现金可能变化，确认完成前暂不重复交易。",
    "unknown_ledger_events_present": "账户仍有待核对事件，当前持仓与现金不能视为完整，暂不交易。",
    "ledger_balance_coverage_incomplete": "历史账本缺少确认日期，无法完整重建当前现金与份额，暂不交易。",
    "settlement_history_incomplete": "卖出、入金或分红记录缺少结算日期，无法确认可用现金，暂不交易。",
    "empty_holdings_not_confirmed": "账本当前没有基金份额，但尚未确认账户确实无其他持仓，暂不建仓。",
}

BLOCKER_NEXT_STEPS = {
    "eligible_universe_empty": "先刷新并核验基金净值、跟踪指数关系和可用历史序列，再重新生成建议。",
    "strategy_source_not_execution_grade": "把候选行情升级为主证据来源，或继续把本次结果仅作为观察清单。",
    "strategy_not_investment_validated": "补齐总收益、历史时点和样本外验证；通过后再开放真实资金动作。",
    "portfolio_condition_missing": "在持仓页补充风险等级、单只上限、投资期限和是否确认空仓。",
    "confirmed_assets_not_positive": "录入已确认现金或真实持仓，确认日期和结算状态必须完整。",
    "portfolio_valuation_incomplete": "先刷新无法估值基金的净值或价格，再重新生成建议。",
    "portfolio_valuation_stale": "补齐持仓基金截至应有交易日的净值或价格。",
    "pending_ledger_events_present": "等待待确认买入、卖出、入金或分红确认后再重算。",
    "unknown_ledger_events_present": "把待核对账本记录更正为确认或删除后再重算。",
    "ledger_balance_coverage_incomplete": "补全账本确认日期，使现金和份额可被重建。",
    "settlement_history_incomplete": "补全卖出、入金或分红的结算日期。",
    "empty_holdings_not_confirmed": "如果账户确实没有其他持仓，在账户条件中确认空仓后再生成建议。",
}


def _number(value: Decimal) -> str:
    if not value.is_finite():
        raise ArithmeticError("策略结果出现非有限数值")
    rendered = format(value, "f")
    rendered = rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    return rendered if rendered and rendered != "-0" else "0"


def _metric_summary(metrics: dict[str, Any] | None) -> str:
    if not metrics:
        return "暂无可展示的趋势指标。"
    parts = []
    labels = (
        ("short_return_pct", "短期"),
        ("medium_return_pct", "中期"),
        ("long_return_pct", "长期"),
        ("peak_drawdown_pct", "近期高点回撤"),
    )
    for key, label in labels:
        value = metrics.get(key)
        if value is not None:
            try:
                rendered = Decimal(str(value)).quantize(Decimal("0.01"))
                sign = "+" if rendered > 0 and key != "peak_drawdown_pct" else ""
                parts.append(f"{label}{sign}{rendered}%")
            except (InvalidOperation, ValueError):
                continue
    if metrics.get("used_through"):
        parts.append(f"数据截至{metrics['used_through']}")
    return "；".join(parts) if parts else "暂无可展示的趋势指标。"


def _research_action(decision: dict[str, Any], raw_target: Decimal, *, blocked: bool) -> dict[str, Any]:
    action = str(decision["action"])
    label = ACTION_LABELS.get(action, action)
    reason = str(decision["reason"])
    metrics = decision.get("metrics") if isinstance(decision.get("metrics"), dict) else None
    if action == "WAIT":
        summary = f"研究信号为等待：{reason}"
    else:
        summary = f"研究信号为{label}：{reason}"
    if blocked:
        summary += " 正式交易前仍需补齐下方条件。"
    return {
        "action": action,
        "label": label,
        "target_weight_pct": _number(raw_target * 100),
        "reason_code": decision["reason_code"],
        "reason": reason,
        "summary": summary,
        "metric_summary": _metric_summary(metrics),
        "formal_execution": "blocked" if blocked else "available",
    }


def _guidance(blockers: list[str], items: list[dict[str, Any]]) -> dict[str, Any]:
    research_counts = Counter(
        str(action.get("action"))
        for item in items
        for action in (item.get("research_action"),)
        if isinstance(action, dict)
    )
    trade_signals = [
        item for item in items
        if isinstance(item.get("research_action"), dict)
        and item["research_action"].get("action") in {"BUY", "ADD", "REDUCE", "EXIT"}
    ]
    if trade_signals:
        names = "、".join(
            f"{item.get('fund_name') or item.get('fund_code')}（{item['research_action']['label']}）"
            for item in trade_signals[:5]
        )
        research_summary = f"研究层面已有可观察动作：{names}。"
    elif research_counts:
        research_summary = "研究层面当前没有触发买入、加仓、减仓或清仓；结论是继续等待并观察触发条件。"
    else:
        research_summary = "当前没有足够研究对象形成基金层面的动作。"
    if blockers:
        next_steps = [BLOCKER_NEXT_STEPS[value] for value in blockers if value in BLOCKER_NEXT_STEPS]
        summary = "本次已生成研究建议，但正式交易动作暂缓。"
    else:
        next_steps = ["若采纳买入、加仓、减仓或清仓，请先保存个人决定；真实成交后再回填账本。"]
        summary = "本次建议已可进入个人决策记录。"
    return {
        "summary": summary,
        "research_summary": research_summary,
        "formal_action_available": not blockers,
        "next_steps": list(dict.fromkeys(next_steps)),
        "research_action_summary": {
            action: research_counts[action]
            for action in ("BUY", "ADD", "HOLD", "REDUCE", "EXIT", "WAIT")
        },
    }


def _cutoff(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise AppError("strategy_input_invalid", "cutoff必须是有效的YYYY-MM-DD日期", 422) from exc
    if parsed.isoformat() != value:
        raise AppError("strategy_input_invalid", "cutoff必须是有效的YYYY-MM-DD日期", 422)
    return parsed


def _series_rows(series: dict[str, Any], through: str) -> list[dict[str, str]]:
    if series.get("kind") == "price":
        rows = [
            {"date": str(row["date"]), "close": str(row["close"])}
            for row in series.get("rows", [])
            if row.get("close") is not None and str(row["date"]) <= through
        ]
    elif series.get("kind") == "nav":
        rows = [
            {"date": str(row["date"]), "close": str(row["unit_nav"])}
            for row in series.get("rows", [])
            if row.get("unit_nav") is not None and str(row["date"]) <= through
        ]
    else:
        rows = []
    return rows


def _latest_reviewed_session(cutoff: date) -> tuple[str, Any]:
    calendar = get_calendar()
    try:
        calendar.require(cutoff)
        sessions = calendar.sessions(date(cutoff.year, 1, 1), cutoff)
        if not sessions and cutoff.year - 1 in calendar.years:
            sessions = calendar.sessions(date(cutoff.year - 1, 1, 1), cutoff)
    except CalendarUnavailable as exc:
        raise AppError("strategy_calendar_unavailable", str(exc), 422) from exc
    if not sessions:
        raise AppError("strategy_calendar_unavailable", "截止日前没有已核验交易日", 422)
    return sessions[-1].isoformat(), calendar


def _condition_and_last_trades(settings: Settings, account_id: str, cutoff: str) -> tuple[dict | None, dict[str, str]]:
    migrate(settings)
    with connection_scope(settings) as connection:
        condition = connection.execute(
            """SELECT * FROM portfolio_condition_revision
               WHERE account_id=? AND effective_date<=?
               ORDER BY effective_date DESC,version DESC LIMIT 1""",
            (account_id, cutoff),
        ).fetchone()
        rows = connection.execute(
            """SELECT e.fund_code,e.business_date,e.confirmation_date
               FROM ledger_event e
               LEFT JOIN ledger_event_correction c ON c.target_event_id=e.event_id
               WHERE e.account_id=? AND c.correction_id IS NULL
                 AND e.event_type IN ('buy','sell') AND e.state='confirmed'
                 AND e.business_date<=? AND e.confirmation_date IS NOT NULL
                 AND e.confirmation_date<=?
               ORDER BY e.confirmation_date,e.business_date,e.recorded_at,e.event_id""",
            (account_id, cutoff, cutoff),
        ).fetchall()
    current_condition = dict(condition) if condition else None
    if current_condition:
        current_condition.pop("idempotency_key", None)
        current_condition.pop("request_hash", None)
        current_condition["no_holdings_confirmed"] = bool(current_condition["no_holdings_confirmed"])
    last_trades = {str(row["fund_code"]): str(row["confirmation_date"] or row["business_date"]) for row in rows}
    return current_condition, last_trades


def _observed_trade_session(rows: list[dict[str, str]], value: str | None) -> str | None:
    if value is None:
        return None
    eligible = [row["date"] for row in rows if row["date"] <= value]
    return eligible[-1] if eligible else None


def _position_series(settings: Settings, code: str, through: str) -> tuple[dict | None, list[dict[str, str]]]:
    try:
        series = get_timeseries(settings, code)
    except AppError:
        return None, []
    rows = _series_rows(series, through)
    return series, rows


def _build_recommendation(settings: Settings, *, account_id: str, cutoff: str) -> tuple[dict, dict, str, str]:
    """Build deterministic recommendation inputs/output without saving them.

    The requested cutoff is translated to the latest reviewed exchange session.
    Missing conditions, incomplete valuations, stale series, and unsupported
    holdings fail closed to WAIT; no fixture or name-based fallback is used.
    """

    cutoff_date = _cutoff(cutoff)
    expected_as_of, calendar = _latest_reviewed_session(cutoff_date)
    snapshot = portfolio_snapshot(settings, account_id, expected_as_of)
    condition, last_trades = _condition_and_last_trades(settings, account_id, expected_as_of)
    universe = eligible_fund_universe(settings)
    policy = StrategyPolicy()
    selected = {str(item["code"]): item for item in universe["items"]}
    positions = {str(item["fund_code"]): item for item in snapshot["positions"]}

    series_by_code: dict[str, dict[str, Any]] = {}
    rows_by_code: dict[str, list[dict[str, str]]] = {}
    for code, item in selected.items():
        rows = [row for row in item["rows"] if row["date"] <= expected_as_of]
        series_by_code[code], rows_by_code[code] = item, rows
    for code in positions:
        if code not in rows_by_code:
            series, rows = _position_series(settings, code, expected_as_of)
            if series is not None:
                series_by_code[code] = series
            rows_by_code[code] = rows

    valued_positions: dict[str, Decimal] = {}
    unvalued_codes: list[str] = []
    stale_valued_codes: list[str] = []
    for code, position in positions.items():
        rows = rows_by_code.get(code, [])
        if not rows:
            unvalued_codes.append(code)
            continue
        units = Decimal(str(position["confirmed_units"]))
        valued_positions[code] = units * Decimal(rows[-1]["close"])
        if rows[-1]["date"] != expected_as_of:
            stale_valued_codes.append(code)
    cash = Decimal(str(snapshot["cash"]["available"]))
    known_market_value = sum(valued_positions.values(), Decimal(0))
    total_assets = cash + known_market_value
    valuation_complete = not unvalued_codes

    blockers: list[str] = []
    warnings: list[str] = []
    if not selected:
        blockers.append("eligible_universe_empty")
    execution_readiness = universe.get("execution_readiness", {})
    investment_readiness = universe.get("investment_readiness", {})
    if execution_readiness.get("status") != "ready":
        blockers.append("strategy_source_not_execution_grade")
    if investment_readiness.get("status") != "ready":
        blockers.append("strategy_not_investment_validated")
    if condition is None:
        blockers.append("portfolio_condition_missing")
    if total_assets <= 0:
        blockers.append("confirmed_assets_not_positive")
    if not valuation_complete:
        blockers.append("portfolio_valuation_incomplete")
    if stale_valued_codes:
        blockers.append("portfolio_valuation_stale")
    if snapshot["pending_events"]:
        blockers.append("pending_ledger_events_present")
        warnings.append("pending_ledger_events_excluded")
    if snapshot["unknown_events"]:
        blockers.append("unknown_ledger_events_present")
        warnings.append("unknown_ledger_events_excluded")
    if snapshot["coverage"]["cash_and_units"] != "confirmed":
        blockers.append("ledger_balance_coverage_incomplete")
    if snapshot["coverage"]["settlement_history"] != "complete":
        blockers.append("settlement_history_incomplete")
    if (condition is not None and not positions
            and not bool(condition["no_holdings_confirmed"])):
        blockers.append("empty_holdings_not_confirmed")

    count = len(selected)
    max_single = (Decimal(str(condition["max_single_position_pct"])) / 100
                  if condition is not None else Decimal(0))
    risk_level = str(condition["risk_level"]) if condition else None
    risk_exposure_cap = {
        "conservative": Decimal("0.35"),
        "balanced": Decimal("0.60"),
        "aggressive": Decimal("0.80"),
    }.get(risk_level, Decimal(0))
    horizon_days = int(condition["investment_horizon_days"]) if condition else 0
    # The account field is calendar days while the signal window is exchange
    # sessions.  Convert explicitly using the same 244-session convention as
    # validation instead of equating 120 calendar days with 120 sessions.
    minimum_horizon_days = (policy.long_window * 365 + 244 - 1) // 244
    horizon_allows_increase = horizon_days >= minimum_horizon_days
    items: list[dict[str, Any]] = []
    evaluated_dates: list[str] = []
    evaluations: dict[str, dict[str, Any]] = {}
    for code in sorted(selected):
        source = selected[code]
        rows = rows_by_code[code]
        position = positions.get(code)
        units = Decimal(str(position["confirmed_units"])) if position else Decimal(0)
        price = Decimal(rows[-1]["close"]) if rows else None
        current_value = valued_positions.get(code, Decimal(0))
        current_weight = current_value / total_assets if total_assets > 0 else Decimal(0)
        sleeve_weight = Decimal(1) / count if count else Decimal(0)
        local_weight = min(current_weight / sleeve_weight, Decimal(1)) if sleeve_weight else Decimal(0)
        visible = rows[-(policy.long_window + 1):]
        last_session = _observed_trade_session(visible, last_trades.get(code))
        decision = evaluate_strategy(
            visible,
            as_of=expected_as_of,
            current_weight=local_weight,
            calendar=calendar,
            last_trade_date=last_session,
            policy=policy,
        )
        if rows:
            evaluated_dates.append(rows[-1]["date"])
        raw_target = Decimal(str(decision["target_weight"]))
        # WAIT is a no-change action.  In particular, an overweight position's
        # local weight may be capped at 1 solely for signal evaluation; it must
        # never leak back as a lower displayed portfolio target.
        target_weight = (current_weight if decision["action"] == "WAIT" else
                         min(raw_target * sleeve_weight, max_single)
                         if condition is not None else current_weight)
        evaluations[code] = {
            "source": source,
            "rows": rows,
            "units": units,
            "price": price,
            "current_value": current_value,
            "current_weight": current_weight,
            "sleeve_weight": sleeve_weight,
            "decision": decision,
            "raw_target": raw_target,
            "base_target": target_weight,
        }

    unsupported_weight = (
        sum((value for code, value in valued_positions.items() if code not in selected), Decimal(0)) / total_assets
        if total_assets > 0 else Decimal(0)
    )
    selected_exposure_cap = max(risk_exposure_cap - unsupported_weight, Decimal(0))
    locked_wait_weight = sum(
        (entry["current_weight"] for entry in evaluations.values()
         if entry["decision"]["action"] == "WAIT"),
        Decimal(0),
    )
    scalable_target = sum(
        (entry["base_target"] for entry in evaluations.values()
         if entry["decision"]["action"] != "WAIT"),
        Decimal(0),
    )
    scalable_cap = max(selected_exposure_cap - locked_wait_weight, Decimal(0))
    target_scale = (min(Decimal(1), scalable_cap / scalable_target)
                    if scalable_target > 0 else Decimal(1))

    for code in sorted(selected):
        entry = evaluations[code]
        source = entry["source"]
        rows = entry["rows"]
        units = entry["units"]
        price = entry["price"]
        current_value = entry["current_value"]
        current_weight = entry["current_weight"]
        sleeve_weight = entry["sleeve_weight"]
        decision = entry["decision"]
        raw_target = entry["raw_target"]
        target_weight = (current_weight if decision["action"] == "WAIT" else
                         entry["base_target"] * target_scale)
        band = policy.rebalance_band * sleeve_weight
        action = str(decision["action"])
        reason_code = str(decision["reason_code"])
        reason = str(decision["reason"])
        research_action = _research_action(decision, raw_target, blocked=bool(blockers))
        proposed_amount: str | None = None
        proposed_units: str | None = None
        risk_alert: dict[str, Any] | None = None

        if blockers:
            if units > 0 and action in {"REDUCE", "EXIT"}:
                risk_alert = {
                    "status": "manual_price_and_evidence_review_required",
                    "label": "风险降低信号待人工核价/复核",
                    "original_action": action,
                    "original_target_weight_pct": _number(raw_target * 100),
                    "original_reason_code": reason_code,
                    "original_reason": reason,
                    "blockers": list(blockers),
                    "direct_execution_allowed": False,
                    "instruction": (
                        "该信号来自当前策略计算，但正式建议的数据或组合条件未通过。"
                        "请先人工核对最新价格、持仓和主证据；核验完成并重新生成建议前不可直接成交。"
                    ),
                }
            action = "WAIT"
            target_weight = current_weight
            reason_code = blockers[0].upper()
            reason = BLOCKER_REASONS[blockers[0]]
        elif action != "WAIT":
            difference = target_weight - current_weight
            if current_weight == 0 and target_weight == 0:
                action, reason_code = "WAIT", "RISK_BUDGET_EXHAUSTED"
            elif current_weight == 0 and target_weight > 0:
                action, reason_code = "BUY", "PORTFOLIO_TARGET_BUY"
            elif current_weight > 0 and target_weight == 0:
                action, reason_code = "EXIT", "PORTFOLIO_TARGET_EXIT"
            elif difference > band:
                action, reason_code = "ADD", "PORTFOLIO_TARGET_ADD"
            elif difference < -band:
                action, reason_code = "REDUCE", "PORTFOLIO_TARGET_REDUCE"
            else:
                action, reason_code = "HOLD", "PORTFOLIO_WITHIN_BAND"
            reason = {
                "BUY": "策略目标仓位高于零，且账户当前没有该基金持仓。",
                "ADD": "策略目标市值高于当前市值，差额超过组合再平衡容差。",
                "REDUCE": "策略目标市值低于当前市值，差额超过组合再平衡容差。",
                "EXIT": "策略目标仓位为零，建议卖出全部已确认份额。",
                "HOLD": "当前市值处于策略目标的组合再平衡容差内。",
                "WAIT": "现有持仓已经占满风险仓位预算，本基金不新增仓位。",
            }[action]
            if action in {"BUY", "ADD"} and not horizon_allows_increase:
                action = "WAIT"
                target_weight = current_weight
                proposed_amount = None
                proposed_units = None
                reason_code = "HORIZON_TOO_SHORT_FOR_INCREASE"
                reason = (f"投资期限短于约{minimum_horizon_days}个自然日（对应策略的"
                          f"{policy.long_window}个交易观察日），新买入或加仓已被阻止；减仓与退出仍可执行。")
            if action in {"BUY", "ADD", "REDUCE", "EXIT"} and price is not None:
                delta = target_weight * total_assets - current_value
                proposed_amount = _number(current_value if action == "EXIT" else abs(delta))
                proposed_units = _number(units if action == "EXIT" else abs(delta) / price)

        items.append({
            "fund_code": code,
            "fund_name": source["name"],
            "index_code": source["index_code"],
            "index_name": source["index_name"],
            "supported": True,
            "action": action,
            "target_weight_pct": _number(target_weight * 100),
            "current_weight_pct": _number(current_weight * 100),
            "current_units": _number(units),
            "current_value": _number(current_value),
            "valuation_price": _number(price) if price is not None else None,
            "valuation_as_of": rows[-1]["date"] if rows else None,
            "proposed_amount": proposed_amount,
            "proposed_units": proposed_units,
            "deferred_target_weight_pct": None,
            "reason_code": reason_code,
            "reason": reason,
            "research_action": research_action,
            "strategy_signal_action": decision["action"],
            "strategy_signal_target_weight_pct": _number(raw_target * 100),
            "strategy_reason_code": decision["reason_code"],
            "strategy_reason": decision["reason"],
            "risk_alert": risk_alert,
            "risk_adjusted": target_scale < 1 and decision["action"] != "WAIT",
            "metrics": decision.get("metrics"),
            "data_quality": decision["data_quality"],
            "source": {
                "series_kind": source["series_kind"],
                "series_source_id": source["series_source_id"],
                "series_updated_at": source["series_updated_at"],
                "relation_source_id": source["relation_source_id"],
                "relation_verified_at": source["relation_verified_at"],
                "relation_evidence_url": source["relation_evidence_url"],
                "data_eligibility": source.get("data_eligibility"),
                "catalog_eligibility": source.get("catalog_eligibility"),
                "relation_eligibility": source.get("relation_eligibility"),
            },
        })

    required_buy_cash = sum(
        (Decimal(str(item["proposed_amount"])) for item in items
         if item["action"] in {"BUY", "ADD"} and item["proposed_amount"] is not None),
        Decimal(0),
    )
    if required_buy_cash > cash:
        # Never fund an increase with sale proceeds that have not settled.  A
        # rebalance that needs those proceeds is deliberately split into two
        # recommendations: reduce/exit now, then rebuild after settlement.
        for item in items:
            if item["action"] not in {"BUY", "ADD"}:
                continue
            item["deferred_target_weight_pct"] = item["target_weight_pct"]
            item["action"] = "WAIT"
            item["target_weight_pct"] = item["current_weight_pct"]
            item["proposed_amount"] = None
            item["proposed_units"] = None
            item["reason_code"] = "REBALANCE_SELLS_FIRST"
            item["reason"] = (
                "当前已结算可用现金不足以同时完成全部增仓；请先执行本次减仓或清仓，"
                "待卖出款结算后重新生成建议。"
            )
        warnings.append("sell_settlement_required_before_buys")

    for code in sorted(set(positions) - set(selected)):
        position = positions[code]
        value = valued_positions.get(code)
        current_weight = value / total_assets if value is not None and total_assets > 0 else None
        rows = rows_by_code.get(code, [])
        items.append({
            "fund_code": code,
            "fund_name": position.get("fund_name"),
            "index_code": None,
            "index_name": None,
            "supported": False,
            "action": "WAIT",
            "target_weight_pct": _number(current_weight * 100) if current_weight is not None else None,
            "current_weight_pct": _number(current_weight * 100) if current_weight is not None else None,
            "current_units": str(position["confirmed_units"]),
            "current_value": _number(value) if value is not None else None,
            "valuation_price": rows[-1]["close"] if rows else None,
            "valuation_as_of": rows[-1]["date"] if rows else None,
            "proposed_amount": None,
            "proposed_units": None,
            "deferred_target_weight_pct": None,
            "reason_code": "UNSUPPORTED_HOLDING",
            "reason": "该真实持仓不在当前关系与历史序列均通过核验的策略池中，不按名称猜测操作。",
            "research_action": None,
            "strategy_signal_action": None,
            "strategy_signal_target_weight_pct": None,
            "strategy_reason_code": None,
            "strategy_reason": None,
            "metrics": None,
            "data_quality": {"status": "blocked", "errors": ["unsupported_holding"]},
            "source": None,
        })
        warnings.append(f"unsupported_holding:{code}")

    if not items:
        # Empty data must still produce an explicit account-level decision so
        # the interface never collapses into an ambiguous blank state.
        items.append({
            "fund_code": None,
            "fund_name": None,
            "index_code": None,
            "index_name": None,
            "supported": False,
            "action": "WAIT",
            "target_weight_pct": None,
            "current_weight_pct": None,
            "current_units": None,
            "current_value": None,
            "valuation_price": None,
            "valuation_as_of": None,
            "proposed_amount": None,
            "proposed_units": None,
            "deferred_target_weight_pct": None,
            "reason_code": "ELIGIBLE_UNIVERSE_EMPTY",
            "reason": "当前没有关系与真实历史序列均通过核验的基金，账户保持现金并等待数据补齐。",
            "research_action": None,
            "strategy_signal_action": None,
            "strategy_signal_target_weight_pct": None,
            "strategy_reason_code": None,
            "strategy_reason": None,
            "risk_adjusted": False,
            "metrics": None,
            "data_quality": {"status": "blocked", "errors": ["eligible_universe_empty"]},
            "source": None,
        })

    if not blockers and any(item["data_quality"]["status"] != "ready" for item in items if item["supported"]):
        warnings.append("one_or_more_series_not_ready")
    status = "blocked" if blockers else "partial" if warnings else "actionable"
    action_counts = Counter(str(item["action"]) for item in items)
    guidance = _guidance(blockers, items)
    actual_as_of = max(evaluated_dates) if evaluated_dates else expected_as_of
    try:
        next_session = calendar.next_session(date.fromisoformat(expected_as_of)).isoformat()
    except CalendarUnavailable:
        next_session = None

    clean_snapshot = {key: value for key, value in snapshot.items() if key != "generated_at"}
    universe_items = [
        {key: value for key, value in item.items() if key != "rows"}
        for item in universe["items"]
    ]
    series_identity = {
        code: {
            "kind": series_by_code.get(code, {}).get("series_kind", series_by_code.get(code, {}).get("kind")),
            "source_id": series_by_code.get(code, {}).get("series_source_id", series_by_code.get(code, {}).get("source_id")),
            "updated_at": series_by_code.get(code, {}).get("series_updated_at", series_by_code.get(code, {}).get("updated_at")),
            "through": rows[-1]["date"] if rows else None,
            "observations_through_cutoff": len(rows),
            "hash": digest(rows),
        }
        for code, rows in sorted(rows_by_code.items())
    }
    inputs = {
        "service_version": SERVICE_VERSION,
        "strategy_policy": policy_document(policy),
        "calendar_version": calendar.version,
        "calendar_hash": calendar.hash,
        "requested_cutoff": cutoff,
        "expected_market_session": expected_as_of,
        "portfolio_snapshot": clean_snapshot,
        "portfolio_condition": condition,
        "last_confirmed_trade_dates": last_trades,
        "universe_version": UNIVERSE_VERSION,
        "universe_selection": universe["selection"],
        "universe_items": universe_items,
        "universe_excluded": universe["excluded"],
        "execution_readiness": execution_readiness,
        "investment_readiness": investment_readiness,
        "series_identity": series_identity,
    }
    output = {
        "version": SERVICE_VERSION,
        "strategy_version": policy.version,
        "status": status,
        "requested_cutoff": cutoff,
        "portfolio_cutoff": expected_as_of,
        "expected_market_session": expected_as_of,
        "latest_available_data_date": actual_as_of,
        "execution_policy": {
            "signal_basis": "completed_daily_close_or_published_unit_nav",
            "earliest_execution_session": next_session,
            "latest_execution_session": next_session,
            "sizing": "BUY、ADD、REDUCE按当前已确认资产达到目标权重的差额；EXIT卖出全部已确认份额。",
            "fee_basis": "建议金额未预扣未知的真实申购、赎回或交易费用；实际费用以确认单为准。",
            "ordering": "同一批建议如包含资金不足的增仓，先执行REDUCE或EXIT；卖出款结算并回填账本后重新计算，再决定BUY或ADD。",
            "automatic_ordering": False,
        },
        "validity": {
            "state": "blocked" if status == "blocked" else "valid_until_inputs_change",
            "must_recalculate_after": ["新的价格或净值入库", "持仓或现金变化", "用户约束修订"],
            "instruction": "建议只供下一可执行交易日参考；输入变化后须重新生成，不沿用旧金额。",
        },
        "account": snapshot["account"],
        "condition": condition,
        "risk_controls": {
            "risk_level": risk_level,
            "total_market_exposure_cap_pct": _number(risk_exposure_cap * 100),
            "max_single_position_pct": _number(max_single * 100),
            "unsupported_current_exposure_pct": _number(unsupported_weight * 100),
            "target_scale": _number(target_scale),
            "investment_horizon_days": horizon_days,
            "minimum_horizon_for_buy_or_add_calendar_days": minimum_horizon_days,
            "minimum_horizon_observation_sessions": policy.long_window,
            "horizon_conversion_sessions_per_year": 244,
            "buy_or_add_allowed_by_horizon": horizon_allows_increase,
        },
        "data_eligibility": execution_readiness,
        "investment_eligibility": investment_readiness,
        "valuation": {
            "currency": snapshot["cash"]["currency"],
            "confirmed_cash": str(snapshot["cash"]["confirmed"]),
            "available_cash": _number(cash),
            "pending_settlement_cash": str(snapshot["cash"]["pending_settlement"]),
            "known_market_value": _number(known_market_value),
            "known_total_assets": _number(total_assets),
            "complete": valuation_complete,
            "unvalued_fund_codes": unvalued_codes,
            "stale_fund_codes": stale_valued_codes,
        },
        "items": items,
        "action_summary": {action: action_counts[action] for action in ("BUY", "ADD", "HOLD", "REDUCE", "EXIT", "WAIT")},
        "research_action_summary": guidance["research_action_summary"],
        "guidance": guidance,
        "blockers": blockers,
        "warnings": list(dict.fromkeys(warnings)),
        "excluded_universe": universe["excluded"],
        "limitations": [
            "建议只覆盖跟踪关系和本地真实价格或净值序列均通过核验的基金；其他持仓明确返回WAIT。",
            "建议金额按已结算可用现金、已确认份额和本地最近真实价格或净值计算；待确认、待核对及未结算款项不用于新增交易。",
            "场外基金净值用于估值；实际申购、赎回确认金额和费用必须由成交确认单回填账本。",
            "这是可复算的规则建议，不是收益保证，也不会自动向交易渠道下单。",
            "只有来源注册表明确允许作为主证据的序列才能生成操作；候选事实和发现来源只用于诊断。",
            "真实资金操作还必须通过历史时点、总收益、产品执行规则与滚动样本外验证门槛。",
        ],
    }
    return inputs, output, expected_as_of, actual_as_of


def build_recommendation(settings: Settings, *, account_id: str, cutoff: str) -> tuple[dict, dict, str, str]:
    """Build a recommendation with precision independent of the caller."""
    with localcontext() as context:
        context.prec = 40
        return _build_recommendation(settings, account_id=account_id, cutoff=cutoff)


def create_recommendation(settings: Settings, *, account_id: str, cutoff: str) -> dict:
    inputs, output, input_cutoff, data_as_of = build_recommendation(
        settings, account_id=account_id, cutoff=cutoff,
    )
    return save_recommendation(
        settings,
        account_id=account_id,
        strategy_version=str(output["strategy_version"]),
        input_cutoff=input_cutoff,
        data_as_of=data_as_of,
        status=str(output["status"]),
        inputs=inputs,
        output=output,
    )
