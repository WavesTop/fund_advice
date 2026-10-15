"""Paired incumbent/partial action experiments with real FIFO fund accounts."""
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

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.class_return_forecast import class_records
from backend.analysis.cross_asset_trend import class_statistics, prepare_class_proxies
from backend.analysis.fund_allocation import simulate_allocations
from backend.analysis.gradual_class_allocation import GradualClassAllocator
from backend.analysis.matured_trend_forecast import MaturedForecaster
from backend.core.trading_calendar import TradingCalendar
from scripts.research_class_return_forecasts import forecast_trial
from scripts.research_cross_asset_trends import EXECUTION
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import carry_slice, effective_episode
from scripts.search_fund_allocations import metrics
from scripts.validate_class_return_forecast import audit_class_records
from scripts.validate_cross_asset_trend import audit_current_holdings
from scripts.validate_fund_allocation import validate_allocation
from scripts.validate_gradual_class_allocation import audit_gradual_choice

PROTOCOL = ROOT / "config/gradual-class-allocation-research-v1.json"


def gradual_trial(data, calendar, mode, action, review, *, start=252, end=None, fee=Decimal(10), phase=0, lag=1, detail=True, deadline=False):
    allocator = GradualClassAllocator(data, mode, data["class_records"][mode], action=action, fee_bps=float(fee),
                                      forecaster=data["class_forecasters"][mode])
    execution = replace(EXECUTION, initial_review=True, review_sessions=review, execution_lag_sessions=lag,
                        deadline_entry_guard_days=7 if deadline else 0)
    account = simulate_allocations(data, calendar, allocator, start=start, end=end, phase=phase, fee_bps=fee,
                                   execution=execution, include_portfolio_state=True)
    checked = validate_allocation(account, data, calendar)
    indices = {day: i for i, day in enumerate(data["days"])}
    previous, previous_targets = None, None
    for entry in allocator.trace:
        index = indices[entry["signal_date"]]
        audit_gradual_choice(entry, data, index, mode, action, data["class_records"][mode], previous,
                             training_cache=data["training_audit_cache"][mode], previous_targets=previous_targets)
        previous = index
        previous_targets = entry["targets"]
    measured = metrics(account)
    values = np.asarray([float(row["equity"]) for row in account["equity_curve"]])
    measured["realized_annual_volatility_pct"] = float(np.std(values[1:] / values[:-1] - 1) * np.sqrt(252)) * 100
    measured["mean_market_exposure_pct"] = float(np.mean([float(row["market_value"]) / float(row["equity"]) for row in account["equity_curve"]])) * 100
    result = {"metrics": measured, "ledger_validation": checked,
              "gradual_validation": {"status": "passed", "decisions": len(allocator.trace),
                  "incumbent_selected": sum(e["gradual_choice"]["chosen"] == "incumbent" for e in allocator.trace),
                  "fitted_decisions": sum(e["gradual_choice"]["immediate_decision"]["forecast_choice"]["learning"]["status"] == "fitted" for e in allocator.trace)},
              "current_holdings_validation": audit_current_holdings(account, allocator.trace, data)}
    if detail:
        result.update(account=account, signal_trace=allocator.trace)
    return result


def run():
    protocol = json.loads(PROTOCOL.read_text())
    reference_path = ROOT / protocol["reference"]
    reference = json.load(gzip.open(reference_path, "rt", encoding="utf-8"))
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Gradual frozen input changed: {key}")
    changes = {}
    for path, digest in reference["source_hashes"].items():
        current = sha256((ROOT / path).read_bytes()).hexdigest()
        if current != digest:
            if path not in protocol["reference_source_changes_allowed"]:
                raise ValueError(f"Unrelated gradual source changed: {path}")
            changes[path] = {"reference": digest, "current": current}
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, fund_audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    results, unchanged = {}, 0
    for name in protocol["cohorts"]:
        frozen = reference["cohorts"][name]
        data = prepare_class_proxies(prepare_local(funds, frozen["codes"], calendar))
        data["cross_stats"] = {i: class_statistics(data, i) for i in range(252, len(data["days"]))}
        data["class_records"] = {mode: class_records(data, mode) for mode in protocol["modes"]}
        record_checks = {m: audit_class_records(data, rows, m) for m, rows in data["class_records"].items()}
        data["class_forecasters"] = {m: MaturedForecaster(rows) for m, rows in data["class_records"].items()}
        data["training_audit_cache"] = {m: {} for m in protocol["modes"]}
        full, stress = {}, {}
        for mode in ("raw", "static", *protocol["modes"]):
            full[mode] = {"immediate": {}}
            for review in protocol["reviews"]:
                trial = forecast_trial(data, calendar, mode, review)
                if trial["metrics"] != frozen["full"][mode][str(review)]["metrics"]:
                    raise ValueError("Gradual support changed a frozen immediate account")
                full[mode]["immediate"][str(review)] = trial
                unchanged += 1
            if mode not in protocol["modes"]:
                continue
            stress[mode] = {}
            for action in protocol["actions"]:
                full[mode][action], stress[mode][action] = {}, {}
                for review in protocol["reviews"]:
                    full[mode][action][str(review)] = gradual_trial(data, calendar, mode, action, review)
                    for fee in protocol["stress"]["ordinary_fee_bps_each_side"]:
                        stress[mode][action][f"review{review}_fee{fee}"] = gradual_trial(data, calendar, mode, action, review, fee=Decimal(fee), detail=False)
                    phases = protocol["stress"]["five_day_phases" if review == 5 else "twenty_day_phases"]
                    for phase in phases:
                        stress[mode][action][f"review{review}_phase{phase}"] = gradual_trial(data, calendar, mode, action, review, phase=phase, detail=False)
                    stress[mode][action][f"review{review}_lag2"] = gradual_trial(data, calendar, mode, action, review, lag=2, detail=False)
        episodes = {}
        for episode in reference["regime_protocol"]["episodes"]:
            coverage = effective_episode(data["days"], episode)
            if coverage != frozen["episodes"][episode["id"]]["coverage"]:
                raise ValueError("Gradual study changed frozen episode coverage")
            variants, controls = {}, {}
            if coverage["status"] != "insufficient_shared_history":
                for mode, actions in full.items():
                    controls[mode] = {str(review): {"fresh_reference": frozen["episodes"][episode["id"]]["strategies"][mode][str(review)]["fresh"]["metrics"],
                        "carry": carry_slice(actions["immediate"][str(review)]["account"], coverage["start_date"], coverage["end_date"])} for review in protocol["reviews"]}
                    if mode not in protocol["modes"]:
                        continue
                    variants[mode] = {}
                    for action in protocol["actions"]:
                        variants[mode][action] = {}
                        for review in protocol["reviews"]:
                            fresh = gradual_trial(data, calendar, mode, action, review, start=coverage["start_index"], end=coverage["end_index"], detail=False, deadline=True)
                            carried = carry_slice(actions[action][str(review)]["account"], coverage["start_date"], coverage["end_date"])
                            variants[mode][action][str(review)] = {"fresh": fresh, "carry": carried}
            episodes[episode["id"]] = {"coverage": coverage, "strategies": variants, "immediate_controls": controls}
        results[name] = {"codes": frozen["codes"], "full": full, "stress": stress, "episodes": episodes, "training_record_validation": record_checks}
        print(json.dumps({"cohort": name, "return_drawdown": {m: {a: {v: [t["metrics"]["total_return_pct"], t["metrics"]["max_drawdown_pct"]]
            for v, t in variants.items()} for a, variants in actions.items()} for m, actions in full.items()}}, ensure_ascii=False), flush=True)
    paths = set(reference["source_hashes"]) | {"backend/analysis/gradual_class_allocation.py", "scripts/validate_gradual_class_allocation.py", "scripts/research_gradual_class_allocations.py"}
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "protocol": protocol,
            "regime_protocol": reference["regime_protocol"], "market_episodes": reference["market_episodes"],
            "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "source_snapshots": snapshots,
            "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()},
            "input_sha256": reference["input_sha256"], "bond_input_sha256": reference["bond_input_sha256"],
            "calendar": reference["calendar"], "calendar_sha256": reference["calendar_sha256"],
            "reference_sha256": sha256(reference_path.read_bytes()).hexdigest(), "reference_source_differences": changes,
            "immediate_baseline_accounts_unchanged": unchanged, "fund_audit": fund_audit + bond_audit}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/gradual-class-allocation-research-v1.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))


if __name__ == "__main__":
    main()
