"""Stress and entry-window validation of the observed broad-market trend candidate."""
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

from backend.analysis.cross_asset_trend import class_statistics, prepare_class_proxies
from backend.core.trading_calendar import TradingCalendar
from scripts.research_cross_asset_trends import EXECUTION, replay
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds, rolling_summary
from scripts.research_local_trend_models import prepare_local

PROTOCOL = ROOT / "config/broad-return-ranking-stress-v1.json"


def run():
    protocol = json.loads(PROTOCOL.read_text())
    reference_path = ROOT / protocol["reference"]
    reference = json.load(gzip.open(reference_path, "rt", encoding="utf-8"))
    differences = {}
    for path, digest in reference["source_hashes"].items():
        current = sha256((ROOT / path).read_bytes()).hexdigest()
        if current != digest:
            if path not in ("backend/analysis/fund_allocation.py", "scripts/research_cross_asset_trends.py"):
                raise ValueError(f"Reference source changed: {path}")
            differences[path] = {"reference": digest, "current": current}
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Reference input changed: {key}")
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, fund_audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    data = prepare_class_proxies(prepare_local(funds, protocol["fund_codes"], calendar))
    data["cross_stats"] = {i: class_statistics(data, i) for i in range(252, len(data["days"]))}
    results = {}
    for policy in protocol["policies"]:
        main = replay(data, calendar, policy)
        if main["metrics"] != reference["cohorts"]["long4"]["runs"][policy]["metrics"]:
            raise ValueError(f"Fresh main account differs from frozen reference: {policy}")
        scenarios = [(f"fee{f}", Decimal(f), 0, EXECUTION) for f in (50, 100)]
        scenarios += [(f"phase{p}", Decimal(10), p, EXECUTION) for p in (5, 10, 15)]
        scenarios += [("execution_lag2", Decimal(10), 0, replace(EXECUTION, execution_lag_sessions=2))]
        stress = {name: replay(data, calendar, policy, fee=fee, phase=phase, execution=execution, detail=False)
                  for name, fee, phase, execution in scenarios}
        rolling = {}
        for horizon in (20, 60, 120):
            rows, checks = [], []
            for start in range(252, len(data["days"]) - horizon, horizon):
                trial = replay(data, calendar, policy, start=start, end=start + horizon, detail=False,
                               execution=replace(EXECUTION, deadline_entry_guard_days=7))
                rows.append({"start_date": trial["metrics"]["start_date"], "end_date": trial["metrics"]["end_date"],
                             "return_pct": trial["metrics"]["total_return_pct"], "drawdown_pct": trial["metrics"]["max_drawdown_pct"]})
                checks.append({"ledger": trial["ledger_validation"], "signals_and_risk": trial["signal_and_risk_validation"]})
            rolling[str(horizon)] = {"summary": rolling_summary(rows), "windows": rows, "validation": checks}
        results[policy] = {"main": main, "stress": stress, "rolling": rolling}
        print(json.dumps({"policy": policy, "main": main["metrics"],
                          "stress_return_drawdown": {name: [r["metrics"]["total_return_pct"], r["metrics"]["max_drawdown_pct"]]
                                                     for name, r in stress.items()},
                          "rolling": {h: r["summary"] for h, r in rolling.items()}}, ensure_ascii=False), flush=True)
    paths = set(reference["source_hashes"]) | {"scripts/research_broad_return_ranking.py"}
    snapshots = {path: (ROOT / path).read_text(encoding="utf-8") for path in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "results": results,
            "fund_audit": fund_audit + bond_audit, "protocol": protocol, "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(),
            "reference_sha256": sha256(reference_path.read_bytes()).hexdigest(), "reference_main_metrics_match": True,
            "reference_source_differences": differences,
            "input_sha256": reference["input_sha256"], "bond_input_sha256": reference["bond_input_sha256"],
            "calendar": reference["calendar"], "calendar_sha256": reference["calendar_sha256"],
            "source_snapshots": snapshots, "source_hashes": {path: sha256(s.encode()).hexdigest() for path, s in snapshots.items()}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/broad-return-ranking-stress-v2.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))


if __name__ == "__main__":
    main()
