"""Local mature-label training, restored warmup history and satellite expiries."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

from backend.analysis.fund_allocation import simulate_allocations
from backend.analysis.matured_trend_forecast import MaturedForecaster, audit_training_visibility, feature_vector
from backend.core.trading_calendar import TradingCalendar
from scripts.research_calibrated_trends import CalibratedAllocator, EXECUTION, outcome_records
from scripts.research_defensive_allocations import BONDS, separate_peer_scores
from scripts.research_fund_rotation import CALENDAR, INPUT, cohort_frames, fixed_cohorts, load_funds, rolling_summary
from scripts.search_fund_allocations import metrics
from scripts.validate_fund_allocation import validate_allocation

PROTOCOL = ROOT / "config/fund-local-model-protocol-v1.json"


@dataclass(frozen=True)
class LocalPlan:
    name: str
    family: str
    domain: str = "local"
    training_start: int = 122
    expiry_sessions: int | None = None


def plans(transfer=False):
    result = [LocalPlan("static40", "static_40_40_20"), LocalPlan("static24", "static_24_64_12")]
    for start in (252, 122):
        for horizon in (20, 60):
            result.append(LocalPlan(f"local{start}_{horizon}", f"ridge{horizon}", training_start=start))
    for horizon in (20, 60):
        result.append(LocalPlan(f"local122_expire{horizon}", f"ridge{horizon}", expiry_sessions=horizon))
    result.append(LocalPlan("local122_gate_expire20", "validated_ridge20", expiry_sessions=20))
    if transfer:
        for start in (252, 122):
            for horizon in (20, 60):
                result.append(LocalPlan(f"transfer{start}_{horizon}", f"ridge{horizon}", "teacher", start))
        result.append(LocalPlan("blend122_expire20", "ridge20", "blend", expiry_sessions=20))
    return result


def prepare_local(funds, codes, calendar):
    data = cohort_frames(funds, codes, calendar)
    data["roles"] = {code: funds[code]["role"] for code in codes}
    # Do not run 252-day features at index 120: negative indexes would read the future.
    for index in range(120, len(data["days"])):
        for entry in data["frames"][index]["entries"]:
            values = data["marks"][entry["code"]]
            entry["daily60"] = [math.log(values[i] / values[i - 1]) for i in range(index - 59, index + 1)]
            entry["annual_vol60"] = statistics.pstdev(entry["daily60"]) * math.sqrt(252)
    separate_peer_scores(data, start=120)
    data["vectors"] = {i: {e["code"]: feature_vector(data, e["code"], i) for e in frame["entries"]}
                       for i, frame in data["frames"].items()}
    return data


class ExpiringAllocator(CalibratedAllocator):
    def __init__(self, data, family, forecasters, *, expiry=None, fee=Decimal(10)):
        super().__init__(data, family, forecasters, fee=fee)
        if expiry is not None and expiry < 1:
            raise ValueError("Invalid target expiry")
        self.expiry, self.deadlines, self.renewals = expiry, {}, []

    def __call__(self, index, held):
        expired = [code for code in self.active if self.expiry is not None and index >= self.deadlines[code]]
        self.active = [code for code in self.active if code not in expired]
        for code in expired:
            self.deadlines.pop(code)
        result = super().__call__(index, held)
        if self.expiry is not None:
            self.deadlines = {code: self.deadlines.get(code, index + self.expiry) for code in self.active}
            self.renewals.append({"signal_index": index, "signal_date": self.data["days"][index],
                                  "expired": expired, "renewed": [code for code in expired if code in self.active],
                                  "deadlines": dict(self.deadlines)})
        return result


class BlendedForecast:
    def __init__(self, models):
        self.models = models

    def predict(self, features):
        return np.mean([m.predict(features) for m in self.models], axis=0)


class BlendedForecaster:
    def __init__(self, teacher, local):
        self.components = {"teacher": teacher, "local": local}

    def at(self, asof):
        fitted = {name: predictor.at(asof) for name, predictor in self.components.items()}
        models = [model for model, _ in fitted.values() if model is not None]
        metadata = {"status": "blended" if models else "insufficient_history", "asof": asof,
                    "validation_rank_ic": None, "available_components": [name for name, (model, _) in fitted.items() if model is not None],
                    "components": {name: meta for name, (_, meta) in fitted.items()}}
        return (BlendedForecast(models) if models else None), metadata


def audit_forecaster(predictor, metadata):
    if isinstance(predictor, BlendedForecaster):
        available = [name for name, meta in metadata["components"].items() if meta["status"] == "fitted"]
        if available != metadata["available_components"] or any(meta["asof"] != metadata["asof"] for meta in metadata["components"].values()):
            raise ValueError("Blend used unavailable or later component models")
        return {"status": "passed" if available else "not_fitted", "available_components": available,
                "components": {name: audit_training_visibility(predictor.components[name].records, meta)
                               for name, meta in metadata["components"].items()}}
    return audit_training_visibility(predictor.records, metadata)


def replay_local(data, calendar, plan, forecasters, registry, *, start=252, end=None, phase=0, fee=Decimal(10), execution=EXECUTION,
                 registry_prefix="local", detail=True):
    allocator = ExpiringAllocator(data, plan.family, forecasters, expiry=plan.expiry_sessions, fee=fee)
    account = simulate_allocations(data, calendar, allocator, start=start, end=end, phase=phase, fee_bps=fee, execution=execution)
    check = validate_allocation(account, data, calendar)
    trace = []
    for item in allocator.trace:
        key = f"{registry_prefix}/{item['horizon']}/{item['asof']}"
        if key not in registry:
            registry[key] = {"metadata": item["metadata"], "validation": audit_forecaster(forecasters[item["horizon"]], item["metadata"])}
        trace.append({key_: value for key_, value in item.items() if key_ != "metadata"} | {"model_ref": key})
    result = {"metrics": metrics(account), "ledger_validation": check, "forecast_models": sorted({r["model_ref"] for r in trace}),
              "expiry_events": allocator.renewals}
    result["funding_diagnostics"] = {
        "decisions": len(account["decisions"]),
        "decisions_with_cash_gap": sum(Decimal(d["cash_funding_gap"]) > 1 for d in account["decisions"]),
        "cash_gap_and_ignored_overweight": sum(Decimal(d["cash_funding_gap"]) > 1 and Decimal(d["overweight_ignored_by_band"]) > 1
                                              for d in account["decisions"]),
        "scope": "Decision counts, not independent trades, actual losses or proof that skipped sales alone caused gaps"}
    if detail:
        result.update(account=account, forecast_trace=trace)
    return result


def run(execution=EXECUTION):
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, _ = load_funds(INPUT, calendar)
    groups = fixed_cohorts(funds)
    bonds, _ = load_funds(BONDS, calendar)
    if set(bonds) != {"000016", "006669", "009803"}:
        raise ValueError("Defensive universe incomplete")
    funds.update(bonds)
    definitions = {"all13": groups["all_13_themes"] + list(bonds),
                   "older9": groups["themes_available_2020"] + ["000016"],
                   "older4": groups["themes_available_2015"] + ["000016"],
                   "long4": ["000217", "000051", "000008", "000016"]}
    data_sets = {name: prepare_local(funds, codes, calendar) for name, codes in definitions.items()}
    learning, predictors = {}, {}
    for name, data in data_sets.items():
        learning[name], predictors[name] = {}, {}
        for start in (252, 122):
            learning[name][start] = {h: outcome_records(data, calendar, h, start=start) for h in (20, 60)}
            predictors[name][start] = {h: MaturedForecaster(rows) for h, rows in learning[name][start].items()}
    registry, results = {}, {}
    for name, data in data_sets.items():
        candidates = plans(transfer=name == "all13")
        runs, annual, rolling, stress = {}, {}, {}, {}
        for plan in candidates:
            teacher_name = "older9" if name == "all13" else name
            teacher = predictors[teacher_name][plan.training_start]
            local = predictors[name][plan.training_start]
            selected = teacher if plan.domain == "teacher" else {
                h: BlendedForecaster(teacher[h], local[h]) for h in (20, 60)} if plan.domain == "blend" else local
            prefix = f"{teacher_name if plan.domain == 'teacher' else name}/{plan.domain}/{plan.training_start}"
            runs[plan.name] = replay_local(data, calendar, plan, selected, registry, registry_prefix=prefix, execution=execution)
            for year in sorted({day[:4] for day in data["days"]}):
                indexes = [i for i, d in enumerate(data["days"]) if d.startswith(year)]
                if indexes[0] < 252 or len(indexes) < 20:
                    continue
                annual.setdefault(year, {})[plan.name] = replay_local(data, calendar, plan, selected, registry,
                    start=indexes[0], end=indexes[-1], registry_prefix=prefix, detail=False, execution=execution)
            if name == "all13":
                rolling[plan.name] = {}
                for horizon in (20, 60, 120):
                    rows, checks = [], []
                    for start in range(252, len(data["days"]) - horizon, horizon):
                        result = replay_local(data, calendar, plan, selected, registry, start=start, end=start + horizon,
                            registry_prefix=prefix, detail=False, execution=replace(execution, deadline_entry_guard_days=7))
                        rows.append({"start_date": result["metrics"]["start_date"], "end_date": result["metrics"]["end_date"],
                                     "return_pct": result["metrics"]["total_return_pct"], "drawdown_pct": result["metrics"]["max_drawdown_pct"]})
                        checks.append({"ledger": result["ledger_validation"], "models": result["forecast_models"]})
                    rolling[plan.name][str(horizon)] = {"summary": rolling_summary(rows), "windows": rows, "validation": checks}
                scenarios = [(f"fee{f}", Decimal(f), 0, execution) for f in (50, 100)]
                scenarios += [(f"phase{p}", Decimal(10), p, execution) for p in (2, 5, 7)]
                scenarios += [("delay2", Decimal(10), 0, replace(execution, execution_lag_sessions=2))]
                stress[plan.name] = {label: replay_local(data, calendar, plan, selected, registry, fee=fee, phase=p,
                    execution=scenario_execution, registry_prefix=prefix, detail=False) for label, fee, p, scenario_execution in scenarios}
        results[name] = {"codes": definitions[name], "runs": runs, "annual": annual, "rolling": rolling, "stress": stress}
        print(json.dumps({"cohort": name, "metrics": {p: [round(v["metrics"]["total_return_pct"], 3),
                  round(v["metrics"]["max_drawdown_pct"], 3)] for p, v in runs.items()}}, ensure_ascii=False), flush=True)
    paths = ("scripts/research_local_trend_models.py", "scripts/research_calibrated_trends.py", "scripts/research_defensive_allocations.py",
             "scripts/research_fund_rotation.py", "scripts/diagnose_trend_predictivity.py", "scripts/validate_fund_allocation.py",
             "scripts/search_fund_allocations.py", "backend/analysis/matured_trend_forecast.py", "backend/analysis/fund_allocation.py",
             "backend/analysis/fund_holding_costs.py", "backend/analysis/sector_trend.py", "backend/analysis/strategy_risk.py",
             "backend/core/trading_calendar.py")
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in paths}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "model_registry": registry,
            "learning_records": learning, "protocol": json.loads(PROTOCOL.read_text()),
            "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "input_sha256": sha256(INPUT.read_bytes()).hexdigest(),
            "bond_input_sha256": sha256(BONDS.read_bytes()).hexdigest(), "calendar_sha256": sha256(CALENDAR.read_bytes()).hexdigest(),
            "source_hashes": {p: sha256(s.encode("utf-8")).hexdigest() for p, s in snapshots.items()}, "source_snapshots": snapshots,
            "numpy_version": np.__version__, "execution": {
                "review_sessions": execution.review_sessions, "absolute_band": execution.rebalance_band,
                "relative_band": execution.relative_rebalance_band}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/fund-local-models-v1.json.gz")
    parser.add_argument("--relative-band", type=float)
    args = parser.parse_args()
    report = run(replace(EXECUTION, relative_rebalance_band=args.relative_band))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8", compresslevel=6) as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output.resolve()), "models_audited": len(report["model_registry"])}))


if __name__ == "__main__":
    main()
