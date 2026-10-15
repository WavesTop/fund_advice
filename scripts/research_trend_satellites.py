"""Continuous core/satellite exit rules and explicit trading-gap ablations."""
import argparse
from dataclasses import asdict, replace
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

from backend.analysis.fund_allocation import AllocationExecution, simulate_allocations
from backend.analysis.portfolio_increment import prepare_economic
from backend.analysis.trend_satellite import TrendSatelliteAllocator, policies
from backend.core.trading_calendar import TradingCalendar
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import carry_slice, effective_episode
from scripts.research_phased_class_trends import phased_trial
from scripts.search_fund_allocations import metrics
from scripts.validate_fund_allocation import validate_allocation
from scripts.validate_trend_satellite import audit_satellite

PROTOCOL = ROOT / "config/trend-satellite-research-v1.json"
EXECUTION = AllocationExecution(review_sessions=1, initial_review=True, rebalance_band=.03,
                                relative_rebalance_band=.25, reinvest_on_settlement=True)


def satellite_trial(data, calendar, policy, *, start=252, end=None, phase=0, lag=1,
                    fee=Decimal(10), detail=True, deadline=False, cache=None):
    if policy == "inverse20":
        return phased_trial(data, calendar, "risky_pair_static60", "grid20", start=start, end=end,
                            phase=phase, lag=lag, fee=fee, detail=detail, deadline=deadline)
    allocate = TrendSatelliteAllocator(data, policy, start=start, phase=phase)
    execution = replace(EXECUTION, execution_lag_sessions=lag, deadline_entry_guard_days=7 if deadline else 0)
    account = simulate_allocations(data, calendar, allocate, start=start, end=end,
                                    execution=execution, fee_bps=fee, include_portfolio_state=True)
    ledger = validate_allocation(account, data, calendar)
    signals = audit_satellite(account, allocate.checks, data, policy, start=start, phase=phase, cache=cache)
    measured = metrics(account)
    equities = np.asarray([float(r["equity"]) for r in account["equity_curve"]])
    measured["realized_annual_volatility_pct"] = float(np.std(equities[1:]/equities[:-1]-1)*np.sqrt(252))*100
    measured["mean_market_exposure_pct"] = float(np.mean([float(r["market_value"])/float(r["equity"]) for r in account["equity_curve"]]))*100
    result = {"metrics": measured, "policy": asdict(policy), "ledger_validation": ledger,
              "signal_validation": signals, "events": {e: sum(r["event"] == e for r in allocate.checks)
                for e in ("entry", "funded", "absolute_failure", "funding_timeout", "expiry", "score_decay", "volatility_trail")}}
    if detail:
        result.update(account=account, daily_checks=allocate.checks)
    return result


def run():
    protocol = json.loads(PROTOCOL.read_text())
    reference_path = ROOT / protocol["reference"]
    reference = json.load(gzip.open(reference_path, "rt"))
    prior_path = ROOT / reference["protocol"]["reference"]
    if sha256(prior_path.read_bytes()).hexdigest() != reference["reference_sha256"]:
        raise ValueError("Holding-action prior reference changed")
    prior = json.load(gzip.open(prior_path, "rt"))
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key] or reference[key] != prior[key]:
            raise ValueError(f"Satellite frozen input changed: {key}")
    for path, digest in reference["source_hashes"].items():
        if sha256((ROOT/path).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Unrelated satellite reference source changed: {path}")
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    definitions = {p.name: p for p in policies()} | {"inverse20": "inverse20"}
    results, compatible = {}, 0
    for name in protocol["cohorts"]:
        codes = reference["cohorts"][name]["codes"]
        data = prepare_economic(prepare_local(funds, codes, calendar))
        cache = {}
        full = {p: satellite_trial(data, calendar, definitions[p], cache=cache) for p in protocol["policies"]}
        frozen = prior["cohorts"][name]["full"]["inverse_unleased20"]
        if full["inverse20"]["metrics"] != frozen["metrics"] or full["inverse20"]["account"] != frozen["account"]:
            raise ValueError("Satellite study changed the earlier inverse20 control")
        compatible += 1
        stress = {}
        for key in ("matched_core", "score_gap5"):
            policy = definitions[key]
            stress[key] = {}
            for fee in protocol["stress"]["fee_bps"]:
                stress[key][f"fee{fee}"] = satellite_trial(data, calendar, policy, fee=Decimal(fee), detail=False, cache=cache)
            for phase in protocol["stress"]["core_and_check_phases"]:
                stress[key][f"phase{phase}"] = satellite_trial(data, calendar, policy, phase=phase, detail=False, cache=cache)
            stress[key]["lag2"] = satellite_trial(data, calendar, policy, lag=2, detail=False, cache=cache)
        for frequency in protocol["stress"]["score_check_sessions"]:
            stress["score_gap5"][f"check{frequency}"] = satellite_trial(data, calendar,
                replace(definitions["score_gap5"], check_sessions=frequency), detail=False, cache=cache)
        episodes = {}
        for episode in prior["regime_protocol"]["episodes"]:
            coverage = effective_episode(data["days"], episode)
            variants = {}
            if coverage["status"] != "insufficient_shared_history":
                for key, policy in definitions.items():
                    variants[key] = {"fresh": satellite_trial(data, calendar, policy, start=coverage["start_index"],
                        end=coverage["end_index"], detail=False, deadline=True, cache=cache),
                        "carry": carry_slice(full[key]["account"], coverage["start_date"], coverage["end_date"])}
            episodes[episode["id"]] = {"coverage": coverage, "strategies": variants}
        results[name] = {"codes": codes, "full": full, "stress": stress, "episodes": episodes}
        print(json.dumps({"cohort": name, "return_drawdown": {p: [v["metrics"]["total_return_pct"],
              v["metrics"]["max_drawdown_pct"]] for p, v in full.items()},
              "score_events": full["score_gap5"]["events"]}, ensure_ascii=False), flush=True)
    paths = set(reference["source_hashes"]) | {"backend/analysis/trend_satellite.py",
        "scripts/research_trend_satellites.py", "scripts/validate_trend_satellite.py"}
    snapshots = {p: (ROOT/p).read_text() for p in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results,
        "protocol": protocol, "regime_protocol": prior["regime_protocol"], "unchanged_inverse20_controls": compatible,
        "source_snapshots": snapshots, "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()},
        "reference_sha256": sha256(reference_path.read_bytes()).hexdigest(),
        "prior_reference_sha256": sha256(prior_path.read_bytes()).hexdigest(), "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(),
        **{k: reference[k] for k in ("input_sha256", "bond_input_sha256", "calendar_sha256")}, "fund_audit": audit+bond_audit}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT/"runtime/verification/trend-satellite-research-v1.json.gz")
    args = parser.parse_args()
    report = run()
    with gzip.open(args.output, "wt", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))
