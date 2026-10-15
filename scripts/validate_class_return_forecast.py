"""Independent raw-data, matured-training and capital-payoff forecast audit."""
from datetime import date, timedelta
from hashlib import sha256
import json
import math

import numpy as np

from backend.analysis.matured_trend_forecast import audit_training_visibility
from backend.analysis.sector_trend import trend_metrics
from scripts.validate_cross_asset_trend import audit_cross_asset_decision, rebuild_proposal, rebuild_signals

CLASSES = ("stock", "gold", "bond")


def rebuilt_features(data, name, index, mode):
    result = [float(name == c) for c in CLASSES]
    if mode == "class_mean":
        return result
    if mode != "trend_ridge":
        raise ValueError("Unknown independently audited class forecast")
    groups, signals = rebuild_signals(data, index)
    members = groups[name]
    returns = np.asarray([[data["marks"][c][i] / data["marks"][c][i - 1] - 1 for i in range(index - 62, index + 1)] for c in members]).mean(axis=0)
    descriptor = trend_metrics(np.concatenate(([1.], np.cumprod(1 + returns))))
    s = signals[name]
    return result + [s["log_returns"][w] for w in (21, 63, 252)] + [s["annual_vol60"], s["normalised_return_score"], s["vote"],
        descriptor["fitted_return_pct"] / 100, descriptor["r2"], descriptor["efficiency"], descriptor["max_drawdown_pct"] / 100]


def audit_class_records(data, records, mode):
    wanted = {(data["days"][i], c): i for i in range(252, len(data["days"]) - 61, 10) for c in CLASSES}
    rows = {(r["signal_date"], r["code"]): r for r in records}
    if len(rows) != len(records) or set(rows) != set(wanted):
        raise ValueError("Class training records do not match the declared observation grid")
    for key, i in wanted.items():
        row, name = rows[key], key[1]
        first, last = i + 1, i + 61
        if row["label_end"] != data["days"][last]:
            raise ValueError("Class label maturity does not match next-NAV plus60sessions")
        features = rebuilt_features(data, name, i, mode)
        if not np.isfinite(row["features"]).all() or not np.allclose(row["features"], features, rtol=1e-8, atol=1e-9):
            raise ValueError("Class feature records differ from past raw fund prices")
        members = data["class_groups"][name]
        units = {c: 1 / len(members) / data["prices"][c][first] for c in members}
        ending = math.fsum(units[c] * data["prices"][c][last] for c in members)
        cash = math.fsum(units[c] * data["distributions"][c][j] for c in members for j in range(first + 1, last + 1))
        if len(row["outcomes"]) != 1 or not math.isfinite(row["outcomes"][0]) or abs(row["outcomes"][0] - (ending + cash - 1)) > 1e-10:
            raise ValueError("Gross holding label does not match fixed fund units and cash distributions")
    return {"status": "passed", "records": len(records), "signal_dates": len(wanted) // 3}


def audit_forecast_choice(entry, data, index, mode, records, *, training_cache=None):
    choice = entry["forecast_choice"]
    learning, day = choice["learning"], data["days"][index]
    if (choice["mode"] != mode or mode not in ("class_mean", "trend_ridge") or choice["horizon_sessions"] != 60
            or learning["asof"] != day or learning["window_start"] != (date.fromisoformat(day) - timedelta(days=1096)).isoformat()
            or choice["payoff_tie_tolerance"] != 1e-10
            or choice["proxy_ignores_short_fee_and_funding_delay"] is not True):
        raise ValueError("Forecast visibility or proxy declaration differs")
    visible = [r for r in records if learning["window_start"] <= r["signal_date"] and r["label_end"] <= day]
    if (learning["status"] == "fitted" and (learning["penalty"] != 1 or len({r["signal_date"] for r in visible}) < 24)
            or learning["status"] == "insufficient_history" and learning["minimum_dates"] != 24):
        raise ValueError("Class model used a different fitting threshold or penalty")
    key = sha256(json.dumps(learning, sort_keys=True, allow_nan=False).encode()).hexdigest()
    if training_cache is None or key not in training_cache:
        audit_training_visibility(records, learning)
        if training_cache is not None:
            training_cache[key] = True
    groups, signals = rebuild_signals(data, index)
    if learning["status"] == "insufficient_history":
        if (choice["chosen"] != "raw_fallback" or choice["candidates"] or choice["predicted_gross_returns"] or choice["raw_predictions"]):
            raise ValueError("An unfitted model must use the declared raw fallback")
        proposal = rebuild_proposal("dual_return60", signals)
    else:
        x = np.asarray([rebuilt_features(data, c, index, mode) for c in CLASSES])
        z = np.clip((x - np.asarray(learning["means"])) / np.asarray(learning["scales"]), -5, 5)
        raw = z @ np.asarray(learning["coefficients"]) + np.asarray(learning["intercept"])
        predicted = dict(zip(CLASSES, (max(-1., float(v)) for v in raw[:, 0])))
        if set(choice["raw_predictions"]) != set(CLASSES) or set(choice["predicted_gross_returns"]) != set(CLASSES):
            raise ValueError("Forecast class set differs")
        for at, c in enumerate(CLASSES):
            if (not math.isfinite(choice["raw_predictions"][c]) or not math.isfinite(choice["predicted_gross_returns"][c])
                    or abs(choice["raw_predictions"][c] - raw[at, 0]) > 1e-9 or abs(choice["predicted_gross_returns"][c] - predicted[c]) > 1e-9):
                raise ValueError("Current predictions do not follow the audited matured model")
        proposals = {"cash": dict.fromkeys(CLASSES, 0.)}
        bond = signals["bond"]["vote"] >= 2/3
        if bond:
            proposals["defensive"] = {"stock": 0., "gold": 0., "bond": .6}
        for c in ("stock", "gold"):
            if signals[c]["vote"] >= 2/3:
                proposals[c] = dict.fromkeys(CLASSES, 0.) | {c: .6, "bond": .4 if bond else 0.}
        if all(signals[c]["vote"] >= 2/3 for c in ("stock", "gold")):
            proposals["pair"] = rebuild_proposal("risky_pair60", signals)
        codes = list(data["prices"])
        daily = np.asarray([[math.log(data["marks"][c][j] / data["marks"][c][j - 1]) for j in range(index - 59, index + 1)] for c in codes])
        current = entry["current_fund_weights"]
        rows = {}
        fee = choice["ordinary_fee_bps_proxy"]
        if not math.isfinite(fee) or not 0 <= fee < 10000:
            raise ValueError("Invalid independently audited ordinary fee proxy")
        for name, p in proposals.items():
            weights = {c: min(.9, p[group] / len(members)) if group == "bond" else p[group] / len(members)
                       for group, members in groups.items() for c in members}
            risk = float(np.std(np.asarray([weights.get(c, 0) for c in codes]) @ daily) * np.sqrt(252))
            scale = min(1., .1 / risk) if risk > 0 else 1.
            weights = {c: w * scale for c, w in weights.items()}
            gross = math.fsum(math.fsum(weights[c] for c in members) * predicted[group] for group, members in groups.items())
            turnover = math.fsum(abs(weights[c] - current[c]) for c in weights)
            penalty = turnover * fee / 10000
            rows[name] = {"proposal": p, "predicted_gross_return": gross, "turnover_weight": turnover,
                          "ordinary_cost_proxy": penalty, "capital_payoff_proxy": gross - penalty, "risky_scale": scale}
        if set(choice["candidates"]) != set(rows):
            raise ValueError("Candidate portfolio set differs")
        for name, row in rows.items():
            observed = choice["candidates"][name]
            for k, v in row.items():
                if k == "proposal":
                    if set(observed[k]) != set(v) or any(not math.isfinite(observed[k][c]) or abs(observed[k][c] - w) > 1e-9 for c, w in v.items()):
                        raise ValueError("Forecast portfolio proposal differs")
                elif not math.isfinite(observed[k]) or abs(observed[k] - v) > 1e-9:
                    raise ValueError("Risk-adjusted capital payoff or ordinary cost penalty differs")
        best = max(row["capital_payoff_proxy"] for row in rows.values())
        selected = min(name for name, row in rows.items() if best - row["capital_payoff_proxy"] <= 1e-10)
        if choice["chosen"] != selected:
            raise ValueError("Forecast did not select the highest declared capital payoff")
        proposal = rows[selected]["proposal"]
    audit_cross_asset_decision(entry, data, index, "dual_return60", exit_mode="absolute_or_current_risk", expected_proposal=proposal)
