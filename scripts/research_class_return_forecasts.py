"""Calibrated class holding-return forecasts versus historical-mean controls."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
import json
from pathlib import Path
import statistics
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.class_return_forecast import CLASSES, ClassForecastAllocator, class_features, class_holding_return, class_records
from backend.analysis.cross_asset_trend import class_statistics, prepare_class_proxies
from backend.analysis.fund_allocation import simulate_allocations
from backend.analysis.matured_trend_forecast import MaturedForecaster
from backend.core.trading_calendar import TradingCalendar
from scripts.research_cross_asset_trends import EXECUTION
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import carry_slice, effective_episode
from scripts.research_phased_class_trends import phased_trial
from scripts.search_fund_allocations import metrics
from scripts.validate_class_return_forecast import audit_class_records, audit_forecast_choice
from scripts.validate_cross_asset_trend import audit_current_holdings
from scripts.validate_fund_allocation import validate_allocation

PROTOCOL = ROOT / "config/class-return-forecast-research-v1.json"


def forecast_trial(data, calendar, mode, review, *, start=252, end=None, fee=Decimal(10), phase=0, lag=1, detail=True, deadline=False):
    if mode in ("raw", "static"):
        policy = "dual_return60" if mode == "raw" else "risky_pair_static60"
        return phased_trial(data, calendar, policy, f"grid{review}", start=start, end=end, fee=fee, phase=phase,
                            lag=lag, detail=detail, deadline=deadline)
    allocator = ClassForecastAllocator(data, mode, data["class_records"][mode], fee_bps=float(fee),
                                       forecaster=data["class_forecasters"][mode])
    execution = replace(EXECUTION, initial_review=True, review_sessions=review, execution_lag_sessions=lag,
                        deadline_entry_guard_days=7 if deadline else 0)
    account = simulate_allocations(data, calendar, allocator, start=start, end=end, phase=phase, fee_bps=fee,
                                   execution=execution, include_portfolio_state=True)
    check = validate_allocation(account, data, calendar)
    indices = {day: i for i, day in enumerate(data["days"])}
    for entry in allocator.trace:
        audit_forecast_choice(entry, data, indices[entry["signal_date"]], mode, data["class_records"][mode],
                              training_cache=data["training_audit_cache"][mode])
    measured = metrics(account)
    equities = np.asarray([float(row["equity"]) for row in account["equity_curve"]])
    measured["realized_annual_volatility_pct"] = float(np.std(equities[1:] / equities[:-1] - 1) * np.sqrt(252)) * 100
    measured["mean_market_exposure_pct"] = float(np.mean([float(row["market_value"]) / float(row["equity"]) for row in account["equity_curve"]])) * 100
    result = {"metrics": measured, "ledger_validation": check,
              "signal_and_risk_validation": {"status": "passed", "decisions": len(allocator.trace)},
              "current_holdings_validation": audit_current_holdings(account, allocator.trace, data),
              "forecast_validation": {"status": "passed", "fitted_decisions": sum(e["forecast_choice"]["learning"]["status"] == "fitted" for e in allocator.trace)}}
    if detail:
        result.update(account=account, signal_trace=allocator.trace)
    return result


def prediction_check(data):
    rows = []
    for at in range(252, len(data["days"]) - 61, 60):
        forecasts = {}
        for mode in ("class_mean", "trend_ridge"):
            model, learning = data["class_forecasters"][mode].at(data["days"][at])
            if model is None:
                continue
            predicted = dict(zip(CLASSES, (max(-1., float(v)) for v in model.predict([class_features(data, c, at, mode) for c in CLASSES])[:, 0])))
            forecasts[mode] = predicted
        if len(forecasts) != 2:
            continue
        actual = {c: class_holding_return(data, c, at) for c in CLASSES}
        rows.append({"signal_date": data["days"][at], "label_end": data["days"][at + 61], "actual": actual, "predictions": forecasts})
    summaries = {}
    for mode in ("class_mean", "trend_ridge"):
        errors, correct = [], []
        for row in rows:
            actual = row["actual"]["stock"] - row["actual"]["gold"]
            predicted = row["predictions"][mode]["stock"] - row["predictions"][mode]["gold"]
            errors.append((predicted - actual) ** 2)
            if abs(actual) > 1e-12:
                correct.append((predicted > 0) == (actual > 0))
        summaries[mode] = {"dates": len(rows), "stock_gold_spread_rmse_pp": (statistics.fmean(errors) ** .5 * 100) if errors else None,
                           "stock_gold_winner_accuracy": statistics.fmean(correct) if correct else None}
    return {"summaries": summaries, "windows": rows}


def run():
    protocol = json.loads(PROTOCOL.read_text())
    reference_path = ROOT / protocol["reference"]
    reference = json.load(gzip.open(reference_path, "rt", encoding="utf-8"))
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Frozen forecast input differs: {key}")
    differences = {}
    for path, digest in reference["source_hashes"].items():
        current = sha256((ROOT / path).read_bytes()).hexdigest()
        if current != digest:
            if path != "scripts/validate_cross_asset_trend.py":
                raise ValueError(f"Unrelated forecast reference source changed: {path}")
            differences[path] = {"reference": digest, "current": current}
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, fund_audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    results, unchanged = {}, 0
    for name in protocol["cohorts"]:
        frozen = reference["cohorts"][name]
        data = prepare_class_proxies(prepare_local(funds, frozen["codes"], calendar))
        data["cross_stats"] = {i: class_statistics(data, i) for i in range(252, len(data["days"]))}
        data["class_records"] = {mode: class_records(data, mode) for mode in ("class_mean", "trend_ridge")}
        record_checks = {m: audit_class_records(data, records, m) for m, records in data["class_records"].items()}
        data["class_forecasters"] = {m: MaturedForecaster(records, penalty=1., minimum_dates=24) for m, records in data["class_records"].items()}
        data["training_audit_cache"] = {m: {} for m in data["class_records"]}
        full, stress = {}, {}
        for mode in protocol["modes"]:
            full[mode], stress[mode] = {}, {}
            for review in protocol["reviews"]:
                full[mode][str(review)] = forecast_trial(data, calendar, mode, review)
                if mode in ("raw", "static"):
                    policy = "dual_return60" if mode == "raw" else "risky_pair_static60"
                    if full[mode][str(review)]["metrics"] != frozen["full"][policy][f"grid{review}"]["baseline"]["metrics"]:
                        raise ValueError("Forecast support changed corrected baseline account")
                    unchanged += 1
                else:
                    for fee in protocol["stress"]["ordinary_fee_bps_each_side"]:
                        stress[mode][f"review{review}_fee{fee}"] = forecast_trial(data, calendar, mode, review, fee=Decimal(fee), detail=False)
                    phases = protocol["stress"]["five_day_phases" if review == 5 else "twenty_day_phases"]
                    for phase in phases:
                        stress[mode][f"review{review}_phase{phase}"] = forecast_trial(data, calendar, mode, review, phase=phase, detail=False)
                    stress[mode][f"review{review}_lag2"] = forecast_trial(data, calendar, mode, review,
                        lag=protocol["stress"]["execution_lag_sessions"], detail=False)
        episodes = {}
        for episode in reference["regime_protocol"]["episodes"]:
            coverage = effective_episode(data["days"], episode)
            if coverage != frozen["episodes"][episode["id"]]["coverage"]:
                raise ValueError("Forecast study changed frozen regime coverage")
            segments = {}
            if coverage["status"] != "insufficient_shared_history":
                for mode in protocol["modes"]:
                    segments[mode] = {}
                    for review in protocol["reviews"]:
                        fresh = forecast_trial(data, calendar, mode, review, start=coverage["start_index"],
                            end=coverage["end_index"], detail=False, deadline=True)
                        carried = carry_slice(full[mode][str(review)]["account"], coverage["start_date"], coverage["end_date"])
                        segments[mode][str(review)] = {"fresh": fresh, "carry": carried}
            episodes[episode["id"]] = {"coverage": coverage, "strategies": segments}
        predictive = prediction_check(data)
        results[name] = {"codes": frozen["codes"], "full": full, "stress": stress, "episodes": episodes,
                         "training_record_validation": record_checks, "prediction_check": predictive}
        print(json.dumps({"cohort": name, "return_drawdown": {m: {r: [t["metrics"]["total_return_pct"], t["metrics"]["max_drawdown_pct"]]
            for r, t in cases.items()} for m, cases in full.items()}, "prediction": predictive["summaries"]}, ensure_ascii=False), flush=True)
    paths = set(reference["source_hashes"]) | {"backend/analysis/class_return_forecast.py", "backend/analysis/matured_trend_forecast.py",
                                              "scripts/validate_class_return_forecast.py", "scripts/research_class_return_forecasts.py"}
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "protocol": protocol,
            "regime_protocol": reference["regime_protocol"], "market_episodes": reference["market_episodes"],
            "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "source_snapshots": snapshots,
            "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()},
            "input_sha256": reference["input_sha256"], "bond_input_sha256": reference["bond_input_sha256"],
            "calendar": reference["calendar"], "calendar_sha256": reference["calendar_sha256"],
            "reference_sha256": sha256(reference_path.read_bytes()).hexdigest(), "reference_source_differences": differences,
            "corrected_baseline_accounts_unchanged": unchanged, "fund_audit": fund_audit + bond_audit}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/class-return-forecast-research-v1.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))


if __name__ == "__main__":
    main()
