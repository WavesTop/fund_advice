"""Independent sector targets, cash cycles and economic increment model audit."""
from datetime import date, timedelta
from decimal import Decimal, localcontext
import math
import statistics

import numpy as np

from backend.analysis.matured_trend_forecast import audit_training_visibility
from backend.analysis.sector_trend import midpoint_percentiles, trend_metrics, trend_score
from scripts.validate_cross_asset_trend import audit_current_holdings


def raw_context(data, index):
    entries = {e["code"]: e for e in data["frames"][120]["entries"]}
    groups = {"stock": [], "gold": [], "bond": []}
    for code in data["prices"]:
        group = "bond" if data["roles"][code] == "defensive" else "gold" if entries[code]["defensive_asset"] else "stock"
        groups[group].append(code)
    codes = list(data["prices"])
    daily = np.asarray([[math.log(data["marks"][c][j]/data["marks"][c][j-1]) for j in range(index-59, index+1)] for c in codes])
    votes = {c: sum(math.log(data["marks"][c][index]/data["marks"][c][index-w]) > 1e-12 for w in (21,63,252))/3 for c in codes}
    return {"groups": groups, "codes": codes, "daily": daily, "votes": votes}


def raw_risk(context, weights):
    return float(np.std(np.asarray([weights[c] for c in context["codes"]]) @ context["daily"]) * np.sqrt(252))


def rebuilt_entry(context, candidate=None, kind="core"):
    groups, votes = context["groups"], context["votes"]
    if kind not in ("core", "static_core", "inverse_static") or candidate is not None and candidate not in [*groups["stock"], "cash"]:
        raise ValueError("Invalid independently rebuilt economic entry")
    stock, gold, bond = groups["stock"], groups["gold"], groups["bond"]
    if kind == "inverse_static":
        vols = {}
        for group, members in (("stock", stock), ("gold", gold)):
            # Class proxy is equal daily returns; it is not a tradable label.
            returns = np.asarray([[math.expm1(v) for v in context["daily"][context["codes"].index(c)]] for c in members]).mean(axis=0)
            vols[group] = max(.05, float(np.std(np.log1p(returns))*np.sqrt(252)))
        stock_budget = .6*vols["gold"]/(vols["stock"]+vols["gold"])
        gold_budget = .6-stock_budget
    else:
        stock_budget, gold_budget = (.4 if candidate is None else .2), .2
    weights = {c: min(.35, stock_budget/len(stock)+(.2 if c==candidate else 0.)) for c in stock}
    weights.update({c: gold_budget/len(gold) for c in gold})
    weights.update({c: .4/len(bond) for c in bond})
    if kind == "core":
        weights = {c: w if votes[c]>=2/3 else 0. for c,w in weights.items()}
    original = raw_risk(context, weights)
    scale = min(1., .1/original) if original else 1.
    return {c:w*scale for c,w in weights.items()}, weights, original, scale


def rebuilt_features(data, candidate, index, mode, *, context=None):
    context = raw_context(data,index) if context is None else context
    groups, codes, votes = context["groups"], context["codes"], context["votes"]
    choices = [*groups["stock"],"cash"]
    if mode not in ("state_only","trend") or candidate not in choices:
        raise ValueError("Unknown economic feature mode or candidate")
    core_scale = rebuilt_entry(context)[3]
    candidate_scale = rebuilt_entry(context,candidate)[3]
    vol = 0. if candidate=="cash" else float(np.std(context["daily"][codes.index(candidate)])*np.sqrt(252))
    base = [float(candidate==c) for c in choices]+[float(candidate=="cash" or votes[candidate]>=2/3),
        *(statistics.fmean(votes[c]>=2/3 for c in groups[g]) for g in ("stock","gold","bond")),core_scale,candidate_scale,vol]
    if mode=="state_only":
        return base
    if "extra_vectors" not in context:
        descriptors = {c: {w:trend_metrics(data["marks"][c][index-w:index+1]) for w in (20,60,120)} for c in groups["stock"]}
        ranks = {w:midpoint_percentiles([descriptors[c][w]["return_pct"] for c in groups["stock"]]) for w in (20,60,120)}
        vectors = {}
        for at,c in enumerate(groups["stock"]):
            p = data["marks"][c]; d = descriptors[c][60]
            vectors[c] = [trend_score(descriptors[c][w],ranks[w][at])/100 for w in (20,60,120)]+[p[index]/p[index-w]-1 for w in (5,20,60,120)]+[
                d["max_drawdown_pct"]/100,p[index]/statistics.fmean(p[index-59:index+1])-1,d["efficiency"],d["r2"]]
        context["extra_vectors"] = vectors
    vectors = context["extra_vectors"]
    extra = [statistics.fmean(v[j] for v in vectors.values()) for j in range(11)] if candidate=="cash" else vectors[candidate]
    return base+extra


def close_weights(actual,expected):
    if actual is None or set(actual)!=set(expected) or any(not math.isfinite(actual[c]) or abs(actual[c]-w)>1e-9 for c,w in expected.items()):
        raise ValueError("Economic fund weights differ from independently rebuilt targets")


def audit_model(learning,predicted,data,index,mode,records,*,cache=None):
    day=data["days"][index]
    if (learning["asof"]!=day or learning["window_start"]!=(date.fromisoformat(day)-timedelta(days=1096)).isoformat()
            or learning["status"]=="fitted" and learning["penalty"]!=1
            or learning["status"]=="insufficient_history" and learning["minimum_dates"]!=24):
        raise ValueError("Economic model uses different fitting dates or constraints")
    if cache is None or day not in cache:
        audit_training_visibility(records,learning)
        if cache is not None:
            cache[day]=learning
    elif cache[day]!=learning:
        raise ValueError("Economic model metadata changed inside its immutable training cache")
    if learning["status"]!="fitted":
        if predicted:
            raise ValueError("Unfitted economic model invented predictions")
        return None
    if learning["distinct_dates"]<24:
        raise ValueError("Economic model fitted before24matured dates")
    context=raw_context(data,index); choices=[*context["groups"]["stock"],"cash"]
    x=np.asarray([rebuilt_features(data,c,index,mode,context=context) for c in choices])
    z=np.clip((x-np.asarray(learning["means"]))/np.asarray(learning["scales"]),-5,5)
    forecast=z@np.asarray(learning["coefficients"])+np.asarray(learning["intercept"])
    if set(predicted)!=set(choices) or any(not math.isfinite(predicted[c]) or abs(predicted[c]-forecast[i,0])>1e-9 for i,c in enumerate(choices)):
        raise ValueError("Economic increment forecasts do not follow matured coefficients")
    valid={c:v for c,v in predicted.items() if c=="cash" or context["votes"][c]>=2/3}
    best=max([0.,*valid.values()]); choices=[c for c,v in valid.items() if best-v<=1e-10]
    return min(choices) if best>1e-10 and choices else None


def audit_economic_account(account,checks,data,*,policy,mode=None,records=None,fixed_candidate=None,start=252,phase=0,lag=1,model_cache=None):
    dates={d:i for i,d in enumerate(data["days"])}; end=dates[account["end_date"]]
    if account["execution"]["review_sessions"]!=1 or account["execution"]["execution_lag_sessions"]!=lag:
        raise ValueError("Economic account did not use its declared daily safety/lag clock")
    applied={dates[d["signal_date"]]:d for d in account["decisions"]}
    blocked={i for d in applied for i in range(d+1,d+lag)}
    expected=[i for i in range(start,end-lag) if i not in blocked]
    if [e["signal_index"] for e in checks]!=expected or len({e["signal_index"] for e in checks})!=len(checks):
        raise ValueError("Economic account omitted an actionable daily safety check")
    rows={e["signal_date"]:e for e in checks}; by_day={}
    for t in account["trades"]:
        by_day.setdefault(t["execution_date"],[]).append(t)
    with localcontext() as ctx:
        ctx.prec=40; units=dict.fromkeys(data["prices"],Decimal(0))
        for row in account["equity_curve"]:
            for t in by_day.get(row["date"],[]):
                units[t["code"]]+=Decimal(t["units"])*(1 if t["action"]=="BUY" else -1)
            if row["date"] in rows:
                i=dates[row["date"]]
                observed={c:float(n*Decimal(str(data["prices"][c][i]))/Decimal(row["equity"])) for c,n in units.items()}
                close_weights(rows[row["date"]]["current_fund_weights"],observed)
    deadline, next_entry, cycle=None,start+phase,None
    trace=[]
    for entry in checks:
        i=entry["signal_index"]
        if (entry["signal_date"]!=data["days"][i] or entry["policy"]!=policy or entry["deadline_before"]!=deadline
                or entry["next_entry_before"]!=next_entry or entry["cycle_entry_before"]!=cycle):
            raise ValueError("Economic cash/lease state did not follow actual earlier decisions")
        context=raw_context(data,i); current=entry["current_fund_weights"]; risk=raw_risk(context,current)
        close_weights(entry["own_votes"],context["votes"])
        if not math.isfinite(entry["current_projected_volatility"]) or abs(entry["current_projected_volatility"]-risk)>1e-9:
            raise ValueError("Economic actual holdings risk differs")
        exits=[]; target=None
        if deadline is None and i<next_entry:
            action="wait_cash"
        elif deadline is None:
            if any(v>0 for v in current.values()):
                raise ValueError("Economic new entry still has old positions")
            choice=fixed_candidate
            if policy in ("state_only","trend"):
                choice=audit_model(entry["learning"],entry["predictions"],data,i,mode,records,cache=model_cache)
            elif entry["learning"] is not None or entry["predictions"]:
                raise ValueError("Nonlearning economic control contains an invented model")
            kind=policy if policy in ("static_core","inverse_static") else "core"
            target,raw,original,scale=rebuilt_entry(context,choice,kind)
            if entry["choice"]!=choice or entry["kind"]!=kind:
                raise ValueError("Economic entry choice differs")
            close_weights(entry["unscaled_fund_targets"],raw)
            for field,value in (("unconstrained_projected_volatility",original),("risky_scale",scale)):
                if not math.isfinite(entry[field]) or abs(entry[field]-value)>1e-9:
                    raise ValueError("Economic initial risk scaling differs")
            cycle,deadline=i,i+20; next_entry=deadline+lag+2; action="entry"
        elif i>=deadline:
            action="expiry"; target=dict.fromkeys(current,0.); deadline=None; next_entry=max(next_entry,i+lag+2)
        else:
            failed={c for c,v in current.items() if v>0 and policy not in ("static_core","inverse_static") and context["votes"][c]<2/3}
            exits=sorted(c for c,v in current.items() if v>0) if risk>.1+1e-12 else sorted(failed)
            if not exits:
                action="hold"
            else:
                action="risk"; kept={c:0. if c in failed else v for c,v in current.items()}
                remaining=raw_risk(context,kept); ratio=min(1.,.1/remaining) if remaining else 1.
                target={c:v*ratio for c,v in kept.items()}
                if i+lag>deadline:
                    action="early_expiry"; target=dict.fromkeys(current,0.); deadline=None
        if entry["action"]!=action or entry["risk_exit_codes"]!=exits:
            raise ValueError("Economic safety action or urgency differs from actual current risk/eligibility")
        if action!="entry" and (entry["learning"] is not None or entry["predictions"]):
            raise ValueError("Economic nonentry action contains an invented forecast")
        if target is None:
            if entry["targets"] is not None or i in applied:
                raise ValueError("Economic hold/wait fabricated a rebalance decision")
        else:
            if i not in applied:
                raise ValueError("Economic required action was not executed by the planner")
            close_weights(entry["targets"],target); close_weights(applied[i]["targets"],target)
            projected=raw_risk(context,target)
            if projected>.1+1e-9 or not math.isfinite(entry["projected_volatility"]) or abs(entry["projected_volatility"]-projected)>1e-9:
                raise ValueError("Economic target forecast risk violates its constraint")
            trace.append(entry)
        if entry["deadline_after"]!=deadline or entry["next_entry_after"]!=next_entry or entry["cycle_entry_after"]!=cycle:
            raise ValueError("Economic cash ready date spent future redemption proceeds")
    holdings=audit_current_holdings(account,trace,data)
    return {"status":"passed","daily_checks":len(checks),"decisions":len(trace),"holdings":holdings}
