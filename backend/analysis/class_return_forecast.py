"""Matured class holding-return forecasts and cost-aware capital-payoff choices."""
import math

import numpy as np

from backend.analysis.cross_asset_trend import cash_scaled_class_target, class_proposal, expand_class_weights
from backend.analysis.matured_trend_forecast import MaturedForecaster
from backend.analysis.sector_trend import trend_metrics
from backend.analysis.trend_risk_budget import projected_volatility

CLASSES = ("stock", "gold", "bond")
MODES = ("class_mean", "trend_ridge")
FEATURE_NAMES = ("is_stock", "is_gold", "is_bond", "return21", "return63", "return252", "annual_vol60",
                 "normalised_score", "positive_vote", "fitted_return63", "fit63", "efficiency63", "drawdown63")
HORIZON = 60


def class_features(data, name, index, mode):
    if mode not in MODES or name not in CLASSES or not 252 <= index < len(data["days"]):
        raise ValueError("Invalid class return forecast feature request")
    identity = [float(name == c) for c in CLASSES]
    if mode == "class_mean":
        return identity
    stats = data["cross_stats"][index]
    if stats["signal_index"] != index:
        raise ValueError("Class features require the actual past observation date")
    signals = stats["signals"][name]
    members = data["class_groups"][name]
    changes = [math.fsum(data["marks"][c][i] / data["marks"][c][i - 1] - 1 for c in members) / len(members)
               for i in range(index - 62, index + 1)]
    local = np.concatenate(([1.], np.cumprod(1 + np.asarray(changes))))
    descriptor = trend_metrics(local)
    return identity + [signals["log_returns"][w] for w in (21, 63, 252)] + [
        signals["annual_vol60"], signals["normalised_return_score"], signals["vote"],
        descriptor["fitted_return_pct"] / 100, descriptor["r2"], descriptor["efficiency"], descriptor["max_drawdown_pct"] / 100]


def class_holding_return(data, name, signal_index):
    first, last = signal_index + 1, signal_index + 1 + HORIZON
    if name not in CLASSES or signal_index < 252 or last >= len(data["days"]):
        raise ValueError("Class holding outcome requires a complete future interval")
    values = []
    for code in data["class_groups"][name]:
        price = data["prices"][code]
        cash = math.fsum(data["distributions"][code][first + 1:last + 1])
        values.append((price[last] + cash) / price[first] - 1)
    return math.fsum(values) / len(values)


def class_records(data, mode):
    if mode not in MODES:
        raise ValueError("Unknown class forecast mode")
    return [{"signal_date": data["days"][i], "label_end": data["days"][i + 1 + HORIZON], "code": name,
             "features": class_features(data, name, i, mode), "outcomes": [class_holding_return(data, name, i)]}
            for i in range(252, len(data["days"]) - HORIZON - 1, 10) for name in CLASSES]


def proposal_candidates(signals):
    candidates = {"cash": dict.fromkeys(CLASSES, 0.)}
    bond = signals["bond"]["vote"] >= 2/3
    if bond:
        candidates["defensive"] = {"stock": 0., "gold": 0., "bond": .6}
    for name in ("stock", "gold"):
        if signals[name]["vote"] >= 2/3:
            candidates[name] = dict.fromkeys(CLASSES, 0.) | {name: .6, "bond": .4 if bond else 0.}
    if all(signals[c]["vote"] >= 2/3 for c in ("stock", "gold")):
        candidates["pair"] = class_proposal("risky_pair60", signals)
    return candidates


def capital_payoff_choice(data, stats, current, predictions, fee_bps):
    if (set(predictions) != set(CLASSES) or any(not math.isfinite(v) or v < -1 for v in predictions.values())
            or not math.isfinite(fee_bps) or not 0 <= fee_bps < 10000):
        raise ValueError("Invalid predicted returns or turnover penalty")
    rows = {}
    for name, proposal in proposal_candidates(stats["signals"]).items():
        unscaled = expand_class_weights(data, proposal)
        risk = projected_volatility(stats["fund_risk"], unscaled)
        scale = min(1., .1 / risk) if risk > 0 else 1.
        targets = {c: w * scale for c, w in unscaled.items()}
        class_weights = {group: math.fsum(targets[c] for c in members) for group, members in data["class_groups"].items()}
        gross = math.fsum(class_weights[c] * predictions[c] for c in CLASSES)
        turnover = math.fsum(abs(targets[c] - current[c]) for c in targets)
        penalty = turnover * fee_bps / 10000
        rows[name] = {"proposal": proposal, "predicted_gross_return": gross, "turnover_weight": turnover,
                      "ordinary_cost_proxy": penalty, "capital_payoff_proxy": gross - penalty, "risky_scale": scale}
    best = max(row["capital_payoff_proxy"] for row in rows.values())
    chosen = min(name for name, row in rows.items() if best - row["capital_payoff_proxy"] <= 1e-10)
    return rows[chosen]["proposal"], chosen, rows


class ClassForecastAllocator:
    def __init__(self, data, mode, records, *, fee_bps=10, forecaster=None):
        if mode not in MODES:
            raise ValueError("Unknown class return model")
        fee_bps = float(fee_bps)
        if not math.isfinite(fee_bps) or not 0 <= fee_bps < 10000:
            raise ValueError("Invalid forecast turnover penalty")
        self.data, self.mode, self.fee_bps = data, mode, fee_bps
        if forecaster is not None and forecaster.records != sorted(records, key=lambda r: (r["signal_date"], r["code"])):
            raise ValueError("Shared class model cache contains different training records")
        self.forecaster = forecaster if forecaster is not None else MaturedForecaster(records, penalty=1., minimum_dates=24)
        self.trace = []

    def __call__(self, index, held, state):
        data, stats = self.data, self.data["cross_stats"][index]
        model, learning = self.forecaster.at(data["days"][index])
        predictions, raw, rows, chosen = {}, {}, {}, "raw_fallback"
        if model is None:
            proposal = class_proposal("dual_return60", stats["signals"])
        else:
            features = [class_features(data, name, index, self.mode) for name in CLASSES]
            raw = dict(zip(CLASSES, (float(v) for v in model.predict(features)[:, 0])))
            predictions = {c: max(-1., v) for c, v in raw.items()}
            proposal, chosen, rows = capital_payoff_choice(data, stats, dict(state.weights), predictions, self.fee_bps)
        target, meta = cash_scaled_class_target(data, "dual_return60", index, held, proposal, stats=stats,
                                                exit_mode="absolute_or_current_risk", portfolio_state=state)
        meta["forecast_choice"] = {"mode": self.mode, "horizon_sessions": HORIZON, "learning": learning,
                                    "raw_predictions": raw, "predicted_gross_returns": predictions, "chosen": chosen,
                                    "candidates": rows, "ordinary_fee_bps_proxy": self.fee_bps,
                                    "payoff_tie_tolerance": 1e-10,
                                    "proxy_ignores_short_fee_and_funding_delay": True}
        self.trace.append(meta | {"targets": target.weights})
        return target
