"""Pure, deterministic risk-quality metrics for observed equity curves.

Every return uses only the current and immediately preceding observations.
The helpers do not fetch data, fill missing dates, or infer future sessions.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, DecimalException, InvalidOperation, localcontext


DECIMAL_PRECISION = 40
OBSERVATIONS_PER_YEAR = Decimal("244")
VERSION = "strategy-risk-quality-v1"


def _number(value: Decimal) -> str:
    if not value.is_finite():
        raise ArithmeticError("风险质量计算出现非有限数值")
    rendered = format(value, "f")
    rendered = rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    return rendered if rendered and rendered != "-0" else "0"


def _equity_rows(curve: Sequence[Mapping[str, object]]) -> list[tuple[str, Decimal]]:
    if isinstance(curve, (str, bytes)) or not isinstance(curve, Sequence) or len(curve) < 2:
        raise ValueError("风险质量至少需要两个有序观察点")
    rows: list[tuple[str, Decimal]] = []
    for index, raw in enumerate(curve):
        if not isinstance(raw, Mapping):
            raise ValueError(f"curve[{index}]必须是对象")
        raw_date = raw.get("date")
        if not isinstance(raw_date, str):
            raise ValueError(f"curve[{index}].date必须是YYYY-MM-DD字符串")
        try:
            parsed_date = date.fromisoformat(raw_date)
        except ValueError as error:
            raise ValueError(f"curve[{index}].date必须是有效日期") from error
        if parsed_date.isoformat() != raw_date:
            raise ValueError(f"curve[{index}].date必须是规范的YYYY-MM-DD")
        raw_equity = raw.get("equity")
        if not isinstance(raw_equity, str) or not raw_equity:
            raise ValueError(f"curve[{index}].equity必须是十进制字符串")
        try:
            equity = Decimal(raw_equity)
        except InvalidOperation as error:
            raise ValueError(f"curve[{index}].equity不是有效十进制数") from error
        if not equity.is_finite() or equity <= 0:
            raise ValueError(f"curve[{index}].equity必须是大于0的有限十进制数")
        rows.append((raw_date, equity))
    dates = [item[0] for item in rows]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise ValueError("风险质量曲线日期必须严格递增且不重复")
    return rows


def summarize_curve_risk(
    curve: Sequence[Mapping[str, object]],
    *,
    observations_per_year: Decimal = OBSERVATIONS_PER_YEAR,
) -> dict[str, str | int | None]:
    """Summarize realized close-to-close risk without forward-looking inputs.

    Volatility uses the population standard deviation of observed returns.
    Sharpe uses a zero cash/risk-free return. Sortino uses a zero minimum
    acceptable return and the root mean square of negative returns over all
    return observations. Ratios are undefined when their denominator is zero.
    """

    if not isinstance(observations_per_year, Decimal):
        raise TypeError("observations_per_year必须是Decimal")
    if not observations_per_year.is_finite() or observations_per_year <= 0:
        raise ValueError("observations_per_year必须是大于0的有限Decimal")

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        rows = _equity_rows(curve)
        returns = [
            rows[index][1] / rows[index - 1][1] - Decimal(1)
            for index in range(1, len(rows))
        ]
        if any(not value.is_finite() or value <= Decimal(-1) for value in returns):
            raise ArithmeticError("观察收益率必须有限且大于-100%")

        count = Decimal(len(returns))
        mean = sum(returns, Decimal(0)) / count
        variance = sum(((value - mean) ** 2 for value in returns), Decimal(0)) / count
        volatility = context.sqrt(variance)
        annualized_volatility = volatility * context.sqrt(observations_per_year)

        downside_variance = sum(
            ((min(value, Decimal(0))) ** 2 for value in returns), Decimal(0),
        ) / count
        downside_deviation = context.sqrt(downside_variance)
        try:
            annualized_return = (
                context.power(
                    rows[-1][1] / rows[0][1], observations_per_year / count,
                ) - Decimal(1)
            )
        except DecimalException as error:
            raise ArithmeticError("风险质量年化计算超出有限Decimal范围") from error

        peak = rows[0][1]
        max_drawdown = Decimal(0)
        current_duration = 0
        max_duration = 0
        for _, equity in rows:
            if equity >= peak:
                peak = equity
                current_duration = 0
            else:
                current_duration += 1
                max_duration = max(max_duration, current_duration)
                max_drawdown = min(max_drawdown, equity / peak - Decimal(1))

        sharpe = (mean / volatility * context.sqrt(observations_per_year)
                  if volatility > 0 else None)
        sortino = (mean / downside_deviation * context.sqrt(observations_per_year)
                   if downside_deviation > 0 else None)
        calmar = (annualized_return / abs(max_drawdown) if max_drawdown < 0 else None)
        positive_count = sum(value > 0 for value in returns)
        positive_ratio = Decimal(positive_count) / count
        worst = min(returns)

        return {
            "version": VERSION,
            "return_observations": len(returns),
            "observations_per_year": _number(observations_per_year),
            "risk_free_return_assumption_pct": "0",
            "minimum_acceptable_return_assumption_pct": "0",
            "annualized_return_pct": _number(annualized_return * Decimal(100)),
            "annualized_volatility_pct": _number(annualized_volatility * Decimal(100)),
            "sharpe_ratio": _number(sharpe) if sharpe is not None else None,
            "sortino_ratio": _number(sortino) if sortino is not None else None,
            "calmar_ratio": _number(calmar) if calmar is not None else None,
            "max_drawdown_pct": _number(max_drawdown * Decimal(100)),
            "max_drawdown_duration_observations": max_duration,
            "positive_return_observation_pct": _number(positive_ratio * Decimal(100)),
            "worst_observation_return_pct": _number(worst * Decimal(100)),
        }
