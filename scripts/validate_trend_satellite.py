"""Rebuild satellite transitions from raw past marks and executed holdings."""
from decimal import Decimal, localcontext
from hashlib import sha256
import math

import numpy as np

from backend.analysis.sector_trend import midpoint_percentiles, trend_metrics, trend_score
from scripts.validate_cross_asset_trend import audit_current_holdings
from scripts.validate_portfolio_increment import raw_context, raw_risk


def close(actual, expected):
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            raise ValueError("Satellite audit record keys differ")
        for k, v in expected.items():
            close(actual[k], v)
    elif isinstance(expected, float):
        if isinstance(actual, bool) or not math.isfinite(actual) or abs(actual-expected) > 1e-9:
            raise ValueError("Satellite audit numeric state differs")
    elif actual != expected:
        raise ValueError("Satellite audit state or action differs")


def rebuild_executed_weights(account, data):
    indexes = {day: index for index, day in enumerate(data["days"])}
    by_day = {}
    for trade in account["trades"]:
        by_day.setdefault(trade["execution_date"], []).append(trade)
    lots, seen, result = {}, set(), {}
    with localcontext() as context:
        context.prec = 40
        for point in account["equity_curve"]:
            for trade in by_day.get(point["date"], []):
                lot_id, code, units = trade["lot_id"], trade["code"], Decimal(trade["units"])
                if code not in data["prices"] or not units.is_finite() or units <= 0:
                    raise ValueError("Invalid executed lot units or code")
                if trade["action"] == "BUY":
                    if lot_id in seen:
                        raise ValueError("Duplicate executed purchase lot")
                    seen.add(lot_id)
                    lots[lot_id] = {"code": code, "units": units}
                elif trade["action"] == "SELL":
                    lot = lots.get(lot_id)
                    if lot is None or lot["code"] != code or units > lot["units"]:
                        raise ValueError("Executed sale exceeds its matching lot")
                    lot["units"] -= units
                    if lot["units"] == 0:
                        del lots[lot_id]
                else:
                    raise ValueError("Unknown executed lot action")
            # Reset totals from surviving lots, rather than accumulate cancellation dust.
            totals = dict.fromkeys(data["prices"], Decimal(0))
            for lot in lots.values():
                totals[lot["code"]] += lot["units"]
            index = indexes[point["date"]]
            equity = Decimal(point["equity"])
            if not equity.is_finite() or equity <= 0:
                raise ValueError("Invalid executed account wealth")
            result[point["date"]] = {code: float(units * Decimal(str(data["prices"][code][index])) / equity)
                                    for code, units in totals.items()}
    return result


def audit_satellite(account, checks, data, policy, *, start=252, phase=0, cache=None):
    days = data["days"]
    lookup = {d: i for i, d in enumerate(days)}
    lag = account["execution"]["execution_lag_sessions"]
    end = lookup[account["end_date"]]
    applied = {lookup[r["signal_date"]]: r for r in account["decisions"]}
    blocked = {i for d in applied for i in range(d+1, d+lag)}
    if [r["signal_index"] for r in checks] != [i for i in range(start, end-lag) if i not in blocked]:
        raise ValueError("Satellite account omitted a daily check or read a queued-order date")
    executed = rebuild_executed_weights(account, data)
    actual_weights = {}
    for record in checks:
        observed = executed[record["signal_date"]]
        close(record["current_fund_weights"], observed)
        actual_weights[record["signal_index"]] = observed
    memory = {"raw_core": {}, "candidate": None, "entry_index": None, "funded_index": None,
        "entry_base_weight": 0., "requested_excess": 0., "peak_score": 0., "peak_mark": 0.,
        "ready_at": start, "closing_candidate": None, "work": False}
    separate = policy.repair_scope == "separate"
    basket = policy.basket_size > 1
    individual = policy.member_exit != "together"
    if separate:
        memory.update(core_work=False, satellite_work=False)
    if basket:
        memory.update(members=[], entry_bases={}, entry_excesses={})
    if individual:
        memory.update(plan_members=[], funded_members={})
    applied_trace = []
    for record in checks:
        index = record["signal_index"]
        if record["signal_date"] != days[index]:
            raise ValueError("Satellite signal date/index mismatch")
        close(record["memory_before"], memory)
        if cache is not None and index in cache:
            context, scores = cache[index]
        else:
            context = raw_context(data, index)
            stocks = context["groups"]["stock"]
            descriptors = {c: trend_metrics(data["marks"][c][index-60:index+1]) for c in stocks}
            ranks = midpoint_percentiles([descriptors[c]["return_pct"] for c in stocks])
            scores = {c: trend_score(descriptors[c], ranks[j]) for j, c in enumerate(stocks)}
            if cache is not None:
                cache[index] = context, scores
        stocks = context["groups"]["stock"]
        close(record["scores"], scores)
        close(record["own_votes"], context["votes"])
        gate_open = True
        if policy.market_gate == "competition":
            eligible_codes = sorted(c for c in stocks if context["votes"][c] >= 2/3 and scores[c] >= 60)
            gate_open = len(eligible_codes) >= 2
            close(record.get("market_observation"), {"mode": "competition", "eligible_codes": eligible_codes,
                                                      "enabled": gate_open})
        elif policy.market_gate != "none":
            if "market_observation" not in context:
                daily_stock = np.asarray([math.fsum(data["marks"][c][j]/data["marks"][c][j-1]-1 for c in stocks)/len(stocks)
                    for j in range(index-62, index+1)])
                market_returns = {w: float(np.log1p(daily_stock[-w:]).sum()) for w in (21, 63)}
                breadth = sum(data["marks"][c][index] > float(np.mean(data["marks"][c][index-119:index+1]))*(1+1e-12)
                              for c in stocks)/len(stocks)
                context["market_observation"] = market_returns, breadth
            market_returns, breadth = context["market_observation"]
            gate_open = all(v > 1e-12 for v in market_returns.values()) and (policy.market_gate == "direction" or breadth >= .6)
            market = {"mode": policy.market_gate, "log_returns": market_returns, "breadth_ma120": breadth, "enabled": gate_open}
            recorded = record.get("market_observation")
            if recorded is None or recorded.get("mode") != policy.market_gate:
                raise ValueError("Satellite gate omitted or mismatched its past market observation")
            recorded = dict(recorded)
            recorded["log_returns"] = {int(w): v for w, v in recorded["log_returns"].items()}
            close(recorded, market)
        elif "market_observation" in record:
            raise ValueError("Ungated satellite has unexpected gate metadata")
        core_day = index == start or (index >= start+phase and (index-start-phase) % 20 == 0)
        check_day = index == start or (index >= start+phase and (index-start-phase) % policy.check_sessions == 0)
        if core_day:
            vols = {}
            for group in ("stock", "gold"):
                members = context["groups"][group]
                arithmetic = np.mean([np.expm1(context["daily"][context["codes"].index(c)]) for c in members], axis=0)
                vols[group] = max(.05, float(np.std(np.log1p(arithmetic))*np.sqrt(252)))
            stock_budget = .6*vols["gold"]/(vols["stock"]+vols["gold"])
            budgets = {"stock": stock_budget, "gold": .6-stock_budget, "bond": .4}
            memory["raw_core"] = {c: budgets[g]/len(members) for g, members in context["groups"].items() for c in members}
            memory["work"] = True
            if separate:
                memory["core_work"] = True

        def scale(weights):
            risk = raw_risk(context, weights)
            factor = min(1., .1/risk) if risk else 1.
            return {c: w*factor for c, w in weights.items()}

        def goal(candidate, members=None):
            raw = scale(memory["raw_core"]) if policy.sizing == "headroom" else dict(memory["raw_core"])
            neutral_core = dict(raw)
            if candidate is not None:
                selected = ((memory["plan_members"] if individual else memory["members"]) if members is None else members) if basket else [candidate]
                donors = [c for c in stocks if c not in selected]
                total = sum(raw[c] for c in donors)
                if len(selected) == 1:
                    increases = {candidate: min(.1, total, max(0., .35-raw[candidate]))}
                else:
                    increases = {c: max(0., min(.35-raw[c], min(.1, total)/len(selected))) for c in selected}
                moved = sum(increases.values())
                if total:
                    for c in donors:
                        raw[c] *= 1-moved/total
                for c, increase in increases.items():
                    raw[c] += increase
                if individual and members is None:
                    complete = dict(raw)
                    active = memory["members"]
                    if active != selected:
                        raw = dict(memory["raw_core"])
                        transferred = sum(complete[c] - raw[c] for c in active)
                        if total:
                            for c in donors:
                                raw[c] *= 1 - transferred / total
                        for c in active:
                            raw[c] = complete[c]
                    risk_limit = max(raw_risk(context, raw), raw_risk(context, complete))
                    factor = min(1., .1 / risk_limit) if risk_limit else 1.
                    target = {c: weight * factor for c, weight in raw.items()}
                    if policy.restore_defensive and len(active) < len(selected):
                        neutral = scale(memory["raw_core"])
                        defensive = context["groups"]["gold"] + context["groups"]["bond"]
                        desired = {c: max(weight, neutral[c]) if c in defensive else weight for c, weight in target.items()}
                        increase = sum(desired.values()) - sum(target.values())
                        if increase > 0:
                            low, high = 0., min(1., max(0., (1 - sum(target.values())) / increase))
                            for _ in range(64):
                                fraction = (low + high) / 2
                                trial = {c: weight + fraction * (desired[c] - weight) for c, weight in target.items()}
                                if raw_risk(context, trial) <= .1:
                                    low = fraction
                                else:
                                    high = fraction
                            target = {c: weight + low * (desired[c] - weight) for c, weight in target.items()}
                    return target
            if policy.sizing == "whole":
                return scale(raw)
            if raw_risk(context, raw) <= .1:
                return raw
            # Independent bisection on raw past returns, not the allocator's quadratic.
            low, high = 0., 1.
            for _ in range(64):
                middle = (low+high)/2
                trial = {c: neutral_core[c]+middle*(raw[c]-neutral_core[c]) for c in raw}
                if raw_risk(context, trial) <= .1:
                    low = middle
                else:
                    high = middle
            return {c: neutral_core[c]+low*(raw[c]-neutral_core[c]) for c in raw}

        current = actual_weights[index]
        neutral = goal(None)
        fits = lambda target: all(abs(current[c]-w) <= min(.03, w*.25)+1e-12 for c, w in target.items())
        closing_goal = {c: neutral[c] for c in stocks} if separate and not memory["core_work"] else neutral
        if memory["closing_candidate"] is not None and fits(closing_goal):
            memory["closing_candidate"] = None
        event, urgent, member_events = "hold", None, []
        code = memory["candidate"]
        if code is not None:
            members = memory["members"] if basket else [code]
            memory["peak_score"] = max(memory["peak_score"], scores[code])
            funded = (all(current[c] >= memory["entry_bases"][c]+memory["entry_excesses"][c]*.5 for c in members)
                      if basket else current[code] >= memory["entry_base_weight"]+memory["requested_excess"]*.5)
            if memory["funded_index"] is None and funded:
                memory["funded_index"] = index
                memory["peak_mark"] = data["marks"][code][index]
                event = "funded"
            if memory["funded_index"] is not None:
                memory["peak_mark"] = max(memory["peak_mark"], data["marks"][code][index])
            reason = None
            failed = [c for c in members if context["votes"][c] < 2/3]
            if individual:
                for c in members:
                    if memory["funded_members"][c] is None and current[c] >= memory["entry_bases"][c] + memory["entry_excesses"][c]*.5:
                        memory["funded_members"][c] = index
                    funded_at = memory["funded_members"][c]
                    label = None
                    if context["votes"][c] < 2/3:
                        label = "absolute_failure"
                    elif policy.member_exit == "individual" and funded_at is None and index - memory["entry_index"] >= 10:
                        label = "funding_timeout"
                    elif policy.member_exit == "individual" and funded_at is not None and index - funded_at >= 20:
                        label = "expiry"
                    elif policy.member_exit == "funded" and memory["funded_index"] is not None and index - memory["funded_index"] >= 20:
                        label = "expiry"
                    if label is not None:
                        member_events.append({"code": c, "reason": label})
                if policy.member_exit == "funded" and memory["funded_index"] is None:
                    if failed or index - memory["entry_index"] >= 10:
                        member_events = [{"code": c, "reason": ("absolute_failure" if c in failed else "basket_failure")
                                          if failed else "basket_timeout"} for c in members]
                if member_events:
                    labels = {"basket_failure": "absolute_failure", "basket_timeout": "funding_timeout"}
                    reason = next(label for label in ("absolute_failure", "funding_timeout", "expiry")
                                  if any(labels.get(item["reason"], item["reason"]) == label for item in member_events))
                    retired = {item["code"] for item in member_events}
                    urgent = failed or None
                    memory["members"] = [c for c in members if c not in retired]
                    memory["work"] = True
                    event = reason
                    if memory["members"]:
                        reason = None
            elif failed:
                reason = "absolute_failure"
            elif memory["funded_index"] is None and index-memory["entry_index"] >= 10:
                reason = "funding_timeout"
            elif check_day and policy.exit_on_gate and not gate_open:
                reason = "market_exit"
            elif memory["funded_index"] is not None:
                age = index-memory["funded_index"]
                if policy.exit_rule in ("fixed20", "fixed60") and age >= int(policy.exit_rule[5:]):
                    reason = "expiry"
                elif check_day and policy.exit_rule == "score" and (scores[code] < 55 or memory["peak_score"]-scores[code] >= 10):
                    reason = "score_decay"
                elif check_day and policy.exit_rule == "price":
                    vol = float(np.std(context["daily"][context["codes"].index(code)])*np.sqrt(252))
                    threshold = max(.01, 2*vol*math.sqrt(5/252))
                    if math.log(data["marks"][code][index]/memory["peak_mark"]) <= -threshold:
                        reason = "volatility_trail"
            if reason:
                urgent = (failed if basket else code) if reason == "absolute_failure" else None
                memory.update(closing_candidate=code, candidate=None, entry_index=None, funded_index=None,
                              ready_at=index+policy.cooldown, work=True)
                if basket:
                    memory.update(members=[], entry_bases={}, entry_excesses={})
                if individual:
                    memory.update(plan_members=[], funded_members={})
                if separate:
                    memory["satellite_work"] = True
                event = reason
        if (policy.exit_rule != "none" and memory["candidate"] is None and memory["closing_candidate"] is None
                and index >= memory["ready_at"] and check_day and gate_open):
            eligible = [c for c in stocks if context["votes"][c] >= 2/3 and scores[c] >= 60]
            if eligible:
                if policy.random_seed is None:
                    code = sorted(eligible, key=lambda c: (-scores[c], c))[0]
                else:
                    code = sorted(eligible, key=lambda c: (
                        sha256(f"{policy.random_seed}|{days[index]}|{c}".encode()).hexdigest(), c))[0]
                members = sorted(eligible, key=lambda c: (-scores[c], c))[:policy.basket_size] if basket else [code]
                proposed = goal(code, members)
                extra = proposed[code]-neutral[code]
                excesses = {c: proposed[c]-neutral[c] for c in members}
                admitted = (sum(excesses.values()) >= .005 and min(excesses.values()) > 1e-12) if basket else extra >= .005
                if admitted:
                    memory.update(candidate=code, entry_index=index, entry_base_weight=neutral[code],
                        requested_excess=extra, funded_index=None, peak_score=scores[code], peak_mark=0., work=True)
                    if basket:
                        memory.update(members=members, entry_bases={c: neutral[c] for c in members}, entry_excesses=excesses)
                    if individual:
                        memory.update(plan_members=list(members), funded_members=dict.fromkeys(members))
                    if separate:
                        memory["satellite_work"] = True
                    event = "entry"
        intended = goal(memory["candidate"])
        risk = raw_risk(context, current)
        exits = (sorted(c for c, w in current.items() if w > 0) if risk > .1+1e-12 else
                 sorted(urgent) if basket and urgent else [urgent] if urgent else [])
        if risk > .1+1e-12:
            expected = {c: w*.1/risk for c, w in current.items()}
            memory["work"] = policy.risk_repair == "immediate"
            if separate:
                memory["core_work"] = memory["work"]
                memory["satellite_work"] = False
            action = "risk"
        elif separate:
            relevant = intended if memory["core_work"] else {c: intended[c] for c in stocks}
            pending = memory["core_work"] or memory["satellite_work"]
            if pending and not fits(relevant):
                expected, action = intended, "repair"
            else:
                if pending:
                    memory["core_work"] = memory["satellite_work"] = False
                expected, action = None, "hold"
            memory["work"] = memory["core_work"] or memory["satellite_work"]
        elif memory["work"] and not fits(intended):
            expected, action = intended, "repair"
        else:
            if memory["work"]:
                memory["work"] = False
            expected, action = None, "hold"
        close(record["memory_after"], memory)
        if individual:
            close(record.get("member_events"), member_events)
        elif "member_events" in record:
            raise ValueError("Together basket has unexpected member retirement events")
        close(record["core_day"], core_day)
        close(record["check_day"], check_day)
        close(record["event"], event)
        close(record["action"], action)
        if record["risk_exit_codes"] != exits:
            raise ValueError(f"Satellite risk exit codes differ on {days[index]}: recorded={record['risk_exit_codes']!r}, rebuilt={exits!r}")
        close(record["current_projected_volatility"], risk)
        close(record["targets"], expected)
        decision = applied.get(index)
        if (expected is None) != (decision is None):
            raise ValueError("Satellite recorded action differs from the actual engine decision")
        if decision is not None:
            close(decision["targets"], expected)
            applied_trace.append(record)
    state_audit = audit_current_holdings(account, applied_trace, data)
    return {"status": "passed", "daily_checks": len(checks), "applied_state_validation": state_audit}
