"""Long-only trend-directed equity budgets with past-only covariance constraints."""
from dataclasses import dataclass
import math
import statistics

import numpy as np

from backend.analysis.fund_allocation import AllocationTargets
from backend.analysis.sector_trend import trend_metrics


@dataclass(frozen=True)
class TrendBudgetPolicy:
    name: str
    driver: str = "static40"
    volatility_target: float | None = None


def budget_policies():
    return [TrendBudgetPolicy("static40"), TrendBudgetPolicy("static24", "static24"),
            TrendBudgetPolicy("static40_vol10", "static40", .10), TrendBudgetPolicy("static35_vol10", "static35", .10),
            TrendBudgetPolicy("breadth", "breadth"), TrendBudgetPolicy("breadth_vol10", "breadth", .10),
            TrendBudgetPolicy("quality", "quality"), TrendBudgetPolicy("quality_vol10", "quality", .10),
            TrendBudgetPolicy("benchmark", "benchmark"), TrendBudgetPolicy("hybrid_vol10", "hybrid", .10)]


def budget_statistics(data, index):
    if index < 120:
        raise ValueError("Risk statistics require 120 past sessions")
    entries = {e["code"]: e for e in data["frames"][index]["entries"]}
    stocks = [code for code, e in entries.items() if data["roles"][code] != "defensive" and not e["defensive_asset"]]
    if not stocks:
        raise ValueError("Trend budget requires stock peers")
    breadth = statistics.fmean(sum((entries[c]["price"] > entries[c]["ma60"],
                                   entries[c]["price"] > entries[c]["ma120"],
                                   data["marks"][c][index] > data["marks"][c][index - 20])) for c in stocks) / 3
    quality, stock_quality = [], {}
    for code in stocks:
        components = []
        for window in (60, 120):
            descriptor = trend_metrics(data["marks"][code][index - window:index + 1])
            sign = 1 if descriptor["fitted_return_pct"] > 0 else -1 if descriptor["fitted_return_pct"] < 0 else 0
            value = sign * descriptor["r2"]
            quality.append(value)
            components.append(value)
        stock_quality[code] = statistics.fmean(components)
    flags = [data["frames"][index]["context"].get(f"benchmark_above_ma{n}") for n in (60, 120, 200)]
    market = statistics.fmean(.5 if flag is None else float(flag) for flag in flags)
    codes = list(entries)
    daily = np.asarray([entries[c]["daily60"] for c in codes], dtype=float)
    if not np.isfinite(daily).all():
        raise ValueError("Invalid risk observations")
    centered = daily - daily.mean(axis=1, keepdims=True)
    covariance = centered @ centered.T / centered.shape[1] * 252
    return {"signal_index": index, "codes": codes, "stocks": stocks, "stock_quality": stock_quality, "breadth": breadth, "quality": statistics.fmean(quality),
            "benchmark": market, "covariance": covariance}


def _weights(data, stats, stock_budget, gold_budget, stock_distribution=None):
    entries = {e["code"]: e for e in data["frames"][120]["entries"]}
    bonds = [code for code in stats["codes"] if data["roles"][code] == "defensive"]
    gold = [code for code in stats["codes"] if entries[code]["defensive_asset"]]
    if not bonds or not gold:
        raise ValueError("Risk budgeting requires stocks, bonds and gold")
    weights = (dict.fromkeys(stats["stocks"], stock_budget / len(stats["stocks"])) if stock_distribution is None
               else {code: stock_budget * stock_distribution[code] for code in stats["stocks"]})
    weights.update(dict.fromkeys(gold, gold_budget / len(gold)))
    remainder = max(0., 1 - stock_budget - gold_budget)
    weights.update(dict.fromkeys(bonds, min(.9, remainder / len(bonds))))
    return weights


def projected_volatility(stats, weights):
    vector = np.asarray([weights.get(code, 0) for code in stats["codes"]])
    covariance = stats["covariance"]
    if not np.isfinite(vector).all() or not np.isfinite(covariance).all():
        raise ValueError("Invalid forecast risk matrix or weights")
    variance = float(vector @ covariance @ vector)
    if variance < -1e-10:
        raise ValueError("Forecast covariance produced negative variance")
    return math.sqrt(max(0., variance))


def risk_budget_target(data, policy, index, held, *, stats=None, stock_budget_override=None, stock_distribution=None):
    if index < 120:
        raise ValueError("Risk budget lacks past observations")
    stats = stats or budget_statistics(data, index)
    if stats["signal_index"] != index:
        raise ValueError("Risk budget used a different observation date")
    if stock_distribution is not None:
        if (set(stock_distribution) != set(stats["stocks"])
                or any(isinstance(v, bool) or not math.isfinite(v) or v < 0 for v in stock_distribution.values())
                or abs(sum(stock_distribution.values()) - 1) > 1e-12):
            raise ValueError("Stock distribution must cover all stock peers and sum to one")
    if policy.driver == "static40":
        proposed, gold = .4, .2
    elif policy.driver == "static24":
        proposed, gold = .24, .12
    elif policy.driver == "static35":
        proposed, gold = .35, .2
    elif policy.driver == "breadth":
        proposed, gold = .1 + .5 * stats["breadth"], .2
    elif policy.driver == "quality":
        proposed, gold = .35 + .25 * stats["quality"], .2
    elif policy.driver == "benchmark":
        proposed, gold = .1 + .5 * stats["benchmark"], .2
    elif policy.driver == "hybrid":
        proposed, gold = .1 + .5 * (.5 * stats["breadth"] + .5 * (stats["quality"] + 1) / 2), .2
    else:
        raise ValueError("Unknown risk budget driver")
    if stock_budget_override is not None:
        proposed = float(stock_budget_override)
        if not math.isfinite(proposed) or not 0 <= proposed <= 1 - gold:
            raise ValueError("Invalid overridden stock budget")
    weights = _weights(data, stats, proposed, gold, stock_distribution)
    original_vol = projected_volatility(stats, weights)
    scale = 1.
    if policy.volatility_target is not None:
        if not math.isfinite(policy.volatility_target) or policy.volatility_target <= 0:
            raise ValueError("Invalid forecast volatility target")
        if original_vol > policy.volatility_target:
            feasible = []
            # Search a risk constraint on known covariance, not past profit parameters.
            for candidate in np.linspace(0, 1, 101):
                candidate_weights = _weights(data, stats, proposed * candidate, gold * candidate, stock_distribution)
                if projected_volatility(stats, candidate_weights) <= policy.volatility_target:
                    feasible.append((float(candidate), candidate_weights))
            if feasible:
                scale, weights = feasible[-1]
            else:
                scale, weights = 0., {}
    bearish = proposed < .25 or scale < 1
    risk_exits = frozenset(code for code in held if not weights or (bearish and code in stats["stocks"])
                          or (scale < 1 and data["roles"][code] != "defensive"))
    metadata = {"signal_date": data["days"][index], "breadth": stats["breadth"], "quality": stats["quality"],
                "benchmark": stats["benchmark"], "proposed_stock_budget": proposed, "risky_scale": scale,
                "target_stock_budget": sum(weights.get(c, 0) for c in stats["stocks"]),
                "projected_volatility": projected_volatility(stats, weights), "unconstrained_projected_volatility": original_vol}
    if stock_distribution is not None:
        metadata["stock_distribution"] = dict(stock_distribution)
    return AllocationTargets(weights, risk_exits), metadata
