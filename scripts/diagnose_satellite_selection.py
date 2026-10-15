"""Cost/timing stresses and deterministic eligible-sector random controls."""
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

from backend.analysis.portfolio_increment import prepare_economic
from backend.analysis.trend_satellite import policies
from backend.core.trading_calendar import TradingCalendar
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import carry_slice
from scripts.research_trend_satellites import satellite_trial

PROTOCOL = ROOT / "config/satellite-selection-diagnostic-v1.json"


def distribution(values):
    x = np.asarray(values, dtype=float)
    if x.ndim != 1 or not len(x) or not np.isfinite(x).all():
        raise ValueError("Random selection summary needs finite nonempty observations")
    return {"mean": float(x.mean()), "median": float(np.median(x)), "p10": float(np.percentile(x, 10)),
            "p90": float(np.percentile(x, 90)), "min": float(x.min()), "max": float(x.max())}


def compare_returns(values, trend_return):
    distribution(values)
    if not np.isfinite(trend_return):
        raise ValueError("Invalid observed trend return")
    below = sum(v < trend_return-1e-8 for v in values)
    above = sum(v > trend_return+1e-8 for v in values)
    ties = len(values)-below-above
    return {"count": len(values), "random_below_trend": below, "random_above_trend": above,
            "ties": ties, "observed_trend_path_percentile": 100*(below+.5*ties)/len(values),
            "trend_minus_random_median_pp": trend_return-float(np.median(values))}


def summarize(trials, trend, controls):
    seeds = [r["seed"] for r in trials]
    if len(seeds) != len(set(seeds)):
        raise ValueError("Random summary duplicated a seed")
    values = [r["metrics"]["total_return_pct"] for r in trials]
    measured = {f: distribution([abs(r["metrics"][f]) if f == "max_drawdown_pct" else r["metrics"][f] for r in trials])
                for f in ("total_return_pct", "max_drawdown_pct", "realized_annual_volatility_pct", "mean_market_exposure_pct")}
    return {"distributions": measured, **compare_returns(values, trend["metrics"]["total_return_pct"]),
            "random_above_controls": {p: sum(v > c["metrics"]["total_return_pct"]+1e-8 for v in values) for p, c in controls.items()}}


def run():
    protocol = json.loads(PROTOCOL.read_text())
    reference_path = ROOT/protocol["reference"]
    if sha256(reference_path.read_bytes()).hexdigest() != protocol["reference_sha256"]:
        raise ValueError("Frozen satellite-selection reference report changed")
    reference = json.load(gzip.open(reference_path, "rt"))
    if sha256((ROOT/protocol["reference_protocol"]).read_bytes()).hexdigest() != reference["protocol_sha256"]:
        raise ValueError("Satellite-selection earlier protocol changed")
    prior_path = ROOT/reference["protocol"]["reference"]
    if sha256(prior_path.read_bytes()).hexdigest() != reference["reference_sha256"]:
        raise ValueError("Satellite-selection earlier holding reference changed")
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Frozen random-control input changed: {key}")
    allowed = {"backend/analysis/trend_satellite.py", "scripts/validate_trend_satellite.py"}
    changed = {}
    for path, digest in reference["source_hashes"].items():
        actual = sha256((ROOT/path).read_bytes()).hexdigest()
        if actual != digest:
            if path not in allowed:
                raise ValueError(f"Unrelated satellite-selection source changed: {path}")
            changed[path] = {"reference": digest, "current": actual}
    seeds = protocol["random_seeds"]
    if (not seeds or any(not isinstance(s, int) or isinstance(s, bool) or s < 0 for s in seeds)
            or seeds != sorted(set(seeds))):
        raise ValueError("Random-control seeds must be unique sorted nonnegative integers")
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    definitions = {p.name: p for p in policies()} | {"inverse20": "inverse20"}
    results, compatible = {}, 0
    for name in protocol["cohorts"]:
        frozen = reference["cohorts"][name]
        data = prepare_economic(prepare_local(funds, frozen["codes"], calendar))
        cache = {}
        full = {}
        for key in reference["protocol"]["policies"]:
            trial = satellite_trial(data, calendar, definitions[key], cache=cache)
            old = frozen["full"][key]
            if trial["metrics"] != old["metrics"] or trial["account"] != old["account"]:
                raise ValueError(f"Random selector changed default actual account: {name}/{key}")
            if key != "inverse20" and trial["daily_checks"] != old["daily_checks"]:
                raise ValueError("Random selector changed an old daily check/state transition")
            compatible += 1
            full[key] = trial
        stress = {}
        for key in protocol["stress_policies"]:
            policy = definitions[key]
            stress[key] = {}
            for fee in protocol["stress"]["fee_bps"]:
                stress[key][f"fee{fee}"] = satellite_trial(data, calendar, policy, fee=Decimal(fee), detail=False, cache=cache)
            for phase in protocol["stress"]["core_and_check_phases"]:
                stress[key][f"phase{phase}"] = satellite_trial(data, calendar, policy, phase=phase, detail=False, cache=cache)
            stress[key]["lag2"] = satellite_trial(data, calendar, policy, lag=2, detail=False, cache=cache)
        random_trials, summaries, episodes = {}, {}, {}
        for key in protocol["random_policies"]:
            trials = []
            for seed in seeds:
                policy = replace(definitions[key], name=f"random_{key}_{seed}", random_seed=seed)
                trial = satellite_trial(data, calendar, policy, cache=cache)
                trial["seed"] = seed
                cuts = {}
                for episode in reference["regime_protocol"]["episodes"]:
                    coverage = frozen["episodes"][episode["id"]]["coverage"]
                    if coverage["status"] != "insufficient_shared_history":
                        cuts[episode["id"]] = carry_slice(trial["account"], coverage["start_date"], coverage["end_date"])
                trial["carry_episodes"] = cuts
                if seed not in (seeds[0], seeds[-1]):
                    trial.pop("account")
                    trial.pop("daily_checks")
                trials.append(trial)
            random_trials[key] = trials
            summaries[key] = summarize(trials, full[key], {p: full[p] for p in ("matched_core", "inverse20")})
            for episode in reference["regime_protocol"]["episodes"]:
                identifier = episode["id"]
                coverage = frozen["episodes"][identifier]["coverage"]
                if coverage["status"] == "insufficient_shared_history":
                    episodes.setdefault(identifier, {"coverage": coverage, "policies": {}})
                    continue
                values = [r["carry_episodes"][identifier]["total_return_pct"] for r in trials]
                observed = frozen["episodes"][identifier]["strategies"][key]["carry"]
                episodes.setdefault(identifier, {"coverage": coverage, "policies": {}})["policies"][key] = {
                    "random_return_distribution": distribution(values), "trend_carry": observed,
                    **compare_returns(values, observed["total_return_pct"])}
        results[name] = {"codes": frozen["codes"], "full": full, "stress": stress,
                         "random_trials": random_trials, "random_summaries": summaries, "episodes": episodes}
        print(json.dumps({"cohort": name, "comparisons": {p: {k: v for k, v in s.items() if k != "distributions"}
                          for p, s in summaries.items()}}, ensure_ascii=False), flush=True)
    paths = set(reference["source_hashes"]) | {"scripts/diagnose_satellite_selection.py"}
    snapshots = {p: (ROOT/p).read_text() for p in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results,
        "protocol": protocol, "regime_protocol": reference["regime_protocol"], "unchanged_previous_main_accounts": compatible,
        "allowed_source_changes": changed, "source_snapshots": snapshots,
        "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()},
        "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "reference_sha256": protocol["reference_sha256"],
        "prior_reference_sha256": reference["reference_sha256"],
        **{k: reference[k] for k in ("input_sha256", "bond_input_sha256", "calendar_sha256")}, "fund_audit": audit+bond_audit}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT/"runtime/verification/satellite-selection-diagnostic-v1.json.gz")
    args = parser.parse_args()
    report = run()
    with gzip.open(args.output, "wt", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))
