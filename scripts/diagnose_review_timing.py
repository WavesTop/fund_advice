"""Separate initial funding delay from subsequent rebalance-grid sensitivity."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.cross_asset_trend import class_statistics, prepare_class_proxies
from backend.core.trading_calendar import TradingCalendar
from scripts.research_cross_asset_trends import EXECUTION, replay
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local

PROTOCOL = ROOT / "config/review-timing-diagnostic-v1.json"


def continuous_year_returns(account):
    last = {}
    for row in account["equity_curve"]:
        last[row["date"][:4]] = float(row["equity"])
    previous = float(account["initial_equity"])
    result = {}
    for year, equity in sorted(last.items()):
        result[year] = equity / previous - 1
        previous = equity
    if abs(math.prod(1 + v for v in result.values()) - float(account["final_equity"]) / float(account["initial_equity"])) > 1e-10:
        raise ValueError("Continuous calendar-year attribution does not reconcile")
    return result


def compare_curves(base, other):
    left, right = base["account"]["equity_curve"], other["account"]["equity_curve"]
    if len(left) != len(right) or any(a["date"] != b["date"] for a, b in zip(left, right)):
        raise ValueError("Phased account curves have different dates")
    original = continuous_year_returns(base["account"])
    changed = continuous_year_returns(other["account"])
    if set(original) != set(changed):
        raise ValueError("Phased account years differ")
    attribution = {year: {"base_return_pct": original[year] * 100, "other_return_pct": changed[year] * 100,
                          "relative_log_wealth": math.log1p(changed[year]) - math.log1p(original[year])}
                   for year in original}
    total = math.log(float(other["account"]["final_equity"]) / float(base["account"]["final_equity"]))
    if abs(sum(v["relative_log_wealth"] for v in attribution.values()) - total) > 1e-10:
        raise ValueError("Phased log-wealth attribution does not reconcile")
    differences = [(a["date"], float(b["equity"]) / float(a["equity"]) - 1) for a, b in zip(left, right)]
    day, difference = max(differences, key=lambda item: abs(item[1]))
    return {"annual_continuous": attribution, "final_relative_log_wealth": total,
            "largest_relative_equity_gap": {"date": day, "other_over_base_pct": difference * 100}}


def initial_orders(result):
    account = result["account"]
    first = account["decisions"][0]["signal_date"]
    return [trade for trade in account["trades"] if trade.get("signal_date") == first]


def run():
    protocol = json.loads(PROTOCOL.read_text())
    reference_path = ROOT / protocol["reference"]
    reference = json.load(gzip.open(reference_path, "rt", encoding="utf-8"))
    differences = {}
    for path, digest in reference["source_hashes"].items():
        current = sha256((ROOT / path).read_bytes()).hexdigest()
        if current != digest:
            if path not in ("backend/analysis/fund_allocation.py", "scripts/research_cross_asset_trends.py",
                            "scripts/research_broad_return_ranking.py"):
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
    results = {}
    for policy in protocol["policies"]:
        modes = {}
        for mode, enabled in (("delayed_entry", False), ("matched_entry", True)):
            runs = {}
            for phase in protocol["phases"]:
                trial = replay(data, calendar, policy, phase=phase, execution=replace(EXECUTION, initial_review=enabled))
                if not enabled:
                    expected = reference["results"][policy]["main"] if phase == 0 else reference["results"][policy]["stress"][f"phase{phase}"]
                    if trial["metrics"] != expected["metrics"]:
                        raise ValueError(f"Disabled initial review changed prior account: {policy}/{phase}")
                runs[str(phase)] = trial
            modes[mode] = runs
        baseline = modes["delayed_entry"]["0"]
        if modes["matched_entry"]["0"]["metrics"] != baseline["metrics"]:
            raise ValueError("Initial review changed an already immediate zero-phase account")
        if any(initial_orders(run) != initial_orders(baseline) for run in modes["matched_entry"].values()):
            raise ValueError("Matched entry did not reproduce identical initial fund orders")
        comparisons = {mode: {phase: compare_curves(baseline, run) for phase, run in runs.items()} for mode, runs in modes.items()}
        results[policy] = {"runs": modes, "comparisons": comparisons, "identical_initial_orders": True}
        print(json.dumps({"policy": policy, "return_drawdown": {mode: {p: [v["metrics"]["total_return_pct"], v["metrics"]["max_drawdown_pct"]]
                                            for p, v in runs.items()} for mode, runs in modes.items()}}, ensure_ascii=False), flush=True)
    paths = set(reference["source_hashes"]) | {"scripts/diagnose_review_timing.py"}
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "results": results, "protocol": protocol,
            "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "fund_audit": audit + bond_audit,
            "reference_sha256": sha256(reference_path.read_bytes()).hexdigest(), "reference_source_differences": differences,
            "all_delayed_entry_metrics_match_reference": True, "input_sha256": reference["input_sha256"],
            "bond_input_sha256": reference["bond_input_sha256"], "calendar": reference["calendar"], "calendar_sha256": reference["calendar_sha256"],
            "source_snapshots": snapshots, "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/review-timing-diagnostic-v2.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))


if __name__ == "__main__":
    main()
