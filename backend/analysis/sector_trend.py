"""Experimental close-price trend descriptors; not connected to the live API."""
from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

METHOD_VERSION = "sector-trend-experiment-v2"
WEIGHTS = {"relative": 20, "fit": 25, "efficiency": 20, "recent": 25, "persistence": 10}


def _clip(value: float, low: float = -1, high: float = 1) -> float:
    return max(low, min(high, value))


def _regression(log_prices: Sequence[float]) -> tuple[float, float, float]:
    n = len(log_prices)
    x_mean, y_mean = (n - 1) / 2, statistics.fmean(log_prices)
    xx = sum((i - x_mean) ** 2 for i in range(n))
    yy = sum((value - y_mean) ** 2 for value in log_prices)
    xy = sum((i - x_mean) * (value - y_mean) for i, value in enumerate(log_prices))
    slope = xy / xx
    r2 = _clip(xy * xy / (xx * yy), 0, 1) if yy > 1e-20 else 0
    fitted_return = math.expm1(slope * (n - 1))
    return slope, r2, fitted_return


def _drawdown(prices: Sequence[float]) -> float:
    peak, drawdown = prices[0], 0.0
    for price in prices:
        peak = max(peak, price)
        drawdown = min(drawdown, price / peak - 1)
    return drawdown


def trend_metrics(prices: Sequence[float]) -> dict:
    """Require a complete, prevalidated observation grid at the caller boundary."""
    if len(prices) < 21:
        raise ValueError("Trend needs at least 21 closing prices")
    if any(not math.isfinite(p) or p <= 0 for p in prices):
        raise ValueError("Closing prices must be finite and positive")
    logs = [math.log(p / prices[0]) for p in prices]
    daily = [right - left for left, right in zip(logs, logs[1:])]
    window = len(prices) - 1
    recent_window = window // 4
    slope, r2, fitted_return = _regression(logs)
    recent_slope, recent_r2, recent_return = _regression(logs[-recent_window - 1:])
    prior_slope, _, _ = _regression(logs[-2 * recent_window - 1:-recent_window])
    variation = sum(abs(value) for value in daily)
    efficiency = logs[-1] / variation if variation > 1e-15 else 0
    persistence = sum(1 if value > 1e-12 else .5 if abs(value) <= 1e-12 else 0 for value in daily) / window
    volatility = statistics.pstdev(daily)
    fast_ma = statistics.fmean(prices[-recent_window:])
    slow_ma = statistics.fmean(prices[-window // 2:])
    price_bias, ma_spread = prices[-1] / fast_ma - 1, fast_ma / slow_ma - 1
    noise_scale = max(.005, volatility * math.sqrt(recent_window))
    fit = _clip(fitted_return / .02) * r2
    recent = (.5 * _clip(recent_return / .01) * recent_r2
              + .25 * _clip(price_bias / noise_scale) + .25 * _clip(ma_spread / noise_scale))
    drawdown = _drawdown(prices)
    recent_drawdown = _drawdown(prices[-recent_window - 1:])
    penalty = (10 * _clip((-drawdown - .05) / .15, 0, 1)
               + 5 * (1 - abs(efficiency)) * min(volatility / .01, 1))
    net_return = prices[-1] / prices[0] - 1
    # State gates keep a strong old window from hiding a recent reversal.
    if recent_return < -.005 and price_bias < 0 and (fitted_return > .01 or net_return > .01):
        state = "weakening"
    elif recent_return > .005 and price_bias > 0 and (fitted_return < -.01 or net_return < -.01):
        state = "recovery"
    elif fitted_return < -.01 and recent_return < -.005:
        state = "down"
    elif fitted_return > .01 and recent_return > .005 and prices[-1] >= slow_ma:
        if recent_drawdown <= -.08 or volatility >= .025:
            state = "volatile_up"
        elif r2 < .35 or recent_r2 < .2:
            state = "range"
        elif recent_slope > max(0, prior_slope) * 1.2 and recent_slope - prior_slope > .0002:
            state = "strengthening"
        else:
            state = "steady_up"
    else:
        state = "range"
    return {
        "return_pct": net_return * 100, "fitted_return_pct": fitted_return * 100,
        "daily_log_slope": slope, "r2": r2, "efficiency": efficiency,
        "up_day_ratio": persistence, "recent_fitted_return_pct": recent_return * 100,
        "recent_r2": recent_r2, "prior_daily_log_slope": prior_slope,
        "recent_daily_log_slope": recent_slope, "fast_ma_bias_pct": price_bias * 100,
        "fast_slow_ma_spread_pct": ma_spread * 100,
        "max_drawdown_pct": drawdown * 100, "recent_drawdown_pct": recent_drawdown * 100,
        "daily_volatility_pct": volatility * 100,
        "state": state, "recent_sessions": recent_window, "risk_penalty": penalty,
        "factors": {"fit": 50 * (1 + fit), "efficiency": 50 * (1 + efficiency),
                    "recent": 50 * (1 + recent), "persistence": 100 * persistence},
    }


def midpoint_percentiles(values: Sequence[float]) -> list[float | None]:
    if len(values) < 2:
        return [None] * len(values)
    ordered = sorted(values)
    positions = {}
    for i, value in enumerate(ordered):
        first, _ = positions.get(value, (i, i))
        positions[value] = (first, i)
    return [100 * sum(positions[value]) / (2 * (len(values) - 1)) for value in values]


def trend_score(metrics: dict, relative_percentile: float | None, *, relative_weight: int = 20) -> float:
    weights = {key: value * (100 - relative_weight) / 80 for key, value in WEIGHTS.items() if key != "relative"}
    factors = metrics["factors"]
    if relative_percentile is None:
        score = sum(weights[key] * factors[key] for key in weights) / (100 - relative_weight)
    else:
        score = (relative_weight * relative_percentile + sum(weights[key] * factors[key] for key in weights)) / 100
    return _clip(score - metrics["risk_penalty"], 0, 100)


def original_proposal_score(metrics: dict, relative_percentile: float, slope_percentile: float) -> float:
    """Concrete normalization of the earlier, incomplete 30/25/20/15/-10 proposal."""
    ma = 50 * (1 + _clip(metrics["fast_slow_ma_spread_pct"] / 2))
    risk = min(100, -metrics["max_drawdown_pct"] / 20 * 100)
    return (.3 * relative_percentile + .25 * slope_percentile
            + .2 * metrics["up_day_ratio"] * 100 + .15 * ma - .1 * risk)
