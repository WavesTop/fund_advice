"""Require a real qualified choice set before a single-sector trend tilt."""
import argparse
from bisect import bisect_left
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.portfolio_increment import prepare_economic
from backend.analysis.trend_satellite import SatellitePolicy, policies
from backend.core.trading_calendar import TradingCalendar
from scripts.export_strategy_trial import checked_metrics
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import carry_slice, effective_episode
from scripts.research_satellite_headroom import event_repair_policies
from scripts.research_satellite_low_fees import compact
from scripts.research_satellite_market_gates import gate_trial, require_unchanged
from scripts.research_satellite_separate_repair import reference_case, separate_policies, unchanged_execution
from scripts.research_trend_satellites import satellite_trial

PROTOCOL = ROOT / "config/satellite-competition-research-v1.json"


def competition_policies():
    return {policy.name: policy for policy in (
        SatellitePolicy("competition_entry", "fixed20", market_gate="competition"),
        SatellitePolicy("competition_exit", "fixed20", market_gate="competition", exit_on_gate=True))}


def comparisons(trials, core, benchmark, overlay=None):
    base, inverse = checked_metrics(core), checked_metrics(benchmark)
    bounds = (core["metrics"]["start_date"], core["metrics"]["end_date"])
    result = {}
    for key, trial in trials.items():
        value = checked_metrics(trial)
        if (trial["metrics"]["start_date"], trial["metrics"]["end_date"]) != bounds or (
                benchmark["metrics"]["start_date"], benchmark["metrics"]["end_date"]) != bounds:
            raise ValueError("Qualified-choice comparison dates differ")
        old = None
        if overlay is not None:
            if (overlay["metrics"]["start_date"], overlay["metrics"]["end_date"]) != bounds:
                raise ValueError("Qualified-choice original-overlay dates differ")
            old = value["total_return_pct"] - checked_metrics(overlay)["total_return_pct"]
        result[key] = {"core_return_pct": base["total_return_pct"], "inverse_return_pct": inverse["total_return_pct"],
                       "trend_return_pct": value["total_return_pct"], "trend_max_drawdown_pct": abs(value["max_drawdown_pct"]),
                       "trend_minus_core_pp": value["total_return_pct"] - base["total_return_pct"],
                       "trend_minus_inverse_pp": value["total_return_pct"] - inverse["total_return_pct"],
                       "trend_minus_original_pp": old}
    return result


def entry_opportunities(trial):
    counts = Counter(sum(row["own_votes"][code] >= 2/3 and score >= 60 for code, score in row["scores"].items())
                     for row in trial["daily_checks"] if row["event"] == "entry")
    return {"entry_count": sum(counts.values()), "eligible_count_distribution": dict(sorted(counts.items())),
            "scope": "Original controller actual-entry dates only, not all possible check dates or independent market samples"}


def run():
    protocol = json.loads(PROTOCOL.read_text())
    references, changes = {}, {}
    allowed = {"backend/analysis/trend_satellite.py", "scripts/validate_trend_satellite.py"}
    for label, definition in protocol["references"].items():
        path = ROOT / definition["path"]
        if sha256(path.read_bytes()).hexdigest() != definition["sha256"]:
            raise ValueError("Frozen competition reference changed")
        with gzip.open(path, "rt") as stream:
            references[label] = json.load(stream)
        changes[label] = {}
        for path, digest in references[label]["source_hashes"].items():
            actual = sha256((ROOT / path).read_bytes()).hexdigest()
            if actual != digest:
                if path not in allowed:
                    raise ValueError(f"Unrelated competition source changed: {path}")
                changes[label][path] = {"reference": digest, "current": actual}
        for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
            if sha256(path.read_bytes()).hexdigest() != references[label][key]:
                raise ValueError("Frozen competition input changed")
    paths = set().union(*(value["source_hashes"] for value in references.values())) | {
        "scripts/research_satellite_competition.py", str(PROTOCOL.relative_to(ROOT))}
    snapshots = {path: (ROOT / path).read_text() for path in sorted(paths)}
    reference, low, separate = (references[key] for key in ("ordinary10", "ordinary0", "separate"))
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, fund_audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    definitions = {policy.name: policy for policy in policies()} | event_repair_policies() | separate_policies() | {"inverse20": "inverse20"}
    variants = competition_policies()
    if set(variants) != set(protocol["variants"]):
        raise ValueError("Competition variant design differs")
    results, executed, compatible, reused = {}, 0, 0, 0
    for name in protocol["cohorts"]:
        frozen, low_cohort = reference["cohorts"][name], low["cohorts"][name]
        if frozen["codes"] != low_cohort["codes"] or frozen["codes"] != separate["cohorts"][name]["codes"]:
            raise ValueError("Competition references use different fund pools")
        data = prepare_economic(prepare_local(funds, frozen["codes"], calendar))
        cache = {}
        for fee in protocol["ordinary_fees_bps"]:
            source = low_cohort["scenarios"]["default"]["policies"] if fee == 0 else frozen["full"]
            source = {key: source[key] for key in ("matched_core", "fixed20", "event_core", "event_fixed20", "inverse20")} | (
                separate["cohorts"][name]["scenarios"][f"fee{fee}_default"]["policies"])
            for key, old in source.items():
                trial = satellite_trial(data, calendar, definitions[key], fee=Decimal(fee), cache=cache)
                require_unchanged(trial, old)
                if "signal_trace" in old and json.loads(json.dumps(trial["signal_trace"])) != old["signal_trace"]:
                    raise ValueError("Competition extension changed inverse trace")
                compatible += 1
                executed += 1
        cases = [(fee, timing) for fee in protocol["ordinary_fees_bps"] for timing in protocol["timing"]]
        cases += [(fee, protocol["timing"][0]) for fee in protocol["extra_fee_stress_bps"]]
        scenarios, defaults = {}, {}
        for fee, timing in cases:
            key = f"fee{fee}_{timing['id']}"
            trials = {policy: gate_trial(data, calendar, variant, fee=Decimal(fee), phase=timing["phase"],
                        lag=timing["lag"], cache=cache) for policy, variant in variants.items()}
            executed += len(trials)
            for trial in trials.values():
                unchanged_execution(trial["account"], frozen["full"]["matched_core"]["account"], Decimal(fee), timing["lag"])
            base, benchmark, overlay = reference_case(frozen, low_cohort, fee, timing["id"])
            comparison = comparisons(trials, base, benchmark, overlay)
            episodes = {}
            for episode in reference["regime_protocol"]["episodes"]:
                coverage = effective_episode(data["days"], episode)
                episodes[episode["id"]] = {"coverage": coverage, "policies": {} if coverage["status"] == "insufficient_shared_history"
                    else {policy: carry_slice(trial["account"], coverage["start_date"], coverage["end_date"])
                          for policy, trial in trials.items()}}
            retain = timing["id"] == "default" and fee in protocol["ordinary_fees_bps"]
            if retain:
                defaults[fee] = trials
            scenarios[key] = {"comparisons": comparison, "episodes": episodes,
                             "policies": trials if retain else {policy: compact(trial) for policy, trial in trials.items()}}
            print(json.dumps({"cohort": name, "scenario": key, "account_runs": executed,
                "vs_inverse_pp": {policy: value["trend_minus_inverse_pp"] for policy, value in comparison.items()}}), flush=True)
        fresh = {}
        for fee in protocol["ordinary_fees_bps"]:
            for day in protocol["fresh_entry_dates"]:
                key = f"fee{fee}_{day}"
                start = max(252, bisect_left(data["days"], day))
                if start >= len(data["days"]) - 21:
                    fresh[key] = {"status": "insufficient_shared_history", "policies": {}, "comparisons": None}
                    continue
                if start == 252:
                    trials = defaults[fee]
                    reused += len(trials)
                else:
                    trials = {policy: gate_trial(data, calendar, variant, fee=Decimal(fee), start=start, cache=cache)
                              for policy, variant in variants.items()}
                    executed += len(trials)
                for trial in trials.values():
                    unchanged_execution(trial["account"], frozen["full"]["matched_core"]["account"], Decimal(fee), 1)
                source = (low_cohort if fee == 0 else frozen)["fresh_entry"][day]["policies"]
                fresh[key] = {"status": "partial" if day < data["days"][252] else "complete",
                              "actual_start": data["days"][start],
                              "comparisons": comparisons(trials, source["matched_core"], source["inverse20"], source.get("fixed20")),
                              "policies": {policy: compact(trial) for policy, trial in trials.items()}}
        results[name] = {"codes": frozen["codes"], "original_entry_opportunities": entry_opportunities(frozen["full"]["fixed20"]),
                         "scenarios": scenarios, "fresh_entry": fresh}
        print(json.dumps({"cohort": name, "completed_account_runs": executed}), flush=True)
    for path, source in snapshots.items():
        if (ROOT / path).read_text() != source:
            raise ValueError(f"Competition source changed during research: {path}")
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol,
            "protocol_sha256": sha256(snapshots[str(PROTOCOL.relative_to(ROOT))].encode()).hexdigest(),
            "source_snapshots": snapshots, "source_hashes": {path: sha256(source.encode()).hexdigest() for path, source in snapshots.items()},
            "reference_sha256": {label: definition["sha256"] for label, definition in protocol["references"].items()},
            "allowed_source_changes": changes, "regime_protocol": reference["regime_protocol"],
            **{key: reference[key] for key in ("input_sha256", "bond_input_sha256", "calendar_sha256")},
            "cohorts": results, "formal_account_runs": executed, "compatible_old_accounts": compatible,
            "new_variant_account_runs": executed - compatible, "fresh_reused_main": reused,
            "fund_audit": fund_audit + bond_audit}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/satellite-competition-research-v1.json.gz")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Preserve previous competition research")
    result = run()
    with args.output.open("xb") as stream:
        with gzip.open(stream, "wt", encoding="utf-8") as compressed:
            json.dump(result, compressed, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"output": str(args.output), "accounts": result["formal_account_runs"], "compatible": result["compatible_old_accounts"]}), flush=True)
