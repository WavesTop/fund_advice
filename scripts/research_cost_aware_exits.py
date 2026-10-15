"""Test holding-aware exit urgency without changing trend selection or fee floors."""
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

from backend.analysis.class_selection_events import selection_event_indices
from backend.analysis.cross_asset_trend import class_statistics, prepare_class_proxies
from backend.core.trading_calendar import TradingCalendar
from scripts.research_cross_asset_trends import EXECUTION, replay
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import carry_slice, effective_episode
from scripts.research_selection_event_reviews import audit_event_sets

PROTOCOL = ROOT / "config/cost-aware-exit-research-v1.json"


def trial(data, calendar, policy, schedule, *, exit_mode="legacy", start=252, end=None,
          fee=Decimal(10), phase=0, lag=1, detail=True, deadline=False):
    execution = replace(EXECUTION, initial_review=True, execution_lag_sessions=lag,
                        deadline_entry_guard_days=7 if deadline else 0)
    extras = data["event_sets"][policy] if schedule == "event20" else frozenset()
    if schedule not in ("grid20", "event20"):
        raise ValueError("Cost-aware study requires its declared fixed or event schedule")
    return replay(data, calendar, policy, start=start, end=end, phase=phase, fee=fee, execution=execution,
                  detail=detail, extra_review_indices=extras, exit_mode=exit_mode)


def run():
    protocol = json.loads(PROTOCOL.read_text())
    reference_path = ROOT / protocol["reference"]
    reference = json.load(gzip.open(reference_path, "rt", encoding="utf-8"))
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Frozen regime input differs: {key}")
    allowed = {"backend/analysis/fund_allocation.py", "backend/analysis/cross_asset_trend.py",
               "scripts/research_cross_asset_trends.py", "scripts/validate_cross_asset_trend.py"}
    differences = {}
    for path, digest in reference["source_hashes"].items():
        current = sha256((ROOT / path).read_bytes()).hexdigest()
        if current != digest:
            if path not in allowed:
                raise ValueError(f"Unrelated reference source changed: {path}")
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
        data["event_sets"] = {p: selection_event_indices(data, p) for p in protocol["policies"]}
        audit_event_sets(data, protocol["policies"], data["event_sets"])
        full, stress = {}, {}
        for policy in protocol["policies"]:
            full[policy], stress[policy] = {}, {}
            for schedule in protocol["schedules"]:
                legacy = trial(data, calendar, policy, schedule)
                if legacy["metrics"] != frozen["full"][policy][schedule]["metrics"]:
                    raise ValueError(f"Default exit behavior changed: {name}/{policy}/{schedule}")
                unchanged += 1
                updated = trial(data, calendar, policy, schedule, exit_mode=protocol["exit_mode"])
                full[policy][schedule] = {"legacy": legacy, "current_risk": updated}
                for fee in protocol["stress"]["ordinary_fee_bps_each_side"]:
                    stress[policy][f"{schedule}_fee{fee}"] = trial(data, calendar, policy, schedule,
                        exit_mode=protocol["exit_mode"], fee=Decimal(fee), detail=False)
            for phase in protocol["stress"]["grid20_common_entry_phases"]:
                stress[policy][f"grid20_phase{phase}"] = trial(data, calendar, policy, "grid20",
                    exit_mode=protocol["exit_mode"], phase=phase, detail=False)
            stress[policy]["grid20_lag2"] = trial(data, calendar, policy, "grid20", exit_mode=protocol["exit_mode"],
                lag=protocol["stress"]["execution_lag_sessions"], detail=False)
        episodes = {}
        for episode in reference["protocol"]["episodes"]:
            coverage = effective_episode(data["days"], episode)
            if coverage != frozen["episodes"][episode["id"]]["coverage"]:
                raise ValueError("Cost-aware followup changed declared regime coverage")
            segments = {}
            if coverage["status"] != "insufficient_shared_history":
                for policy in protocol["policies"]:
                    segments[policy] = {}
                    for schedule in protocol["schedules"]:
                        fresh = trial(data, calendar, policy, schedule, exit_mode=protocol["exit_mode"],
                            start=coverage["start_index"], end=coverage["end_index"], detail=False, deadline=True)
                        carried = carry_slice(full[policy][schedule]["current_risk"]["account"],
                                              coverage["start_date"], coverage["end_date"])
                        segments[policy][schedule] = {"fresh": fresh, "carry": carried}
            episodes[episode["id"]] = {"coverage": coverage, "strategies": segments}
        results[name] = {"codes": frozen["codes"], "full": full, "stress": stress, "episodes": episodes,
                         "selection_event_validation": {"status": "passed"}}
        print(json.dumps({"cohort": name, "return_drawdown_fees": {
            p: {s: {mode: [v["metrics"]["total_return_pct"], v["metrics"]["max_drawdown_pct"], float(v["metrics"]["total_fees"])]
                    for mode, v in pair.items()} for s, pair in schedules.items()} for p, schedules in full.items()}}, ensure_ascii=False), flush=True)
    paths = set(reference["source_hashes"]) | {"scripts/research_cost_aware_exits.py"}
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "protocol": protocol,
            "market_episodes": reference["market_episodes"], "regime_protocol": reference["protocol"],
            "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "source_snapshots": snapshots,
            "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()},
            "input_sha256": reference["input_sha256"], "bond_input_sha256": reference["bond_input_sha256"],
            "calendar": reference["calendar"], "calendar_sha256": reference["calendar_sha256"],
            "fund_audit": fund_audit + bond_audit, "reference_sha256": sha256(reference_path.read_bytes()).hexdigest(),
            "reference_source_differences": differences, "all24legacy_full_metrics_unchanged": unchanged == 24,
            "legacy_full_accounts_unchanged": unchanged}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/cost-aware-exit-research-v1.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))


if __name__ == "__main__":
    main()
