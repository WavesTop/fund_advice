"""Predeclared portfolio families, passive baselines and past-only annual selection."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.fund_allocation import AllocationExecution, simulate_allocations
from backend.analysis.fund_allocation_signals import AllocationPlan, allocation_at, extend_features, plan_document, plans
from backend.core.trading_calendar import TradingCalendar
from scripts.backtest_sector_rotation import load_policy
from scripts.replay_fund_rotation import POLICY
from scripts.research_fund_rotation import CALENDAR, INPUT, cohort_frames, fixed_cohorts, load_funds, rolling_summary, run_account
from scripts.validate_fund_allocation import validate_allocation
from scripts.validate_sector_rotation import validate_ledger

PROTOCOL = ROOT / "config/fund-allocation-research-protocol-v1.json"


def metrics(account):
    elapsed = (date.fromisoformat(account["end_date"]) - date.fromisoformat(account["start_date"])).days / 365.2425
    equity = [float(p["equity"]) for p in account["equity_curve"]]
    changes = [b / a - 1 for a, b in zip(equity, equity[1:])]
    vol = statistics.pstdev(changes) if changes else 0
    return {key: account[key] for key in ("start_date", "end_date", "total_return_pct", "max_drawdown_pct", "total_fees",
                                        "trade_count", "short_redemption_sales", "final_equity")} | {
        "annualized_return_pct": ((equity[-1] / float(account["initial_equity"])) ** (1 / elapsed) - 1) * 100,
        "zero_cash_rate_sharpe": statistics.fmean(changes) / vol * math.sqrt(252) if vol > 1e-12 else None}


def run_plan(data, calendar, plan, *, start=252, end=None, execution=AllocationExecution(), fee=Decimal(10), phase=0):
    result = simulate_allocations(data, calendar, lambda i, held: allocation_at(data, plan, i, held),
                                  start=start, end=end, execution=execution, fee_bps=fee, phase=phase)
    check = validate_allocation(result, data, calendar)
    return result, check


def walk_forward(data, calendar, candidates):
    days = data["days"]
    years = sorted({int(day[:4]) for day in days})
    folds = []
    for year in years:
        training = [i for i, day in enumerate(days) if f"{year - 3}-01-01" <= day < f"{year}-01-01"]
        testing = [i for i, day in enumerate(days) if day.startswith(str(year))]
        if not training or training[0] < 252 or len(training) < 3 * 200 or len(testing) < 20:
            continue
        # Every selection account ends before every next-year test begins.
        train_results = {}
        for plan in candidates:
            account, check = run_plan(data, calendar, plan, start=training[0], end=training[-1])
            measured = metrics(account)
            train_results[plan.name] = {"metrics": measured, "validation": check,
                                       "utility": measured["annualized_return_pct"] - .5 * abs(measured["max_drawdown_pct"])}
        chosen = max(candidates, key=lambda p: (train_results[p.name]["utility"], p.name))
        chosen = chosen if train_results[chosen.name]["utility"] > 0 else None
        if chosen:
            account, check = run_plan(data, calendar, chosen, start=testing[0], end=testing[-1])
        else:
            account = simulate_allocations(data, calendar, lambda i, held: {}, start=testing[0], end=testing[-1])
            check = validate_allocation(account, data, calendar)
        passive, passive_check = run_plan(data, calendar, AllocationPlan("passive_hold", "hold"),
                                         start=testing[0], end=testing[-1])
        folds.append({"year": year, "train_start": days[training[0]], "train_end": days[training[-1]],
                      "selected": chosen.name if chosen else "cash", "training": train_results,
                      "test_metrics": metrics(account), "test_validation": check,
                      "passive_metrics": metrics(passive), "passive_validation": passive_check,
                      "excess_return_pct": account["total_return_pct"] - passive["total_return_pct"],
                      "account": account})
    return {"folds": folds, "summary": {
        "fold_count": len(folds),
        "positive_folds": sum(f["test_metrics"]["total_return_pct"] > 0 for f in folds),
        "beat_passive_folds": sum(f["excess_return_pct"] > 0 for f in folds),
        "mean_return_pct": statistics.fmean(f["test_metrics"]["total_return_pct"] for f in folds) if folds else None,
        "mean_excess_return_pct": statistics.fmean(f["excess_return_pct"] for f in folds) if folds else None,
        "scope": "Fresh annual accounts; not a compounded continuous executable portfolio. Previously viewed histories."}}


def run():
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, audit = load_funds(INPUT, calendar)
    groups = {k: v for k, v in fixed_cohorts(funds).items() if k in
              ("all_13_themes", "themes_available_2020", "themes_available_2015")}
    groups["long_multi_asset"] = ["000217", "000051", "000008"]
    candidates = plans()
    baselines = [AllocationPlan("passive_hold", "hold"), AllocationPlan("equal_monthly", "equal")]
    results = {}
    for name, codes in groups.items():
        data = cohort_frames(funds, codes, calendar)
        extend_features(data)
        runs, checks = {}, {}
        for plan in candidates + baselines:
            account, check = run_plan(data, calendar, plan)
            runs[plan.name], checks[plan.name] = metrics(account), check
        previous = run_account(data, calendar, replace(load_policy(POLICY), max_positions=min(10, len(codes))), start=252)
        runs["previous_frozen"] = metrics(previous)
        checks["previous_frozen"] = validate_ledger(previous, data["days"], data["prices"], calendar=calendar,
                                                  distributions=data["distributions"])
        validation = walk_forward(data, calendar, candidates)
        # Do not call a best whole-history result a selected forward strategy.
        results[name] = {"codes": codes, "full_history": runs, "validation": checks, "walk_forward": validation}
        print(json.dumps({"cohort": name, "results": {key: [round(v["total_return_pct"], 3), round(v["max_drawdown_pct"], 3)]
                                                     for key, v in runs.items()}, "annual_validation": validation["summary"]}, ensure_ascii=False), flush=True)
        if name == "all_13_themes":
            # Fixed research candidates, not selected from this cohort's final returns.
            sensitivity = {}
            for plan in candidates:
                scenarios = [(f"fee_{fee}", Decimal(fee), 0, AllocationExecution()) for fee in (50, 100)]
                scenarios += [(f"phase_{phase}", Decimal(10), phase, AllocationExecution()) for phase in (5, 10, 15)]
                scenarios += [("execution_delay2", Decimal(10), 0, AllocationExecution(execution_lag_sessions=2))]
                sensitivity[plan.name] = {}
                for label, fee, phase, execution in scenarios:
                    account, check = run_plan(data, calendar, plan, fee=fee, phase=phase, execution=execution)
                    sensitivity[plan.name][label] = {"metrics": metrics(account), "validation": check}
            results[name]["sensitivity"] = sensitivity
            rolling = {}
            for plan in candidates + [baselines[0]]:
                rolling[plan.name] = {}
                for horizon in (20, 60, 120):
                    rows, validations = [], []
                    for start in range(252, len(data["days"]) - horizon, horizon):
                        execution = AllocationExecution(deadline_entry_guard_days=7)
                        account, check = run_plan(data, calendar, plan, start=start, end=start + horizon, execution=execution)
                        rows.append({"start_date": account["start_date"], "end_date": account["end_date"],
                                     "return_pct": account["total_return_pct"], "drawdown_pct": account["max_drawdown_pct"]})
                        validations.append(check)
                    rolling[plan.name][str(horizon)] = {"summary": rolling_summary(rows), "windows": rows, "validation": validations}
            results[name]["rolling"] = rolling
    source_files = ("backend/analysis/fund_allocation.py", "backend/analysis/fund_allocation_signals.py",
                    "backend/analysis/fund_holding_costs.py", "backend/analysis/sector_trend.py",
                    "scripts/search_fund_allocations.py", "scripts/validate_fund_allocation.py", "scripts/research_fund_rotation.py")
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "protocol": json.loads(PROTOCOL.read_text()),
            "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "input_sha256": sha256(INPUT.read_bytes()).hexdigest(),
            "calendar_sha256": sha256(CALENDAR.read_bytes()).hexdigest(),
            "source_hashes": {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in source_files},
            "plans": [plan_document(p) for p in candidates], "data_audit": audit, "cohorts": results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/fund-allocation-search-v1.json")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
