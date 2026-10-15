"""Re-audit frozen default accounts after a wrong-gate diagnostic guard fix."""
import argparse
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.portfolio_increment import prepare_economic
from backend.core.trading_calendar import TradingCalendar
from scripts.export_strategy_trial import checked_metrics
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_satellite_competition import competition_policies
from scripts.validate_fund_allocation import validate_allocation
from scripts.validate_trend_satellite import audit_satellite

REFERENCE = ROOT / "runtime/verification/satellite-competition-research-v1.json.gz"
DIGEST = "03ea7745015c1a81ff6c06cdcd3125f35dc2d4145da32fa6931aa5bffcd9ef93"


def run():
    if sha256(REFERENCE.read_bytes()).hexdigest() != DIGEST:
        raise ValueError("Frozen qualified-choice report changed")
    with gzip.open(REFERENCE, "rt") as stream:
        reference = json.load(stream)
    for field, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[field]:
            raise ValueError("Qualified-choice review input changed")
    changes = {}
    for path, digest in reference["source_hashes"].items():
        actual = sha256((ROOT / path).read_bytes()).hexdigest()
        if actual != digest:
            if path != "scripts/validate_trend_satellite.py":
                raise ValueError(f"Unexpected qualified-choice source change: {path}")
            changes[path] = {"reference": digest, "current": actual}
    paths = set(reference["source_hashes"]) | {"scripts/verify_satellite_competition_review.py"}
    snapshots = {path: (ROOT / path).read_text() for path in sorted(paths)}
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, _ = load_funds(INPUT, calendar)
    bonds, _ = load_funds(BONDS, calendar)
    funds.update(bonds)
    policies = competition_policies()
    results = {}
    for name, cohort in reference["cohorts"].items():
        data = prepare_economic(prepare_local(funds, cohort["codes"], calendar))
        cache = {}
        for scenario in ("fee0_default", "fee10_default"):
            for policy, trial in cohort["scenarios"][scenario]["policies"].items():
                checked_metrics(trial)
                key = f"{name}/{scenario}/{policy}"
                results[key] = {"ledger": validate_allocation(trial["account"], data, calendar),
                    "signals": audit_satellite(trial["account"], trial["daily_checks"], data, policies[policy], cache=cache)}
        print(json.dumps({"cohort": name, "checked_frozen_accounts": len(results)}), flush=True)
    if len(results) != 16:
        raise ValueError("Qualified-choice review omitted a full default account")
    for path, source in snapshots.items():
        if (ROOT / path).read_text() != source:
            raise ValueError("Qualified-choice review source changed during verification")
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "reference_sha256": DIGEST,
            "source_snapshots": snapshots, "source_hashes": {path: sha256(source.encode()).hexdigest() for path, source in snapshots.items()},
            "allowed_source_changes": changes, "checked_frozen_accounts": results, "new_account_replays": 0,
            "scope": "Current independent ledger and raw-past signal auditor on all16retained full defaults. Original212formal runs retain their original archived auditor; its later wrong-mode ValueError guard only concerns direction/wide parsing, not any formal none/competition path. No claim of repeating compacted nondefault account logs"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/satellite-competition-audit-review-v1.json")
    args = parser.parse_args()
    result = run()
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"output": str(args.output), "checked_accounts": len(result["checked_frozen_accounts"])}), flush=True)
