"""Theme-relative valuation/price tilts with neutral missing-data allocations."""
import math

from backend.analysis.fund_allocation_signals import _capped_weights
from backend.analysis.trend_risk_budget import TrendBudgetPolicy, risk_budget_target
from backend.analysis.valuation_trend_budget import ValuationHistory

POLICIES = ("equal40", "trend_tilt40", "trend_matched40", "theme_value40", "theme_blend40", "theme_confirmed40")


class ThemeValuationHistory:
    def __init__(self, payload, evidence, calendar):
        items = payload["items"]
        self.mapping = {item["fund_code"]: item["relation"]["index_code"] for item in items}
        if len(self.mapping) != len(items) or len(set(self.mapping.values())) != len(items):
            raise ValueError("Theme fund/index mapping must be unique")
        indices, self.eligibility = {}, {}
        for item in items:
            code = item["relation"]["index_code"]
            published = item["index_publication_date"]
            if any(row["date"] < published for row in item["rows"]):
                raise ValueError("Theme valuation includes unpublished backtested index history")
            check = evidence["checks"][item["fund_code"]]
            usable = check["status"] == "provisional_dated_factsheet_match"
            if check["index_code"] != code:
                raise ValueError("Theme semantic evidence belongs to a different index")
            indices[code] = {"rows": item["rows"] if usable else []}
            self.eligibility[item["fund_code"]] = {"status": check["status"], "positive_field_cross_check": usable,
                                                   "history_collection_status": item["status"]}
        self.history = ValuationHistory(indices, calendar, cutoff=payload["cutoff"], required_codes=set(indices))

    def state(self, day, *, lag_sessions=1):
        context = self.history.state(day, lag_sessions=lag_sessions)
        return {"signal_date": day, "lag_sessions": lag_sessions,
                "funds": {fund: context["indices"][code] | {"index_code": code} for fund, code in self.mapping.items()}}


def stock_distribution(stocks, quality, state, policy):
    if policy not in POLICIES or not stocks or set(quality) != set(stocks):
        raise ValueError("Theme tilt requires its declared stock peers and price quality")
    if any(not math.isfinite(v) or not -1 <= v <= 1 for v in quality.values()):
        raise ValueError("Invalid per-theme signed regression quality")
    n = len(stocks)
    multipliers = dict.fromkeys(stocks, 1.)
    known = []
    for code in stocks:
        observation = state["funds"][code]
        ready = observation["status"] == "ready"
        percentile = observation["percentile"]
        if ready and (percentile is None or not math.isfinite(percentile) or not 0 <= percentile <= 1):
            raise ValueError("Invalid own-theme valuation percentile")
        if policy == "trend_tilt40" or (ready and policy not in ("equal40",)):
            known.append(code)
        if policy in ("trend_tilt40", "trend_matched40") and (ready or policy == "trend_tilt40"):
            multipliers[code] = 1 + .5 * quality[code]
        elif ready and policy == "theme_value40":
            multipliers[code] = 1.5 - percentile
        elif ready and policy == "theme_blend40":
            multipliers[code] = 1 + .25 * quality[code] + .5 * (.5 - percentile)
        elif ready and policy == "theme_confirmed40":
            if percentile <= .2 and quality[code] > 0:
                multipliers[code] = 1.5
            elif percentile >= .8 and quality[code] < 0:
                multipliers[code] = .5
    weights = dict.fromkeys(stocks, 1 / n)
    if known:
        subgroup = _capped_weights({code: multipliers[code] for code in known}, min(1., 2 / len(known)))
        weights.update({code: value * len(known) / n for code, value in subgroup.items()})
    return weights, {"known_funds": known, "multipliers": multipliers, "distribution": weights}


def theme_tilt_target(data, policy, index, held, state, *, stats):
    if state["signal_date"] != data["days"][index]:
        raise ValueError("Theme valuation and price signal dates differ")
    distribution, tilt = stock_distribution(stats["stocks"], stats["stock_quality"], state, policy)
    target, meta = risk_budget_target(data, TrendBudgetPolicy(policy, "static40", .1), index, held,
                                      stats=stats, stock_distribution=None if policy == "equal40" else distribution)
    meta["stock_distribution"] = dict(distribution)
    return target, meta | {"theme_valuation": state, "theme_tilt": tilt}
