"""Risk-headroom-only trend tilts, paired costs/clocks and fresh entry checks."""
import argparse
from bisect import bisect_left
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
from backend.analysis.trend_satellite import SatellitePolicy, policies, scaled
from backend.core.trading_calendar import TradingCalendar
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import carry_slice, effective_episode
from scripts.research_satellite_market_gates import require_unchanged
from scripts.research_trend_satellites import satellite_trial

PROTOCOL = ROOT / "config/satellite-headroom-research-v1.json"


def headroom_policies():
    return {p.name: p for p in (
        SatellitePolicy("headroom_fixed20", "fixed20", sizing="headroom"),
        SatellitePolicy("headroom_score", "score", sizing="headroom"))}


def event_repair_policies():
    return {p.name: p for p in (
        SatellitePolicy("event_core", "none", risk_repair="event"),
        SatellitePolicy("event_fixed20", "fixed20", risk_repair="event"),
        SatellitePolicy("event_headroom20", "fixed20", sizing="headroom", risk_repair="event"))}


def defensive_target_diagnostic(run, data):
    checks = run.get("daily_checks", [])
    differences = []
    for record in checks:
        if record["action"] != "repair" or record["memory_after"]["candidate"] is None:
            continue
        neutral = scaled(data, record["signal_index"], record["memory_after"]["raw_core"])
        defensive = data["class_groups"]["gold"]+data["class_groups"]["bond"]
        differences.append(sum(record["targets"][c]-neutral[c] for c in defensive)*100)
    return {"active_tilt_repair_dates": len(differences),
            "defensive_target_reduced_dates": sum(value < -1e-9 for value in differences),
            "defensive_target_increased_dates": sum(value > 1e-9 for value in differences),
            "max_absolute_defensive_target_shift_pp": max(map(abs, differences), default=0.),
            "scope": "Intended repair targets versus same-day neutral core; excludes daily actual-risk emergency reductions. Not a guarantee of unchanged filled holdings"}


def run(protocol_path=PROTOCOL):
    protocol_path = protocol_path.resolve()
    protocol_relative = str(protocol_path.relative_to(ROOT))
    protocol = json.loads(protocol_path.read_text())
    path = ROOT / protocol["reference"]
    if sha256(path.read_bytes()).hexdigest() != protocol["reference_sha256"]:
        raise ValueError("Frozen headroom reference changed")
    with gzip.open(path, "rt") as stream:
        reference = json.load(stream)
    if sha256((ROOT/protocol["reference_protocol"]).read_bytes()).hexdigest() != reference["protocol_sha256"]:
        raise ValueError("Frozen reference protocol changed")
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Frozen input changed: {key}")
    allowed = {"backend/analysis/trend_satellite.py", "scripts/validate_trend_satellite.py",
               "scripts/research_satellite_headroom.py"}
    changed = {}
    for path, digest in reference["source_hashes"].items():
        actual = sha256((ROOT/path).read_bytes()).hexdigest()
        if actual != digest:
            if path not in allowed:
                raise ValueError(f"Unrelated headroom reference source changed: {path}")
            changed[path] = {"reference": digest, "current": actual}
    paths = set(reference["source_hashes"]) | {"scripts/research_satellite_headroom.py",
        "scripts/research_satellite_market_gates.py", protocol_relative}
    snapshots = {path: (ROOT/path).read_text() for path in sorted(paths)}
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    definitions = {p.name: p for p in policies()} | headroom_policies() | event_repair_policies() | {"inverse20": "inverse20"}
    retained = set(protocol["stress_policies"]) | set(protocol["variants"]) | set(protocol.get(
        "comparison_policies", ("matched_core", "inverse20", "fixed20", "score_gap5")))
    results, compatible, executed, reused = {}, 0, 0, 0
    for name in protocol["cohorts"]:
        frozen = reference["cohorts"][name]
        data = prepare_economic(prepare_local(funds, frozen["codes"], calendar))
        cache, full = {}, {}
        for key, previous in frozen["full"].items():
            current = satellite_trial(data, calendar, definitions[key], cache=cache)
            require_unchanged(current, previous)
            if "signal_trace" in previous:
                # Frozen JSON stores numeric horizon keys as strings; values remain exact.
                serialized = json.loads(json.dumps(current["signal_trace"], allow_nan=False))
                if serialized != previous["signal_trace"]:
                    raise ValueError("Headroom extension changed the inverse-core signal trace")
            compatible += 1
            executed += 1
            if key in retained:
                full[key] = current
        for key in protocol["variants"]:
            full[key] = satellite_trial(data, calendar, definitions[key], cache=cache)
            executed += 1
        diagnostics = {key: defensive_target_diagnostic(value, data) for key, value in full.items()
                       if key != "inverse20"}
        if any(diagnostics[key]["max_absolute_defensive_target_shift_pp"] > 1e-9 for key in protocol["variants"]
               if definitions[key].sizing == "headroom"):
            raise ValueError("Headroom sizing altered defensive neutral targets")
        stress = {}
        for key in protocol["stress_policies"]:
            scenarios = [(f"fee{fee}", {"fee": Decimal(fee)}) for fee in protocol["stress"]["fee_bps"]]
            scenarios += [(f"phase{phase}", {"phase": phase}) for phase in protocol["stress"]["core_and_check_phases"]]
            scenarios += [("lag2", {"lag": protocol["stress"]["execution_lag_sessions"]})]
            stress[key] = {label: satellite_trial(data, calendar, definitions[key], detail=False, cache=cache, **kwargs)
                           for label, kwargs in scenarios}
            executed += len(scenarios)
        fresh = {}
        for requested in protocol["fresh_entry_dates"]:
            start = max(252, bisect_left(data["days"], requested))
            if start >= len(data["days"])-21:
                fresh[requested] = {"status": "insufficient_shared_history", "policies": {}}
                continue
            trials = {}
            for key in protocol["stress_policies"]:
                if start == 252:
                    trials[key] = {k: v for k, v in full[key].items() if k not in ("account", "daily_checks", "signal_trace")}
                    reused += 1
                else:
                    trials[key] = satellite_trial(data, calendar, definitions[key], start=start, detail=False, cache=cache)
                    executed += 1
            fresh[requested] = {"status": "partial" if requested < data["days"][252] else "complete",
                                "actual_start": data["days"][start], "policies": trials}
        episodes = {}
        for episode in reference["regime_protocol"]["episodes"]:
            coverage = effective_episode(data["days"], episode)
            episodes[episode["id"]] = {"coverage": coverage, "policies": {} if coverage["status"] == "insufficient_shared_history"
                else {key: carry_slice(value["account"], coverage["start_date"], coverage["end_date"])
                      for key, value in full.items()}}
        results[name] = {"codes": frozen["codes"], "full": full, "sizing_diagnostics": diagnostics,
                         "stress": stress, "fresh_entry": fresh, "episodes": episodes}
        print(json.dumps({"cohort": name, "return_drawdown": {key: [value["metrics"]["total_return_pct"],
            value["metrics"]["max_drawdown_pct"]] for key, value in full.items()},
            "entries": {key: full[key]["events"]["entry"] for key in protocol["variants"]}}, ensure_ascii=False), flush=True)
    for path, source in snapshots.items():
        if (ROOT/path).read_text() != source:
            raise ValueError(f"Research source changed during run: {path}")
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol,
        "protocol_file": protocol_relative,
        "protocol_sha256": sha256(snapshots[protocol_relative].encode()).hexdigest(),
        "reference_sha256": protocol["reference_sha256"], "regime_protocol": reference["regime_protocol"],
        "unchanged_previous_main_accounts": compatible, "new_account_runs": executed, "fresh_reused_main": reused,
        "allowed_source_changes": changed, "source_snapshots": snapshots,
        "source_hashes": {path: sha256(source.encode()).hexdigest() for path, source in snapshots.items()},
        **{key: reference[key] for key in ("input_sha256", "bond_input_sha256", "calendar_sha256")},
        "fund_audit": audit+bond_audit, "cohorts": results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path, default=PROTOCOL)
    parser.add_argument("--output", type=Path, default=ROOT/"runtime/verification/satellite-headroom-research-v1.json.gz")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output already exists; preserve earlier research")
    report = run(args.protocol)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as file:
        with gzip.open(file, "wt", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"output": str(args.output), "account_runs": report["new_account_runs"]}), flush=True)


if __name__ == "__main__":
    main()
