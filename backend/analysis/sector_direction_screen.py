"""Experimental eligibility-first sector lists; keep model preference separate."""
import math

from backend.analysis.sector_trend import midpoint_percentiles

GATES = ("direction", "state")
SORTS = ("return", "rank", "maximin", "low_vol")
DIRECTION_FIELDS = ("return_pct", "fitted_return_pct", "recent_fitted_return_pct",
                    "fast_ma_bias_pct", "fast_slow_ma_spread_pct")


def gate_members(entries):
    codes = [entry["code"] for entry in entries]
    if len(codes) < 2 or len(codes) != len(set(codes)):
        raise ValueError("Direction screen requires distinct comparable peers")
    fields = (*DIRECTION_FIELDS, "daily_volatility_pct")
    if any(not math.isfinite(entry[key]) for entry in entries for key in fields):
        raise ValueError("Invalid direction-screen descriptor")
    return {"direction": [entry["code"] for entry in entries if all(entry[key] > 0 for key in DIRECTION_FIELDS)],
            "state": [entry["code"] for entry in entries if entry["state"] in ("steady_up", "strengthening")]}


def screened_choices(entries, predicted):
    gates = gate_members(entries)
    codes = [entry["code"] for entry in entries]
    if set(predicted) != set(codes) or any(not math.isfinite(predicted[code][key])
                                          for code in codes for key in ("return_rank", "safety_rank")):
        raise ValueError("Screen predictions differ from the full peer pool")
    returns = midpoint_percentiles([predicted[code]["return_rank"] for code in codes])
    safety = midpoint_percentiles([predicted[code]["safety_rank"] for code in codes])
    scores = {"return": {entry["code"]: entry["return_pct"] for entry in entries},
              "rank": {code: predicted[code]["return_rank"] for code in codes},
              "maximin": {code: min(a, b) for code, a, b in zip(codes, returns, safety)},
              "low_vol": {entry["code"]: -entry["daily_volatility_pct"] for entry in entries}}
    selected = {f"{gate}_{sort}": sorted(allowed, key=lambda code: (-scores[sort][code], code))[:10]
                for gate, allowed in gates.items() for sort in SORTS}
    for gate, allowed in gates.items():
        selected[f"{gate}_count_return"] = sorted(codes, key=lambda code: (-scores["return"][code], code))[:min(10, len(allowed))]
    return gates, selected
