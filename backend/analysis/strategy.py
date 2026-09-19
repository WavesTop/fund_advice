"""Versioned, long-only target-weight decisions from completed daily prices.

The module is deliberately pure: callers provide the reviewed trading calendar,
the price rows that are available to them and the current portfolio state.  It
does not read the database or the clock.  The policy is an initial transparent
baseline, not a claim that its thresholds predict future returns.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
from typing import Mapping, Sequence

from backend.core.trading_calendar import CalendarUnavailable, TradingCalendar, day


TRADE_ACTIONS = frozenset({"BUY", "ADD", "REDUCE", "EXIT"})
ALL_ACTIONS = frozenset({*TRADE_ACTIONS, "HOLD", "WAIT"})


class ObservedSessionCalendar:
    """A closed calendar made only from explicitly supplied real observations.

    This is a fallback for historical series outside the reviewed exchange
    calendar's year coverage.  It never infers weekdays or exchange holidays.
    Callers must build it from the common, quality-checked observation dates of
    every series used in the simulation.  Therefore it can detect a missing row
    relative to that frozen set, but cannot detect a session missing from every
    source.
    """

    version = "observed-sessions-v1"
    basis = "explicit_common_observation_dates"

    def __init__(self, dates: Sequence[str], *, source: str):
        if not isinstance(source, str) or not source.strip():
            raise ValueError("观察日日历必须说明实际日期来源")
        parsed = [day(value) for value in dates]
        if len(parsed) < 2 or parsed != sorted(set(parsed)):
            raise ValueError("观察日日历日期必须至少两天、有序且唯一")
        self._sessions = tuple(parsed)
        self._session_set = frozenset(parsed)
        self.source = source.strip()
        self.coverage_start = parsed[0]
        self.coverage_end = parsed[-1]
        self.limitations = (
            "会话仅来自调用方冻结的共同实际观察日期，不推定工作日或交易所休市日。",
            "若所有输入来源在同一天同时缺失，本日历无法识别该缺口。",
        )
        document = {
            "version": self.version,
            "basis": self.basis,
            "source": self.source,
            "dates": [value.isoformat() for value in parsed],
            "limitations": list(self.limitations),
        }
        self.hash = sha256(json.dumps(document, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.document = {
            "version": self.version,
            "basis": self.basis,
            "source": self.source,
            "coverage_start": self.coverage_start.isoformat(),
            "coverage_end": self.coverage_end.isoformat(),
            "session_count": len(parsed),
            "hash": self.hash,
            "limitations": list(self.limitations),
        }

    def require(self, value: date) -> None:
        if not self.coverage_start <= value <= self.coverage_end:
            raise CalendarUnavailable("日期超出明确传入的实际观察日范围")

    def is_session(self, value: date) -> bool:
        self.require(value)
        return value in self._session_set

    def sessions(self, start: date, end: date) -> list[date]:
        if start > end:
            raise ValueError("起始日晚于截止日")
        self.require(start)
        self.require(end)
        return list(self._sessions[bisect_left(self._sessions, start):bisect_right(self._sessions, end)])

    def validate_grid(self, dates: list[str], *, end: date, minimum: int = 1) -> dict[str, object]:
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
            raise ValueError("最小样本量无效")
        self.require(end)
        parsed = [day(value) for value in dates]
        errors: list[str] = []
        if len(parsed) != len(set(parsed)):
            errors.append("duplicate_dates")
        if parsed != sorted(parsed):
            errors.append("unordered_dates")
        if any(value not in self._session_set for value in parsed):
            errors.append("dates_outside_observed_sessions")
        if any(value > end for value in parsed):
            errors.append("unsettled_or_future_dates")
        eligible = sorted({value for value in parsed if value <= end and value in self._session_set})
        missing: list[date] = []
        if eligible:
            missing = sorted(set(self.sessions(eligible[0], end)) - set(eligible))
            if missing:
                errors.append("missing_observed_sessions")
            if eligible[-1] != end:
                errors.append("stale_last_session")
        if len(eligible) < minimum:
            errors.append("insufficient_observations")
        return {
            "status": "ready" if not errors else "blocked",
            "errors": errors,
            "missing_sessions": [value.isoformat() for value in missing],
            "expected_as_of": end.isoformat(),
            "observed_count": len(eligible),
            "calendar_version": self.version,
            "calendar_hash": self.hash,
            "calendar_basis": self.basis,
            "calendar_source": self.source,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class StrategyPolicy:
    """Business-readable parameters for the first price/risk baseline."""

    version: str = "long-only-price-risk-v2"
    short_window: int = 20
    medium_window: int = 60
    long_window: int = 120
    strong_short_return: Decimal = Decimal("0.04")
    strong_medium_return: Decimal = Decimal("0.08")
    strong_long_return: Decimal = Decimal("0.12")
    recovery_short_return: Decimal = Decimal("0.03")
    recovery_medium_return: Decimal = Decimal("0")
    recovery_long_ceiling: Decimal = Decimal("0")
    recovery_target: Decimal = Decimal("0.20")
    positive_target: Decimal = Decimal("0.40")
    strong_target: Decimal = Decimal("0.60")
    rebalance_band: Decimal = Decimal("0.05")
    risk_peak_window: int = 20
    risk_drawdown: Decimal = Decimal("-0.12")
    risk_short_return: Decimal = Decimal("-0.08")
    cooldown_sessions: int = 5

    def validate(self) -> None:
        windows = (self.short_window, self.medium_window, self.long_window)
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 2 for value in windows):
            raise ValueError("观察窗口必须是至少两个交易日的整数")
        if not self.short_window < self.medium_window < self.long_window:
            raise ValueError("短中长期观察窗口必须严格递增")
        if not 2 <= self.risk_peak_window <= self.long_window:
            raise ValueError("风险回撤窗口必须位于有效观察范围内")
        if not isinstance(self.cooldown_sessions, int) or isinstance(self.cooldown_sessions, bool) or self.cooldown_sessions < 0:
            raise ValueError("冷却期必须是非负交易日整数")
        targets = (self.recovery_target, self.positive_target, self.strong_target)
        if not Decimal(0) < targets[0] < targets[1] < targets[2] < Decimal(1):
            raise ValueError("目标仓位必须在0和1之间严格递增")
        if not Decimal(0) < self.rebalance_band < Decimal(1):
            raise ValueError("再平衡容差必须在0和1之间")
        if self.risk_drawdown >= 0 or self.risk_short_return >= 0:
            raise ValueError("风险退出阈值必须为负数")


def policy_document(policy: StrategyPolicy) -> dict[str, object]:
    """JSON-safe policy snapshot retained beside every simulation result."""

    return {
        field.name: (_number(value) if isinstance(value, Decimal) else value)
        for field in fields(policy)
        for value in (getattr(policy, field.name),)
    }


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label}不是有效数字")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label}不是有效数字") from exc
    if not result.is_finite():
        raise ValueError(f"{label}不是有限数字")
    return result


def _known_prices(rows: Sequence[Mapping[str, object]], as_of: str) -> list[tuple[date, Decimal]]:
    cutoff = day(as_of)
    known: list[tuple[date, Decimal]] = []
    for row in rows:
        row_day = day(row.get("date"))  # type: ignore[arg-type]
        if row_day > cutoff:
            continue
        close = _decimal(row.get("close"), "收盘价")
        if close <= 0:
            raise ValueError("收盘价必须大于0")
        known.append((row_day, close))
    return known


def _pct(value: Decimal) -> str:
    return format(value * 100, "f")


def _number(value: Decimal) -> str:
    return format(value, "f")


def _evaluate_strategy(
    rows: Sequence[Mapping[str, object]],
    *,
    as_of: str,
    current_weight: object,
    calendar: TradingCalendar | ObservedSessionCalendar,
    last_trade_date: str | None = None,
    policy: StrategyPolicy | None = None,
) -> dict[str, object]:
    """Return one reproducible decision using only rows known on ``as_of``.

    Rows after ``as_of`` are ignored by construction.  Missing, stale or
    non-session history returns WAIT instead of filling prices or inferring a
    trading calendar.
    """

    policy = policy or StrategyPolicy()
    policy.validate()
    weight = _decimal(current_weight, "当前仓位")
    if not Decimal(0) <= weight <= Decimal(1):
        raise ValueError("当前仓位必须在0和1之间")
    cutoff = day(as_of)
    calendar.require(cutoff)
    known = _known_prices(rows, as_of)
    base: dict[str, object] = {
        "version": policy.version,
        "as_of": as_of,
        "action": "WAIT",
        "target_weight": _number(weight),
        "current_weight": _number(weight),
        "risk_exit": False,
    }
    dates = [value.isoformat() for value, _ in known]
    quality = calendar.validate_grid(dates, end=cutoff, minimum=policy.long_window + 1)
    if quality["status"] != "ready":
        return {
            **base,
            "reason_code": "DATA_INSUFFICIENT",
            "reason": "截至决策日的完整交易日价格不足，保持现状并等待数据补齐。",
            "data_quality": quality,
        }

    prices = [value for _, value in known]
    returns = {
        "short": prices[-1] / prices[-policy.short_window - 1] - 1,
        "medium": prices[-1] / prices[-policy.medium_window - 1] - 1,
        "long": prices[-1] / prices[-policy.long_window - 1] - 1,
    }
    moving_average = sum(prices[-policy.short_window:]) / policy.short_window
    peak = max(prices[-policy.risk_peak_window - 1:])
    drawdown = prices[-1] / peak - 1
    metrics = {
        "close": _number(prices[-1]),
        "short_return_pct": _pct(returns["short"]),
        "medium_return_pct": _pct(returns["medium"]),
        "long_return_pct": _pct(returns["long"]),
        "short_moving_average": _number(moving_average),
        "peak_drawdown_pct": _pct(drawdown),
        "observations": len(known),
        "used_through": known[-1][0].isoformat(),
    }

    risk_exit = weight > 0 and (
        drawdown <= policy.risk_drawdown
        or (returns["short"] <= policy.risk_short_return and prices[-1] < moving_average)
    )
    above_average = prices[-1] >= moving_average
    if risk_exit:
        target = Decimal(0)
        tier = "risk_exit"
    elif (
        above_average
        and returns["short"] >= policy.strong_short_return
        and returns["medium"] >= policy.strong_medium_return
        and returns["long"] >= policy.strong_long_return
    ):
        target, tier = policy.strong_target, "strong_trend"
    elif above_average and all(value > 0 for value in returns.values()):
        target, tier = policy.positive_target, "positive_trend"
    elif (
        above_average
        and returns["short"] >= policy.recovery_short_return
        and returns["medium"] > policy.recovery_medium_return
        and returns["long"] <= policy.recovery_long_ceiling
    ):
        target, tier = policy.recovery_target, "recovery"
    else:
        target, tier = Decimal(0), "no_long_entry"

    if weight == 0 and target == 0:
        action, reason_code = "WAIT", "NO_LONG_ENTRY"
    elif target == 0:
        action, reason_code = "EXIT", "RISK_EXIT" if risk_exit else "TREND_EXIT"
    elif weight == 0:
        action, reason_code = "BUY", "TARGET_ABOVE_ZERO"
    elif target - weight > policy.rebalance_band:
        action, reason_code = "ADD", "TARGET_INCREASED"
    elif weight - target > policy.rebalance_band:
        action, reason_code = "REDUCE", "TARGET_DECREASED"
    else:
        action, reason_code = "HOLD", "WITHIN_REBALANCE_BAND"

    cooldown_remaining = 0
    if last_trade_date is not None:
        last = day(last_trade_date)
        if last > cutoff or not calendar.is_session(last):
            raise ValueError("最近成交日必须是不晚于决策日的交易日")
        elapsed = len(calendar.sessions(last, cutoff)) - 1
        cooldown_remaining = max(0, policy.cooldown_sessions - elapsed)
        if action in TRADE_ACTIONS and not risk_exit and cooldown_remaining > 0:
            action, reason_code = "WAIT", "COOLDOWN"

    explanations = {
        "RISK_EXIT": "价格触发预先设定的回撤或短期跌幅风险线，风险退出不受冷却期限制。",
        "TREND_EXIT": "当前价格趋势不再满足长仓条件，目标仓位降为0。",
        "TARGET_ABOVE_ZERO": "当前无持仓，价格趋势达到首版长仓分档。",
        "TARGET_INCREASED": "目标仓位高于当前仓位且超过再平衡容差。",
        "TARGET_DECREASED": "目标仓位低于当前仓位且超过再平衡容差。",
        "WITHIN_REBALANCE_BAND": "当前仓位处于目标仓位容差内，继续持有。",
        "NO_LONG_ENTRY": "价格趋势未达到首版长仓条件，继续等待。",
        "COOLDOWN": "距最近成交不足冷却交易日，暂不重复调整。",
    }
    return {
        **base,
        "action": action,
        "target_weight": _number(target),
        "risk_exit": risk_exit,
        "signal_tier": tier,
        "reason_code": reason_code,
        "reason": explanations[reason_code],
        "cooldown_sessions_remaining": cooldown_remaining,
        "metrics": metrics,
        "data_quality": quality,
    }


def evaluate_strategy(
    rows: Sequence[Mapping[str, object]],
    *,
    as_of: str,
    current_weight: object,
    calendar: TradingCalendar | ObservedSessionCalendar,
    last_trade_date: str | None = None,
    policy: StrategyPolicy | None = None,
) -> dict[str, object]:
    """Evaluate with a versioned precision independent of caller state."""
    with localcontext() as context:
        context.prec = 40
        return _evaluate_strategy(
            rows,
            as_of=as_of,
            current_weight=current_weight,
            calendar=calendar,
            last_trade_date=last_trade_date,
            policy=policy,
        )
