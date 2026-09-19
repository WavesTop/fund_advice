"""Deterministic target-weight account simulation for the v1 strategy core."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Mapping, Sequence

from backend.analysis.strategy import (
    ObservedSessionCalendar, StrategyPolicy, TRADE_ACTIONS, _decimal, evaluate_strategy, policy_document,
)
from backend.core.trading_calendar import TradingCalendar, day


@dataclass(frozen=True)
class SimulationCosts:
    """Explicit transaction costs; published NAV expenses must not be repeated."""

    buy_fee_bps: Decimal = Decimal("10")
    sell_fee_bps: Decimal = Decimal("10")

    def __post_init__(self) -> None:
        object.__setattr__(self, "buy_fee_bps", _decimal(self.buy_fee_bps, "买入费率"))
        object.__setattr__(self, "sell_fee_bps", _decimal(self.sell_fee_bps, "卖出费率"))

    def validate(self) -> None:
        for value in (self.buy_fee_bps, self.sell_fee_bps):
            if not value.is_finite() or not Decimal(0) <= value < Decimal("10000"):
                raise ValueError("交易费率必须是0至10000之间的有限基点数")


def _text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value, "f")


def _percent(value: Decimal) -> str:
    return _text(value * 100)


def _validated_rows(
    rows: Sequence[Mapping[str, object]], calendar: TradingCalendar | ObservedSessionCalendar,
) -> list[dict[str, object]]:
    if not rows:
        raise ValueError("模拟价格序列不能为空")
    normalized: list[dict[str, object]] = []
    for row in rows:
        value_day = day(row.get("date"))  # type: ignore[arg-type]
        price = _decimal(row.get("close"), "收盘价")
        if price <= 0:
            raise ValueError("收盘价必须大于0")
        normalized.append({"date": value_day.isoformat(), "close": price})
    quality = calendar.validate_grid(
        [row["date"] for row in normalized],  # type: ignore[list-item]
        end=day(normalized[-1]["date"]),  # type: ignore[arg-type]
        minimum=2,
    )
    if quality["status"] != "ready":
        raise ValueError(f"模拟价格序列未通过交易日校验：{quality['errors']}")
    return normalized


def _trade_to_target(
    *,
    cash: Decimal,
    units: Decimal,
    price: Decimal,
    target: Decimal,
    costs: SimulationCosts,
) -> tuple[Decimal, Decimal, dict[str, Decimal | str] | None]:
    market_value = units * price
    equity = cash + market_value
    if equity <= 0:
        return cash, units, None
    current_weight = market_value / equity
    if current_weight == target:
        return cash, units, None

    if target > current_weight:
        fee_rate = costs.buy_fee_bps / Decimal("10000")
        proposed_gross = (target * equity - market_value) / (Decimal(1) + target * fee_rate)
        proposed_gross = min(proposed_gross, cash / (Decimal(1) + fee_rate))
        if proposed_gross <= 0:
            return cash, units, None
        delta_units = proposed_gross / price
        # Make the public trade ledger the canonical arithmetic source.  A
        # validator must be able to reproduce gross exactly from the serialized
        # unit change and execution price, without depending on an unreported
        # pre-division amount that may differ by one Decimal tail digit.
        gross = delta_units * price
        fee = gross * fee_rate
        cash_after = cash - gross - fee
        units_after = units + delta_units
        action = "BUY" if units == 0 else "ADD"
    else:
        fee_rate = costs.sell_fee_bps / Decimal("10000")
        if target == 0:
            # Preserve the exact lot balance on a full exit.  Recomputing units
            # as (units * price) / price can acquire a negative Decimal tail.
            gross = market_value
            delta_units = -units
            units_after = Decimal(0)
        else:
            denominator = Decimal(1) - target * fee_rate
            proposed_gross = min((market_value - target * equity) / denominator, market_value)
            delta_units = -(proposed_gross / price)
            gross = (-delta_units) * price
            units_after = units + delta_units
        if gross <= 0:
            return cash, units, None
        fee = gross * fee_rate
        cash_after = cash + gross - fee
        action = "EXIT" if target == 0 else "REDUCE"

    if cash_after < 0 or units_after < 0:
        raise ArithmeticError("模拟成交导致现金或份额为负")
    post_equity = cash_after + units_after * price
    return cash_after, units_after, {
        "action": action,
        "price": price,
        "units_delta": delta_units,
        "gross_amount": gross,
        "fee": fee,
        "cash_after": cash_after,
        "units_after": units_after,
        "equity_after": post_equity,
    }


def _simulate_strategy(
    rows: Sequence[Mapping[str, object]],
    *,
    calendar: TradingCalendar | ObservedSessionCalendar,
    initial_cash: object,
    initial_units: object = "0",
    costs: SimulationCosts | None = None,
    policy: StrategyPolicy | None = None,
) -> dict[str, object]:
    """Run a next-session-close simulation without external cash flows.

    A decision made from one completed close is eligible for execution only at
    the next session's close.  Each equity point is recorded after that day's
    possible execution and must equal cash plus marked position value.
    """

    policy = policy or StrategyPolicy()
    policy.validate()
    costs = costs or SimulationCosts()
    costs.validate()
    normalized = _validated_rows(rows, calendar)
    cash = _decimal(initial_cash, "初始现金")
    units = _decimal(initial_units, "初始份额")
    if cash < 0 or units < 0 or cash + units * normalized[0]["close"] <= 0:  # type: ignore[operator]
        raise ValueError("初始现金和份额不能为负，且初始资产必须大于0")

    first_price: Decimal = normalized[0]["close"]  # type: ignore[assignment]
    initial_equity = cash + units * first_price
    peak_equity = initial_equity
    last_trade_date: str | None = None
    pending: dict[str, object] | None = None
    decisions: list[dict[str, object]] = []
    trades: list[dict[str, object]] = []
    curve: list[dict[str, str]] = []

    for index, row in enumerate(normalized):
        trade_date: str = row["date"]  # type: ignore[assignment]
        price: Decimal = row["close"]  # type: ignore[assignment]
        if pending is not None:
            cash, units, execution = _trade_to_target(
                cash=cash,
                units=units,
                price=price,
                target=Decimal(str(pending["target_weight"])),
                costs=costs,
            )
            if execution is not None:
                trades.append({
                    "signal_date": pending["as_of"],
                    "execution_date": trade_date,
                    "signal_action": pending["action"],
                    "action": execution["action"],
                    "target_weight": pending["target_weight"],
                    **{key: _text(value) if isinstance(value, Decimal) else value
                       for key, value in execution.items() if key != "action"},
                })
                last_trade_date = trade_date
            pending = None

        market_value = units * price
        equity = cash + market_value
        if cash < 0 or units < 0 or equity <= 0:
            raise ArithmeticError("模拟账户产生无效余额")
        peak_equity = max(peak_equity, equity)
        curve.append({
            "date": trade_date,
            "price": _text(price),
            "cash": _text(cash),
            "units": _text(units),
            "market_value": _text(market_value),
            "equity": _text(equity),
            "return_pct": _percent(equity / initial_equity - 1),
            "drawdown_pct": _percent(equity / peak_equity - 1),
        })

        current_weight = market_value / equity
        # The policy uses at most long_window + 1 closes.  Passing only that
        # already validated prefix tail preserves the no-future contract while
        # keeping multi-year replays linear in history length.
        visible_start = max(0, index - policy.long_window)
        visible = [
            {"date": value["date"], "close": value["close"]}
            for value in normalized[visible_start:index + 1]
        ]
        decision = evaluate_strategy(
            visible,
            as_of=trade_date,
            current_weight=current_weight,
            calendar=calendar,
            last_trade_date=last_trade_date,
            policy=policy,
        )
        decisions.append(decision)
        if decision["action"] in TRADE_ACTIONS and index + 1 < len(normalized):
            pending = decision

    final_equity = Decimal(curve[-1]["equity"])
    max_drawdown = min(Decimal(point["drawdown_pct"]) for point in curve)
    action_counts = Counter(str(decision["action"]) for decision in decisions)
    execution_counts = Counter(str(trade["action"]) for trade in trades)
    total_fees = sum((Decimal(str(trade["fee"])) for trade in trades), Decimal(0))
    return {
        "version": "target-weight-simulation-v2",
        "strategy_version": policy.version,
        "strategy_policy": policy_document(policy),
        "cost_policy": {
            "buy_fee_bps": _text(costs.buy_fee_bps),
            "sell_fee_bps": _text(costs.sell_fee_bps),
            "nav_embedded_expenses_recharged": False,
        },
        "calendar_version": calendar.version,
        "calendar_hash": calendar.hash,
        "calendar_basis": getattr(calendar, "basis", "reviewed_exchange_calendar"),
        "calendar_document": getattr(calendar, "document", None),
        "execution_policy": "decision_close_then_next_session_close",
        "decimal_precision": 40,
        "start_date": normalized[0]["date"],
        "end_date": normalized[-1]["date"],
        "initial_equity": _text(initial_equity),
        "final_equity": _text(final_equity),
        "profit_loss": _text(final_equity - initial_equity),
        "total_return_pct": _percent(final_equity / initial_equity - 1),
        "max_drawdown_pct": _text(max_drawdown),
        "total_fees": _text(total_fees),
        "action_counts": {action: action_counts.get(action, 0)
                          for action in ("BUY", "ADD", "HOLD", "REDUCE", "EXIT", "WAIT")},
        "executed_action_counts": {action: execution_counts.get(action, 0)
                                   for action in ("BUY", "ADD", "REDUCE", "EXIT")},
        "decisions": decisions,
        "trades": trades,
        "equity_curve": curve,
        "limitations": [
            "这是固定规则的历史模拟，不代表未来收益。",
            "成交按下一交易日收盘价模拟；未包含滑点、限购、停牌及真实基金确认时差。",
            "费用仅使用显式传入的买卖费率；净值已包含的管理费不重复扣除。",
        ],
    }


def simulate_strategy(
    rows: Sequence[Mapping[str, object]],
    *,
    calendar: TradingCalendar | ObservedSessionCalendar,
    initial_cash: object,
    initial_units: object = "0",
    costs: SimulationCosts | None = None,
    policy: StrategyPolicy | None = None,
) -> dict[str, object]:
    """Simulate with fixed precision so hashes never depend on caller context."""
    with localcontext() as context:
        context.prec = 40
        return _simulate_strategy(
            rows,
            calendar=calendar,
            initial_cash=initial_cash,
            initial_units=initial_units,
            costs=costs,
            policy=policy,
        )
