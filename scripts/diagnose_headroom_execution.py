"""Reproduce the adverse delayed execution paths with complete ledgers."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.portfolio_increment import prepare_economic
from backend.analysis.trend_satellite import SatellitePolicy
from backend.core.trading_calendar import TradingCalendar
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import carry_slice, effective_episode
from scripts.research_trend_satellites import satellite_trial

REFERENCE = ROOT/"runtime/verification/satellite-headroom-research-v1.json.gz"
DIGEST = "8c7abd324cdbbfec425ed562f5e1239b2669eccf074dcaafc926b34a75391a86"


def run():
    if sha256(REFERENCE.read_bytes()).hexdigest() != DIGEST:
        raise ValueError("Frozen headroom diagnostic reference changed")
    with gzip.open(REFERENCE, "rt") as stream:
        reference = json.load(stream)
    for path, source in reference["source_snapshots"].items():
        if sha256(source.encode()).hexdigest() != reference["source_hashes"][path]:
            raise ValueError(f"Archived diagnostic source changed: {path}")
        # This driver is not called here; its later unused-import cleanup has no execution role.
        if path != "scripts/research_satellite_headroom.py" and (ROOT/path).read_text() != source:
            raise ValueError(f"Active diagnostic source changed: {path}")
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Frozen execution input changed: {key}")
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, _ = load_funds(INPUT, calendar)
    bonds, _ = load_funds(BONDS, calendar)
    funds.update(bonds)
    frozen = reference["cohorts"]["all13"]
    data = prepare_economic(prepare_local(funds, frozen["codes"], calendar))
    definitions = {"inverse20": "inverse20", "fixed20": SatellitePolicy("fixed20", "fixed20"),
                   "headroom_fixed20": SatellitePolicy("headroom_fixed20", "fixed20", sizing="headroom")}
    results, cache = {}, {}
    for key, policy in definitions.items():
        result = satellite_trial(data, calendar, policy, lag=2, cache=cache)
        if result["metrics"] != frozen["stress"][key]["lag2"]["metrics"]:
            raise ValueError("Full delayed ledger failed to reproduce the earlier metrics")
        checks = result.get("daily_checks", [])
        main = frozen["full"][key]
        result["diagnostic"] = {
            "actions": dict(Counter(row["action"] for row in checks)),
            "normal_actions": dict(Counter(row["action"] for row in main.get("daily_checks", []))),
            "episodes": {episode["id"]: carry_slice(result["account"], coverage["start_date"], coverage["end_date"])
                for episode in reference["regime_protocol"]["episodes"]
                if (coverage := effective_episode(data["days"], episode))["status"] != "insufficient_shared_history"},
            "delta_return_vs_normal_pp": result["metrics"]["total_return_pct"]-main["metrics"]["total_return_pct"],
            "delta_fees_vs_normal": float(result["metrics"]["total_fees"])-float(main["metrics"]["total_fees"]),
        }
        results[key] = result
        print(json.dumps({"policy": key, "metrics": result["metrics"], "diagnostic": result["diagnostic"]}), flush=True)
    source = Path(__file__).read_text()
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "reference_sha256": DIGEST,
            "new_account_runs": 3, "unchanged_prior_delayed_metrics": 3,
            "source_snapshot": source, "source_sha256": sha256(source.encode()).hexdigest(), "results": results,
            "scope": "Adverse path reproduction, not parameter tuning or a causal per-action profit attribution. Timing changes observations, FIFO/cash state and later decisions; retain complete executed ledgers and audits"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT/"runtime/verification/headroom-execution-diagnostic-v1.json.gz")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output exists; do not overwrite historical research")
    result = run()
    with args.output.open("xb") as stream:
        with gzip.open(stream, "wt", encoding="utf-8") as output:
            json.dump(result, output, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"output": str(args.output)}), flush=True)
