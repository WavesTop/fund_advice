"""Bull/bear episodes, fresh and carry-in accounts, with causal review clocks."""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import gzip
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.class_selection_events import selection_event_indices
from backend.analysis.cross_asset_trend import class_statistics, prepare_class_proxies
from backend.analysis.market_review_cadence import adaptive_review_indices, market_context
from backend.core.trading_calendar import TradingCalendar
from scripts.research_cross_asset_trends import EXECUTION, replay
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, fixed_cohorts, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_selection_event_reviews import audit_event_sets

PROTOCOL = ROOT / "config/market-regime-strategy-research-v1.json"


def reference_market(fund):
    days = sorted(fund["rows"])
    prices = [float(fund["rows"][days[0]]["unit_nav"])]
    for left, right in zip(days, days[1:]):
        prices.append(prices[-1] * (float(fund["rows"][right]["unit_nav"]) + fund["distributions"][right])
                      / float(fund["rows"][left]["unit_nav"]))
    return {"days": days, "prices": prices, "code": fund["code"]}


def market_return(benchmark, start, end):
    left, right = bisect_left(benchmark["days"], start), bisect_right(benchmark["days"], end) - 1
    if left < 1 or left > right:
        raise ValueError("Evaluation market interval has insufficient reference coverage")
    return (benchmark["prices"][right] / benchmark["prices"][left - 1] - 1) * 100


def effective_episode(days, episode):
    first = max(252, bisect_left(days, episode["start"]))
    last = min(len(days) - 1, bisect_right(days, episode["end"]) - 1)
    if first >= last or last - first < 20:
        return {"status": "insufficient_shared_history", "requested_start": episode["start"], "requested_end": episode["end"]}
    partial = episode["start"] < days[252] or episode["end"] > days[-1]
    return {"status": "partial" if partial else "complete", "requested_start": episode["start"], "requested_end": episode["end"],
            "start_date": days[first], "end_date": days[last], "start_index": first, "end_index": last}


def carry_slice(account, start, end):
    rows = account["equity_curve"]
    dates = [r["date"] for r in rows]
    first, last = bisect_left(dates, start), bisect_right(dates, end) - 1
    if first > last or first >= len(rows):
        raise ValueError("Continuous account does not cover evaluation interval")
    initial = float(rows[first - 1]["equity"]) if first else float(account["initial_equity"])
    peak, drawdown = initial, 0.
    for row in rows[first:last + 1]:
        equity = float(row["equity"])
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    return {"start_date": rows[first]["date"], "end_date": rows[last]["date"],
            "reference_date": rows[first - 1]["date"] if first else None,
            "total_return_pct": (float(rows[last]["equity"]) / initial - 1) * 100, "max_drawdown_pct": drawdown * 100,
            "opening_equity": initial, "closing_equity": float(rows[last]["equity"])}


def audit_market_context(benchmark, days, recorded):
    indexes = {day: i for i, day in enumerate(benchmark["days"])}
    for index in range(252, len(days)):
        at = indexes[days[index]]
        history = benchmark["prices"][at - 199:at + 1]
        if len(history) != 200:
            raise ValueError("Adaptive market context lacks complete past reference")
        average = math.fsum(history) / 200
        change = history[-1] / benchmark["prices"][at - 63] - 1
        state = ("up" if history[-1] > average * (1 + 1e-12) and change > 1e-12 else
                 "down" if history[-1] < average * (1 - 1e-12) and change < -1e-12 else "neutral")
        observed = recorded[index]
        if (observed["state"] != state or observed["signal_date"] != days[index]
                or abs(observed["ma200"] / average - 1) > 1e-10 or abs(observed["return63"] - change) > 1e-10):
            raise ValueError("Adaptive market context differs from past-only reconstruction")


def scheduled_trial(data, calendar, policy, schedule, *, start=252, end=None, detail=True, deadline=False):
    end = len(data["days"]) - 1 if end is None else end
    execution = replace(EXECUTION, initial_review=True, deadline_entry_guard_days=7 if deadline else 0)
    extras = frozenset()
    if schedule.startswith("grid"):
        execution = replace(execution, review_sessions=int(schedule[4:]))
    elif schedule == "daily":
        execution = replace(execution, review_sessions=1)
    elif schedule == "event20":
        extras = data["event_sets"][policy]
    elif schedule == "adaptive":
        execution = replace(execution, review_sessions=len(data["days"]) + 1)
        extras = adaptive_review_indices(data["market_context"], start, end)
        wanted, previous = {start}, start
        for index in range(start + 1, end + 1):
            interval = {"up": 20, "down": 5, "neutral": 10}[data["market_context"][index]["state"]]
            if index - previous >= interval:
                wanted.add(index)
                previous = index
        if extras != frozenset(wanted):
            raise ValueError("Adaptive review clock differs from declared past-only rule")
    else:
        raise ValueError("Unknown review schedule")
    return replay(data, calendar, policy, start=start, end=end, execution=execution,
                  extra_review_indices=extras, detail=detail)


def run():
    protocol = json.loads(PROTOCOL.read_text())
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, audit = load_funds(INPUT, calendar)
    groups = fixed_cohorts(funds)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    benchmark = reference_market(funds["000051"])
    market_episodes = {}
    for episode in protocol["episodes"]:
        value = market_return(benchmark, episode["start"], episode["end"])
        label = episode["evaluation_group"]
        if (label == "bull" and value < 20) or (label == "bear" and value > -20):
            raise ValueError("Fixed bull/bear label not confirmed by full reference interval")
        market_episodes[episode["id"]] = episode | {"gross_reference_return_pct": value}
    definitions = {"long4": ["000217", "000051", "000008", "000016"], "older4": groups["themes_available_2015"] + ["000016"],
                   "older9": groups["themes_available_2020"] + ["000016"], "all13": groups["all_13_themes"] + list(bonds)}
    results = {}
    for name, codes in definitions.items():
        data = prepare_class_proxies(prepare_local(funds, codes, calendar))
        data["cross_stats"] = {i: class_statistics(data, i) for i in range(252, len(data["days"]))}
        data["market_context"] = market_context(benchmark, data["days"])
        audit_market_context(benchmark, data["days"], data["market_context"])
        data["event_sets"] = {p: selection_event_indices(data, p) for p in protocol["policies"]}
        audit_event_sets(data, protocol["policies"], data["event_sets"])
        full = {p: {s: scheduled_trial(data, calendar, p, s) for s in protocol["schedules"]} for p in protocol["policies"]}
        episodes = {}
        for episode in protocol["episodes"]:
            coverage = effective_episode(data["days"], episode)
            if coverage["status"] == "insufficient_shared_history":
                episodes[episode["id"]] = {"coverage": coverage}
                continue
            segment = {}
            for policy in protocol["policies"]:
                segment[policy] = {}
                for schedule in protocol["schedules"]:
                    fresh = scheduled_trial(data, calendar, policy, schedule, start=coverage["start_index"],
                                            end=coverage["end_index"], detail=False, deadline=True)
                    carried = carry_slice(full[policy][schedule]["account"], coverage["start_date"], coverage["end_date"])
                    segment[policy][schedule] = {"fresh": fresh, "carry": carried}
            episodes[episode["id"]] = {"coverage": coverage, "effective_market_return_pct": market_return(
                benchmark, coverage["start_date"], coverage["end_date"]), "strategies": segment}
        results[name] = {"codes": codes, "full": full, "episodes": episodes,
                         "market_context_validation": {"status": "passed", "dates": len(data["days"]) - 252},
                         "selection_event_validation": {"status": "passed"}}
        print(json.dumps({"cohort": name, "episodes": {k: v["coverage"]["status"] for k, v in episodes.items()},
                          "full_return_drawdown": {p: {s: [v["metrics"]["total_return_pct"], v["metrics"]["max_drawdown_pct"]]
                                                          for s, v in cases.items()} for p, cases in full.items()}}, ensure_ascii=False), flush=True)
    reference = json.load(gzip.open(ROOT / "runtime/verification/class-selection-event-review-v2.json.gz", "rt", encoding="utf-8"))
    if not set(reference["protocol"]["policies"]).issubset(protocol["policies"]):
        raise ValueError("Original event-study controls were removed")
    for policy in reference["protocol"]["policies"]:
        for schedule, mode in (("grid20", "grid20"), ("event20", "event20"), ("daily", "daily")):
            if results["long4"]["full"][policy][schedule]["metrics"] != reference["results"][policy]["modes"][mode]["runs"]["0"]["metrics"]:
                raise ValueError("Existing long full-history reference changed during regime study")
    prior_path = ROOT / "runtime/verification/market-regime-strategy-research-v1.json.gz"
    prior = json.load(gzip.open(prior_path, "rt", encoding="utf-8"))
    unchanged = 0
    for name, old in prior["cohorts"].items():
        for policy, schedules in old["full"].items():
            for schedule, trial in schedules.items():
                if trial["metrics"] != results[name]["full"][policy][schedule]["metrics"]:
                    raise ValueError("Original full-period controls changed during pair followup")
                unchanged += 1
        for episode, segment in old["episodes"].items():
            if segment["coverage"] != results[name]["episodes"][episode]["coverage"]:
                raise ValueError("Regime coverage changed during pair followup")
            for policy, schedules in segment.get("strategies", {}).items():
                for schedule, trial in schedules.items():
                    current = results[name]["episodes"][episode]["strategies"][policy][schedule]
                    if trial["fresh"]["metrics"] != current["fresh"]["metrics"] or trial["carry"] != current["carry"]:
                        raise ValueError("Original regime controls changed during pair followup")
                    unchanged += 1
    paths = set(reference["source_hashes"]) | {"scripts/research_market_regimes.py", "backend/analysis/market_review_cadence.py"}
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "market_episodes": market_episodes,
            "protocol": protocol, "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "fund_audit": audit + bond_audit,
            "input_sha256": sha256(INPUT.read_bytes()).hexdigest(), "bond_input_sha256": sha256(BONDS.read_bytes()).hexdigest(),
            "calendar": json.loads(CALENDAR.read_text()), "calendar_sha256": sha256(CALENDAR.read_bytes()).hexdigest(),
            "reference_sha256": sha256((ROOT / "runtime/verification/class-selection-event-review-v2.json.gz").read_bytes()).hexdigest(),
            "all15prior_long_full_metrics_match": True,
            "initial_regime_reference_sha256": sha256(prior_path.read_bytes()).hexdigest(), "initial_regime_accounts_unchanged": unchanged,
            "source_snapshots": snapshots, "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/market-regime-strategy-research-pair-v2.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))


if __name__ == "__main__":
    main()
