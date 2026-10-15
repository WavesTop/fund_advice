"""Experimental direct path-continuation ordering, not calibrated probabilities."""
import math

from backend.analysis.sector_direction_screen import gate_members
from backend.analysis.sector_trend import midpoint_percentiles


def continuation_choices(entries, predicted, historical):
    codes = [entry["code"] for entry in entries]
    gates = gate_members(entries)
    for values in (predicted, historical):
        if (set(values) != set(codes) or any(len(values[code]) != 2 for code in codes)
                or any(not math.isfinite(value) for code in codes for value in values[code])):
            raise ValueError("Continuation predictions require the complete finite peer pool")

    def order(values, allowed):
        first = midpoint_percentiles([values[code][0] for code in codes])
        second = midpoint_percentiles([values[code][1] for code in codes])
        scores = {code: min(a, b) for code, a, b in zip(codes, first, second)}
        return sorted(allowed, key=lambda code: (-scores[code], code))[:10]

    positive = [code for code in gates["state"] if all(value > 0 for value in predicted[code])]
    by_code = {entry["code"]: entry for entry in entries}
    count = len(positive[:10])
    return {"continuation_top10": order(predicted, codes),
            "direction_continuation": order(predicted, gates["direction"]),
            "state_continuation": order(predicted, gates["state"]),
            "state_historical_continuation": order(historical, gates["state"]),
            "state_positive_continuation": order(predicted, positive),
            "positive_count_state_return": sorted(gates["state"], key=lambda code: (-by_code[code]["return_pct"], code))[:count],
            "positive_count_state_low_vol": sorted(gates["state"], key=lambda code: (by_code[code]["daily_volatility_pct"], code))[:count],
            "positive_count_universe_return": sorted(codes, key=lambda code: (-by_code[code]["return_pct"], code))[:count]}
