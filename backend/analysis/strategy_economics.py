"""Pure economic diagnostics for an integrated strategy backtest.

The helpers in this module deliberately depend only on already observed curve
and trade rows.  They neither fetch data nor infer missing trading sessions.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any


DECIMAL_PRECISION = 40
TRADE_ACTIONS = frozenset({"BUY", "ADD", "REDUCE", "EXIT"})


def _number(value: Decimal) -> str:
    if not value.is_finite():
        raise ArithmeticError("经济性分析出现非有限数值")
    rendered = format(value, "f")
    rendered = rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    return rendered if rendered and rendered != "-0" else "0"


def _decimal(value: object, path: str) -> Decimal:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path}必须是十进制字符串")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{path}不是有效十进制数") from error
    if not parsed.is_finite():
        raise ValueError(f"{path}必须是有限十进制数")
    return parsed


def _date(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path}必须是YYYY-MM-DD字符串")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{path}必须是有效的YYYY-MM-DD日期") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{path}必须是有效的YYYY-MM-DD日期")
    return value


def _curve_rows(
    curve: Sequence[Mapping[str, object]], *, require_exposure: bool, label: str,
) -> list[dict[str, Any]]:
    if isinstance(curve, (str, bytes)) or not isinstance(curve, Sequence) or not curve:
        raise ValueError(f"{label}不能为空且必须是有序曲线")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(curve):
        if not isinstance(raw, Mapping):
            raise ValueError(f"{label}[{index}]必须是对象")
        value_date = _date(raw.get("date"), f"{label}[{index}].date")
        equity = _decimal(raw.get("equity"), f"{label}[{index}].equity")
        if equity <= 0:
            raise ValueError(f"{label}[{index}].equity必须大于0")
        row: dict[str, Any] = {"date": value_date, "equity": equity}
        if require_exposure:
            cash = _decimal(raw.get("cash"), f"{label}[{index}].cash")
            market_value = _decimal(
                raw.get("market_value"), f"{label}[{index}].market_value",
            )
            if cash < 0 or market_value < 0:
                raise ValueError(f"{label}[{index}]的现金和市值不能为负")
            if equity != cash + market_value:
                raise ValueError(f"{label}[{index}]不满足资产恒等式")
            exposure = market_value / equity
            if not Decimal(0) <= exposure <= Decimal(1):
                raise ValueError(f"{label}[{index}]的市场敞口必须在0至1之间")
            row.update({"cash": cash, "market_value": market_value, "exposure": exposure})
        rows.append(row)
    dates = [str(row["date"]) for row in rows]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise ValueError(f"{label}日期必须严格递增且不能重复")
    return rows


def build_exposure_matched_curve(
    strategy_curve: Sequence[Mapping[str, object]],
    benchmark_curve: Sequence[Mapping[str, object]],
) -> list[dict[str, str]]:
    """Return a no-cost benchmark path carrying the strategy's daily exposure.

    The return from observation ``t-1`` to ``t`` is the equal-weight benchmark
    return multiplied by the strategy exposure observed at ``t-1``.  After the
    close at ``t``, the counterfactual account is rebalanced for free to the
    strategy exposure observed at ``t``.  Consequently, a position established
    at today's close cannot benefit from today's already realised return.
    """

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        strategy = _curve_rows(
            strategy_curve, require_exposure=True, label="strategy_curve",
        )
        benchmark = _curve_rows(
            benchmark_curve, require_exposure=False, label="benchmark_curve",
        )
        strategy_dates = [row["date"] for row in strategy]
        benchmark_dates = [row["date"] for row in benchmark]
        if strategy_dates != benchmark_dates:
            raise ValueError("策略曲线与基准曲线日期必须逐日完全对齐")

        initial_equity: Decimal = strategy[0]["equity"]
        equity = initial_equity
        peak = initial_equity
        result: list[dict[str, str]] = []
        for index, row in enumerate(strategy):
            benchmark_daily_return = Decimal(0)
            if index:
                previous_benchmark: Decimal = benchmark[index - 1]["equity"]
                current_benchmark: Decimal = benchmark[index]["equity"]
                benchmark_daily_return = current_benchmark / previous_benchmark - Decimal(1)
                previous_exposure: Decimal = strategy[index - 1]["exposure"]
                equity *= Decimal(1) + previous_exposure * benchmark_daily_return
                if equity <= 0:
                    raise ArithmeticError("敞口对照曲线产生非正资产")

            exposure: Decimal = row["exposure"]
            market_value = equity * exposure
            cash = equity - market_value
            # Use the account identity as the canonical serialized equity.  It
            # also protects against an implementation later adding an
            # unreported cost or residual.
            equity = cash + market_value
            if cash < 0 or market_value < 0 or equity <= 0 or equity != cash + market_value:
                raise ArithmeticError("敞口对照曲线不满足正资产恒等式")
            peak = max(peak, equity)
            result.append({
                "date": str(row["date"]),
                "equity": _number(equity),
                "cash": _number(cash),
                "market_value": _number(market_value),
                "strategy_exposure": _number(exposure),
                "strategy_exposure_pct": _number(exposure * Decimal(100)),
                "benchmark_daily_return_pct": _number(benchmark_daily_return * Decimal(100)),
                "return_pct": _number((equity / initial_equity - Decimal(1)) * Decimal(100)),
                "drawdown_pct": _number((equity / peak - Decimal(1)) * Decimal(100)),
            })
        return result


def build_sleeve_exposure_matched_curve(
    strategy_curve: Sequence[Mapping[str, object]],
    sleeve_curves: Mapping[str, Sequence[Mapping[str, object]]],
) -> list[dict[str, object]]:
    """Build a no-cost counterfactual from each fund's lagged actual exposure.

    From observation ``t-1`` to ``t``, every fund contributes its source return
    multiplied by that fund sleeve's *actual* market value at ``t-1`` divided by
    the aggregate strategy equity at ``t-1``.  The uninvested weight earns
    exactly zero.  This preserves the strategy's fund-selection weights while
    removing same-close lookahead and modeled transaction costs; unlike an
    aggregate-exposure/equal-weight benchmark, it does not misclassify relative
    fund performance as timing or execution.

    Sleeve points must expose ``price`` as well as the account identity fields.
    The returned contribution rows are serialized evidence for an independent
    validator, rather than inputs trusted by this calculation.
    """

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        strategy = _curve_rows(
            strategy_curve, require_exposure=True, label="strategy_curve",
        )
        if not isinstance(sleeve_curves, Mapping) or not sleeve_curves:
            raise ValueError("sleeve_curves必须是包含至少一个基金分账户的映射")

        sleeves: dict[str, list[dict[str, Any]]] = {}
        strategy_dates = [str(row["date"]) for row in strategy]
        for raw_code in sorted(sleeve_curves):
            if not isinstance(raw_code, str) or not raw_code.strip():
                raise ValueError("sleeve_curves的基金代码必须是非空字符串")
            code = raw_code.strip()
            if code != raw_code or code in sleeves:
                raise ValueError("sleeve_curves的基金代码必须规范且唯一")
            raw_curve = sleeve_curves[raw_code]
            rows = _curve_rows(
                raw_curve, require_exposure=True, label=f"sleeve_curves[{code}]",
            )
            if [str(row["date"]) for row in rows] != strategy_dates:
                raise ValueError("策略曲线与各基金分账户日期必须逐日完全对齐")
            enriched: list[dict[str, Any]] = []
            for index, (raw, row) in enumerate(zip(raw_curve, rows)):
                price = _decimal(raw.get("price"), f"sleeve_curves[{code}][{index}].price")
                if price <= 0:
                    raise ValueError(f"sleeve_curves[{code}][{index}].price必须大于0")
                enriched.append({**row, "price": price})
            sleeves[code] = enriched

        # The aggregate curve is not an independent source of fund weights.
        # Require it to reconcile exactly to the serialized sleeve cash and
        # market values before using any of those weights.
        for index, aggregate in enumerate(strategy):
            sleeve_cash = sum((rows[index]["cash"] for rows in sleeves.values()), Decimal(0))
            sleeve_market = sum(
                (rows[index]["market_value"] for rows in sleeves.values()), Decimal(0),
            )
            if aggregate["cash"] != sleeve_cash or aggregate["market_value"] != sleeve_market:
                raise ValueError(f"strategy_curve[{index}]不能由基金分账户现金和市值汇总复算")

        initial_equity: Decimal = strategy[0]["equity"]
        equity = initial_equity
        peak = initial_equity
        result: list[dict[str, object]] = []
        for index, aggregate in enumerate(strategy):
            matched_daily_return = Decimal(0)
            contributions: list[dict[str, str]] = []
            if index:
                previous_strategy_equity: Decimal = strategy[index - 1]["equity"]
                for code, rows in sleeves.items():
                    previous = rows[index - 1]
                    current = rows[index]
                    asset_daily_return = current["price"] / previous["price"] - Decimal(1)
                    prior_strategy_weight = previous["market_value"] / previous_strategy_equity
                    contribution = prior_strategy_weight * asset_daily_return
                    matched_daily_return += contribution
                    contributions.append({
                        "code": code,
                        "prior_strategy_weight": _number(prior_strategy_weight),
                        "asset_daily_return_pct": _number(asset_daily_return * Decimal(100)),
                        "contribution_pct": _number(contribution * Decimal(100)),
                    })
                equity *= Decimal(1) + matched_daily_return
                if equity <= 0:
                    raise ArithmeticError("逐基金同敞口对照曲线产生非正资产")
            else:
                contributions = [{
                    "code": code,
                    "prior_strategy_weight": "0",
                    "asset_daily_return_pct": "0",
                    "contribution_pct": "0",
                } for code in sleeves]

            exposure: Decimal = aggregate["exposure"]
            market_value = equity * exposure
            cash = equity - market_value
            equity = cash + market_value
            if cash < 0 or market_value < 0 or equity <= 0:
                raise ArithmeticError("逐基金同敞口对照曲线不满足正资产恒等式")
            peak = max(peak, equity)
            result.append({
                "date": str(aggregate["date"]),
                "equity": _number(equity),
                "cash": _number(cash),
                "market_value": _number(market_value),
                "strategy_exposure": _number(exposure),
                "strategy_exposure_pct": _number(exposure * Decimal(100)),
                "matched_daily_return_pct": _number(matched_daily_return * Decimal(100)),
                "cash_daily_return_pct": "0",
                "return_pct": _number((equity / initial_equity - Decimal(1)) * Decimal(100)),
                "drawdown_pct": _number((equity / peak - Decimal(1)) * Decimal(100)),
                "sleeve_contributions": contributions,
            })
        return result


def summarize_exposure(
    strategy_curve: Sequence[Mapping[str, object]],
) -> dict[str, int | str | None]:
    """Summarize post-close market exposure on observed curve dates."""

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        rows = _curve_rows(strategy_curve, require_exposure=True, label="strategy_curve")
        exposures = sorted(row["exposure"] for row in rows)
        count = len(exposures)
        middle = count // 2
        median = (exposures[middle] if count % 2
                  else (exposures[middle - 1] + exposures[middle]) / Decimal(2))
        average = sum(exposures, Decimal(0)) / Decimal(count)
        active_returns = [
            rows[index]["equity"] / rows[index - 1]["equity"] - Decimal(1)
            for index in range(1, len(rows))
            if rows[index - 1]["exposure"] > 0
        ]
        positive_active = sum(value > 0 for value in active_returns)
        return {
            "observation_count": count,
            "average_exposure": _number(average),
            "average_exposure_pct": _number(average * Decimal(100)),
            "median_exposure": _number(median),
            "median_exposure_pct": _number(median * Decimal(100)),
            "full_cash_observation_days": sum(value == 0 for value in exposures),
            "below_10_percent_exposure_days": sum(value < Decimal("0.10") for value in exposures),
            "invested_observation_days": sum(value > 0 for value in exposures),
            "active_exposure_return_observations": len(active_returns),
            "positive_active_exposure_return_observations": positive_active,
            "positive_active_exposure_return_pct": (
                _number(Decimal(positive_active) / Decimal(len(active_returns)) * Decimal(100))
                if active_returns else None
            ),
        }


def _observation_dates(observation_dates: Sequence[str]) -> list[str]:
    if (isinstance(observation_dates, (str, bytes))
            or not isinstance(observation_dates, Sequence) or not observation_dates):
        raise ValueError("observation_dates不能为空且必须是有序日期")
    normalized = [_date(value, f"observation_dates[{index}]")
                  for index, value in enumerate(observation_dates)]
    if normalized != sorted(normalized) or len(normalized) != len(set(normalized)):
        raise ValueError("observation_dates必须严格递增且不能重复")
    return normalized


def summarize_holding_cycles(
    trades: Sequence[Mapping[str, object]], observation_dates: Sequence[str],
) -> dict[str, object]:
    """Summarize completed BUY-to-EXIT cycles on an observed-date grid.

    Holding observations are the number of grid intervals from the BUY close
    to the EXIT close.  Thus a BUY at index 2 and EXIT at index 7 represents
    five holding observations: the position exists after indices 2 through 6
    and is gone after index 7.  Open cycles are reported but excluded from the
    completed-cycle median and short-cycle counts.
    """

    dates = _observation_dates(observation_dates)
    date_index = {value: index for index, value in enumerate(dates)}
    if isinstance(trades, (str, bytes)) or not isinstance(trades, Sequence):
        raise ValueError("trades必须是成交对象数组")

    normalized: list[dict[str, object]] = []
    previous_index = -1
    for index, raw in enumerate(trades):
        if not isinstance(raw, Mapping):
            raise ValueError(f"trades[{index}]必须是对象")
        code = raw.get("code")
        action = raw.get("action")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(f"trades[{index}].code必须是非空字符串")
        if action not in TRADE_ACTIONS:
            raise ValueError(f"trades[{index}].action必须是BUY、ADD、REDUCE或EXIT")
        execution_date = _date(raw.get("execution_date"), f"trades[{index}].execution_date")
        if execution_date not in date_index:
            raise ValueError(f"trades[{index}].execution_date不在观察日期中")
        execution_index = date_index[execution_date]
        if execution_index < previous_index:
            raise ValueError("trades必须按execution_date非递减排列")
        previous_index = execution_index
        normalized.append({
            "code": code.strip(), "action": action,
            "execution_date": execution_date, "execution_index": execution_index,
        })

    opened: dict[str, dict[str, object]] = {}
    completed: list[dict[str, object]] = []
    for trade in normalized:
        code = str(trade["code"])
        action = str(trade["action"])
        if action == "BUY":
            if code in opened:
                raise ValueError(f"{code}在持仓未清空时再次出现BUY")
            opened[code] = trade
        elif action in {"ADD", "REDUCE"}:
            if code not in opened:
                raise ValueError(f"{code}在没有已开启持仓周期时出现{action}")
        else:
            if code not in opened:
                raise ValueError(f"{code}在没有已开启持仓周期时出现EXIT")
            entry = opened.pop(code)
            holding_observations = int(trade["execution_index"]) - int(entry["execution_index"])
            completed.append({
                "code": code,
                "entry_date": str(entry["execution_date"]),
                "exit_date": str(trade["execution_date"]),
                "holding_observations": holding_observations,
            })

    durations = sorted(int(cycle["holding_observations"]) for cycle in completed)
    median: str | None = None
    if durations:
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            middle = len(durations) // 2
            median_value = (Decimal(durations[middle]) if len(durations) % 2
                            else (Decimal(durations[middle - 1]) + Decimal(durations[middle]))
                            / Decimal(2))
            median = _number(median_value)

    return {
        "trade_count": len(normalized),
        "completed_cycle_count": len(completed),
        "open_cycle_count": len(opened),
        "median_holding_observation_days": median,
        "cycles_lte_10_observation_days": sum(value <= 10 for value in durations),
        "cycles_lte_20_observation_days": sum(value <= 20 for value in durations),
        "completed_cycles": completed,
        "open_cycles": [
            {"code": code, "entry_date": str(opened[code]["execution_date"])}
            for code in sorted(opened)
        ],
    }
