"""Aggregate trend/risk budgets with proportional trading bands and controls."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

from backend.analysis.fund_allocation import AllocationExecution, simulate_allocations
from backend.analysis.trend_risk_budget import budget_policies, budget_statistics, projected_volatility, risk_budget_target
from backend.core.trading_calendar import TradingCalendar
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, fixed_cohorts, load_funds, rolling_summary
from scripts.research_local_trend_models import prepare_local
from scripts.search_fund_allocations import metrics
from scripts.validate_fund_allocation import validate_allocation

PROTOCOL = ROOT / "config/trend-risk-budget-protocol-v1.json"
EXECUTION = AllocationExecution(review_sessions=10, rebalance_band=.03, relative_rebalance_band=.25)


def replay(data, calendar, policy, *, start=252, end=None, phase=0, fee=Decimal(10), execution=EXECUTION, detail=True):
    trace = []
    def allocate(index, held):
        targets, meta = risk_budget_target(data, policy, index, held, stats=data["risk_stats"][index])
        trace.append(meta | {"targets": targets.weights})
        return targets
    account = simulate_allocations(data, calendar, allocate, start=start, end=end, phase=phase, fee_bps=fee, execution=execution)
    check = validate_allocation(account, data, calendar)
    checks = []
    by_day = {d: i for i, d in enumerate(data["days"])}
    for entry in trace:
        # Reconstruct risk from observed mark returns, not the policy's stored matrix.
        index = by_day[entry["signal_date"]]
        codes = list(data["prices"])
        daily = np.asarray([[np.log(data["marks"][c][i] / data["marks"][c][i - 1])
                             for i in range(index - 59, index + 1)] for c in codes])
        realized_series = np.asarray([entry["targets"].get(c, 0) for c in codes]) @ daily
        rebuilt = float(np.std(realized_series) * np.sqrt(252))
        if abs(rebuilt - entry["projected_volatility"]) > 1e-10:
            raise ValueError("Risk budget forecast cannot be reconstructed from past observations")
        if policy.volatility_target and entry["projected_volatility"] > policy.volatility_target + 1e-10:
            raise ValueError("Risk target exceeds its declared forecast constraint")
        checks.append(entry["signal_date"])
    result = {"metrics": metrics(account), "ledger_validation": check,
              "risk_validation": {"status": "passed", "decisions": len(checks), "first": min(checks, default=None), "last": max(checks, default=None)}}
    if detail:
        result.update(account=account, risk_trace=trace)
    return result


def run(account_start=252):
    if account_start not in (120, 252):
        raise ValueError("Unsupported account warmup")
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, _ = load_funds(INPUT, calendar)
    groups = fixed_cohorts(funds)
    bonds, _ = load_funds(BONDS, calendar)
    if set(bonds) != {"000016", "006669", "009803"}:
        raise ValueError("Defensive universe incomplete")
    funds.update(bonds)
    definitions = {"all13": groups["all_13_themes"] + list(bonds), "older9": groups["themes_available_2020"] + ["000016"],
                   "older4": groups["themes_available_2015"] + ["000016"], "long4": ["000217", "000051", "000008", "000016"]}
    results = {}
    for name, codes in definitions.items():
        data = prepare_local(funds, codes, calendar)
        data["risk_stats"] = {i: budget_statistics(data, i) for i in range(account_start, len(data["days"]))}
        runs, annual, rolling, stress = {}, {}, {}, {}
        for policy in budget_policies():
            runs[policy.name] = replay(data, calendar, policy, start=account_start)
            for year in sorted({d[:4] for d in data["days"]}):
                indexes = [i for i, d in enumerate(data["days"]) if d.startswith(year)]
                if indexes[0] < account_start or len(indexes) < 20:
                    continue
                annual.setdefault(year, {})[policy.name] = replay(data, calendar, policy,
                                                                  start=indexes[0], end=indexes[-1], detail=False)
            if name == "all13":
                rolling[policy.name] = {}
                for horizon in (20, 60, 120):
                    rows, checks = [], []
                    for start in range(account_start, len(data["days"]) - horizon, horizon):
                        result = replay(data, calendar, policy, start=start, end=start + horizon, detail=False,
                                        execution=replace(EXECUTION, deadline_entry_guard_days=7))
                        rows.append({"start_date": result["metrics"]["start_date"], "end_date": result["metrics"]["end_date"],
                                     "return_pct": result["metrics"]["total_return_pct"], "drawdown_pct": result["metrics"]["max_drawdown_pct"]})
                        checks.append({"ledger": result["ledger_validation"], "risk": result["risk_validation"]})
                    rolling[policy.name][str(horizon)] = {"summary": rolling_summary(rows), "windows": rows, "validation": checks}
                scenarios = [(f"fee{f}", Decimal(f), 0, EXECUTION) for f in (50, 100)]
                scenarios += [(f"phase{p}", Decimal(10), p, EXECUTION) for p in (2, 5, 7)]
                scenarios += [("delay2", Decimal(10), 0, replace(EXECUTION, execution_lag_sessions=2))]
                stress[policy.name] = {label: replay(data, calendar, policy, start=account_start, fee=fee, phase=p, execution=execution, detail=False)
                                       for label, fee, p, execution in scenarios}
        results[name] = {"codes": codes, "runs": runs, "annual": annual, "rolling": rolling, "stress": stress}
        print(json.dumps({"cohort": name, "metrics": {p: [round(r["metrics"]["total_return_pct"], 3),
                            round(r["metrics"]["max_drawdown_pct"], 3)] for p, r in runs.items()}}, ensure_ascii=False), flush=True)
    paths = ("scripts/research_trend_risk_budget.py", "backend/analysis/trend_risk_budget.py", "backend/analysis/fund_allocation.py",
             "backend/analysis/fund_holding_costs.py", "backend/analysis/sector_trend.py", "backend/analysis/strategy_risk.py",
             "backend/analysis/matured_trend_forecast.py", "scripts/research_local_trend_models.py", "scripts/research_calibrated_trends.py",
             "scripts/research_defensive_allocations.py", "scripts/research_fund_rotation.py", "scripts/validate_fund_allocation.py",
             "scripts/search_fund_allocations.py", "backend/core/trading_calendar.py")
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in paths}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "account_start_sessions": account_start,
            "policies": [asdict(p) for p in budget_policies()], "protocol": json.loads(PROTOCOL.read_text()),
            "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "input_sha256": sha256(INPUT.read_bytes()).hexdigest(),
            "bond_input_sha256": sha256(BONDS.read_bytes()).hexdigest(), "calendar_sha256": sha256(CALENDAR.read_bytes()).hexdigest(),
            "source_hashes": {p: sha256(s.encode("utf-8")).hexdigest() for p, s in snapshots.items()}, "source_snapshots": snapshots}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/trend-risk-budget-controlled-v2.json.gz")
    parser.add_argument("--account-start", type=int, choices=(120, 252), default=252)
    args = parser.parse_args()
    report = run(args.account_start)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8", compresslevel=6) as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
