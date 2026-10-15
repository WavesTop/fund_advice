"""Shared-account calendar diversification versus pure higher-frequency review."""
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

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.cross_asset_trend import class_statistics, prepare_class_proxies
from backend.analysis.fund_allocation import simulate_allocations
from backend.analysis.fund_holding_costs import FundHoldingCosts
from backend.analysis.phased_class_trend import phased_class_target
from backend.core.trading_calendar import TradingCalendar
from scripts.research_cross_asset_trends import EXECUTION, replay
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import carry_slice, effective_episode
from scripts.search_fund_allocations import metrics
from scripts.validate_cross_asset_trend import audit_current_holdings
from scripts.validate_fund_allocation import validate_allocation
from scripts.validate_phased_class_trend import audit_phased_decision

PROTOCOL = ROOT / "config/phased-class-trend-research-v1.json"


def phased_trial(data, calendar, policy, variant, *, start=252, end=None, phase=0, fee=Decimal(10),
                 lag=1, detail=True, deadline=False, costs=FundHoldingCosts(), minimum_holding_days=None):
    if variant not in ("grid20", "grid5", "phaseblend"):
        raise ValueError("Unknown phased trend variant")
    execution = replace(EXECUTION, initial_review=True, review_sessions=20 if variant == "grid20" else 5,
                        execution_lag_sessions=lag, deadline_entry_guard_days=7 if deadline else 0,
                        minimum_holding_days=EXECUTION.minimum_holding_days if minimum_holding_days is None else minimum_holding_days)
    if variant != "phaseblend":
        return replay(data, calendar, policy, start=start, end=end, phase=phase, fee=fee, execution=execution,
                      detail=detail, exit_mode="absolute_or_current_risk", costs=costs)
    if not isinstance(phase, int) or isinstance(phase, bool) or not 0 <= phase < 5:
        raise ValueError("Blend study only uses the five nonalias clock phases")
    trace = []
    def allocate(index, held, state):
        target, meta = phased_class_target(data, policy, index, held, start=start, phase=phase, portfolio_state=state)
        trace.append(meta | {"targets": target.weights})
        return target
    account = simulate_allocations(data, calendar, allocate, start=start, end=end, phase=phase, fee_bps=fee,
                                   execution=execution, include_portfolio_state=True, costs=costs)
    ledger = validate_allocation(account, data, calendar)
    indexes = {day: i for i, day in enumerate(data["days"])}
    for entry in trace:
        audit_phased_decision(entry, data, indexes[entry["signal_date"]], policy, start=start, phase=phase)
    measured = metrics(account)
    equities = np.asarray([float(row["equity"]) for row in account["equity_curve"]])
    measured["realized_annual_volatility_pct"] = float(np.std(equities[1:] / equities[:-1] - 1) * np.sqrt(252)) * 100
    measured["mean_market_exposure_pct"] = float(np.mean([float(row["market_value"]) / float(row["equity"]) for row in account["equity_curve"]])) * 100
    result = {"metrics": measured, "ledger_validation": ledger,
              "signal_and_risk_validation": {"status": "passed", "decisions": len(trace), "past_views_per_decision": 4},
              "current_holdings_validation": audit_current_holdings(account, trace, data)}
    if detail:
        result.update(account=account, signal_trace=trace)
    return result


def run():
    protocol = json.loads(PROTOCOL.read_text())
    reference_path = ROOT / protocol["reference"]
    reference = json.load(gzip.open(reference_path, "rt", encoding="utf-8"))
    prior_path = ROOT / reference["protocol"]["reference"]
    prior = json.load(gzip.open(prior_path, "rt", encoding="utf-8"))
    if sha256(prior_path.read_bytes()).hexdigest() != reference["reference_sha256"]:
        raise ValueError("Frozen prior cost-aware reference changed")
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key] or prior[key] != reference[key]:
            raise ValueError(f"Frozen phased input differs: {key}")
    allowed = {"backend/analysis/cross_asset_trend.py", "scripts/validate_cross_asset_trend.py"}
    differences = {}
    for path, digest in reference["source_hashes"].items():
        current = sha256((ROOT / path).read_bytes()).hexdigest()
        if current != digest:
            if path not in allowed:
                raise ValueError(f"Unrelated phase reference source changed: {path}")
            differences[path] = {"reference": digest, "current": current}
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, fund_audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    results, unchanged_main, unchanged_phase = {}, 0, 0
    for name in protocol["cohorts"]:
        frozen = reference["cohorts"][name]
        data = prepare_class_proxies(prepare_local(funds, frozen["codes"], calendar))
        data["cross_stats"] = {i: class_statistics(data, i) for i in range(252, len(data["days"]))}
        full, stress = {}, {}
        for policy in protocol["policies"]:
            full[policy] = {v: phased_trial(data, calendar, policy, v) for v in protocol["variants"]}
            if full[policy]["grid20"]["metrics"] != frozen["full"][policy]["grid20"]["prior"]["metrics"]:
                raise ValueError("Shared risk-helper extraction changed prior grid20 account")
            unchanged_main += 1
            stress[policy] = {}
            for phase in protocol["stress"]["five_day_clock_phases"]:
                for variant in ("grid5", "phaseblend"):
                    stress[policy][f"{variant}_phase{phase}"] = phased_trial(data, calendar, policy, variant, phase=phase, detail=False)
            for phase in protocol["stress"]["grid20_phases"]:
                trial = phased_trial(data, calendar, policy, "grid20", phase=phase, detail=False)
                if trial["metrics"] != prior["cohorts"][name]["stress"][policy][f"grid20_phase{phase}"]["metrics"]:
                    raise ValueError("Shared risk-helper extraction changed prior phase control")
                unchanged_phase += 1
                stress[policy][f"grid20_phase{phase}"] = trial
            for fee in protocol["stress"]["ordinary_fee_bps_each_side"]:
                stress[policy][f"phaseblend_fee{fee}"] = phased_trial(data, calendar, policy, "phaseblend", fee=Decimal(fee), detail=False)
            stress[policy]["phaseblend_lag2"] = phased_trial(data, calendar, policy, "phaseblend",
                lag=protocol["stress"]["execution_lag_sessions"], detail=False)
        episodes = {}
        for episode in reference["regime_protocol"]["episodes"]:
            coverage = effective_episode(data["days"], episode)
            if coverage != frozen["episodes"][episode["id"]]["coverage"]:
                raise ValueError("Phase blend changed fixed regime coverage")
            segments = {}
            if coverage["status"] != "insufficient_shared_history":
                for policy in protocol["policies"]:
                    segments[policy] = {}
                    for variant in protocol["variants"]:
                        fresh = phased_trial(data, calendar, policy, variant, start=coverage["start_index"],
                                             end=coverage["end_index"], detail=False, deadline=True)
                        carried = carry_slice(full[policy][variant]["account"], coverage["start_date"], coverage["end_date"])
                        segments[policy][variant] = {"fresh": fresh, "carry": carried}
            episodes[episode["id"]] = {"coverage": coverage, "strategies": segments}
        results[name] = {"codes": frozen["codes"], "full": full, "stress": stress, "episodes": episodes}
        print(json.dumps({"cohort": name, "return_drawdown_fees": {p: {v: [r["metrics"]["total_return_pct"],
            r["metrics"]["max_drawdown_pct"], float(r["metrics"]["total_fees"])] for v, r in cases.items()}
            for p, cases in full.items()}}, ensure_ascii=False), flush=True)
    paths = set(reference["source_hashes"]) | {"backend/analysis/phased_class_trend.py", "scripts/validate_phased_class_trend.py",
                                              "scripts/research_phased_class_trends.py"}
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "protocol": protocol,
            "regime_protocol": reference["regime_protocol"], "market_episodes": reference["market_episodes"],
            "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "source_snapshots": snapshots,
            "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()},
            "input_sha256": reference["input_sha256"], "bond_input_sha256": reference["bond_input_sha256"],
            "calendar": reference["calendar"], "calendar_sha256": reference["calendar_sha256"],
            "reference_sha256": sha256(reference_path.read_bytes()).hexdigest(), "prior_reference_sha256": sha256(prior_path.read_bytes()).hexdigest(),
            "reference_source_differences": differences, "prior_main_accounts_unchanged": unchanged_main,
            "prior_phase_accounts_unchanged": unchanged_phase, "fund_audit": fund_audit + bond_audit}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/phased-class-trend-research-v1.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))


if __name__ == "__main__":
    main()
