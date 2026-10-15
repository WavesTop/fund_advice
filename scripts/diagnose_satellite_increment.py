"""Separate trend overlay increments from controller lifecycle effects."""
import argparse
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.export_strategy_trial import checked_metrics

PROTOCOL = ROOT / "config/satellite-increment-attribution-v1.json"


def factorial_returns(values):
    if len(values) != 5 or any(not math.isfinite(v) or v <= -100 for v in values):
        raise ValueError("Attribution requires five finite positive-wealth returns")
    core, trend, event_core, event_trend, benchmark = values
    terms = {"cadence_vs_inverse_pp": core - benchmark,
             "original_trend_increment_pp": trend - core,
             "core_repair_increment_pp": event_core - core,
             "repair_trend_interaction_pp": (event_trend - event_core) - (trend - core)}
    total = event_trend - benchmark
    if not math.isclose(sum(terms.values()), total, abs_tol=1e-10):
        raise ValueError("Attribution identity failed")
    return {**terms, "event_trend_increment_pp": event_trend - event_core,
            "event_trend_vs_inverse_pp": total}


def matched_policy(trend, core):
    a, b = dict(trend), dict(core)
    if a.pop("exit_rule") != "fixed20" or b.pop("exit_rule") != "none":
        raise ValueError("Expected fixed20 overlay versus no-overlay core")
    a.pop("name")
    b.pop("name")
    if a != b:
        raise ValueError("Trend and core controller settings differ")


def checked_pair(trend, core):
    matched_policy(trend["policy"], core["policy"])
    a, b = checked_metrics(trend), checked_metrics(core)
    if any(trend["metrics"][k] != core["metrics"][k] for k in ("start_date", "end_date")):
        raise ValueError("Paired runs cover different dates")
    return {"trend_return_pct": a["total_return_pct"], "core_return_pct": b["total_return_pct"],
            "increment_pp": a["total_return_pct"] - b["total_return_pct"],
            "trend_max_drawdown_pct": abs(a["max_drawdown_pct"]),
            "core_max_drawdown_pct": abs(b["max_drawdown_pct"]),
            "trend_minus_core_fee": a["total_fees"] - b["total_fees"]}


def aligned_curves(trend, core):
    days = [row["date"] for row in trend]
    if not days or days != sorted(set(days)) or days != [row["date"] for row in core]:
        raise ValueError("Paired curves need identical strictly increasing dates")
    for day in days:
        datetime.strptime(day, "%Y-%m-%d")
    a, b = [[float(row["equity"]) for row in curve] for curve in (trend, core)]
    if any(not math.isfinite(v) or v <= 0 for v in a + b):
        raise ValueError("Paired wealth must be finite and positive")
    return days, a, b


def paired_interval(days, a, b, first, last):
    if not 0 <= first <= last < len(days):
        raise ValueError("Invalid paired interval")
    prior = max(0, first - 1)
    ratios = [(a[i] / a[prior]) / (b[i] / b[prior]) for i in range(first, last + 1)]
    peak, dd = 1., 0.
    for ratio in ratios:
        peak = max(peak, ratio)
        dd = min(dd, ratio / peak - 1)
    ar, br = (a[last] / a[prior] - 1) * 100, (b[last] / b[prior] - 1) * 100
    return {"start_date": days[first], "end_date": days[last],
            "reference_date": days[prior], "return_intervals": last - prior,
            "trend_return_pct": ar, "core_return_pct": br, "increment_pp": ar - br,
            "relative_wealth_return_pct": (ratios[-1] - 1) * 100,
            "relative_wealth_max_drawdown_pct": abs(dd * 100)}


def paired_curves(trend, core, block_sessions):
    if isinstance(block_sessions, bool) or not isinstance(block_sessions, int) or block_sessions < 1:
        raise ValueError("Block length must be a positive integer")
    days, a, b = aligned_curves(trend, core)
    annual = {}
    for year in sorted({day[:4] for day in days}):
        first, last = bisect_left(days, year + "-01-01"), bisect_right(days, year + "-12-31") - 1
        annual[year] = {**paired_interval(days, a, b, first, last),
                        "partial_calendar_year": days[0] > year + "-01-01" or days[-1] < year + "-12-31"}
    blocks = []
    for first in range(1, len(days), block_sessions):
        last = min(first + block_sessions - 1, len(days) - 1)
        blocks.append({**paired_interval(days, a, b, first, last),
                       "complete_block": last - first + 1 == block_sessions})
    full = [row for row in blocks if row["complete_block"]]
    return {"full": paired_interval(days, a, b, 0, len(days) - 1), "annual": annual,
            "nonoverlapping_blocks": blocks,
            "complete_block_count": len(full),
            "complete_blocks_trend_ahead": sum(row["increment_pp"] > 1e-8 for row in full),
            "block_scope": "Continuous-account intervals, not fresh cash entries. Adjacent return intervals do not overlap but share market history and controller state; not independent probability. Partial tail kept separately. Relative-wealth drawdown is benchmark-relative, not portfolio drawdown"}


def checked_episode(trend, core, coverage, saved_trend, saved_core):
    days, a, b = aligned_curves(trend, core)
    first = bisect_left(days, coverage["start_date"])
    last = bisect_right(days, coverage["end_date"]) - 1
    if first >= len(days) or days[first] != coverage["start_date"] or days[last] != coverage["end_date"]:
        raise ValueError("Episode coverage does not match account dates")
    result = paired_interval(days, a, b, first, last)
    for key, saved in (("trend_return_pct", saved_trend), ("core_return_pct", saved_core)):
        if not math.isclose(result[key], saved["total_return_pct"], abs_tol=1e-8):
            raise ValueError("Raw account curve does not reproduce saved episode return")
    return result


def run():
    protocol = json.loads(PROTOCOL.read_text())
    reference = ROOT / protocol["reference"]
    if sha256(reference.read_bytes()).hexdigest() != protocol["reference_sha256"]:
        raise ValueError("Frozen attribution report changed")
    with gzip.open(reference, "rt") as stream:
        report = json.load(stream)
    if set(report["cohorts"]) != set(protocol["cohorts"]):
        raise ValueError("Attribution omitted or added a cohort")
    results = {}
    for name in protocol["cohorts"]:
        cohort = report["cohorts"][name]
        main = cohort["full"]
        policies = protocol["factorial_arms"] + ["inverse20"]
        measured = {key: checked_metrics(main[key]) for key in policies}
        days = [[row["date"] for row in main[key]["account"]["equity_curve"]] for key in policies]
        if any(value != days[0] for value in days[1:]):
            raise ValueError("Factorial arms have different account coverage")
        # Only the repair flag may differ between the two versions of a given arm.
        for old, new in (("matched_core", "event_core"), ("fixed20", "event_fixed20")):
            a, b = dict(main[old]["policy"]), dict(main[new]["policy"])
            a.pop("name")
            b.pop("name")
            if a.pop("risk_repair") != "immediate" or b.pop("risk_repair") != "event" or a != b:
                raise ValueError("Factorial arms changed something besides repair lifecycle")
        comparisons = {}
        for label, trend, core in (("immediate", "fixed20", "matched_core"),
                                    ("event", "event_fixed20", "event_core")):
            comparisons[label] = {"metrics": checked_pair(main[trend], main[core]),
                                  **paired_curves(main[trend]["account"]["equity_curve"],
                                                  main[core]["account"]["equity_curve"],
                                                  protocol["nonoverlapping_sessions"])}
        stress = {}
        for label in report["protocol"]["stress"]["fee_bps"]:
            stress[f"fee{label}"] = {}
        for phase in report["protocol"]["stress"]["core_and_check_phases"]:
            stress[f"phase{phase}"] = {}
        stress["lag2"] = {}
        for key in stress:
            stress[key] = checked_pair(cohort["stress"]["event_fixed20"][key],
                                       cohort["stress"]["event_core"][key])
        fresh = {}
        for day, row in cohort["fresh_entry"].items():
            fresh[day] = {"status": row["status"], "actual_start": row.get("actual_start")}
            if row["status"] == "complete":
                fresh[day]["comparison"] = checked_pair(row["policies"]["event_fixed20"],
                                                         row["policies"]["event_core"])
        episodes = {}
        for episode in report["regime_protocol"]["episodes"]:
            identifier = episode["id"]
            coverage = cohort["episodes"][identifier]["coverage"]
            pair = None
            if coverage["status"] != "insufficient_shared_history":
                t, c = [cohort["episodes"][identifier]["policies"][key]
                        for key in ("event_fixed20", "event_core")]
                pair = checked_episode(main["event_fixed20"]["account"]["equity_curve"],
                                       main["event_core"]["account"]["equity_curve"], coverage, t, c)
            episodes[identifier] = {"coverage": coverage, "group": episode["evaluation_group"], "comparison": pair}
        results[name] = {"main": comparisons,
                         "factorial": factorial_returns([measured[key]["total_return_pct"] for key in policies]),
                         "stress": stress, "fresh_entry": fresh, "episodes": episodes}
    source = Path(__file__).read_bytes()
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol,
            "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "reference_sha256": protocol["reference_sha256"],
            "source_snapshot": source.decode(), "source_sha256": sha256(source).hexdigest(),
            "metrics_checker_snapshot": (ROOT / "scripts/export_strategy_trial.py").read_text(),
            "cohorts": results, "new_account_replays": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/satellite-increment-attribution-v1.json")
    args = parser.parse_args()
    result = run()
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"output": str(args.output), "factorial": {k: v["factorial"] for k, v in result["cohorts"].items()}}, ensure_ascii=False))
