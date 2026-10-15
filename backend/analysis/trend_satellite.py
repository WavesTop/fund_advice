"""Incumbent-aware sector tilts over a continuing inverse-risk core."""
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import math

import numpy as np

from backend.analysis.cross_asset_trend import class_proposal, expand_class_weights
from backend.analysis.fund_allocation import AllocationTargets
from backend.analysis.trend_risk_budget import projected_volatility


@dataclass(frozen=True)
class SatellitePolicy:
    name: str
    exit_rule: str = "score"
    cooldown: int = 5
    check_sessions: int = 5
    random_seed: int | None = None
    market_gate: str = "none"
    exit_on_gate: bool = False
    sizing: str = "whole"
    risk_repair: str = "immediate"
    repair_scope: str = "combined"
    basket_size: int = 1
    member_exit: str = "together"
    restore_defensive: bool = False

    def validate(self):
        if self.exit_rule not in ("none", "fixed20", "fixed60", "score", "price"):
            raise ValueError("Unknown satellite exit rule")
        if any(not isinstance(v, int) or isinstance(v, bool) or v < low
               for v, low in ((self.cooldown, 0), (self.check_sessions, 1))):
            raise ValueError("Invalid satellite observation/cooldown clock")
        if self.random_seed is not None and (not isinstance(self.random_seed, int)
                or isinstance(self.random_seed, bool) or self.random_seed < 0 or self.exit_rule == "none"):
            raise ValueError("Random selection needs a nonnegative integer seed and a satellite policy")
        if (self.market_gate not in ("none", "direction", "wide", "competition") or not isinstance(self.exit_on_gate, bool)
                or self.exit_on_gate and self.market_gate == "none"
                or self.market_gate != "none" and self.exit_rule == "none"):
            raise ValueError("Invalid satellite market gate or gate-exit policy")
        if self.sizing not in ("whole", "headroom"):
            raise ValueError("Unknown satellite sizing rule")
        if self.risk_repair not in ("immediate", "event"):
            raise ValueError("Unknown satellite risk-repair rule")
        if self.repair_scope not in ("combined", "separate"):
            raise ValueError("Unknown core/satellite repair scope")
        if isinstance(self.basket_size, bool) or not isinstance(self.basket_size, int) or not 1 <= self.basket_size <= 3:
            raise ValueError("Basket size must be 1, 2 or 3")
        if self.basket_size > 1 and (self.exit_rule not in ("none", "fixed20") or self.random_seed is not None
                or self.market_gate != "none" or self.sizing != "whole" or self.risk_repair != "immediate"
                or self.repair_scope != "combined"):
            raise ValueError("Basket mode supports whole combined immediate fixed20 or core only")
        if self.member_exit not in ("together", "funded", "individual") or (
                self.member_exit != "together" and (self.basket_size == 1 or self.exit_rule != "fixed20")):
            raise ValueError("Independent member exits require a fixed20 multi-member basket")
        if not isinstance(self.restore_defensive, bool) or (self.restore_defensive and self.member_exit == "together"):
            raise ValueError("Defensive restoration requires an independent member-exit policy")


def policies():
    return [SatellitePolicy("matched_core", "none"), SatellitePolicy("fixed20", "fixed20"),
            SatellitePolicy("fixed60", "fixed60"), SatellitePolicy("score_gap5"),
            SatellitePolicy("score_gap0", cooldown=0), SatellitePolicy("price_gap5", "price")]


@dataclass
class SatelliteMemory:
    raw_core: dict = field(default_factory=dict)
    candidate: str | None = None
    entry_index: int | None = None
    funded_index: int | None = None
    entry_base_weight: float = 0.
    requested_excess: float = 0.
    peak_score: float = 0.
    peak_mark: float = 0.
    ready_at: int = 0
    closing_candidate: str | None = None
    work: bool = False


@dataclass
class SeparateSatelliteMemory(SatelliteMemory):
    core_work: bool = False
    satellite_work: bool = False


@dataclass
class BasketSatelliteMemory(SatelliteMemory):
    members: list[str] = field(default_factory=list)
    entry_bases: dict = field(default_factory=dict)
    entry_excesses: dict = field(default_factory=dict)


@dataclass
class IndividualBasketMemory(BasketSatelliteMemory):
    plan_members: list[str] = field(default_factory=list)
    funded_members: dict = field(default_factory=dict)


def scaled(data, index, weights):
    risk = projected_volatility(data["economic_stats"][index]["risk"], weights)
    factor = min(1., .1/risk) if risk else 1.
    return {c: w*factor for c, w in weights.items()}


def tilted(raw, stocks, candidate):
    target = dict(raw)
    if candidate is None:
        return target
    others = [c for c in stocks if c != candidate]
    total = sum(raw[c] for c in others)
    moved = min(.1, total, max(0., .35 - raw[candidate]))
    if total:
        for c in others:
            target[c] *= 1 - moved/total
    target[candidate] += moved
    return target


def tilted_basket(raw, stocks, members):
    if not isinstance(members, (list, tuple)) or not members or len(members) != len(set(members)) or not set(members) <= set(stocks):
        raise ValueError("Basket members must be distinct stock codes")
    if len(members) == 1:
        return tilted(raw, stocks, members[0])
    target = dict(raw)
    donors = [code for code in stocks if code not in members]
    total = sum(raw[code] for code in donors)
    quota = min(.1, total) / len(members)
    additions = {code: min(quota, max(0., .35 - raw[code])) for code in members}
    moved = sum(additions.values())
    if total:
        for code in donors:
            target[code] *= 1 - moved / total
    for code, addition in additions.items():
        target[code] += addition
    return target


def surviving_basket(raw, stocks, planned, active):
    if not active or len(active) != len(set(active)) or not set(active) <= set(planned):
        raise ValueError("Surviving members must be a nonempty part of the original plan")
    complete = tilted_basket(raw, stocks, planned)
    if active == planned:
        return complete, complete
    target = dict(raw)
    donors = [code for code in stocks if code not in planned]
    total = sum(raw[code] for code in donors)
    moved = sum(complete[code] - raw[code] for code in active)
    if total:
        for code in donors:
            target[code] *= 1 - moved / total
    for code in active:
        target[code] = complete[code]
    return target, complete


def restore_defensive_target(risk, base, neutral, defensive):
    desired = {code: max(weight, neutral[code]) if code in defensive else weight for code, weight in base.items()}
    increase = sum(desired.values()) - sum(base.values())
    if increase <= 0:
        return dict(base)
    high = min(1., max(0., (1 - sum(base.values())) / increase))
    if projected_volatility(risk, base) > .1 + 1e-12:
        raise ValueError("Defensive restoration base exceeds projected risk limit")
    low = 0.
    for _ in range(64):
        middle = (low + high) / 2
        weights = {code: weight + middle * (desired[code] - weight) for code, weight in base.items()}
        if projected_volatility(risk, weights) <= .1:
            low = middle
        else:
            high = middle
    return {code: weight + low * (desired[code] - weight) for code, weight in base.items()}


def headroom_target(risk, neutral, stocks, candidate):
    if candidate is None:
        return dict(neutral)
    maximum = tilted(neutral, stocks, candidate)
    if projected_volatility(risk, neutral) > .1+1e-12:
        raise ValueError("Headroom sizing requires a risk-feasible neutral core")
    if projected_volatility(risk, maximum) <= .1:
        return maximum
    codes = risk["codes"]
    base = np.asarray([neutral[c] for c in codes])
    delta = np.asarray([maximum[c]-neutral[c] for c in codes])
    covariance = risk["covariance"]
    a = float(delta @ covariance @ delta)
    b = float(2*base @ covariance @ delta)
    c = min(0., float(base @ covariance @ base)-.1**2)
    # Solve the upper feasible root without reducing the gold/bond core.
    if a > 1e-20:
        root = math.sqrt(max(0., b*b-4*a*c))
        fraction = -2*c/(b+root) if b >= 0 and b+root > 0 else (-b+root)/(2*a)
    else:
        fraction = -c/b if b > 0 else 1.
    fraction = math.nextafter(min(1., max(0., fraction)), 0.)
    return {code: neutral[code]+fraction*(maximum[code]-neutral[code]) for code in neutral}


def in_band(current, targets):
    return all(abs(current[c] - w) <= min(.03, w*.25) + 1e-12 for c, w in targets.items())


def market_observation(data, index, entries, mode):
    stats = data["cross_stats"][index]
    if stats["signal_index"] != index:
        raise ValueError("Market gate uses a different observation date")
    stocks = data["class_groups"]["stock"]
    if mode == "competition":
        economic = data["economic_stats"][index]
        if economic["signal_index"] != index:
            raise ValueError("Qualified-choice gate uses a different observation date")
        eligible = sorted(c for c in stocks if economic["votes"][c] >= 2/3 and entries[c]["scores"]["medium"] >= 60)
        return {"mode": mode, "eligible_codes": eligible, "enabled": len(eligible) >= 2}
    returns = {w: stats["signals"]["stock"]["log_returns"][w] for w in (21, 63)}
    breadth = sum(entries[c]["price"] > entries[c]["ma120"]*(1+1e-12) for c in stocks)/len(stocks)
    direction = all(v > 1e-12 for v in returns.values())
    return {"mode": mode, "log_returns": returns, "breadth_ma120": breadth,
            "enabled": direction and (mode == "direction" or breadth >= .6)}


class TrendSatelliteAllocator:
    def __init__(self, data, policy, *, start=252, phase=0):
        policy.validate()
        self.data, self.policy, self.start, self.phase = data, policy, start, phase
        memory_type = (IndividualBasketMemory if policy.member_exit != "together" else
                       BasketSatelliteMemory if policy.basket_size > 1 else
                       SeparateSatelliteMemory if policy.repair_scope == "separate" else SatelliteMemory)
        self.memory = memory_type(ready_at=start)
        self.checks = []

    def goal(self, index, candidate, members=None):
        raw = self.memory.raw_core
        stocks = self.data["class_groups"]["stock"]
        if self.policy.basket_size > 1:
            if self.policy.member_exit != "together" and candidate is not None and members is None:
                planned, complete = surviving_basket(raw, stocks, self.memory.plan_members, self.memory.members)
                risk = max(projected_volatility(self.data["economic_stats"][index]["risk"], planned),
                           projected_volatility(self.data["economic_stats"][index]["risk"], complete))
                factor = min(1., .1 / risk) if risk else 1.
                target = {code: weight * factor for code, weight in planned.items()}
                if self.policy.restore_defensive and len(self.memory.members) < len(self.memory.plan_members):
                    defensive = self.data["class_groups"]["gold"] + self.data["class_groups"]["bond"]
                    return restore_defensive_target(self.data["economic_stats"][index]["risk"], target,
                                                    scaled(self.data, index, raw), defensive)
                return target
            planned = raw if candidate is None else tilted_basket(raw, stocks,
                self.memory.members if members is None else members)
            return scaled(self.data, index, planned)
        if self.policy.sizing == "whole":
            return scaled(self.data, index, tilted(raw, stocks, candidate))
        return headroom_target(self.data["economic_stats"][index]["risk"],
                               scaled(self.data, index, raw), stocks, candidate)

    def member_transitions(self, index, current, votes):
        memory, mode = self.memory, self.policy.member_exit
        events = []
        for code in memory.members:
            if memory.funded_members[code] is None and current[code] >= memory.entry_bases[code] + memory.entry_excesses[code]*.5:
                memory.funded_members[code] = index
            funded = memory.funded_members[code]
            reason = None
            if votes[code] < 2/3:
                reason = "absolute_failure"
            elif mode == "individual" and funded is None and index - memory.entry_index >= 10:
                reason = "funding_timeout"
            elif mode == "individual" and funded is not None and index - funded >= 20:
                reason = "expiry"
            elif mode == "funded" and memory.funded_index is not None and index - memory.funded_index >= 20:
                reason = "expiry"
            if reason is not None:
                events.append({"code": code, "reason": reason})
        if mode == "funded" and memory.funded_index is None:
            failure = any(event["reason"] == "absolute_failure" for event in events)
            if failure or index - memory.entry_index >= 10:
                events = [{"code": code, "reason": ("absolute_failure" if votes[code] < 2/3 else "basket_failure")
                           if failure else "basket_timeout"} for code in memory.members]
        return events

    def __call__(self, index, held, state):
        data, policy, memory = self.data, self.policy, self.memory
        if state.signal_index != index or state.signal_date != data["days"][index]:
            raise ValueError("Satellite state uses a different observation date")
        stats = data["economic_stats"][index]
        if stats["signal_index"] != index or stats["risk"]["signal_index"] != index:
            raise ValueError("Satellite risk cache uses a different observation date")
        current = dict(state.weights)
        if set(current) != set(data["prices"]) or {c for c, w in current.items() if w > 0} != set(held):
            raise ValueError("Satellite requires actual full-universe holdings")
        before = asdict(memory)
        stocks = data["class_groups"]["stock"]
        separate = policy.repair_scope == "separate"
        basket = policy.basket_size > 1
        individual = policy.member_exit != "together"
        entries = {r["code"]: r for r in data["frames"][index]["entries"]}
        scores = {c: entries[c]["scores"]["medium"] for c in stocks}
        market = market_observation(data, index, entries, policy.market_gate) if policy.market_gate != "none" else None
        gate_open = market is None or market["enabled"]
        core_day = index == self.start or (index >= self.start+self.phase and (index-self.start-self.phase) % 20 == 0)
        check_day = index == self.start or (index >= self.start+self.phase and (index-self.start-self.phase) % policy.check_sessions == 0)
        if core_day:
            memory.raw_core = expand_class_weights(data, class_proposal("risky_pair_static60", data["cross_stats"][index]["signals"]))
            memory.work = True
            if separate:
                memory.core_work = True
        neutral = scaled(data, index, memory.raw_core)
        closing_goal = {c: neutral[c] for c in stocks} if separate and not memory.core_work else neutral
        if memory.closing_candidate is not None and in_band(current, closing_goal):
            memory.closing_candidate = None
        event, exited, member_events = "hold", None, []
        if memory.candidate is not None:
            code = memory.candidate
            members = memory.members if basket else [code]
            memory.peak_score = max(memory.peak_score, scores[code])
            funded = (all(current[c] >= memory.entry_bases[c] + memory.entry_excesses[c]*.5 for c in members)
                      if basket else current[code] >= memory.entry_base_weight + memory.requested_excess*.5)
            if memory.funded_index is None and funded:
                memory.funded_index = index
                memory.peak_mark = data["marks"][code][index]
                event = "funded"
            if memory.funded_index is not None:
                memory.peak_mark = max(memory.peak_mark, data["marks"][code][index])
            reason = None
            failed = [c for c in members if stats["votes"][c] < 2/3]
            if individual:
                member_events = self.member_transitions(index, current, stats["votes"])
                if member_events:
                    aliases = {"basket_failure": "absolute_failure", "basket_timeout": "funding_timeout"}
                    reasons = {aliases.get(item["reason"], item["reason"]) for item in member_events}
                    reason = next(label for label in ("absolute_failure", "funding_timeout", "expiry") if label in reasons)
                    retired = {item["code"] for item in member_events}
                    exited = failed or None
                    memory.members = [code for code in members if code not in retired]
                    memory.work = True
                    event = reason
                    if memory.members:
                        reason = None
            elif failed:
                reason = "absolute_failure"
            elif memory.funded_index is None and index-memory.entry_index >= 10:
                reason = "funding_timeout"
            elif check_day and policy.exit_on_gate and not gate_open:
                reason = "market_exit"
            elif memory.funded_index is not None:
                age = index-memory.funded_index
                if policy.exit_rule in ("fixed20", "fixed60") and age >= int(policy.exit_rule[5:]):
                    reason = "expiry"
                elif check_day and policy.exit_rule == "score" and (scores[code] < 55 or memory.peak_score-scores[code] >= 10):
                    reason = "score_decay"
                elif check_day and policy.exit_rule == "price":
                    threshold = max(.01, 2*entries[code]["annual_vol60"]*math.sqrt(5/252))
                    if math.log(data["marks"][code][index]/memory.peak_mark) <= -threshold:
                        reason = "volatility_trail"
            if reason is not None:
                exited = (failed if basket else code) if reason == "absolute_failure" else None
                memory.closing_candidate = code
                memory.candidate = None
                memory.entry_index = memory.funded_index = None
                if basket:
                    memory.members, memory.entry_bases, memory.entry_excesses = [], {}, {}
                if individual:
                    memory.plan_members, memory.funded_members = [], {}
                memory.ready_at = index+policy.cooldown
                memory.work = True
                if separate:
                    memory.satellite_work = True
                event = reason
        if (policy.exit_rule != "none" and memory.candidate is None and memory.closing_candidate is None
                and index >= memory.ready_at and check_day and gate_open):
            eligible = [c for c in stocks if stats["votes"][c] >= 2/3 and scores[c] >= 60]
            if eligible:
                if policy.random_seed is None:
                    code = min(eligible, key=lambda c: (-scores[c], c))
                else:
                    # Date/code hashing is reproducible and independent of call order.
                    code = min(eligible, key=lambda c: (
                        sha256(f"{policy.random_seed}|{state.signal_date}|{c}".encode()).digest(), c))
                members = sorted(eligible, key=lambda c: (-scores[c], c))[:policy.basket_size] if basket else [code]
                goal = self.goal(index, code, members)
                excess = goal[code]-neutral[code]
                extra = {c: goal[c]-neutral[c] for c in members}
                admitted = sum(extra.values()) >= .005 and all(value > 1e-12 for value in extra.values()) if basket else excess >= .005
                if admitted:
                    memory.candidate, memory.entry_index = code, index
                    memory.entry_base_weight, memory.requested_excess = neutral[code], excess
                    if basket:
                        memory.members = members
                        memory.entry_bases = {c: neutral[c] for c in members}
                        memory.entry_excesses = extra
                    if individual:
                        memory.plan_members = list(members)
                        memory.funded_members = dict.fromkeys(members)
                    memory.funded_index = None
                    memory.peak_score, memory.peak_mark = scores[code], 0.
                    memory.work = True
                    if separate:
                        memory.satellite_work = True
                    event = "entry"
        goal = self.goal(index, memory.candidate)
        current_risk = projected_volatility(stats["risk"], current)
        exits = (frozenset(held) if current_risk > .1+1e-12 else
                 frozenset(exited if basket else [exited]) if exited else frozenset())
        if current_risk > .1+1e-12:
            target = {c: w*.1/current_risk for c, w in current.items()}
            memory.work = policy.risk_repair == "immediate"
            if separate:
                memory.core_work = memory.work
                memory.satellite_work = False
            action = "risk"
        elif separate:
            relevant = goal if memory.core_work else {c: goal[c] for c in stocks}
            if memory.core_work or memory.satellite_work:
                if in_band(current, relevant):
                    if memory.core_work:
                        memory.core_work = False
                    memory.satellite_work = False
                    target, action = None, "hold"
                else:
                    target, action = goal, "repair"
            else:
                target, action = None, "hold"
            memory.work = memory.core_work or memory.satellite_work
        elif memory.work:
            if in_band(current, goal):
                memory.work = False
                target, action = None, "hold"
            else:
                target, action = goal, "repair"
        else:
            target, action = None, "hold"
        self.checks.append({"signal_date": state.signal_date, "signal_index": index, "memory_before": before,
            "memory_after": asdict(memory), "core_day": core_day, "check_day": check_day, "event": event,
            "action": action, "current_fund_weights": current, "scores": scores, "own_votes": stats["votes"],
            "current_projected_volatility": current_risk, "risk_exit_codes": sorted(exits), "targets": target})
        if market is not None:
            self.checks[-1]["market_observation"] = market
        if individual:
            self.checks[-1]["member_events"] = member_events
        return None if target is None else AllocationTargets(target, exits, enforce_risk_exits=True)
