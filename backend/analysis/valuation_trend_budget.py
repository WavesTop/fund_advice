"""Conditional historical valuation context; no current-constituent backfill."""
from datetime import date
import math

from backend.analysis.trend_risk_budget import TrendBudgetPolicy, risk_budget_target

POLICIES = ("static40_vol10", "static50_vol10", "static30_vol10", "quality_vol10", "value_vol10",
            "value_confirmed_vol10", "value_hybrid_vol10")


class ValuationHistory:
    def __init__(self, indices, calendar, *, cutoff, required_codes=None):
        self.days = [d.isoformat() for d in calendar.sessions(date(2009, 1, 1), date.fromisoformat(cutoff))]
        self.day_indexes = {d: i for i, d in enumerate(self.days)}
        self.values, self.coverage = {}, {}
        required = {"000300", "000905"} if required_codes is None else set(required_codes)
        if not required or set(indices) != required:
            raise ValueError("Valuation context requires exactly its declared CSI indices")
        for code, item in indices.items():
            values = [None] * len(self.days)
            seen, excluded = set(), []
            for row in item["rows"]:
                day = date.fromisoformat(row["date"])
                if day.isoformat() != row["date"] or row["date"] in seen or row["date"] > cutoff:
                    raise ValueError("Duplicate, noncanonical or post-cutoff valuation date")
                seen.add(row["date"])
                value = row["peg"]
                if value is not None and (isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0):
                    raise ValueError("Invalid historical candidate valuation")
                if not calendar.is_session(day):
                    excluded.append(row["date"])
                    continue
                values[self.day_indexes[row["date"]]] = float(value) if value is not None else None
            self.values[code] = values
            self.coverage[code] = {"excluded_non_sessions": excluded, "missing_sessions": [d for d in self.days if d not in seen],
                                   "null_session_values": sum(v is None for v in values),
                                   "first_positive_date": next((d for d, v in zip(self.days, values) if v is not None), None)}

    def state(self, asof, *, lag_sessions=1):
        if isinstance(lag_sessions, bool) or not isinstance(lag_sessions, int) or lag_sessions < 1:
            raise ValueError("Valuation lag must be positive trading sessions")
        if asof not in self.day_indexes:
            raise ValueError("Valuation signal date is not a covered trading session")
        known = self.day_indexes[asof] - lag_sessions
        entries = {}
        for code, values in self.values.items():
            quote_index = next((i for i in range(known, max(-1, known - 4), -1) if i >= 0 and values[i] is not None), None)
            entry = {"status": "unavailable", "quote_date": None, "value": None, "percentile": None,
                     "reference_count": 0, "reference_start": None, "reference_end": None, "coverage_fraction": None}
            if quote_index is not None:
                start = max(0, quote_index - 1260)
                first = next((i for i in range(start, quote_index) if values[i] is not None), quote_index)
                reference = [v for v in values[first:quote_index] if v is not None]
                count = len(reference)
                fraction = count / (quote_index - first) if quote_index > first else 0.
                entry.update(status="insufficient_reference", quote_date=self.days[quote_index], value=values[quote_index],
                             reference_count=count, reference_start=self.days[first] if first < quote_index else None,
                             reference_end=self.days[quote_index - 1] if quote_index > first else None, coverage_fraction=fraction)
                if count >= 252 and fraction >= .95:
                    value = values[quote_index]
                    percentile = (sum(v < value for v in reference) + .5 * sum(v == value for v in reference)) / count
                    entry.update(status="ready", percentile=percentile)
            entries[code] = entry
        ready = all(v["status"] == "ready" for v in entries.values())
        return {"signal_date": asof, "lag_sessions": lag_sessions, "status": "ready" if ready else "unavailable",
                "indices": entries, "percentile": sum(v["percentile"] for v in entries.values()) / len(entries) if ready else None}


def valuation_budget_target(data, name, index, held, state, *, stats):
    if name not in POLICIES or state["signal_date"] != data["days"][index]:
        raise ValueError("Unknown valuation policy or mismatched signal date")
    quality_budget = .35 + .25 * stats["quality"]
    proposed = {"static50_vol10": .5, "static30_vol10": .3, "quality_vol10": quality_budget}.get(name, .4)
    if name == "value_hybrid_vol10":
        proposed = quality_budget
    if state["status"] == "ready":
        percentile = state["percentile"]
        if not math.isfinite(percentile) or not 0 <= percentile <= 1:
            raise ValueError("Invalid valuation percentile")
        value_budget = .6 - .4 * percentile
        if name == "value_vol10":
            proposed = value_budget
        elif name == "value_hybrid_vol10":
            proposed = (value_budget + quality_budget) / 2
        elif name == "value_confirmed_vol10":
            if percentile <= .2 and stats["quality"] > 0:
                proposed = .5
            elif percentile >= .8 and stats["quality"] < 0:
                proposed = .3
    target, meta = risk_budget_target(data, TrendBudgetPolicy(name, "static40", .10), index, held,
                                      stats=stats, stock_budget_override=proposed)
    return target, meta | {"valuation": state}
