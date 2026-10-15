"""Past-only market activation of continuous sector satellites."""
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

from backend.analysis.portfolio_increment import prepare_economic
from backend.analysis.trend_satellite import SatellitePolicy, policies
from backend.core.trading_calendar import TradingCalendar
from scripts.diagnose_satellite_selection import compare_returns, distribution, summarize
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import carry_slice
from scripts.research_trend_satellites import satellite_trial

PROTOCOL = ROOT/"config/satellite-market-gate-research-v1.json"


def gate_policies():
    base = SatellitePolicy("fixed20", "fixed20")
    return {p.name: p for p in (
        replace(base, name="direction_entry", market_gate="direction"),
        replace(base, name="wide_entry", market_gate="wide"),
        replace(base, name="wide_exit", market_gate="wide", exit_on_gate=True))}


def gate_trial(data, calendar, policy, *, detail=True, **kwargs):
    result = satellite_trial(data, calendar, policy, detail=True, **kwargs)
    if policy != "inverse20" and policy.market_gate != "none":
        checks = result["daily_checks"]
        result["events"]["market_exit"] = sum(r["event"] == "market_exit" for r in checks)
        result["market_checks"] = {"open": sum(r["market_observation"]["enabled"] for r in checks),
                                   "closed": sum(not r["market_observation"]["enabled"] for r in checks)}
    if not detail:
        result.pop("account")
        result.pop("daily_checks" if policy != "inverse20" else "signal_trace")
    return result


def require_unchanged(trial, frozen):
    for key in ("metrics", "account"):
        if trial[key] != frozen[key]:
            raise ValueError("Market-gate extension changed an old account or its metrics")
    if "daily_checks" in frozen and trial["daily_checks"] != frozen["daily_checks"]:
        raise ValueError("Market-gate extension changed an old daily state/decision")


def run():
    protocol = json.loads(PROTOCOL.read_text())
    reference_path = ROOT/protocol["reference"]
    if sha256(reference_path.read_bytes()).hexdigest() != protocol["reference_sha256"]:
        raise ValueError("Frozen market-gate reference changed")
    reference = json.load(gzip.open(reference_path, "rt"))
    if sha256((ROOT/protocol["reference_protocol"]).read_bytes()).hexdigest() != reference["protocol_sha256"]:
        raise ValueError("Frozen earlier random-control protocol changed")
    prior_path = ROOT/reference["protocol"]["reference"]
    if sha256(prior_path.read_bytes()).hexdigest() != reference["reference_sha256"]:
        raise ValueError("Frozen earlier continuous satellite report changed")
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Frozen market-gate input changed: {key}")
    allowed = {"backend/analysis/trend_satellite.py", "scripts/validate_trend_satellite.py"}
    changed = {}
    for path, digest in reference["source_hashes"].items():
        actual = sha256((ROOT/path).read_bytes()).hexdigest()
        if actual != digest:
            if path not in allowed:
                raise ValueError(f"Unrelated market-gate source changed: {path}")
            changed[path] = {"reference": digest, "current": actual}
    seeds = protocol["random_seeds"]
    if not seeds or seeds != sorted(set(seeds)) or any(not isinstance(s, int) or isinstance(s, bool) or s < 0 for s in seeds):
        raise ValueError("Market-gate controls require unique sorted nonnegative seeds")
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    definitions = {p.name: p for p in policies()} | gate_policies() | {"inverse20": "inverse20"}
    results, compatible_main, compatible_random = {}, 0, 0
    for name in protocol["cohorts"]:
        frozen = reference["cohorts"][name]
        data = prepare_economic(prepare_local(funds, frozen["codes"], calendar))
        cache, full = {}, {}
        for key, old in frozen["full"].items():
            trial = gate_trial(data, calendar, definitions[key], cache=cache)
            require_unchanged(trial, old)
            compatible_main += 1
            if key in ("matched_core", "inverse20", "fixed20"):
                full[key] = trial
        for trials in frozen["random_trials"].values():
            for old in trials:
                if "account" in old:
                    trial = gate_trial(data, calendar, SatellitePolicy(**old["policy"]), cache=cache)
                    require_unchanged(trial, old)
                    compatible_random += 1
        for key in protocol["gates"]:
            full[key] = gate_trial(data, calendar, definitions[key], cache=cache)
        stress = {}
        for key in protocol["stress_policies"]:
            stress[key] = {}
            for fee in protocol["stress"]["fee_bps"]:
                stress[key][f"fee{fee}"] = gate_trial(data, calendar, definitions[key], fee=Decimal(fee), detail=False, cache=cache)
            for phase in protocol["stress"]["core_and_check_phases"]:
                stress[key][f"phase{phase}"] = gate_trial(data, calendar, definitions[key], phase=phase, detail=False, cache=cache)
            stress[key]["lag2"] = gate_trial(data, calendar, definitions[key], lag=2, detail=False, cache=cache)
        episodes = {}
        for episode in reference["regime_protocol"]["episodes"]:
            identifier = episode["id"]
            coverage = frozen["episodes"][identifier]["coverage"]
            variants = {}
            if coverage["status"] != "insufficient_shared_history":
                for key in protocol["gates"]:
                    variants[key] = {"fresh": gate_trial(data, calendar, definitions[key], start=coverage["start_index"],
                        end=coverage["end_index"], deadline=True, detail=False, cache=cache),
                        "carry": carry_slice(full[key]["account"], coverage["start_date"], coverage["end_date"])}
            episodes[identifier] = {"coverage": coverage, "strategies": variants, "random": {}}
        random_trials, summaries = {}, {}
        for key in protocol["gates"]:
            trials = []
            for seed in seeds:
                policy = replace(definitions[key], name=f"random_{key}_{seed}", random_seed=seed)
                trial = gate_trial(data, calendar, policy, cache=cache)
                trial["seed"] = seed
                cuts = {}
                for identifier, episode in episodes.items():
                    coverage = episode["coverage"]
                    if coverage["status"] != "insufficient_shared_history":
                        cuts[identifier] = carry_slice(trial["account"], coverage["start_date"], coverage["end_date"])
                trial["carry_episodes"] = cuts
                if seed not in (seeds[0], seeds[-1]):
                    trial.pop("account")
                    trial.pop("daily_checks")
                trials.append(trial)
            random_trials[key] = trials
            summaries[key] = summarize(trials, full[key], {p: full[p] for p in ("matched_core", "inverse20", "fixed20")})
            for identifier, episode in episodes.items():
                if key not in episode["strategies"]:
                    continue
                values = [r["carry_episodes"][identifier]["total_return_pct"] for r in trials]
                observed = episode["strategies"][key]["carry"]
                episode["random"][key] = {"return_distribution": distribution(values),
                                          **compare_returns(values, observed["total_return_pct"])}
        results[name] = {"codes": frozen["codes"], "full": full, "stress": stress, "episodes": episodes,
            "random_trials": random_trials, "random_summaries": summaries,
            "prior_ungated_random_summary": frozen["random_summaries"]["fixed20"]}
        print(json.dumps({"cohort": name, "return_drawdown": {p: [v["metrics"]["total_return_pct"], v["metrics"]["max_drawdown_pct"]]
                          for p, v in full.items()}, "rank_vs_random_median_pp": {p: s["trend_minus_random_median_pp"]
                          for p, s in summaries.items()}}, ensure_ascii=False), flush=True)
    paths = set(reference["source_hashes"]) | {"scripts/research_satellite_market_gates.py"}
    snapshots = {p: (ROOT/p).read_text() for p in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "protocol": protocol,
        "regime_protocol": reference["regime_protocol"], "unchanged_previous_main_accounts": compatible_main,
        "unchanged_saved_random_accounts": compatible_random, "allowed_source_changes": changed,
        "source_snapshots": snapshots, "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()},
        "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "reference_sha256": protocol["reference_sha256"],
        "prior_reference_sha256": reference["reference_sha256"],
        **{k: reference[k] for k in ("input_sha256", "bond_input_sha256", "calendar_sha256")}, "fund_audit": audit+bond_audit}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT/"runtime/verification/satellite-market-gate-research-v1.json.gz")
    args = parser.parse_args()
    report = run()
    with gzip.open(args.output, "wt", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))
