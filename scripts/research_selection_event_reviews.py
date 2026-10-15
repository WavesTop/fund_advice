"""Test causal class-membership events against grid and daily-review controls."""
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
from scripts.validate_cross_asset_trend import rebuild_proposal, rebuild_signals

PROTOCOL = ROOT / "config/class-selection-event-review-v1.json"


def audit_event_sets(data, policies, event_sets, *, start=252):
    expected = {policy: set() for policy in policies}
    previous = {}
    for index in range(start, len(data["days"])):
        _, signals = rebuild_signals(data, index)
        for policy in policies:
            proposal = rebuild_proposal(policy, signals)
            selection = tuple(sorted(name for name, weight in proposal.items() if weight > 0))
            if policy in previous and selection != previous[policy]:
                expected[policy].add(index)
            previous[policy] = selection
    if set(event_sets) != set(policies) or any(event_sets[p] != frozenset(expected[p]) for p in policies):
        raise ValueError("Class-selection event dates differ from past raw fund history")


def run():
    protocol = json.loads(PROTOCOL.read_text())
    reference_path = ROOT / protocol["reference"]
    reference = json.load(gzip.open(reference_path, "rt", encoding="utf-8"))
    allowed = {"backend/analysis/fund_allocation.py", "scripts/research_cross_asset_trends.py",
               "scripts/diagnose_review_timing.py", "scripts/research_broad_return_ranking.py"}
    differences = {}
    for path, digest in reference["source_hashes"].items():
        current = sha256((ROOT / path).read_bytes()).hexdigest()
        if current != digest:
            if path not in allowed:
                raise ValueError(f"Unrelated reference source changed: {path}")
            differences[path] = {"reference": digest, "current": current}
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Reference inputs changed: {key}")
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    data = prepare_class_proxies(prepare_local(funds, protocol["fund_codes"], calendar))
    data["cross_stats"] = {i: class_statistics(data, i) for i in range(252, len(data["days"]))}
    events = {p: selection_event_indices(data, p) for p in protocol["policies"]}
    audit_event_sets(data, protocol["policies"], events)
    results = {}
    for policy in protocol["policies"]:
        modes = {}
        for mode in ("grid20", "event20", "daily"):
            execution = replace(EXECUTION, initial_review=True, review_sessions=1 if mode == "daily" else 20)
            extras = events[policy] if mode == "event20" else frozenset()
            phases = [0] if mode == "daily" else protocol["phases"]
            cases = {}
            for phase in phases:
                trial = replay(data, calendar, policy, phase=phase, execution=execution, extra_review_indices=extras)
                if mode == "grid20" and trial["metrics"] != reference["results"][policy]["runs"]["matched_entry"][str(phase)]["metrics"]:
                    raise ValueError(f"Optional extra review changed the matched-entry reference: {policy}/{phase}")
                cases[str(phase)] = trial
            stress = {f"fee{fee}": replay(data, calendar, policy, fee=Decimal(fee), execution=execution,
                                           extra_review_indices=extras, detail=False) for fee in (50, 100)}
            modes[mode] = {"runs": cases, "stress": stress}
        results[policy] = {"modes": modes, "events": [data["days"][i] for i in sorted(events[policy])],
                           "event_raw_history_validation": {"status": "passed", "events": len(events[policy])}}
        print(json.dumps({"policy": policy, "events": len(events[policy]),
                          "return_drawdown_fees": {m: {p: [r["metrics"]["total_return_pct"], r["metrics"]["max_drawdown_pct"],
                                                             float(r["metrics"]["total_fees"])] for p, r in v["runs"].items()}
                                                   for m, v in modes.items()},
                          "fee_stress_returns": {m: {s: r["metrics"]["total_return_pct"] for s, r in v["stress"].items()}
                                                 for m, v in modes.items()}}, ensure_ascii=False), flush=True)
    paths = set(reference["source_hashes"]) | {"scripts/research_selection_event_reviews.py", "backend/analysis/class_selection_events.py"}
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "results": results, "protocol": protocol,
            "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "fund_audit": audit + bond_audit,
            "reference_sha256": sha256(reference_path.read_bytes()).hexdigest(), "reference_source_differences": differences,
            "all20matched_entry_metrics_match_reference": True, "input_sha256": reference["input_sha256"],
            "bond_input_sha256": reference["bond_input_sha256"], "calendar": reference["calendar"], "calendar_sha256": reference["calendar_sha256"],
            "source_snapshots": snapshots, "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/class-selection-event-review-v2.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))


if __name__ == "__main__":
    main()
