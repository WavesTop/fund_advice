"""Train on actual candidate-account increments, then replay cash-cycle portfolios."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.fund_allocation import AllocationExecution, simulate_allocations
from backend.analysis.matured_trend_forecast import MaturedForecaster
from backend.analysis.portfolio_increment import EconomicLeaseAllocator, HORIZON, MODES, economic_features, prepare_economic
from backend.core.trading_calendar import TradingCalendar
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import carry_slice, effective_episode
from scripts.research_phased_class_trends import phased_trial
from scripts.search_fund_allocations import metrics
from scripts.validate_fund_allocation import validate_allocation
from scripts.validate_portfolio_increment import audit_economic_account, raw_context, rebuilt_features

PROTOCOL = ROOT / "config/portfolio-increment-research-v1.json"
EXECUTION = AllocationExecution(review_sessions=1, rebalance_band=.03, relative_rebalance_band=.25,
    initial_review=True, reinvest_on_settlement=False)


def economic_trial(data, calendar, policy, *, start=252, end=None, phase=0, lag=1, fee=Decimal(10),
                   fixed_candidate=None, detail=True, deadline=False):
    if policy == "inverse_unleased20":
        return phased_trial(data,calendar,"risky_pair_static60","grid20",start=start,end=end,fee=fee,phase=phase,lag=lag,detail=detail,deadline=deadline)
    learner = data["increment_forecasters"][policy] if policy in MODES else None
    allocator = EconomicLeaseAllocator(data,policy=policy,mode=policy if policy in MODES else None,
        forecaster=learner,fixed_candidate=fixed_candidate,start=start,phase=phase,lag=lag)
    execution=replace(EXECUTION,execution_lag_sessions=lag,deadline_entry_guard_days=7 if deadline else 0)
    account=simulate_allocations(data,calendar,allocator,start=start,end=end,fee_bps=fee,execution=execution,include_portfolio_state=True)
    ledger=validate_allocation(account,data,calendar)
    audit=audit_economic_account(account,allocator.checks,data,policy=policy,mode=policy if policy in MODES else None,
        records=data["increment_records"][policy] if policy in MODES else None,fixed_candidate=fixed_candidate,
        start=start,phase=phase,lag=lag,model_cache=data["increment_model_audit_cache"][policy] if policy in MODES else None)
    measured=metrics(account)
    equities=np.asarray([float(r["equity"]) for r in account["equity_curve"]])
    measured["realized_annual_volatility_pct"]=float(np.std(equities[1:]/equities[:-1]-1)*np.sqrt(252))*100
    measured["mean_market_exposure_pct"]=float(np.mean([float(r["market_value"])/float(r["equity"]) for r in account["equity_curve"]]))*100
    result={"metrics":measured,"ledger_validation":ledger,"economic_validation":audit,
        "entry_choices":[{"date":e["signal_date"],"choice":e["choice"],"fitted":e["learning"] is not None and e["learning"]["status"]=="fitted"}
                         for e in allocator.trace if e["action"]=="entry"]}
    if detail:
        result.update(account=account,signal_trace=allocator.trace,daily_checks=allocator.checks)
    return result


def outcome_records(data,calendar):
    rows={m:[] for m in MODES}; labels=[]
    choices=[*data["class_groups"]["stock"],"cash"]
    for index in range(252,len(data["days"])-HORIZON-3,10):
        end=index+HORIZON+1; context=raw_context(data,index)
        core=economic_trial(data,calendar,"core",start=index,end=end,detail=False)
        candidates={}
        for candidate in choices:
            trial=economic_trial(data,calendar,"core",start=index,end=end,fixed_candidate=candidate,detail=False)
            increment=(trial["metrics"]["total_return_pct"]-core["metrics"]["total_return_pct"])/100
            candidates[candidate]=trial
            for mode in MODES:
                features=economic_features(data,candidate,index,mode)
                rebuilt=rebuilt_features(data,candidate,index,mode,context=context)
                if not np.isfinite(features).all() or not np.allclose(features,rebuilt,rtol=1e-8,atol=1e-9):
                    raise ValueError("Portfolio increment features differ from independent past-data rebuild")
                rows[mode].append({"signal_date":data["days"][index],"label_end":data["days"][index+HORIZON+3],
                    "code":candidate,"features":features,"outcomes":[increment]})
        labels.append({"signal_date":data["days"][index],"exit_date":data["days"][end],"label_end":data["days"][index+HORIZON+3],
                       "core":core,"candidates":candidates})
    return rows,labels


def audit_outcome_differences(rows,labels,calendar):
    dates=[r["signal_date"] for r in labels]
    if dates!=sorted(set(dates)):
        raise ValueError("Economic label dates are duplicated or out of order")
    for label in labels:
        expected_exit=calendar.shift(date.fromisoformat(label["signal_date"]),21).isoformat()
        expected_settled=calendar.shift(date.fromisoformat(expected_exit),2).isoformat()
        if label["exit_date"]!=expected_exit or label["label_end"]!=expected_settled:
            raise ValueError("Economic teacher label matured before its declared lease/cash clock")
        for trial in [label["core"],*label["candidates"].values()]:
            measured=trial["metrics"]
            rebuilt=float((Decimal(measured["final_equity"])/100000-1)*100)
            if (measured["start_date"]!=label["signal_date"] or measured["end_date"]!=expected_exit
                    or not np.isfinite(measured["total_return_pct"]) or abs(rebuilt-measured["total_return_pct"])>1e-10):
                raise ValueError("Economic teacher return differs from its actual final account wealth")
    wanted={(r["signal_date"],c):(r["label_end"],(v["metrics"]["total_return_pct"]-r["core"]["metrics"]["total_return_pct"])/100)
            for r in labels for c,v in r["candidates"].items()}
    observed={(r["signal_date"],r["code"]):r for r in rows}
    if len(rows)!=len(observed) or set(observed)!=set(wanted):
        raise ValueError("Economic training grid differs from audited candidate accounts")
    for key,(maturity,increment) in wanted.items():
        row=observed[key]
        if (row["label_end"]!=maturity or len(row["outcomes"])!=1 or not np.isfinite(row["outcomes"][0])
                or abs(row["outcomes"][0]-increment)>1e-12):
            raise ValueError("Economic label is not the actual cash-settled candidate-minus-core account return")
    return {"status":"passed","records":len(rows),"signal_dates":len(labels)}


def run():
    protocol=json.loads(PROTOCOL.read_text());reference_path=ROOT/protocol["reference"]
    reference=json.load(gzip.open(reference_path,"rt",encoding="utf-8"))
    for key,path in (("input_sha256",INPUT),("bond_input_sha256",BONDS),("calendar_sha256",CALENDAR)):
        if sha256(path.read_bytes()).hexdigest()!=reference[key]:
            raise ValueError(f"Portfolio increment frozen input changed: {key}")
    for path,digest in reference["source_hashes"].items():
        if sha256((ROOT/path).read_bytes()).hexdigest()!=digest:
            raise ValueError(f"Unrelated portfolio increment source changed: {path}")
    calendar=TradingCalendar(json.loads(CALENDAR.read_text()));funds,audit=load_funds(INPUT,calendar)
    bonds,bond_audit=load_funds(BONDS,calendar);funds.update(bonds);results={}
    for name in protocol["cohorts"]:
        frozen=reference["cohorts"][name];data=prepare_economic(prepare_local(funds,frozen["codes"],calendar))
        records,labels=outcome_records(data,calendar)
        checks={m:audit_outcome_differences(rows,labels,calendar) for m,rows in records.items()}
        data["increment_records"]=records
        data["increment_forecasters"]={m:MaturedForecaster(rows) for m,rows in records.items()}
        data["increment_model_audit_cache"]={m:{} for m in MODES}
        full={p:economic_trial(data,calendar,p) for p in [*protocol["controls"],*MODES]}
        stress={}
        for mode in MODES:
            stress[mode]={}
            for fee in protocol["stress"]["actual_fee_bps_each_side"]:
                stress[mode][f"fee{fee}"]=economic_trial(data,calendar,mode,fee=Decimal(fee),detail=False)
            for phase in protocol["stress"]["entry_defer_sessions"]:
                stress[mode][f"defer{phase}"]=economic_trial(data,calendar,mode,phase=phase,detail=False)
            stress[mode]["lag2"]=economic_trial(data,calendar,mode,lag=2,detail=False)
        episodes={}
        for episode in reference["regime_protocol"]["episodes"]:
            coverage=effective_episode(data["days"],episode);variants={}
            if coverage["status"]!="insufficient_shared_history":
                for policy in full:
                    variants[policy]={"fresh":economic_trial(data,calendar,policy,start=coverage["start_index"],end=coverage["end_index"],
                        detail=False,deadline=True),"carry":carry_slice(full[policy]["account"],coverage["start_date"],coverage["end_date"])}
            episodes[episode["id"]]={"coverage":coverage,"strategies":variants}
        results[name]={"codes":frozen["codes"],"full":full,"stress":stress,"episodes":episodes,
            "training_records":records,"label_accounts":labels,"label_validation":checks}
        print(json.dumps({"cohort":name,"return_drawdown":{p:[v["metrics"]["total_return_pct"],v["metrics"]["max_drawdown_pct"]] for p,v in full.items()},
            "teacher_dates":len(labels),"teacher_accounts":sum(1+len(r["candidates"]) for r in labels)},ensure_ascii=False),flush=True)
    paths=set(reference["source_hashes"])|{"backend/analysis/portfolio_increment.py","scripts/research_portfolio_increments.py","scripts/validate_portfolio_increment.py"}
    snapshots={p:(ROOT/p).read_text(encoding="utf-8") for p in sorted(paths)}
    return {"generated_at":datetime.now(timezone.utc).isoformat(),"cohorts":results,"protocol":protocol,"regime_protocol":reference["regime_protocol"],
        "protocol_sha256":sha256(PROTOCOL.read_bytes()).hexdigest(),"source_snapshots":snapshots,"source_hashes":{p:sha256(s.encode()).hexdigest() for p,s in snapshots.items()},
        "reference_sha256":sha256(reference_path.read_bytes()).hexdigest(),"input_sha256":reference["input_sha256"],"bond_input_sha256":reference["bond_input_sha256"],
        "calendar_sha256":reference["calendar_sha256"],"fund_audit":audit+bond_audit}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,default=ROOT/"runtime/verification/portfolio-increment-research-v1.json.gz")
    args=parser.parse_args();report=run();args.output.parent.mkdir(parents=True,exist_ok=True)
    with gzip.open(args.output,"wt",encoding="utf-8") as output:
        json.dump(report,output,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({"output":str(args.output)}))


if __name__=="__main__":
    main()
