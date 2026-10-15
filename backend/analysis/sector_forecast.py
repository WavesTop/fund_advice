"""Experimental horizon-specific sector forecasts, isolated from production ranks."""
from datetime import date, timedelta
import math

import numpy as np

from backend.analysis.matured_trend_forecast import fit_ridge

FEATURES = ("return", "recent_fit_return", "r2", "recent_r2", "efficiency", "up_day_ratio",
            "drawdown", "volatility", "fast_ma_bias", "ma_spread", "relative_percentile")


def features(entry):
    if entry["relative_percentile"] is None:
        raise ValueError("Sector forecast requires a comparable peer pool")
    result = [entry["return_pct"] / 100, entry["recent_fitted_return_pct"] / 100,
              entry["r2"], entry["recent_r2"], entry["efficiency"], entry["up_day_ratio"],
              entry["max_drawdown_pct"] / 100, entry["daily_volatility_pct"] / 100,
              entry["fast_ma_bias_pct"] / 100, entry["fast_slow_ma_spread_pct"] / 100,
              entry["relative_percentile"] / 100]
    if not all(math.isfinite(value) for value in result):
        raise ValueError("Nonfinite sector forecast descriptor")
    return result


def visible_records(records, as_of):
    lower = (date.fromisoformat(as_of) - timedelta(days=1096)).isoformat()
    return [record for record in records if lower <= record["signal_date"] and record["ready_date"] <= as_of]


def fit_period(records, as_of, codes):
    if len(codes) < 2 or len(set(codes)) != len(codes):
        raise ValueError("Forecast requires distinct comparable sector codes")
    visible = visible_records(records, as_of)
    dates = [record["signal_date"] for record in visible]
    if dates != sorted(set(dates)):
        raise ValueError("Sector training dates are not unique and ordered")
    metadata = {"as_of": as_of, "training_dates": dates, "distinct_dates": len(dates), "minimum_dates": 24,
                "window_days": 1096, "penalty": 1., "features": list(FEATURES)}
    if len(dates) < 24:
        return None, None, {**metadata, "status": "not_fitted"}
    if any(record["codes"] != codes or not record["signal_date"] < record["exit_date"] < record["ready_date"]
           for record in visible):
        raise ValueError("Sector training membership or maturity order differs")
    x = np.asarray([record["features"] for record in visible], dtype=float)
    y = np.asarray([record["outcomes"] for record in visible], dtype=float)
    if x.shape != (len(visible), len(codes), len(FEATURES)) or y.shape != (len(visible), len(codes), 2):
        raise ValueError("Sector training matrix shape differs")
    model = fit_ridge(x.reshape(-1, len(FEATURES)), y.reshape(-1, 2), penalty=1.)
    return model, y.mean(axis=0), {**metadata, "status": "fitted", "observations": len(dates) * len(codes),
        "latest_label_end": max(record["exit_date"] for record in visible),
        "latest_ready_date": max(record["ready_date"] for record in visible),
        "means": model.means.tolist(), "scales": model.scales.tolist(),
        "coefficients": model.coefficients.tolist(), "intercept": model.intercept.tolist()}


def predictions(model, matrix):
    values = model.predict(matrix)
    values[:, 1] -= values[:, 1].mean()
    return values


def audit_fit(records, metadata, codes):
    lower = (date.fromisoformat(metadata["as_of"]) - timedelta(days=1096)).isoformat()
    visible = [record for record in records if record["signal_date"] >= lower
               and record["exit_date"] < metadata["as_of"] and record["ready_date"] <= metadata["as_of"]]
    if [record["signal_date"] for record in visible] != metadata["training_dates"]:
        raise ValueError("Sector fit includes unavailable training dates")
    if metadata["distinct_dates"] != len(visible) or metadata["minimum_dates"] != 24 or metadata["window_days"] != 1096 or metadata["penalty"] != 1:
        raise ValueError("Sector fit settings/counts differ")
    if metadata["features"] != list(FEATURES):
        raise ValueError("Sector fit feature definitions differ")
    if metadata["status"] == "not_fitted":
        if len(visible) >= 24:
            raise ValueError("Sector model incorrectly marked unavailable")
        return {"status": "not_fitted"}
    if metadata["status"] != "fitted" or len(visible) < 24 or any(record["codes"] != codes for record in visible):
        raise ValueError("Invalid fitted sector model status or pool")
    x = np.asarray([row for record in visible for row in record["features"]])
    y = np.asarray([row for record in visible for row in record["outcomes"]])
    means, scales = x.mean(axis=0), np.maximum(x.std(axis=0), 1e-8)
    if (not np.allclose(means, metadata["means"], rtol=0, atol=1e-12)
            or not np.allclose(scales, metadata["scales"], rtol=0, atol=1e-12)):
        raise ValueError("Sector fit normalization differs")
    z = np.clip((x - means) / scales, -5, 5)
    coefficients, intercept = np.asarray(metadata["coefficients"]), np.asarray(metadata["intercept"])
    residual = (z.T @ z / len(z) + np.eye(len(FEATURES))) @ coefficients - z.T @ (y - y.mean(axis=0)) / len(z)
    if (not np.isfinite(coefficients).all() or not np.isfinite(intercept).all()
            or not np.allclose(intercept, y.mean(axis=0), rtol=0, atol=1e-12)
            or np.max(np.abs(residual)) > 1e-10):
        raise ValueError("Sector fitted coefficients do not solve registered ridge equations")
    if (metadata["observations"] != len(x) or metadata["latest_label_end"] != max(r["exit_date"] for r in visible)
            or metadata["latest_ready_date"] != max(r["ready_date"] for r in visible)):
        raise ValueError("Sector fitted label availability differs")
    return {"status": "passed", "observations": len(x), "normal_equation_error": float(np.max(np.abs(residual)))}
