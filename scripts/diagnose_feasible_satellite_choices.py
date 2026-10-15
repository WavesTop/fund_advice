"""Count feasible lower-ranked alternatives at real recorded no-entry checks."""
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
from backend.analysis.trend_satellite import headroom_target, scaled
from backend.core.trading_calendar import TradingCalendar
from scripts.diagnose_headroom_execution import DIGEST, REFERENCE
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local


def missed_choices(checks, data):
    records, blocked = [], 0
    stocks = data["class_groups"]["stock"]
    for row in checks:
        memory, index = row["memory_after"], row["signal_index"]
        if (not row["check_day"] or memory["candidate"] is not None or memory["closing_candidate"] is not None
                or index < memory["ready_at"]):
            continue
        eligible = sorted((c for c in stocks if row["own_votes"][c] >= 2/3 and row["scores"][c] >= 60),
                          key=lambda c: (-row["scores"][c], c))
        if not eligible:
            continue
        neutral = scaled(data, index, memory["raw_core"])
        choices = [{"code": c, "score": row["scores"][c],
                    "extra_weight_pp": (headroom_target(data["economic_stats"][index]["risk"], neutral, stocks, c)[c]-neutral[c])*100}
                   for c in eligible]
        if choices[0]["extra_weight_pp"] >= .5:
            raise ValueError("Recorded idle state omitted a feasible highest-score entry")
        blocked += 1
        if any(choice["extra_weight_pp"] >= .5 for choice in choices[1:]):
            records.append({"date": row["signal_date"], "choices": choices,
                            "current_risk_requires_emergency": row["current_projected_volatility"] > .1+1e-12})
    return {"highest_score_blocked_checks": blocked, "feasible_lower_rank_checks": len(records),
            "not_immediately_risk_blocked_checks": sum(not r["current_risk_requires_emergency"] for r in records),
            "records": records}


def run():
    if sha256(REFERENCE.read_bytes()).hexdigest() != DIGEST:
        raise ValueError("Frozen feasibility reference changed")
    with gzip.open(REFERENCE, "rt") as stream:
        reference = json.load(stream)
    for path, source in reference["source_snapshots"].items():
        if sha256(source.encode()).hexdigest() != reference["source_hashes"][path]:
            raise ValueError("Archived feasibility source changed")
        if path != "scripts/research_satellite_headroom.py" and (ROOT/path).read_text() != source:
            raise ValueError(f"Active feasibility source changed: {path}")
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Frozen feasibility input changed: {key}")
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, _ = load_funds(INPUT, calendar)
    bonds, _ = load_funds(BONDS, calendar)
    funds.update(bonds)
    results = {}
    for name, cohort in reference["cohorts"].items():
        data = prepare_economic(prepare_local(funds, cohort["codes"], calendar))
        results[name] = {key: missed_choices(cohort["full"][key]["daily_checks"], data)
                         for key in ("headroom_fixed20", "headroom_score")}
        print(json.dumps({"cohort": name, "summary": {key: {k: v for k, v in value.items() if k != "records"}
            for key, value in results[name].items()}}), flush=True)
    source = Path(__file__).read_text()
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "reference_sha256": DIGEST,
            "source_snapshot": source, "source_sha256": sha256(source.encode()).hexdigest(), "cohorts": results,
            "scope": "Recorded controller no-entry observations, not account replays, independent opportunities or projected profits. Production cached risk sizing is used for diagnosis only; any policy change requires independent raw-return audit and complete portfolio replay"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT/"runtime/verification/feasible-satellite-choice-diagnostic-v1.json")
    args = parser.parse_args()
    result = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"output": str(args.output)}), flush=True)
