"""Experimental ordinal return/risk targets, not investment probabilities."""
from decimal import Decimal, localcontext
import math

import numpy as np

from backend.analysis.sector_trend import midpoint_percentiles


def path_outcomes(prices):
    with localcontext() as context:
        context.prec = 40
        values = [Decimal(str(value)) for value in prices]
        if len(values) < 2 or any(not value.is_finite() or value <= 0 for value in values):
            raise ValueError("Ordinal sector outcomes require a finite positive path")
        peak, drawdown = values[0], Decimal(0)
        for value in values:
            peak = max(peak, value)
            drawdown = min(drawdown, value / peak - 1)
        return values[-1] / values[0] - 1, drawdown


def rank_targets(returns, drawdowns):
    if (len(returns) < 2 or len(returns) != len(drawdowns)
            or any(not math.isfinite(value) for value in [*returns, *drawdowns])
            or any(value > 0 or value < -1 for value in drawdowns)):
        raise ValueError("Invalid ordinal return/risk outcomes")
    return np.column_stack(([value / 100 - .5 for value in midpoint_percentiles(returns)],
                            [value / 100 - .5 for value in midpoint_percentiles(drawdowns)]))


def centered_predictions(model, matrix):
    values = model.predict(matrix)
    return values - values.mean(axis=0)


def choices(entries, codes, predicted, historical):
    values = np.asarray(predicted)
    if (len(codes) < 2 or values.shape != (len(codes), 2) or not np.isfinite(values).all()
            or len(set(codes)) != len(codes) or {entry["code"] for entry in entries} != set(codes)):
        raise ValueError("Invalid ordinal sector predictions")
    indexes = {code: i for i, code in enumerate(codes)}
    peers = np.column_stack([midpoint_percentiles(values[:, i].tolist()) for i in (0, 1)])
    balanced = np.min(peers, axis=1)

    def top(scores, allowed=None):
        return sorted((code for code in codes if allowed is None or code in allowed),
                      key=lambda code: (-scores[indexes[code]], code))[:10]

    eligible = {entry["code"] for entry in entries if entry["return_pct"] > 0
                and entry["recent_fitted_return_pct"] > 0
                and all(values[indexes[entry["code"]], i] > 0 for i in (0, 1))}
    selected = {"historical_rank_top10": top(np.asarray(historical)[:, 0]),
                "rank_top10": top(values[:, 0]), "safety_top10": top(values[:, 1]),
                "maximin_top10": top(balanced), "upward_quality": top(balanced, eligible)}
    selected["upward_count_return"] = [entry["code"] for entry in sorted(entries,
        key=lambda entry: (-entry["return_pct"], entry["code"]))[:len(selected["upward_quality"])]]
    selected["low_vol_top10"] = [entry["code"] for entry in sorted(entries,
        key=lambda entry: (entry["daily_volatility_pct"], entry["code"]))[:10]]
    return selected
