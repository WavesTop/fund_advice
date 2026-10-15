"""Combine staggered past class proposals in one cash-funded FIFO account."""
import math

from backend.analysis.cross_asset_trend import FLOORS, cash_scaled_class_target, class_proposal

CYCLE = 20
OFFSETS = (0, 5, 10, 15)
POLICIES = ("dual_return60", "risky_pair60", "risky_pair_static60")


def phase_view_indices(start, index, phase=0):
    if (any(not isinstance(v, int) or isinstance(v, bool) for v in (start, index, phase))
            or not 0 <= start <= index or not 0 <= phase < CYCLE):
        raise ValueError("Invalid staggered trend observation interval")
    offsets = tuple(sorted((offset + phase) % CYCLE for offset in OFFSETS))
    return tuple(start if index < start + offset else
                 start + offset + ((index - start - offset) // CYCLE) * CYCLE for offset in offsets)


def phased_class_target(data, policy, index, held, *, start, phase=0, portfolio_state):
    if policy not in POLICIES or start < 252 or not start <= index < len(data["days"]):
        raise ValueError("Phased class trend requires its declared policy and warm history")
    indices = phase_view_indices(start, index, phase)
    views = []
    for at in indices:
        stats = data["cross_stats"][at]
        if stats["signal_index"] != at:
            raise ValueError("A staggered view was computed on the wrong date")
        views.append({"signal_index": at, "signal_date": data["days"][at], "class_signals": stats["signals"],
                      "class_proposal": class_proposal(policy, stats["signals"])})
    current = data["cross_stats"][index]
    raw = {name: math.fsum(view["class_proposal"][name] for view in views) / len(views) for name in FLOORS}
    proposal = {name: value if policy == "risky_pair_static60" or current["signals"][name]["vote"] >= 2/3 else 0.
                for name, value in raw.items()}
    target, meta = cash_scaled_class_target(data, policy, index, held, proposal, stats=current,
                                            exit_mode="absolute_or_current_risk", portfolio_state=portfolio_state)
    meta["phase_mix"] = {"start_index": start, "phase": phase, "cycle_sessions": CYCLE,
                         "offsets": sorted((offset + phase) % CYCLE for offset in OFFSETS),
                         "view_indices": list(indices), "views": views, "raw_blended_proposal": raw,
                         "latest_absolute_gate_applied": policy != "risky_pair_static60"}
    return target, meta
