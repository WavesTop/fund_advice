"""Verify cumulative intent clears execution bands, then test its net economics."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.class_return_forecast import class_records
from backend.analysis.cross_asset_trend import class_statistics, prepare_class_proxies
from backend.analysis.matured_trend_forecast import MaturedForecaster
from backend.core.trading_calendar import TradingCalendar
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_gradual_class_allocations import gradual_trial
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import carry_slice, effective_episode
from scripts.validate_class_return_forecast import audit_class_records

PROTOCOL = ROOT / "config/gradual-class-allocation-research-v2.json"


def run():
    protocol = json.loads(PROTOCOL.read_text())
    reference_path = ROOT / protocol["reference"]
    reference = json.load(gzip.open(reference_path, "rt", encoding="utf-8"))
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Accumulated intent frozen input changed: {key}")
    changes = {}
    for path, digest in reference["source_hashes"].items():
        current = sha256((ROOT / path).read_bytes()).hexdigest()
        if current != digest:
            if path not in protocol["reference_source_changes_allowed"]:
                raise ValueError(f"Unrelated intent source changed: {path}")
            changes[path] = {"reference": digest, "current": current}
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    results, unchanged = {}, 0
    for name in protocol["cohorts"]:
        frozen = reference["cohorts"][name]
        data = prepare_class_proxies(prepare_local(funds, frozen["codes"], calendar))
        data["cross_stats"] = {i: class_statistics(data, i) for i in range(252, len(data["days"]))}
        data["class_records"] = {m: class_records(data, m) for m in protocol["modes"]}
        records = {m: audit_class_records(data, rows, m) for m, rows in data["class_records"].items()}
        data["class_forecasters"] = {m: MaturedForecaster(rows) for m, rows in data["class_records"].items()}
        data["training_audit_cache"] = {m: {} for m in protocol["modes"]}
        full, controls, stress = {}, {}, {}
        for mode in protocol["modes"]:
            full[mode], controls[mode], stress[mode] = {}, {}, {}
            for review in protocol["reviews"]:
                control = gradual_trial(data, calendar, mode, "partial_choice", review)
                if control["metrics"] != frozen["full"][mode]["partial_choice"][str(review)]["metrics"]:
                    raise ValueError("Intent extension changed the old partial account")
                controls[mode][str(review)] = control
                unchanged += 1
                full[mode][str(review)] = gradual_trial(data, calendar, mode, "intent_choice", review)
                for fee in protocol["stress"]["ordinary_fee_bps_each_side"]:
                    stress[mode][f"review{review}_fee{fee}"] = gradual_trial(data, calendar, mode, "intent_choice", review, fee=Decimal(fee), detail=False)
                phases = protocol["stress"]["five_day_phases" if review == 5 else "twenty_day_phases"]
                for phase in phases:
                    stress[mode][f"review{review}_phase{phase}"] = gradual_trial(data, calendar, mode, "intent_choice", review, phase=phase, detail=False)
                stress[mode][f"review{review}_lag2"] = gradual_trial(data, calendar, mode, "intent_choice", review, lag=2, detail=False)
        episodes = {}
        for episode in reference["regime_protocol"]["episodes"]:
            coverage = effective_episode(data["days"], episode)
            if coverage != frozen["episodes"][episode["id"]]["coverage"]:
                raise ValueError("Intent study changed episode coverage")
            variants = {}
            if coverage["status"] != "insufficient_shared_history":
                for mode in protocol["modes"]:
                    variants[mode] = {}
                    for review in protocol["reviews"]:
                        v = str(review)
                        variants[mode][v] = {"fresh": gradual_trial(data, calendar, mode, "intent_choice", review,
                            start=coverage["start_index"], end=coverage["end_index"], detail=False, deadline=True),
                            "carry": carry_slice(full[mode][v]["account"], coverage["start_date"], coverage["end_date"]),
                            "partial_control_carry": carry_slice(controls[mode][v]["account"], coverage["start_date"], coverage["end_date"]),
                            "immediate_control_carry_reference": frozen["episodes"][episode["id"]]["immediate_controls"][mode][v]["carry"],
                            "static_control_carry_reference": frozen["episodes"][episode["id"]]["immediate_controls"]["static"][v]["carry"]}
            episodes[episode["id"]] = {"coverage": coverage, "strategies": variants}
        results[name] = {"codes": frozen["codes"], "full": full, "partial_controls": controls, "stress": stress,
                         "episodes": episodes, "training_record_validation": records}
        print(json.dumps({"cohort": name, "intent_return_drawdown": {m: {v: [t["metrics"]["total_return_pct"], t["metrics"]["max_drawdown_pct"]]
            for v, t in variants.items()} for m, variants in full.items()}}, ensure_ascii=False), flush=True)
    paths = set(reference["source_hashes"]) | {"scripts/research_accumulated_class_allocations.py"}
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "protocol": protocol,
            "regime_protocol": reference["regime_protocol"], "market_episodes": reference["market_episodes"],
            "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "source_snapshots": snapshots,
            "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()},
            "input_sha256": reference["input_sha256"], "bond_input_sha256": reference["bond_input_sha256"],
            "calendar": reference["calendar"], "calendar_sha256": reference["calendar_sha256"],
            "reference_sha256": sha256(reference_path.read_bytes()).hexdigest(), "reference_source_differences": changes,
            "partial_baseline_accounts_unchanged": unchanged, "fund_audit": audit + bond_audit}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/gradual-class-allocation-research-v2.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))


if __name__ == "__main__":
    main()
