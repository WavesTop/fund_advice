"""Match start dates and bond exposure when comparing market-gate universes."""
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
from backend.analysis.trend_satellite import policies
from backend.core.trading_calendar import TradingCalendar
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_satellite_market_gates import gate_policies, gate_trial, require_unchanged

PROTOCOL = ROOT/"config/gate-universe-alignment-v1.json"


def run():
    protocol = json.loads(PROTOCOL.read_text())
    path = ROOT/protocol["reference"]
    if sha256(path.read_bytes()).hexdigest() != protocol["reference_sha256"]:
        raise ValueError("Frozen gate-alignment reference changed")
    reference = json.load(gzip.open(path, "rt"))
    for key, source in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(source.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Gate-alignment input changed: {key}")
    for source, digest in reference["source_hashes"].items():
        if sha256((ROOT/source).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Gate-alignment source changed: {source}")
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    older = reference["cohorts"]["older9"]["codes"]
    modern = reference["cohorts"]["all13"]["codes"]
    pure_modern = [c for c in modern if funds[c]["role"] != "defensive"]+ ["000016"]
    universes = {"older9_pure": older, "all13_pure": pure_modern, "all13_threebonds": modern}
    definitions = {p.name: p for p in policies()} | gate_policies() | {"inverse20": "inverse20"}
    results, compatible = {}, 0
    for name in protocol["universes"]:
        data = prepare_economic(prepare_local(funds, universes[name], calendar))
        start = data["days"].index(protocol["start_date"])
        if start < 252:
            raise ValueError("Aligned account lacks complete signal warmup")
        cache = {}
        trials = {}
        for policy in protocol["policies"]:
            trial = gate_trial(data, calendar, definitions[policy], start=start, cache=cache)
            if name == "all13_threebonds":
                # Saved JSON has string horizon keys; compare that exact boundary format.
                serialized = dict(trial)
                if "daily_checks" in serialized:
                    serialized["daily_checks"] = json.loads(json.dumps(serialized["daily_checks"], allow_nan=False))
                require_unchanged(serialized, reference["cohorts"]["all13"]["full"][policy])
                compatible += 1
            final = Decimal(trial["metrics"]["final_equity"])
            if abs(float((final/100000-1)*100)-trial["metrics"]["total_return_pct"]) > 1e-9:
                raise ValueError("Aligned return differs from the actual final account assets")
            trials[policy] = trial
        results[name] = {"codes": universes[name], "start_index": start, "trials": trials}
        print(json.dumps({"universe": name, "returns": {p: [v["metrics"]["total_return_pct"],
                          v["metrics"]["max_drawdown_pct"]] for p, v in trials.items()}}, ensure_ascii=False), flush=True)
    paths = set(reference["source_hashes"]) | {"scripts/diagnose_gate_universes.py"}
    snapshots = {p: (ROOT/p).read_text() for p in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol, "universes": results,
        "unchanged_modern_accounts": compatible, "source_snapshots": snapshots,
        "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()},
        "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "reference_sha256": protocol["reference_sha256"],
        **{k: reference[k] for k in ("input_sha256", "bond_input_sha256", "calendar_sha256")}, "fund_audit": audit+bond_audit}


if __name__ == "__main__":
    report = run()
    path = ROOT/"runtime/verification/gate-universe-alignment-v1.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as output:
        json.dump(report, output, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"output": str(path)}))
