"""Causal benchmark-direction clocks, separate from retrospective market labels."""
from bisect import bisect_left
import math
import statistics

INTERVALS = {"up": 20, "down": 5, "neutral": 10}


def market_context(benchmark, days):
    reference_days, prices = benchmark["days"], benchmark["prices"]
    if len(reference_days) != len(prices) or reference_days != sorted(set(reference_days)):
        raise ValueError("Market reference requires ascending complete observation dates")
    result = {}
    for i, day in enumerate(days):
        position = bisect_left(reference_days, day)
        if position >= len(reference_days) or reference_days[position] != day:
            raise ValueError("Market reference missing a fund observation day")
        if position < 199:
            result[i] = {"state": "unknown", "signal_date": day}
            continue
        history = prices[position - 199:position + 1]
        if any(not math.isfinite(p) or p <= 0 for p in history):
            raise ValueError("Market reference contains invalid past prices")
        current, average = history[-1], statistics.fmean(history)
        change = current / history[-64] - 1
        state = ("up" if current > average * (1 + 1e-12) and change > 1e-12 else
                 "down" if current < average * (1 - 1e-12) and change < -1e-12 else "neutral")
        result[i] = {"state": state, "signal_date": day, "price": current, "ma200": average,
                     "return63": change, "reference_start": reference_days[position - 199]}
    return result


def adaptive_review_indices(context, start, end):
    if not isinstance(start, int) or isinstance(start, bool) or not 0 <= start < end:
        raise ValueError("Adaptive review interval invalid")
    reviews, last = {start}, start
    for index in range(start + 1, end + 1):
        state = context[index]["state"]
        if state not in INTERVALS:
            raise ValueError("Adaptive review lacks benchmark history")
        if index - last >= INTERVALS[state]:
            reviews.add(index)
            last = index
    return frozenset(reviews)
