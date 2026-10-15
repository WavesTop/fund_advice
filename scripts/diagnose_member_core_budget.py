"""Static defensive-core headroom checks, not another portfolio return replay."""
from datetime import datetime, timezone
import argparse
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.portfolio_increment import prepare_economic
from backend.analysis.trend_risk_budget import projected_volatility
from backend.analysis.trend_satellite import scaled, surviving_basket
from backend.core.trading_calendar import TradingCalendar
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local

REFERENCE = ROOT / "runtime/verification/satellite-member-exit-research-v1.json.gz"
DIGEST = "4ab5930a1345b47f075a54ab28ae5e67be5577bae5fe93259ff6c730ff7c65c8"


def defensive_fraction(risk, base, neutral, defensive):
    if (set(base) != set(neutral) or set(base) != set(risk["codes"]) or not set(defensive) <= set(base)
            or any(not math.isfinite(weight) or weight < 0 for weight in (*base.values(), *neutral.values()))
            or sum(base.values()) > 1 + 1e-12 or sum(neutral.values()) > 1 + 1e-12):
        raise ValueError("Diagnostic requires complete finite long-only budget-feasible targets")
    desired = {code: max(weight, neutral[code]) if code in defensive else weight for code, weight in base.items()}
    increase = sum(desired.values()) - sum(base.values())
    high = min(1., max(0., (1 - sum(base.values())) / increase)) if increase > 0 else 0.
    if projected_volatility(risk, base) > .1 + 1e-12:
        raise ValueError("Diagnostic starting target is not risk feasible")
    low = 0.
    for _ in range(64):
        middle = (low + high) / 2
        weights = {code: weight + middle * (desired[code] - weight) for code, weight in base.items()}
        if projected_volatility(risk, weights) <= .1:
            low = middle
        else:
            high = middle
    restored = {code: weight + low * (desired[code] - weight) for code, weight in base.items()}
    return low, (sum(restored.values()) - sum(base.values())) * 100


def run():
    if sha256(REFERENCE.read_bytes()).hexdigest() != DIGEST:
        raise ValueError("Frozen member-budget diagnostic reference changed")
    with gzip.open(REFERENCE, "rt") as stream:
        reference = json.load(stream)
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError("Member-budget diagnostic data changed")
    for path, digest in reference["source_hashes"].items():
        if sha256((ROOT / path).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Member-budget diagnostic source changed: {path}")
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, _ = load_funds(INPUT, calendar)
    bonds, _ = load_funds(BONDS, calendar)
    funds.update(bonds)
    results = {}
    for name, cohort in reference["cohorts"].items():
        data = prepare_economic(prepare_local(funds, cohort["codes"], calendar))
        defensive = data["class_groups"]["gold"] + data["class_groups"]["bond"]
        results[name] = {}
        for policy, trial in cohort["scenarios"]["fee10_default"]["policies"].items():
            rows = []
            for record in trial["daily_checks"]:
                memory = record["memory_after"]
                if record["action"] != "repair" or not 0 < len(memory["members"]) < len(memory["plan_members"]):
                    continue
                index = record["signal_index"]
                risk = data["economic_stats"][index]["risk"]
                active, complete = surviving_basket(memory["raw_core"], data["class_groups"]["stock"],
                                                    memory["plan_members"], memory["members"])
                active_risk, full_risk = projected_volatility(risk, active), projected_volatility(risk, complete)
                active_scale = min(1., .1 / active_risk) if active_risk else 1.
                used_scale = min(1., .1 / max(active_risk, full_risk)) if max(active_risk, full_risk) else 1.
                neutral = scaled(data, index, memory["raw_core"])
                fraction, extra = defensive_fraction(risk, record["targets"], neutral, defensive)
                rows.append({"signal_date": record["signal_date"], "members": memory["members"],
                             "shadow_factor_binds": active_scale > used_scale + 1e-12,
                             "feasible_defensive_fraction": fraction, "extra_defensive_target_pp": extra})
            results[name][policy] = {"partial_repair_dates": len(rows),
                "shadow_bound_dates": sum(row["shadow_factor_binds"] for row in rows),
                "defensive_restoration_feasible_dates": sum(row["extra_defensive_target_pp"] >= .005 for row in rows),
                "max_extra_defensive_target_pp": max((row["extra_defensive_target_pp"] for row in rows), default=0.), "rows": rows}
    source = Path(__file__).read_text()
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "reference_sha256": DIGEST,
            "source_snapshot": source, "source_sha256": sha256(source.encode()).hexdigest(), "cohorts": results,
            "new_account_replays": 0, "revised_return_pct": None,
            "scope": "Same-date static target geometry on partial repair records only; stock targets including survivors unchanged, defensive targets move only toward neutral under10%past risk and total<=1. Not spending actual cash/receivables, not changed orders/FIFO or net return. Repeated dates not independent opportunities. Future full execution replay required"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/member-core-budget-diagnostic-v1.json")
    args = parser.parse_args()
    result = run()
    output = args.output
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"output": str(output), "counts": {key: {policy: {field: value for field, value in row.items() if field != "rows"}
        for policy, row in cohort.items()} for key, cohort in result["cohorts"].items()}}), flush=True)
