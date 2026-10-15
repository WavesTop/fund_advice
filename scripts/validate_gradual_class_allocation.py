"""Independent incumbent preservation, partial clock and candidate payoff audit."""
import math

import numpy as np

from scripts.validate_class_return_forecast import audit_forecast_choice
from scripts.validate_cross_asset_trend import audit_cross_asset_decision, audit_signal_records, rebuild_proposal, rebuild_signals


def same_weights(actual, expected):
    if set(actual) != set(expected) or any(not math.isfinite(actual[c]) or abs(actual[c] - v) > 1e-9 for c, v in expected.items()):
        raise ValueError("Gradual fund weights differ from raw-data reconstruction")


def audit_gradual_choice(entry, data, index, mode, action, records, previous_index, *, training_cache=None, previous_targets=None):
    g = entry["gradual_choice"]
    if (action not in ("stay_choice", "partial_choice", "intent_choice") or g["action"] != action or g["horizon_sessions"] != 60
            or g["previous_signal_index"] != previous_index or g["payoff_tie_tolerance"] != 1e-10
            or previous_index is not None and not previous_index < index):
        raise ValueError("Gradual adjustment declaration or previous observation differs")
    if action == "intent_choice":
        if previous_index is None:
            if previous_targets is not None or g["previous_fund_targets"] is not None:
                raise ValueError("Initial intent must not read a future allocation plan")
        else:
            if previous_targets is None:
                raise ValueError("Intent audit requires the preceding actual decision target")
            same_weights(g["previous_fund_targets"], previous_targets)
    elapsed = None if previous_index is None else index - previous_index
    fraction = 1. if action == "stay_choice" or previous_index is None else min(1., elapsed / 60)
    if g["elapsed_sessions"] != elapsed or not math.isfinite(g["fraction"]) or abs(g["fraction"] - fraction) > 1e-12:
        raise ValueError("Partial speed used a nominal interval instead of actual elapsed sessions")
    immediate = g["immediate_decision"]
    audit_forecast_choice(immediate, data, index, mode, records, training_cache=training_cache)
    same_weights(entry["current_fund_weights"], immediate["current_fund_weights"])
    if immediate["forecast_choice"]["learning"]["status"] == "insufficient_history":
        if g["chosen"] != "raw_fallback" or g["candidates"] or g["safe_incumbent"]:
            raise ValueError("Unfitted gradual model must preserve the raw fallback")
        audit_cross_asset_decision(entry, data, index, "dual_return60", exit_mode="absolute_or_current_risk")
        return
    groups, signals = rebuild_signals(data, index)
    if entry["signal_date"] != data["days"][index]:
        raise ValueError("Gradual observation date differs")
    audit_signal_records(entry["class_signals"], signals)
    codes = list(data["prices"])
    daily = np.asarray([[math.log(data["marks"][c][j] / data["marks"][c][j - 1]) for j in range(index - 59, index + 1)] for c in codes])

    def scaled(weights):
        risk = float(np.std(np.asarray([weights[c] for c in codes]) @ daily) * np.sqrt(252))
        ratio = min(1., .1 / risk) if risk else 1.
        return {c: w * ratio for c, w in weights.items()}

    def expanded(proposal):
        return {c: min(.9, proposal[group] / len(members)) if group == "bond" else proposal[group] / len(members)
                for group, members in groups.items() for c in members}

    current = entry["current_fund_weights"]
    safe = scaled({c: current[c] if signals[group]["vote"] >= 2/3 else 0. for group, members in groups.items() for c in members})
    origin = safe
    if action == "intent_choice" and previous_targets is not None:
        origin = scaled({c: previous_targets[c] if signals[group]["vote"] >= 2/3 else 0. for group, members in groups.items() for c in members})
    same_weights(g["safe_incumbent"], safe)
    proposals = {"cash": dict.fromkeys(groups, 0.)}
    bond = signals["bond"]["vote"] >= 2/3
    if bond:
        proposals["defensive"] = {"stock": 0., "gold": 0., "bond": .6}
    for group in ("stock", "gold"):
        if signals[group]["vote"] >= 2/3:
            proposals[group] = dict.fromkeys(groups, 0.) | {group: .6, "bond": .4 if bond else 0.}
    if all(signals[c]["vote"] >= 2/3 for c in ("stock", "gold")):
        proposals["pair"] = rebuild_proposal("risky_pair60", signals)
    rows = {}
    for name, proposal in proposals.items():
        aim = scaled(expanded(proposal))
        rows[name] = {"proposal": proposal, "unscaled_fund_targets": {c: origin[c] * (1 - fraction) + aim[c] * fraction for c in codes}}
    rows["incumbent"] = {"proposal": {group: math.fsum(safe[c] for c in members) for group, members in groups.items()}, "unscaled_fund_targets": safe}
    forecast = immediate["forecast_choice"]
    fee = forecast["ordinary_fee_bps_proxy"]
    predicted = forecast["predicted_gross_returns"]
    if set(g["candidates"]) != set(rows):
        raise ValueError("Gradual candidate set differs")
    for name, row in rows.items():
        row["targets"] = scaled(row["unscaled_fund_targets"])
        row["predicted_gross_return"] = math.fsum(math.fsum(row["targets"][c] for c in members) * predicted[group] for group, members in groups.items())
        row["turnover_weight"] = math.fsum(abs(row["targets"][c] - current[c]) for c in codes)
        row["ordinary_cost_proxy"] = fee * row["turnover_weight"] / 10000
        row["capital_payoff_proxy"] = row["predicted_gross_return"] - row["ordinary_cost_proxy"]
        observed = g["candidates"][name]
        for field in ("proposal", "targets", "unscaled_fund_targets"):
            same_weights(observed[field], row[field])
        for field in ("predicted_gross_return", "turnover_weight", "ordinary_cost_proxy", "capital_payoff_proxy"):
            if not math.isfinite(observed[field]) or abs(observed[field] - row[field]) > 1e-9:
                raise ValueError("Gradual forecast payoff or turnover penalty differs")
    best = max(row["capital_payoff_proxy"] for row in rows.values())
    selected = min(name for name, row in rows.items() if best - row["capital_payoff_proxy"] <= 1e-10)
    if g["chosen"] != selected:
        raise ValueError("Gradual selection did not maximize its declared payoff")
    wanted = rows[selected]
    for field in ("risk_mode", "exit_mode", "absolute_failure_codes", "risk_exit_codes"):
        if entry[field] != immediate[field]:
            raise ValueError("Gradual action changed independently audited exit urgency")
    if (not math.isfinite(entry["current_projected_volatility"])
            or abs(entry["current_projected_volatility"] - immediate["current_projected_volatility"]) > 1e-9):
        raise ValueError("Gradual actual holdings risk differs")
    same_weights(entry["class_proposal"], wanted["proposal"])
    same_weights(entry["unscaled_fund_targets"], wanted["unscaled_fund_targets"])
    same_weights(entry["targets"], wanted["targets"])
    unscaled = float(np.std(np.asarray([wanted["unscaled_fund_targets"][c] for c in codes]) @ daily) * np.sqrt(252))
    risk = float(np.std(np.asarray([wanted["targets"][c] for c in codes]) @ daily) * np.sqrt(252))
    expected = {"unconstrained_projected_volatility": unscaled, "projected_volatility": risk,
                "risky_scale": min(1., .1/unscaled) if unscaled else 1.,
                "target_cash_weight": max(0., 1 - sum(wanted["targets"].values()))}
    if risk > .1 + 1e-9 or sum(wanted["targets"].values()) > 1 + 1e-12:
        raise ValueError("Gradual target exceeds risk or unlevered capital constraint")
    for field, value in expected.items():
        if not math.isfinite(entry[field]) or abs(entry[field] - value) > 1e-9:
            raise ValueError("Gradual fund risk scaling or remaining cash differs")
