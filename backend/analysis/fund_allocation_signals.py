"""Predeclared trend/momentum allocation families using past observations only."""
from dataclasses import asdict, dataclass
import math
import statistics

from backend.analysis.fund_allocation import AllocationTargets


@dataclass(frozen=True)
class AllocationPlan:
    name: str
    family: str
    top: int = 5
    inverse_volatility: bool = False
    correlation_cap: float | None = None
    volatility_target: float | None = None
    benchmark_gate: bool = False
    asset_cap: float = .35


def plans():
    return [AllocationPlan("trend_equal5", "trend"),
            AllocationPlan("trend_invvol5", "trend", inverse_volatility=True),
            AllocationPlan("trend_diverse5", "trend", inverse_volatility=True, correlation_cap=.8),
            AllocationPlan("trend_regime5", "trend", benchmark_gate=True),
            AllocationPlan("trend_vol12", "trend", inverse_volatility=True, volatility_target=.12),
            AllocationPlan("momentum_top3", "momentum", top=3),
            AllocationPlan("momentum_top5", "momentum"),
            AllocationPlan("momentum_invvol5", "momentum", inverse_volatility=True),
            AllocationPlan("timeseries_equal", "timeseries", top=100),
            AllocationPlan("timeseries_invvol", "timeseries", top=100, inverse_volatility=True),
            AllocationPlan("timeseries_vol12", "timeseries", top=100, inverse_volatility=True, volatility_target=.12),
            AllocationPlan("core_satellite", "core_satellite")]


def extend_features(data):
    for index in range(252, len(data["days"])):
        for entry in data["frames"][index]["entries"]:
            values = data["marks"][entry["code"]]
            entry["returns"] = {window: values[index] / values[index - window] - 1 for window in (63, 126, 252)}
            entry["ma200"] = statistics.fmean(values[index - 199:index + 1])
            entry["daily60"] = [math.log(values[i] / values[i - 1]) for i in range(index - 59, index + 1)]
            entry["annual_vol60"] = statistics.pstdev(entry["daily60"]) * math.sqrt(252)


def _capped_weights(raw, cap):
    # With too few assets, the unallocatable remainder stays in cash.
    result, pending, remaining = {}, dict(raw), 1.
    while pending:
        total = sum(pending.values())
        proposed = {code: remaining * value / total for code, value in pending.items()}
        capped = [code for code, weight in proposed.items() if weight > cap]
        if not capped:
            result.update(proposed)
            break
        for code in capped:
            result[code] = cap
            remaining -= cap
            pending.pop(code)
    return result


def _correlation(left, right):
    sx, sy = statistics.pstdev(left), statistics.pstdev(right)
    if sx < 1e-12 or sy < 1e-12:
        return 0.
    mx, my = statistics.fmean(left), statistics.fmean(right)
    return statistics.fmean((x - mx) * (y - my) for x, y in zip(left, right)) / (sx * sy)


def allocation_at(data, plan, index, held):
    entries = {e["code"]: e for e in data["frames"][index]["entries"]}
    if plan.family in ("hold", "equal"):
        if plan.family == "hold" and held:
            return None
        return AllocationTargets(dict.fromkeys(entries, 1 / len(entries)))
    eligible, quality = [], {}
    context = data["frames"][index]["context"]
    for code, entry in entries.items():
        if plan.family in ("trend", "core_satellite"):
            score = .2 * entry["scores"]["short"] + .4 * entry["scores"]["medium"] + .4 * entry["scores"]["long"]
            ready = score >= 55 and entry["price"] > entry["ma60"] and entry["return30_pct"] > 0
            quality[code] = score
        elif plan.family == "momentum":
            score = .5 * entry["returns"][63] + .3 * entry["returns"][126] + .2 * entry["returns"][252]
            ready = score > 0 and entry["price"] > entry["ma200"]
            quality[code] = score
        elif plan.family == "timeseries":
            score = sum(value > 0 for value in entry["returns"].values()) / 3
            ready = score >= 2 / 3
            quality[code] = score
        else:
            raise ValueError("Unknown allocation family")
        ready = ready and (not plan.benchmark_gate or context.get("benchmark_above_ma200") is True or entry["defensive_asset"])
        if ready:
            eligible.append(code)
    ranked = sorted(eligible, key=lambda code: (-quality[code], code))
    # Retain incumbents in the rank buffer; rank churn alone is a soft exit.
    incumbents = [code for code in ranked[:plan.top + 2] if code in held]
    candidates = incumbents + [code for code in ranked if code not in incumbents]
    selected = []
    for code in candidates:
        if plan.correlation_cap is not None and any(_correlation(entries[code]["daily60"], entries[other]["daily60"])
                                                    > plan.correlation_cap for other in selected):
            continue
        selected.append(code)
        if len(selected) == plan.top:
            break
    raw = {code: 1 / max(.05, entries[code]["annual_vol60"]) if plan.inverse_volatility else 1. for code in selected}
    weights = _capped_weights(raw, plan.asset_cap) if raw else {}
    if plan.family == "timeseries":
        # Breadth controls exposure rather than concentrating into a lone survivor.
        breadth = len(eligible) / len(entries)
        weights = {code: value * breadth for code, value in weights.items()}
    if plan.family == "core_satellite":
        core = [code for code, entry in entries.items() if not entry["defensive_asset"]]
        weights = {code: .5 * weights.get(code, 0) + (.5 / len(core) if code in core else 0)
                   for code in entries} if core else {code: .5 * value for code, value in weights.items()}
        weights = {code: min(plan.asset_cap, weight) for code, weight in weights.items()}
    if plan.volatility_target and weights:
        centered = {code: [r - statistics.fmean(entries[code]["daily60"]) for r in entries[code]["daily60"]]
                    for code in weights}
        portfolio_vol = math.sqrt(statistics.fmean(sum(weight * centered[code][i] for code, weight in weights.items()) ** 2
                                                  for i in range(60)) * 252)
        scale = min(1., plan.volatility_target / max(.01, portfolio_vol))
        weights = {code: value * scale for code, value in weights.items()}
    risk_exits = frozenset(code for code in held if code not in eligible and weights.get(code, 0) == 0)
    return AllocationTargets(weights, risk_exits)


def plan_document(plan):
    return asdict(plan)
