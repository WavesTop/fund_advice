"""Completed-calendar-month trend observations, not investment return forecasts."""
from bisect import bisect_right
from datetime import date
import math
import statistics


def completed_month_indices(days, calendar):
    if not days or days != sorted(set(days)):
        raise ValueError("Monthly observations require unique ascending dates")
    first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    # Include the remainder of the current year to avoid calling a partial
    # final data month a completed exchange month.
    sessions = calendar.sessions(first, date(last.year, 12, 31))
    ends = {}
    for session in sessions:
        ends[session.isoformat()[:7]] = session.isoformat()
    return tuple(i for i, day in enumerate(days) if ends.get(day[:7]) == day)


def monthly_class_signals(data, index, month_indices):
    if not isinstance(index, int) or not 0 <= index < len(data["days"]):
        raise ValueError("Monthly observation index outside fund history")
    known = month_indices[:bisect_right(month_indices, index)]
    if len(known) < 10:
        return {name: {"known": False, "observed_months": len(known)} for name in data["class_proxies"]}
    used = known[-10:]
    result = {}
    for name, proxy in data["class_proxies"].items():
        values = [float(proxy[i]) for i in used]
        if any(not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("Invalid completed monthly class price")
        level = values[-1]
        average = statistics.fmean(values)
        result[name] = {"known": True, "observed_months": len(known), "source_date": data["days"][used[-1]],
                        "reference_dates": [data["days"][i] for i in used], "price": level, "ma10": average,
                        "bias": level / average - 1, "enabled": level > average * (1 + 1e-12)}
    return result
