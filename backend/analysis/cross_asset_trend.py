"""Long-only class momentum with fund-level costs and explicitly cash-scaled risk."""
import math
import statistics

import numpy as np

from backend.analysis.fund_allocation import AllocationState, AllocationTargets
from backend.analysis.fund_allocation_signals import _capped_weights
from backend.analysis.sector_trend import trend_metrics
from backend.analysis.trend_risk_budget import TrendBudgetPolicy, budget_statistics, projected_volatility, risk_budget_target

WINDOWS = (21, 63, 252)
FLOORS = {"stock": .05, "gold": .05, "bond": .005}
POLICIES = ("legacy_into_bonds40", "fixed_cash40", "fixed_cash_equal", "fixed_cash_conservative", "fixed_cash_gold60",
            "fixed_cash_stock60", "inverse_vol_cap60", "ts_anchor", "ts_inverse_vol", "dual_leader60", "dual_recent60", "dual_return60",
            "risky_pair60", "risky_pair_static60")


def prepare_class_proxies(data):
    entries = {e["code"]: e for e in data["frames"][120]["entries"]}
    groups = {name: [] for name in FLOORS}
    for code in data["marks"]:
        name = "bond" if data["roles"][code] == "defensive" else "gold" if entries[code]["defensive_asset"] else "stock"
        groups[name].append(code)
    if any(not codes for codes in groups.values()):
        raise ValueError("Cross-asset study requires stock, gold and bond classes")
    proxies = {}
    for name, codes in groups.items():
        marks = np.asarray([data["marks"][c] for c in codes], dtype=float)
        if marks.shape[1] != len(data["days"]) or not np.isfinite(marks).all() or np.any(marks <= 0):
            raise ValueError("Class signal marks must be complete, finite and positive")
        returns = np.mean(marks[:, 1:] / marks[:, :-1] - 1, axis=0)
        proxies[name] = np.concatenate(([1.], np.cumprod(1 + returns)))
        if not np.isfinite(proxies[name]).all() or np.any(proxies[name] <= 0):
            raise ValueError("Class signal proxy overflowed or became invalid")
    data["class_groups"], data["class_proxies"] = groups, proxies
    return data


def class_statistics(data, index):
    if index < 252 or index >= len(data["days"]):
        raise ValueError("Cross-asset signals require 252 past fund sessions")
    stats = budget_statistics(data, index)
    signals = {}
    for name, prices in data["class_proxies"].items():
        returns = {w: math.log(prices[index] / prices[index - w]) for w in WINDOWS}
        daily = [math.log(prices[i] / prices[i - 1]) for i in range(index - 59, index + 1)]
        vol = statistics.pstdev(daily) * math.sqrt(252)
        floored = max(FLOORS[name], vol)
        # Rebuild the local state at a fixed origin; old cumulative levels can
        # flip strict state thresholds through irrelevant rounding differences.
        members = data["class_groups"][name]
        local_returns = [math.fsum(data["marks"][c][i] / data["marks"][c][i - 1] - 1 for c in members) / len(members)
                         for i in range(index - 62, index + 1)]
        local_prices = np.concatenate(([1.], np.cumprod(1 + np.asarray(local_returns))))
        signals[name] = {"log_returns": returns, "vote": sum(r > 1e-12 for r in returns.values()) / len(WINDOWS),
                         "annual_vol60": vol, "floored_annual_vol": floored,
                         "recent_state": trend_metrics(local_prices)["state"],
                         "normalised_return_score": statistics.fmean(r / (floored * math.sqrt(w / 252)) for w, r in returns.items())}
    return {"signal_index": index, "fund_risk": stats, "signals": signals}


def class_proposal(policy, signals):
    if policy not in POLICIES:
        raise ValueError("Unknown cross-asset policy")
    fixed = {"legacy_into_bonds40": (.4, .2, .4), "fixed_cash40": (.4, .2, .4), "fixed_cash_equal": (1/3, 1/3, 1/3),
             "fixed_cash_conservative": (.2, .2, .6), "fixed_cash_gold60": (.2, .6, .2), "fixed_cash_stock60": (.6, .2, .2)}
    if policy in fixed:
        return dict(zip(FLOORS, fixed[policy]))
    weights = dict.fromkeys(FLOORS, 0.)
    if policy in ("risky_pair60", "risky_pair_static60"):
        unfiltered = policy == "risky_pair_static60"
        eligible = [name for name in ("stock", "gold") if unfiltered or signals[name]["vote"] >= 2/3]
        if len(eligible) == 1:
            weights[eligible[0]] = .6
        elif eligible:
            inverse = {name: 1 / signals[name]["floored_annual_vol"] for name in eligible}
            total = sum(inverse.values())
            weights.update({name: .6 * value / total for name, value in inverse.items()})
        if unfiltered or signals["bond"]["vote"] >= 2/3:
            weights["bond"] = .4 if eligible else .6
        return weights
    if policy == "ts_anchor":
        return {name: signals[name]["vote"] / 3 for name in FLOORS}
    if policy in ("inverse_vol_cap60", "ts_inverse_vol"):
        raw = {name: (signals[name]["vote"] if policy == "ts_inverse_vol" else 1.) / signals[name]["floored_annual_vol"]
               for name in FLOORS if policy == "inverse_vol_cap60" or signals[name]["vote"] > 0}
        weights.update(_capped_weights(raw, .6) if raw else {})
    elif policy in ("dual_leader60", "dual_recent60", "dual_return60"):
        eligible = [name for name in ("stock", "gold") if signals[name]["vote"] >= 2/3
                    and (policy != "dual_recent60" or signals[name]["recent_state"] not in ("weakening", "down"))]
        if eligible:
            scores = {name: signals[name]["normalised_return_score"]
                      * (signals[name]["floored_annual_vol"] if policy == "dual_return60" else 1.) for name in eligible}
            leader = min(eligible, key=lambda name: (-scores[name], name))
            weights[leader] = .6
            weights["bond"] = .4 if signals["bond"]["vote"] >= 2/3 else 0.
        elif signals["bond"]["vote"] >= 2/3:
            weights["bond"] = .6
    return weights


def expand_class_weights(data, proposal):
    if set(proposal) != set(FLOORS) or any(not math.isfinite(v) or v < 0 for v in proposal.values()) or sum(proposal.values()) > 1 + 1e-12:
        raise ValueError("Invalid long-only class allocation")
    targets = {}
    for name, codes in data["class_groups"].items():
        for code in codes:
            targets[code] = min(.9, proposal[name] / len(codes)) if name == "bond" else proposal[name] / len(codes)
    return targets


def cross_asset_target(data, policy, index, held, *, stats, exit_mode="legacy", portfolio_state=None):
    if stats["signal_index"] != index or stats["fund_risk"]["signal_index"] != index:
        raise ValueError("Cross-asset statistics use a different date")
    if exit_mode not in ("legacy", "absolute_or_current_risk"):
        raise ValueError("Unknown cross-asset exit mode")
    if exit_mode != "legacy" and policy not in ("dual_return60", "risky_pair60", "risky_pair_static60"):
        raise ValueError("Current-holdings exits are not defined for this policy")
    proposal = class_proposal(policy, stats["signals"])
    if policy == "legacy_into_bonds40":
        targets, old = risk_budget_target(data, TrendBudgetPolicy(policy, "static40", .1), index, held, stats=stats["fund_risk"])
        return targets, old | {"class_signals": stats["signals"], "class_proposal": proposal, "risk_mode": "legacy_bond_transfer"}
    return cash_scaled_class_target(data, policy, index, held, proposal, stats=stats,
                                    exit_mode=exit_mode, portfolio_state=portfolio_state)


def cash_scaled_class_target(data, policy, index, held, proposal, *, stats, exit_mode="legacy", portfolio_state=None):
    """Apply the same current fund covariance and exit policy to a class proposal."""
    if stats["signal_index"] != index or stats["fund_risk"]["signal_index"] != index:
        raise ValueError("Class allocation risk uses a different observation date")
    if exit_mode not in ("legacy", "absolute_or_current_risk"):
        raise ValueError("Unknown cross-asset exit mode")
    if exit_mode != "legacy" and policy not in ("dual_return60", "risky_pair60", "risky_pair_static60"):
        raise ValueError("Current-holdings exits are not defined for this policy")
    expanded = expand_class_weights(data, proposal)
    original_risk = projected_volatility(stats["fund_risk"], expanded)
    scale = min(1., .1 / original_risk) if original_risk > 0 else 1.
    weights = {code: value * scale for code, value in expanded.items()}
    exits = frozenset(code for code in held if weights.get(code, 0) == 0 or scale < 1)
    meta = {"signal_date": data["days"][index], "class_signals": stats["signals"], "class_proposal": proposal,
            "unscaled_fund_targets": expanded, "unconstrained_projected_volatility": original_risk,
            "risky_scale": scale, "projected_volatility": projected_volatility(stats["fund_risk"], weights),
            "risk_mode": "uniform_cash_scale", "target_cash_weight": max(0., 1 - sum(weights.values()))}
    if exit_mode == "absolute_or_current_risk":
        if (not isinstance(portfolio_state, AllocationState) or portfolio_state.signal_index != index
                or portfolio_state.signal_date != data["days"][index]):
            raise ValueError("Current holdings require the actual signal-date portfolio state")
        current = dict(portfolio_state.weights)
        if (set(current) != set(data["prices"])
                or any(isinstance(w, bool) or not math.isfinite(w) or w < 0 for w in current.values())
                or sum(current.values()) > 1 + 1e-12 or {c for c, w in current.items() if w > 0} != set(held)):
            raise ValueError("Invalid current holdings weights")
        current_risk = projected_volatility(stats["fund_risk"], current)
        class_by_code = {c: name for name, members in data["class_groups"].items() for c in members}
        failed = frozenset(c for c in held if policy != "risky_pair_static60"
                           and stats["signals"][class_by_code[c]]["vote"] < 2/3)
        exits = frozenset(held) if current_risk > .1 + 1e-12 else failed
        meta.update(exit_mode=exit_mode, current_fund_weights=current, current_projected_volatility=current_risk,
                    absolute_failure_codes=sorted(failed), risk_exit_codes=sorted(exits))
    return AllocationTargets(weights, exits, enforce_risk_exits=exit_mode != "legacy"), meta
