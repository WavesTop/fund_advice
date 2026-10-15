"""Explicit incumbent choices and elapsed-session partial allocation research."""
import math

from backend.analysis.class_return_forecast import CLASSES, HORIZON, ClassForecastAllocator, proposal_candidates
from backend.analysis.cross_asset_trend import cash_scaled_class_target, expand_class_weights
from backend.analysis.fund_allocation import AllocationTargets
from backend.analysis.trend_risk_budget import projected_volatility

ACTIONS = ("stay_choice", "partial_choice", "intent_choice")


def cash_scale(stats, weights):
    risk = projected_volatility(stats["fund_risk"], weights)
    scale = min(1., .1 / risk) if risk > 0 else 1.
    return {c: w * scale for c, w in weights.items()}


def gradual_candidates(data, stats, current, predictions, fee_bps, fraction, *, anchor=None):
    if (set(current) != set(data["prices"]) or any(not math.isfinite(w) or w < 0 for w in current.values())
            or sum(current.values()) > 1 + 1e-12 or not math.isfinite(fraction) or not 0 < fraction <= 1
            or set(predictions) != set(CLASSES) or any(not math.isfinite(v) or v < -1 for v in predictions.values())
            or not math.isfinite(fee_bps) or not 0 <= fee_bps < 10000):
        raise ValueError("Invalid gradual allocation inputs")
    if anchor is not None and (set(anchor) != set(current)
            or any(isinstance(w, bool) or not math.isfinite(w) or w < 0 for w in anchor.values())
            or sum(anchor.values()) > 1 + 1e-12):
        raise ValueError("Invalid previous allocation intent")
    failed = {c for group, members in data["class_groups"].items() for c in members if stats["signals"][group]["vote"] < 2/3}
    safe = cash_scale(stats, {c: 0. if c in failed else w for c, w in current.items()})
    origin = safe if anchor is None else cash_scale(stats, {c: 0. if c in failed else w for c, w in anchor.items()})
    candidates = {"incumbent": (dict.fromkeys(CLASSES, 0.), safe)}
    candidates["incumbent"][0].update({group: math.fsum(safe[c] for c in members) for group, members in data["class_groups"].items()})
    for name, proposal in proposal_candidates(stats["signals"]).items():
        aim = cash_scale(stats, expand_class_weights(data, proposal))
        mixed = {c: (1 - fraction) * origin[c] + fraction * aim[c] for c in current}
        candidates[name] = (proposal, mixed)
    rows = {}
    for name, (proposal, mixed) in candidates.items():
        targets = cash_scale(stats, mixed)
        gross = math.fsum(math.fsum(targets[c] for c in members) * predictions[group] for group, members in data["class_groups"].items())
        turnover = math.fsum(abs(targets[c] - current[c]) for c in current)
        penalty = fee_bps / 10000 * turnover
        rows[name] = {"proposal": proposal, "unscaled_fund_targets": mixed, "targets": targets,
                      "predicted_gross_return": gross, "turnover_weight": turnover,
                      "ordinary_cost_proxy": penalty, "capital_payoff_proxy": gross - penalty}
    best = max(r["capital_payoff_proxy"] for r in rows.values())
    chosen = min(name for name, row in rows.items() if best - row["capital_payoff_proxy"] <= 1e-10)
    return chosen, rows, safe


class GradualClassAllocator(ClassForecastAllocator):
    def __init__(self, *args, action, **kwargs):
        super().__init__(*args, **kwargs)
        if action not in ACTIONS:
            raise ValueError("Unknown gradual allocation action")
        self.action, self.previous_index = action, None
        self.previous_targets = None

    def __call__(self, index, held, state):
        if self.previous_index is not None and index <= self.previous_index:
            raise ValueError("Gradual decisions require strictly increasing dates")
        previous = self.previous_index
        anchor = self.previous_targets if self.action == "intent_choice" else None
        immediate = super().__call__(index, held, state)
        original = self.trace.pop()
        forecast = original["forecast_choice"]
        elapsed = None if previous is None else index - previous
        fraction = 1. if self.action == "stay_choice" or previous is None else min(1., elapsed / HORIZON)
        if forecast["learning"]["status"] != "fitted":
            target, meta = immediate, {k: v for k, v in original.items() if k not in ("targets", "forecast_choice")}
            chosen, rows, safe = "raw_fallback", {}, {}
        else:
            stats = self.data["cross_stats"][index]
            chosen, rows, safe = gradual_candidates(self.data, stats, dict(state.weights),
                forecast["predicted_gross_returns"], self.fee_bps, fraction, anchor=anchor)
            row = rows[chosen]
            base, meta = cash_scaled_class_target(self.data, "dual_return60", index, held, row["proposal"], stats=stats,
                exit_mode="absolute_or_current_risk", portfolio_state=state)
            risk = projected_volatility(stats["fund_risk"], row["unscaled_fund_targets"])
            scale = min(1., .1 / risk) if risk > 0 else 1.
            meta.update(unscaled_fund_targets=row["unscaled_fund_targets"], unconstrained_projected_volatility=risk,
                risky_scale=scale, projected_volatility=projected_volatility(stats["fund_risk"], row["targets"]),
                target_cash_weight=max(0., 1 - sum(row["targets"].values())))
            target = AllocationTargets(row["targets"], base.risk_exits, base.enforce_risk_exits)
        meta["gradual_choice"] = {"action": self.action, "previous_signal_index": previous,
            "elapsed_sessions": elapsed, "fraction": fraction, "horizon_sessions": HORIZON,
            "chosen": chosen, "candidates": rows, "safe_incumbent": safe,
            "payoff_tie_tolerance": 1e-10, "immediate_decision": original}
        if self.action == "intent_choice":
            meta["gradual_choice"]["previous_fund_targets"] = None if anchor is None else dict(anchor)
        self.previous_index = index
        self.previous_targets = dict(target.weights)
        self.trace.append(meta | {"targets": target.weights})
        return target
