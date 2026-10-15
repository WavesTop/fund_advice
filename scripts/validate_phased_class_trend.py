"""Rebuild staggered signal dates and blended targets from past raw fund marks."""
import math

from scripts.validate_cross_asset_trend import audit_cross_asset_decision, audit_signal_records, rebuild_proposal, rebuild_signals


def audit_phased_decision(entry, data, index, policy, *, start, phase):
    if (policy not in ("dual_return60", "risky_pair60", "risky_pair_static60")
            or any(not isinstance(v, int) or isinstance(v, bool) for v in (start, index, phase))
            or not 252 <= start <= index < len(data["days"]) or not 0 <= phase < 20):
        raise ValueError("Invalid independently audited phased strategy")
    mix = entry["phase_mix"]
    offsets = sorted((offset + phase) % 20 for offset in (0, 5, 10, 15))
    wanted = []
    for offset in offsets:
        eligible = [i for i in range(start + offset, index + 1, 20)]
        wanted.append(eligible[-1] if eligible else start)
    if (mix["start_index"] != start or mix["phase"] != phase or mix["cycle_sessions"] != 20
            or mix["offsets"] != offsets or mix["view_indices"] != wanted or len(mix["views"]) != 4
            or mix["latest_absolute_gate_applied"] is not (policy != "risky_pair_static60")):
        raise ValueError("Phase views do not match the registered historical calendar")
    proposals = []
    for at, view in zip(wanted, mix["views"]):
        if view["signal_index"] != at or view["signal_date"] != data["days"][at]:
            raise ValueError("Phased view date is not its past calendar observation")
        _, signals = rebuild_signals(data, at)
        audit_signal_records(view["class_signals"], signals)
        proposal = rebuild_proposal(policy, signals)
        if (set(view["class_proposal"]) != set(proposal)
                or any(not math.isfinite(view["class_proposal"][c]) or abs(view["class_proposal"][c] - v) > 1e-9 for c, v in proposal.items())):
            raise ValueError("A phase proposal cannot be rebuilt from its own past signal")
        proposals.append(proposal)
    raw = {name: math.fsum(p[name] for p in proposals) / 4 for name in ("stock", "gold", "bond")}
    if (set(mix["raw_blended_proposal"]) != set(raw)
            or any(not math.isfinite(mix["raw_blended_proposal"][c]) or abs(mix["raw_blended_proposal"][c] - v) > 1e-9 for c, v in raw.items())):
        raise ValueError("Staggered blend is not the arithmetic mean of the four proposals")
    _, current = rebuild_signals(data, index)
    proposal = {c: w if policy == "risky_pair_static60" or current[c]["vote"] >= 2/3 else 0. for c, w in raw.items()}
    audit_cross_asset_decision(entry, data, index, policy, exit_mode="absolute_or_current_risk", expected_proposal=proposal)
