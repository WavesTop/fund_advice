"""Actual fund accounts for long-only class momentum and risk/exposure controls."""
from __future__ import annotations

import argparse
from dataclasses import replace
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

from backend.analysis.cross_asset_trend import POLICIES, class_statistics, cross_asset_target, prepare_class_proxies
from backend.analysis.fund_allocation import AllocationExecution, simulate_allocations
from backend.analysis.fund_holding_costs import FundHoldingCosts
from backend.core.trading_calendar import TradingCalendar
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, fixed_cohorts, load_funds, rolling_summary
from scripts.research_local_trend_models import prepare_local
from scripts.search_fund_allocations import metrics
from scripts.validate_cross_asset_trend import audit_cross_asset_decision, audit_current_holdings
from scripts.validate_fund_allocation import validate_allocation
from scripts.validate_holding_release import audit_holding_release

PROTOCOL = ROOT / "config/fund-cross-asset-protocol-v1.json"
EXECUTION = AllocationExecution(review_sessions=20, rebalance_band=.03, relative_rebalance_band=.25)


def replay(data, calendar, policy, *, start=252, end=None, phase=0, fee=Decimal(10), execution=EXECUTION, detail=True,
           extra_review_indices=frozenset(), exit_mode="legacy", costs=FundHoldingCosts()):
    trace = []

    def allocate(index, held, state=None):
        target, meta = cross_asset_target(data, policy, index, held, stats=data["cross_stats"][index],
                                        exit_mode=exit_mode, portfolio_state=state)
        trace.append(meta | {"targets": target.weights})
        return target

    account = simulate_allocations(data, calendar, allocate, start=start, end=end, phase=phase, fee_bps=fee, execution=execution,
                                   extra_review_indices=extra_review_indices, include_portfolio_state=exit_mode != "legacy", costs=costs)
    check = validate_allocation(account, data, calendar)
    indexes = {day: i for i, day in enumerate(data["days"])}
    for entry in trace:
        audit_cross_asset_decision(entry, data, indexes[entry["signal_date"]], policy, exit_mode=exit_mode)
    measured = metrics(account)
    equities = np.asarray([float(row["equity"]) for row in account["equity_curve"]])
    realized = float(np.std(equities[1:] / equities[:-1] - 1) * np.sqrt(252))
    measured["realized_annual_volatility_pct"] = realized * 100
    measured["mean_market_exposure_pct"] = float(np.mean([float(row["market_value"]) / float(row["equity"]) for row in account["equity_curve"]])) * 100
    result = {"metrics": measured, "ledger_validation": check,
              "signal_and_risk_validation": {"status": "passed", "decisions": len(trace)}}
    if exit_mode != "legacy":
        result["current_holdings_validation"] = audit_current_holdings(account, trace, data)
    if execution.review_on_holding_release:
        result["holding_release_validation"] = audit_holding_release(account, data, calendar, trace)
    if detail:
        result.update(account=account, signal_trace=trace)
    return result


def run():
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, fund_audit = load_funds(INPUT, calendar)
    groups = fixed_cohorts(funds)
    bonds, bond_audit = load_funds(BONDS, calendar)
    if set(bonds) != {"000016", "006669", "009803"}:
        raise ValueError("Defensive universe incomplete")
    funds.update(bonds)
    definitions = {"all13": groups["all_13_themes"] + list(bonds), "older9": groups["themes_available_2020"] + ["000016"],
                   "older4": groups["themes_available_2015"] + ["000016"], "long4": ["000217", "000051", "000008", "000016"]}
    results = {}
    for name, codes in definitions.items():
        data = prepare_class_proxies(prepare_local(funds, codes, calendar))
        data["cross_stats"] = {i: class_statistics(data, i) for i in range(252, len(data["days"]))}
        runs, annual, rolling, stress = {}, {}, {}, {}
        continuity = replay(data, calendar, "legacy_into_bonds40", execution=replace(EXECUTION, review_sessions=10), detail=False)
        for policy in POLICIES:
            runs[policy] = replay(data, calendar, policy)
            for year in sorted({day[:4] for day in data["days"]}):
                indexes = [i for i, day in enumerate(data["days"]) if day.startswith(year)]
                if indexes[0] < 252 or len(indexes) < 20:
                    continue
                annual.setdefault(year, {})[policy] = replay(data, calendar, policy, start=indexes[0], end=indexes[-1], detail=False)
            if name == "all13":
                rolling[policy] = {}
                for horizon in (20, 60, 120):
                    rows, checks = [], []
                    for start in range(252, len(data["days"]) - horizon, horizon):
                        trial = replay(data, calendar, policy, start=start, end=start + horizon, detail=False,
                                       execution=replace(EXECUTION, deadline_entry_guard_days=7))
                        rows.append({"start_date": trial["metrics"]["start_date"], "end_date": trial["metrics"]["end_date"],
                                     "return_pct": trial["metrics"]["total_return_pct"], "drawdown_pct": trial["metrics"]["max_drawdown_pct"]})
                        checks.append({"ledger": trial["ledger_validation"], "signals_and_risk": trial["signal_and_risk_validation"]})
                    rolling[policy][str(horizon)] = {"summary": rolling_summary(rows), "windows": rows, "validation": checks}
                scenarios = [(f"fee{f}", Decimal(f), 0, EXECUTION) for f in (50, 100)]
                scenarios += [(f"phase{p}", Decimal(10), p, EXECUTION) for p in (5, 10, 15)]
                scenarios += [("execution_lag2", Decimal(10), 0, replace(EXECUTION, execution_lag_sessions=2))]
                stress[policy] = {label: replay(data, calendar, policy, fee=fee, phase=p, execution=execution, detail=False)
                                  for label, fee, p, execution in scenarios}
        results[name] = {"codes": codes, "class_groups": data["class_groups"], "runs": runs, "annual": annual,
                         "rolling": rolling, "stress": stress, "continuity_reference_review10": continuity}
        print(json.dumps({"cohort": name, "metrics": {p: [round(v["metrics"]["total_return_pct"], 3),
                            round(v["metrics"]["max_drawdown_pct"], 3), round(v["metrics"]["realized_annual_volatility_pct"], 3)]
                            for p, v in runs.items()}}, ensure_ascii=False), flush=True)
    paths = ("scripts/research_cross_asset_trends.py", "scripts/validate_cross_asset_trend.py", "backend/analysis/cross_asset_trend.py",
             "backend/analysis/fund_allocation.py", "backend/analysis/fund_holding_costs.py", "backend/analysis/trend_risk_budget.py",
             "backend/analysis/fund_allocation_signals.py", "backend/analysis/sector_trend.py", "backend/analysis/strategy_risk.py",
             "scripts/research_local_trend_models.py", "scripts/research_calibrated_trends.py", "scripts/research_defensive_allocations.py",
             "scripts/research_fund_rotation.py", "scripts/search_fund_allocations.py", "scripts/validate_fund_allocation.py",
             "backend/analysis/matured_trend_forecast.py", "backend/core/trading_calendar.py")
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in paths}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "fund_audit": fund_audit + bond_audit,
            "protocol": json.loads(PROTOCOL.read_text()), "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(),
            "input_sha256": sha256(INPUT.read_bytes()).hexdigest(), "bond_input_sha256": sha256(BONDS.read_bytes()).hexdigest(),
            "calendar": json.loads(CALENDAR.read_text()), "calendar_sha256": sha256(CALENDAR.read_bytes()).hexdigest(),
            "source_hashes": {p: sha256(s.encode("utf-8")).hexdigest() for p, s in snapshots.items()}, "source_snapshots": snapshots,
            "limitations": ["Long-only retail funds, not the cited futures long/short strategy",
                            "Synthetic equal-weight class proxies are signals only, actual accounts incur trades and fees",
                            "10% past-covariance constraint is not guaranteed future volatility or maximum drawdown",
                            "Repeated viewed historical pools and current survivor products, no untouched OOS",
                            "Ordinary fee, settlement, dividend and NAV-publication assumptions are not verified per product"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/fund-cross-asset-trends-return-v4.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8", compresslevel=6) as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))


if __name__ == "__main__":
    main()
