"""Shrinkage forecasts trained solely on fully matured historical fund outcomes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import math
import statistics

import numpy as np

from backend.analysis.sector_trend import midpoint_percentiles, trend_metrics

FEATURE_NAMES = ("score20", "score60", "score120", "return5", "return20", "return60", "return120",
                 "annual_vol60", "drawdown60", "bias60", "efficiency60", "fit60",
                 "benchmark_up", "short_score_in_up_market", "pullback_in_long_trend")


def feature_vector(data, code, index):
    if not 120 <= index < len(data["marks"][code]):
        raise ValueError("Forecast features require 120 past sessions")
    entry = next(e for e in data["frames"][index]["entries"] if e["code"] == code)
    prices = data["marks"][code]
    metrics = trend_metrics(prices[index - 60:index + 1])
    returns = {n: prices[index] / prices[index - n] - 1 for n in (5, 20, 60, 120)}
    bull = float(data["frames"][index]["context"].get("benchmark_above_ma200") is True)
    return [entry["scores"]["short"] / 100, entry["scores"]["medium"] / 100, entry["scores"]["long"] / 100,
            *(returns[n] for n in (5, 20, 60, 120)), entry["annual_vol60"], metrics["max_drawdown_pct"] / 100,
            prices[index] / entry["ma60"] - 1, metrics["efficiency"], metrics["r2"], bull,
            bull * entry["scores"]["short"] / 100, max(0., returns[120]) * min(0., returns[20])]


@dataclass(frozen=True)
class RidgeForecast:
    means: np.ndarray
    scales: np.ndarray
    coefficients: np.ndarray
    intercept: np.ndarray

    def predict(self, features):
        x = np.asarray(features, dtype=float)
        if x.ndim != 2 or x.shape[1] != len(self.means) or not np.isfinite(x).all():
            raise ValueError("Invalid forecast feature matrix")
        return np.clip((x - self.means) / self.scales, -5, 5) @ self.coefficients + self.intercept


def fit_ridge(features, outcomes, penalty=1.):
    x, y = np.asarray(features, dtype=float), np.asarray(outcomes, dtype=float)
    if (x.ndim != 2 or y.ndim != 2 or x.shape[0] != y.shape[0] or not x.shape[0]
            or not np.isfinite(x).all() or not np.isfinite(y).all() or not math.isfinite(penalty) or penalty <= 0):
        raise ValueError("Invalid shrinkage training data")
    means, scales = x.mean(axis=0), np.maximum(x.std(axis=0), 1e-8)
    z = np.clip((x - means) / scales, -5, 5)
    center = y.mean(axis=0)
    matrix = z.T @ z / len(z) + penalty * np.eye(x.shape[1])
    coefficients = np.linalg.solve(matrix, z.T @ (y - center) / len(z))
    return RidgeForecast(means, scales, coefficients, center)


class MaturedForecaster:
    def __init__(self, records, *, penalty=1., minimum_dates=24):
        if minimum_dates < 5 or not math.isfinite(penalty) or penalty <= 0:
            raise ValueError("Invalid forecaster constraints")
        self.records = sorted(records, key=lambda r: (r["signal_date"], r["code"]))
        for record in self.records:
            if record["label_end"] <= record["signal_date"]:
                raise ValueError("Outcome does not follow its feature date")
        self.penalty, self.minimum_dates = penalty, minimum_dates
        self.cache = {}

    def at(self, asof):
        if asof in self.cache:
            return self.cache[asof]
        lower = (date.fromisoformat(asof) - timedelta(days=1096)).isoformat()
        visible = [r for r in self.records if lower <= r["signal_date"] and r["label_end"] <= asof]
        dates = sorted({r["signal_date"] for r in visible})
        if len(dates) < self.minimum_dates:
            self.cache[asof] = (None, {"status": "insufficient_history", "asof": asof, "window_start": lower,
                                     "minimum_dates": self.minimum_dates, "distinct_dates": len(dates)})
            return self.cache[asof]
        cutoff = dates[int(len(dates) * .8)]
        # Purge unfinished labels at the inner validation boundary as well.
        training = [r for r in visible if r["label_end"] < cutoff]
        validation = [r for r in visible if r["signal_date"] >= cutoff]
        validation_ic = None
        if len({r["signal_date"] for r in training}) >= 5:
            inner = fit_ridge([r["features"] for r in training], [r["outcomes"] for r in training], self.penalty)
            predicted = inner.predict([r["features"] for r in validation])[:, 0]
            correlations = []
            for day in sorted({r["signal_date"] for r in validation}):
                indexes = [i for i, r in enumerate(validation) if r["signal_date"] == day]
                p = [float(predicted[i]) for i in indexes]
                y = [validation[i]["outcomes"][0] for i in indexes]
                if len(p) > 1 and statistics.pstdev(p) > 1e-12 and statistics.pstdev(y) > 1e-12:
                    a, b = midpoint_percentiles(p), midpoint_percentiles(y)
                    ma, mb = statistics.fmean(a), statistics.fmean(b)
                    correlations.append(statistics.fmean((x - ma) * (v - mb) for x, v in zip(a, b)) /
                                        (statistics.pstdev(a) * statistics.pstdev(b)))
            validation_ic = statistics.fmean(correlations) if correlations else None
        model = fit_ridge([r["features"] for r in visible], [r["outcomes"] for r in visible], self.penalty)
        metadata = {"status": "fitted", "asof": asof, "window_start": lower, "observations": len(visible),
                    "distinct_dates": len(dates), "latest_label_end": max(r["label_end"] for r in visible),
                    "validation_start": cutoff, "inner_training_latest_label_end": max((r["label_end"] for r in training), default=None),
                    "validation_rank_ic": validation_ic, "penalty": self.penalty,
                    "means": model.means.tolist(), "scales": model.scales.tolist(),
                    "coefficients": model.coefficients.tolist(), "intercept": model.intercept.tolist()}
        self.cache[asof] = model, metadata
        return model, metadata


def audit_training_visibility(records, metadata):
    """Independently check both the public as-of and inner-validation cutoffs."""
    visible = [r for r in records if metadata["window_start"] <= r["signal_date"] and r["label_end"] <= metadata["asof"]]
    dates = sorted({r["signal_date"] for r in visible})
    if metadata["status"] == "insufficient_history":
        if len(dates) != metadata["distinct_dates"] or len(dates) >= metadata["minimum_dates"]:
            raise ValueError("Insufficient-history status does not match matured records")
        return {"status": "not_fitted", "distinct_dates": len(dates)}
    if metadata["status"] != "fitted":
        raise ValueError("Unknown forecast status")
    if (len(visible) != metadata["observations"] or len({r["signal_date"] for r in visible}) != metadata["distinct_dates"]
            or max(r["label_end"] for r in visible) != metadata["latest_label_end"]):
        raise ValueError("Forecast training contains unavailable outcomes")
    if metadata["validation_start"] != dates[int(len(dates) * .8)]:
        raise ValueError("Forecast validation boundary is not the registered final 20%")
    training = [r for r in visible if r["label_end"] < metadata["validation_start"]]
    if metadata["inner_training_latest_label_end"] != max((r["label_end"] for r in training), default=None):
        raise ValueError("Inner validation included immature training outcomes")
    x, y = np.asarray([r["features"] for r in visible]), np.asarray([r["outcomes"] for r in visible])
    means, scales = np.asarray(metadata["means"]), np.asarray(metadata["scales"])
    if not np.allclose(means, x.mean(axis=0)) or not np.allclose(scales, np.maximum(x.std(axis=0), 1e-8)):
        raise ValueError("Forecast standardization used a different training set")
    z = np.clip((x - means) / scales, -5, 5)
    coefficients, intercept = np.asarray(metadata["coefficients"]), np.asarray(metadata["intercept"])
    if not np.isfinite(coefficients).all() or not np.isfinite(intercept).all() or metadata["penalty"] <= 0:
        raise ValueError("Invalid forecast parameters")
    if not np.allclose(intercept, y.mean(axis=0), atol=1e-10):
        raise ValueError("Forecast intercept does not match matured data")
    residual = (z.T @ z / len(z) + metadata["penalty"] * np.eye(x.shape[1])) @ coefficients - z.T @ (y - intercept) / len(z)
    if np.max(np.abs(residual)) > 1e-8:
        raise ValueError("Forecast coefficients fail the ridge normal equations")
    expected_ic = None
    if len({r["signal_date"] for r in training}) >= 5:
        validation = [r for r in visible if r["signal_date"] >= metadata["validation_start"]]
        inner = fit_ridge([r["features"] for r in training], [r["outcomes"] for r in training], metadata["penalty"])
        predicted = inner.predict([r["features"] for r in validation])[:, 0]
        daily_ics = []
        for day in sorted({r["signal_date"] for r in validation}):
            indexes = [i for i, r in enumerate(validation) if r["signal_date"] == day]
            raw_a = [float(predicted[i]) for i in indexes]
            raw_b = [validation[i]["outcomes"][0] for i in indexes]
            if len(raw_a) < 2 or statistics.pstdev(raw_a) < 1e-12 or statistics.pstdev(raw_b) < 1e-12:
                continue
            a, b = midpoint_percentiles(raw_a), midpoint_percentiles(raw_b)
            if len(a) < 2 or statistics.pstdev(a) < 1e-12 or statistics.pstdev(b) < 1e-12:
                continue
            ma, mb = statistics.fmean(a), statistics.fmean(b)
            daily_ics.append(statistics.fmean((u - ma) * (v - mb) for u, v in zip(a, b)) /
                             (statistics.pstdev(a) * statistics.pstdev(b)))
        expected_ic = statistics.fmean(daily_ics) if daily_ics else None
    actual_ic = metadata["validation_rank_ic"]
    if (expected_ic is None) != (actual_ic is None) or expected_ic is not None and abs(expected_ic - actual_ic) > 1e-10:
        raise ValueError("Forecast validation gate does not match past-only validation data")
    return {"status": "passed", "observations": len(visible), "latest_label_end": metadata["latest_label_end"],
            "normal_equation_max_error": float(np.max(np.abs(residual)))}
