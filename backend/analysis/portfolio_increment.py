"""Sector-overweight economic outcomes under actual cash-funded lease rules."""
import math
import statistics

from backend.analysis.cross_asset_trend import class_statistics, prepare_class_proxies
from backend.analysis.fund_allocation import AllocationState, AllocationTargets
from backend.analysis.trend_risk_budget import projected_volatility

HORIZON = 20
MODES = ("state_only", "trend")


def prepare_economic(data):
    if "class_groups" not in data:
        prepare_class_proxies(data)
    if "cross_stats" not in data:
        data["cross_stats"] = {i: class_statistics(data, i) for i in range(252, len(data["days"]))}
    data["economic_stats"] = {}
    for i in range(252, len(data["days"])):
        votes = {c: sum(math.log(p[i] / p[i - w]) > 1e-12 for w in (21, 63, 252))/3 for c, p in data["marks"].items()}
        data["economic_stats"][i] = {"signal_index": i, "votes": votes, "risk": data["cross_stats"][i]["fund_risk"]}
    return data


def initial_target(data, index, candidate=None, *, kind="core"):
    if kind not in ("core", "static_core", "inverse_static") or not 252 <= index < len(data["days"]):
        raise ValueError("Invalid economic allocation policy or date")
    groups, stats = data["class_groups"], data["economic_stats"][index]
    if stats["signal_index"] != index or stats["risk"]["signal_index"] != index:
        raise ValueError("Economic entry risk cache has a different observation date")
    stocks, gold, bonds = groups["stock"], groups["gold"], groups["bond"]
    if candidate is not None and (kind != "core" or candidate not in [*stocks, "cash"]):
        raise ValueError("Economic satellite must be a local stock fund or cash")
    weights = dict.fromkeys(data["prices"], 0.)
    if kind == "inverse_static":
        signals = data["cross_stats"][index]["signals"]
        stock_vol, gold_vol = signals["stock"]["floored_annual_vol"], signals["gold"]["floored_annual_vol"]
        stock_budget = .6 * gold_vol/(stock_vol + gold_vol)
        gold_budget = .6 - stock_budget
    else:
        stock_budget, gold_budget = (.4 if candidate is None else .2), .2
    for c in stocks:
        weights[c] = min(.35, stock_budget/len(stocks) + (.2 if c == candidate else 0.))
    for c in gold:
        weights[c] = gold_budget/len(gold)
    for c in bonds:
        weights[c] = .4/len(bonds)
    if kind == "core":
        weights = {c: w if stats["votes"][c] >= 2/3 else 0. for c, w in weights.items()}
    risk = projected_volatility(stats["risk"], weights)
    scale = min(1., .1/risk) if risk else 1.
    return {c: w*scale for c, w in weights.items()}, {"unscaled_fund_targets": weights,
        "unconstrained_projected_volatility": risk, "risky_scale": scale}


def economic_features(data, candidate, index, mode):
    choices = [*data["class_groups"]["stock"], "cash"]
    if candidate not in choices or mode not in MODES or not 252 <= index < len(data["days"]):
        raise ValueError("Invalid economic feature request")
    stats, groups = data["economic_stats"][index], data["class_groups"]
    _, core = initial_target(data, index)
    _, tilted = initial_target(data, index, candidate)
    own_vol = 0. if candidate == "cash" else next(e for e in data["frames"][index]["entries"] if e["code"] == candidate)["annual_vol60"]
    base = [float(candidate == c) for c in choices] + [float(candidate == "cash" or stats["votes"][candidate] >= 2/3),
        *(statistics.fmean(stats["votes"][c] >= 2/3 for c in groups[g]) for g in ("stock", "gold", "bond")),
        core["risky_scale"], tilted["risky_scale"], own_vol]
    if mode == "state_only":
        return base
    vectors = [data["vectors"][index][c] for c in groups["stock"]] if candidate == "cash" else [data["vectors"][index][candidate]]
    # Volatility is already a control feature; do not duplicate its ridge basis.
    return base + [statistics.fmean(v[j] for v in vectors) for j in range(12) if j != 7]


def safety_target(data, index, state, *, filtered=True):
    stats, current = data["economic_stats"][index], dict(state.weights)
    if stats["signal_index"] != index or stats["risk"]["signal_index"] != index:
        raise ValueError("Economic safety risk cache has a different observation date")
    risk = projected_volatility(stats["risk"], current)
    failed = {c for c, w in current.items() if w > 0 and filtered and stats["votes"][c] < 2/3}
    exits = {c for c, w in current.items() if w > 0} if risk > .1 + 1e-12 else failed
    if not exits:
        return None, risk, []
    kept = {c: 0. if c in failed else w for c, w in current.items()}
    remaining_risk = projected_volatility(stats["risk"], kept)
    scale = min(1., .1/remaining_risk) if remaining_risk else 1.
    return {c: w*scale for c, w in kept.items()}, risk, sorted(exits)


class EconomicLeaseAllocator:
    def __init__(self, data, *, policy="core", forecaster=None, mode=None, fixed_candidate=None, start=252, phase=0, lag=1):
        if (policy not in ("core", "static_core", "inverse_static", *MODES)
                or any(not isinstance(v, int) or isinstance(v, bool) or v < minimum for v, minimum in ((lag, 1), (phase, 0)))):
            raise ValueError("Invalid economic lease configuration")
        if policy in MODES and (mode != policy or forecaster is None):
            raise ValueError("Economic prediction policy needs its local matured model")
        if fixed_candidate is not None and policy != "core":
            raise ValueError("Fixed satellites are label-only core allocations")
        self.data, self.policy, self.forecaster, self.mode = data, policy, forecaster, mode
        self.fixed_candidate, self.lag = fixed_candidate, lag
        self.deadline, self.next_entry, self.cycle_entry = None, start + phase, None
        self.trace, self.checks = [], []

    def __call__(self, index, held, state):
        data = self.data
        if (not isinstance(state, AllocationState) or state.signal_index != index or state.signal_date != data["days"][index]
                or set(state.weights) != set(data["prices"]) or any(isinstance(w, bool) or not math.isfinite(w) or w < 0 for w in state.weights.values())
                or sum(state.weights.values()) > 1 + 1e-12 or {c for c, w in state.weights.items() if w > 0} != set(held)):
            raise ValueError("Economic decisions require the actual date and holdings")
        current = dict(state.weights)
        stats = data["economic_stats"][index]
        record = {"signal_date": data["days"][index], "signal_index": index, "policy": self.policy,
            "deadline_before": self.deadline, "next_entry_before": self.next_entry, "cycle_entry_before": self.cycle_entry,
            "current_fund_weights": current, "current_projected_volatility": projected_volatility(stats["risk"], current),
            "own_votes": stats["votes"], "risk_exit_codes": [], "learning": None, "predictions": {}}
        target = None
        if self.deadline is None and index < self.next_entry:
            record["action"] = "wait_cash"
        elif self.deadline is None:
            if held:
                raise ValueError("New economic lease cannot spend or replace unsettled old positions")
            choice = self.fixed_candidate
            if self.policy in MODES:
                model, learning = self.forecaster.at(data["days"][index])
                record["learning"] = learning
                if model is not None:
                    choices = [*data["class_groups"]["stock"], "cash"]
                    predicted = dict(zip(choices, (float(v) for v in model.predict([economic_features(data, c, index, self.mode) for c in choices])[:, 0])))
                    if any(not math.isfinite(v) for v in predicted.values()):
                        raise ValueError("Invalid economic increment predictions")
                    allowed = {c: v for c, v in predicted.items() if c == "cash" or stats["votes"][c] >= 2/3}
                    best = max([0., *allowed.values()])
                    candidates = [c for c, v in allowed.items() if best - v <= 1e-10]
                    choice = min(candidates) if best > 1e-10 and candidates else None
                    record["predictions"] = predicted
            kind = self.policy if self.policy in ("static_core", "inverse_static") else "core"
            weights, entry = initial_target(data, index, choice, kind=kind)
            self.cycle_entry, self.deadline = index, index + HORIZON
            self.next_entry = self.deadline + self.lag + 2
            record.update(action="entry", choice=choice, kind=kind, **entry)
            target = AllocationTargets(weights)
        elif index >= self.deadline:
            record["action"] = "expiry"
            target = AllocationTargets(dict.fromkeys(current, 0.))
            self.deadline = None
            self.next_entry = max(self.next_entry, index + self.lag + 2)
        else:
            weights, risk, exits = safety_target(data, index, state, filtered=self.policy not in ("static_core", "inverse_static"))
            if weights is None:
                record["action"] = "hold"
            else:
                record.update(action="risk", risk_exit_codes=exits)
                if index + self.lag > self.deadline:
                    record["action"] = "early_expiry"
                    weights = dict.fromkeys(current, 0.)
                    self.deadline = None
                target = AllocationTargets(weights, frozenset(exits), enforce_risk_exits=True)
        record.update(deadline_after=self.deadline, next_entry_after=self.next_entry, cycle_entry_after=self.cycle_entry,
                      targets=None if target is None else dict(target.weights))
        if target is not None:
            record["projected_volatility"] = projected_volatility(stats["risk"], target.weights)
            self.trace.append(record)
        self.checks.append(record)
        return target
