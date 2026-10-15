"""Separate trading-cost and holding-wait effects without calling them investable."""
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
from backend.analysis.fund_holding_costs import FundHoldingCosts
from backend.core.trading_calendar import TradingCalendar
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import carry_slice, effective_episode
from scripts.research_phased_class_trends import phased_trial

PROTOCOL = ROOT / "config/trend-friction-diagnostic-v2.json"


def friction_trial(data, calendar, policy, variant, scenario, *, start=252, end=None, phase=0, detail=True, deadline=False):
    trial = phased_trial(data, calendar, policy, variant, start=start, end=end, phase=phase, detail=detail, deadline=deadline,
                         fee=Decimal(scenario["ordinary_fee_bps"]),
                         costs=replace(FundHoldingCosts(), short_redemption_bps=scenario["short_redemption_bps"]),
                         minimum_holding_days=scenario["minimum_holding_days"])
    if scenario["ordinary_fee_bps"] == scenario["short_redemption_bps"] == 0 and Decimal(trial["metrics"]["total_fees"]) != 0:
        raise ValueError("All-fee-free diagnostic charged a trade fee")
    return trial


def run():
    overlay = json.loads(PROTOCOL.read_text())
    base_protocol_path = ROOT / overlay["base_protocol"]
    protocol = json.loads(base_protocol_path.read_text()) | overlay
    reference_path = ROOT / protocol["reference"]
    reference = json.load(gzip.open(reference_path, "rt", encoding="utf-8"))
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Frozen friction input changed: {key}")
    allowed = {"backend/analysis/fund_allocation.py", "scripts/validate_fund_allocation.py", "scripts/diagnose_trend_frictions.py"}
    differences = {}
    for path, digest in reference["source_hashes"].items():
        current = sha256((ROOT / path).read_bytes()).hexdigest()
        if current != digest:
            if path not in allowed:
                raise ValueError(f"Unrelated friction reference source changed: {path}")
            differences[path] = {"reference": digest, "current": current}
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, fund_audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    results, unchanged, corrected = {}, 0, {}
    for name in protocol["cohorts"]:
        frozen = reference["cohorts"][name]
        data = prepare_class_proxies(prepare_local(funds, frozen["codes"], calendar))
        data["cross_stats"] = {i: class_statistics(data, i) for i in range(252, len(data["days"]))}
        full = {}
        for policy in protocol["policies"]:
            full[policy] = {}
            for variant in protocol["variants"]:
                full[policy][variant] = {}
                for scenario, settings in protocol["scenarios"].items():
                    trial = friction_trial(data, calendar, policy, variant, settings)
                    if scenario == "baseline":
                        previous = frozen["full"][policy][variant]["baseline"]["metrics"]
                        if trial["metrics"] == previous:
                            unchanged += 1
                        else:
                            corrected[f"{name}/{policy}/{variant}"] = {"pre_fix": previous, "corrected": trial["metrics"]}
                    full[policy][variant][scenario] = trial
        phases = {}
        diagnostic = protocol["phase_diagnostic"]
        settings = protocol["scenarios"][diagnostic["scenario"]]
        for policy in diagnostic["policies"]:
            phases[policy] = {}
            for variant in protocol["variants"]:
                offsets = diagnostic["grid20_phases"] if variant == "grid20" else diagnostic["five_day_clock_phases"]
                phases[policy][variant] = {str(phase): friction_trial(data, calendar, policy, variant, settings,
                                                                    phase=phase, detail=False) for phase in offsets}
        episodes = {}
        for episode in reference["regime_protocol"]["episodes"]:
            coverage = effective_episode(data["days"], episode)
            if coverage != frozen["episodes"][episode["id"]]["coverage"]:
                raise ValueError("Friction diagnostic changed fixed episode coverage")
            segments = {}
            if coverage["status"] != "insufficient_shared_history":
                for policy in protocol["policies"]:
                    segments[policy] = {}
                    for variant in protocol["variants"]:
                        segments[policy][variant] = {scenario: {"carry": carry_slice(t["account"], coverage["start_date"], coverage["end_date"])}
                                                     for scenario, t in full[policy][variant].items()}
                        if name in ("long4", "all13") and policy == "dual_return60":
                            segments[policy][variant]["all_fee_free"]["fresh"] = friction_trial(data, calendar, policy, variant, protocol["scenarios"]["all_fee_free"],
                                start=coverage["start_index"], end=coverage["end_index"], detail=False, deadline=True)
                            segments[policy][variant]["baseline"]["fresh"] = friction_trial(data, calendar, policy, variant, protocol["scenarios"]["baseline"],
                                start=coverage["start_index"], end=coverage["end_index"], detail=False, deadline=True)
            episodes[episode["id"]] = {"coverage": coverage, "strategies": segments}
        results[name] = {"codes": frozen["codes"], "full": full, "phases": phases, "episodes": episodes}
        print(json.dumps({"cohort": name, "return_drawdown_fees": {p: {v: {s: [t["metrics"]["total_return_pct"],
            t["metrics"]["max_drawdown_pct"], float(t["metrics"]["total_fees"])] for s, t in scenarios.items()}
            for v, scenarios in variants.items()} for p, variants in full.items()}}, ensure_ascii=False), flush=True)
    paths = set(reference["source_hashes"]) | {"scripts/diagnose_trend_frictions.py"}
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "protocol": protocol,
            "regime_protocol": reference["regime_protocol"], "market_episodes": reference["market_episodes"],
            "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "source_snapshots": snapshots,
            "base_protocol_path": overlay["base_protocol"], "base_protocol_sha256": sha256(base_protocol_path.read_bytes()).hexdigest(),
            "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()},
            "input_sha256": reference["input_sha256"], "bond_input_sha256": reference["bond_input_sha256"],
            "calendar": reference["calendar"], "calendar_sha256": reference["calendar_sha256"],
            "reference_sha256": sha256(reference_path.read_bytes()).hexdigest(), "reference_source_differences": differences,
            "all36baseline_full_metrics_unchanged": unchanged == 36, "baseline_full_accounts_unchanged": unchanged,
            "baseline_metric_corrections": corrected, "pre_fix_reference_not_current_investment_evidence": True,
            "fund_audit": fund_audit + bond_audit}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/trend-friction-diagnostic-v2.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))


if __name__ == "__main__":
    main()
