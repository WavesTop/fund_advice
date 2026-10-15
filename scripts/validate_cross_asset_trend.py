"""Independent arithmetic-return signals and cash/legacy risk budget reconstruction."""
import math
from decimal import Decimal, localcontext

import numpy as np
from backend.analysis.sector_trend import trend_metrics

WINDOWS = (21, 63, 252)
FLOORS = {"stock": .05, "gold": .05, "bond": .005}


def rebuild_signals(data, index):
    entries = {e["code"]: e for e in data["frames"][120]["entries"]}
    groups = {name: [] for name in FLOORS}
    for code in data["prices"]:
        name = "bond" if data["roles"][code] == "defensive" else "gold" if entries[code]["defensive_asset"] else "stock"
        groups[name].append(code)
    signals = {}
    for name, codes in groups.items():
        returns = [math.fsum(data["marks"][c][i] / data["marks"][c][i - 1] - 1 for c in codes) / len(codes)
                   for i in range(index - 251, index + 1)]
        logs = np.log1p(np.asarray(returns))
        horizon = {window: float(logs[-window:].sum()) for window in WINDOWS}
        vol = float(np.std(logs[-60:]) * np.sqrt(252))
        floored = max(vol, FLOORS[name])
        signals[name] = {"log_returns": horizon, "vote": sum(v > 1e-12 for v in horizon.values()) / 3,
                         "annual_vol60": vol, "floored_annual_vol": floored,
                         "recent_state": trend_metrics(np.concatenate(([1.], np.cumprod(1 + np.asarray(returns[-63:])))))["state"],
                         "normalised_return_score": sum(horizon[w] / (floored * math.sqrt(w / 252)) for w in WINDOWS) / 3}
    return groups, signals


def rebuild_proposal(policy, signals):
    fixed = {"legacy_into_bonds40": (.4, .2, .4), "fixed_cash40": (.4, .2, .4), "fixed_cash_equal": (1/3, 1/3, 1/3),
             "fixed_cash_conservative": (.2, .2, .6), "fixed_cash_gold60": (.2, .6, .2), "fixed_cash_stock60": (.6, .2, .2)}
    if policy in fixed:
        return dict(zip(FLOORS, fixed[policy]))
    weights = dict.fromkeys(FLOORS, 0.)
    if policy in ("risky_pair60", "risky_pair_static60"):
        valid = [c for c in ("stock", "gold") if policy == "risky_pair_static60" or signals[c]["vote"] >= 2/3]
        if len(valid) == 2:
            stock_vol, gold_vol = signals["stock"]["floored_annual_vol"], signals["gold"]["floored_annual_vol"]
            weights["stock"] = .6 * gold_vol / (stock_vol + gold_vol)
            weights["gold"] = .6 - weights["stock"]
        elif valid:
            weights[valid[0]] = .6
        if policy == "risky_pair_static60" or signals["bond"]["vote"] >= 2/3:
            weights["bond"] = .4 if valid else .6
        return weights
    if policy == "ts_anchor":
        return {c: signals[c]["vote"] / 3 for c in FLOORS}
    if policy in ("inverse_vol_cap60", "ts_inverse_vol"):
        raw = {c: (signals[c]["vote"] if policy == "ts_inverse_vol" else 1.) / signals[c]["floored_annual_vol"]
               for c in FLOORS if policy == "inverse_vol_cap60" or signals[c]["vote"] > 0}
        if len(raw) == 1:
            weights[next(iter(raw))] = .6
        elif raw:
            low, high = 0., 1 / min(raw.values())
            for _ in range(80):
                mid = (low + high) / 2
                if sum(min(.6, mid * v) for v in raw.values()) < 1:
                    low = mid
                else:
                    high = mid
            weights.update({c: min(.6, high * v) for c, v in raw.items()})
    elif policy in ("dual_leader60", "dual_recent60", "dual_return60"):
        valid = [c for c in ("stock", "gold") if signals[c]["vote"] >= 2/3
                 and (policy != "dual_recent60" or signals[c]["recent_state"] not in ("weakening", "down"))]
        if valid:
            scores = {c: (sum(signals[c]["log_returns"][w] / math.sqrt(w / 252) for w in WINDOWS) / 3
                          if policy == "dual_return60" else signals[c]["normalised_return_score"]) for c in valid}
            chosen = sorted(valid, key=lambda c: (-scores[c], c))[0]
            weights[chosen] = .6
            weights["bond"] = .4 if signals["bond"]["vote"] >= 2/3 else 0.
        elif signals["bond"]["vote"] >= 2/3:
            weights["bond"] = .6
    else:
        raise ValueError("Unknown independently audited cross-asset policy")
    return weights


def audit_signal_records(recorded_signals, signals):
    if set(recorded_signals) != set(signals):
        raise ValueError("Cross-asset signal classes differ")
    for name, expected in signals.items():
        recorded = recorded_signals[name]
        values = {int(k): v for k, v in recorded["log_returns"].items()}
        if set(values) != set(WINDOWS) or any(not math.isfinite(values[w]) or abs(values[w] - expected["log_returns"][w]) > 1e-10 for w in WINDOWS):
            raise ValueError("Class proxy returns cannot be reconstructed from past fund marks")
        for key in ("vote", "annual_vol60", "floored_annual_vol", "normalised_return_score"):
            if not math.isfinite(recorded[key]) or abs(recorded[key] - expected[key]) > 1e-9:
                raise ValueError("Class vote/risk/ranking score differs from past observations")
        if recorded["recent_state"] != expected["recent_state"]:
            raise ValueError("Recent class state differs from independently reconstructed price proxy")


def audit_cross_asset_decision(entry, data, index, policy, *, exit_mode="legacy", expected_proposal=None):
    if index < 252 or entry["signal_date"] != data["days"][index]:
        raise ValueError("Cross-asset observation date or warmup differs")
    groups, signals = rebuild_signals(data, index)
    audit_signal_records(entry["class_signals"], signals)
    if expected_proposal is not None and not ({"phase_mix", "forecast_choice"} & set(entry)):
        raise ValueError("An alternate audited proposal requires a declared phase mixture or forecast choice")
    proposal = rebuild_proposal(policy, signals) if expected_proposal is None else expected_proposal
    if set(entry["class_proposal"]) != set(proposal) or any(not math.isfinite(entry["class_proposal"][c]) or abs(entry["class_proposal"][c] - v) > 1e-9 for c, v in proposal.items()):
        raise ValueError("Class allocation proposal differs from the declared rule")
    codes = list(data["prices"])
    daily = np.asarray([[math.log(data["marks"][c][i] / data["marks"][c][i - 1]) for i in range(index - 59, index + 1)] for c in codes])

    def expand(p):
        return {c: min(.9, p[name] / len(members)) if name == "bond" else p[name] / len(members)
                for name, members in groups.items() for c in members}

    def risk(weights):
        series = np.asarray([weights.get(c, 0) for c in codes]) @ daily
        return float(np.std(series) * np.sqrt(252))

    unscaled = expand(proposal)
    original = risk(unscaled)
    if policy == "legacy_into_bonds40":
        if entry["risk_mode"] != "legacy_bond_transfer":
            raise ValueError("Legacy reference was given a different risk policy")
        scale, expected = 1., unscaled
        if original > .1:
            feasible = []
            for candidate in np.linspace(0, 1, 101):
                weights = expand({"stock": .4 * candidate, "gold": .2 * candidate, "bond": 1 - .6 * candidate})
                if risk(weights) <= .1:
                    feasible.append((float(candidate), weights))
            scale, expected = feasible[-1] if feasible else (0., {})
    else:
        if entry["risk_mode"] != "uniform_cash_scale":
            raise ValueError("New cross-asset rule did not use its matched cash scaling")
        scale = min(1., .1 / original) if original > 0 else 1.
        expected = {c: value * scale for c, value in unscaled.items()}
        if set(entry["unscaled_fund_targets"]) != set(unscaled) or any(not math.isfinite(entry["unscaled_fund_targets"][c]) or abs(entry["unscaled_fund_targets"][c] - value) > 1e-9 for c, value in unscaled.items()):
            raise ValueError("Unscaled fund weights differ from the declared class distribution")
        if not math.isfinite(entry["target_cash_weight"]) or abs(entry["target_cash_weight"] - max(0., 1 - sum(expected.values()))) > 1e-9:
            raise ValueError("Cash allocation differs from the remaining unlevered budget")
    if not math.isfinite(entry["risky_scale"]) or abs(entry["risky_scale"] - scale) > 1e-9:
        raise ValueError("Cross-asset risk scale cannot be rebuilt")
    if set(entry["targets"]) != set(expected) or any(not math.isfinite(entry["targets"][c]) or abs(entry["targets"][c] - w) > 1e-9 for c, w in expected.items()):
        raise ValueError("Cross-asset fund targets differ from the independently rebuilt proposal")
    rebuilt = risk(expected)
    if not math.isfinite(entry["projected_volatility"]) or abs(rebuilt - entry["projected_volatility"]) > 1e-9 or rebuilt > .1 + 1e-9:
        raise ValueError("Cross-asset forecast risk exceeds its declared past-only constraint")
    if not math.isfinite(entry["unconstrained_projected_volatility"]) or abs(original - entry["unconstrained_projected_volatility"]) > 1e-9:
        raise ValueError("Original cross-asset risk was altered")
    if exit_mode == "legacy":
        if "exit_mode" in entry:
            raise ValueError("Current-holdings exit metadata appears in a legacy decision")
    elif exit_mode == "absolute_or_current_risk":
        if policy not in ("dual_return60", "risky_pair60", "risky_pair_static60") or entry.get("exit_mode") != exit_mode:
            raise ValueError("Current-holdings exit policy differs")
        current = entry["current_fund_weights"]
        if (set(current) != set(codes) or any(isinstance(w, bool) or not math.isfinite(w) or w < 0 for w in current.values())
                or sum(current.values()) > 1 + 1e-12):
            raise ValueError("Invalid independently checked current holdings")
        actual_risk = risk(current)
        if (not math.isfinite(entry["current_projected_volatility"])
                or abs(entry["current_projected_volatility"] - actual_risk) > 1e-9):
            raise ValueError("Current holdings risk differs from past raw observations")
        failed = sorted(c for name, members in groups.items() for c in members
                        if current[c] > 0 and policy != "risky_pair_static60" and signals[name]["vote"] < 2/3)
        exits = sorted(c for c in codes if current[c] > 0) if actual_risk > .1 + 1e-12 else failed
        if entry["absolute_failure_codes"] != failed or entry["risk_exit_codes"] != exits:
            raise ValueError("Exit urgency is not supported by absolute failure or actual holdings risk")
    else:
        raise ValueError("Unknown independently audited exit mode")


def audit_current_holdings(account, trace, data):
    """Use executed units and audited wealth, not strategy-supplied weights."""
    entries = {r["signal_date"]: r for r in trace}
    if len(entries) != len(trace) or list(entries) != [r["signal_date"] for r in account["decisions"]]:
        raise ValueError("Portfolio state traces do not match executed account decision dates")
    trades_by_date = {}
    for trade in account["trades"]:
        trades_by_date.setdefault(trade["execution_date"], []).append(trade)
        if trade["action"] == "SELL" and trade["reason"] != "terminal_liquidation":
            entry = entries[trade["signal_date"]]
            reason = "risk_exit" if trade["code"] in entry["risk_exit_codes"] else "allocation_reduction"
            if trade["reason"] != reason:
                raise ValueError("Executed sale reason differs from the independently audited exit urgency")
    indexes = {day: i for i, day in enumerate(data["days"])}
    with localcontext() as ctx:
        ctx.prec = 40
        units = dict.fromkeys(data["prices"], Decimal(0))
        for row in account["equity_curve"]:
            day, index = row["date"], indexes[row["date"]]
            for trade in trades_by_date.get(day, []):
                units[trade["code"]] += Decimal(trade["units"]) * (1 if trade["action"] == "BUY" else -1)
            values = {c: n * Decimal(str(data["prices"][c][index])) for c, n in units.items()}
            if abs(sum(values.values(), Decimal(0)) - Decimal(row["market_value"])) > Decimal("1e-20"):
                raise ValueError("Current holdings reconstruction differs from account market value")
            if day in entries:
                observed = entries[day]["current_fund_weights"]
                expected = {c: float(v / Decimal(row["equity"])) for c, v in values.items()}
                if set(observed) != set(expected) or any(not math.isfinite(observed[c]) or abs(observed[c] - v) > 1e-12 for c, v in expected.items()):
                    raise ValueError("Strategy current weights do not match independently replayed units and wealth")
    return {"status": "passed", "decisions": len(trace), "account_dates": len(account["equity_curve"])}
