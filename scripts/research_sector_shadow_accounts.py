"""Continuous, cash-funded hypothetical index accounts from frozen forecasts."""
from __future__ import annotations

import argparse
from bisect import bisect_left
from dataclasses import replace
from datetime import date, datetime, timezone
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
from backend.analysis.sector_shadow_account import METHODS, SectorShadowAllocator
from backend.core.trading_calendar import TradingCalendar
from scripts.research_market_regimes import carry_slice
from scripts.search_fund_allocations import metrics
from scripts.validate_fund_allocation import validate_allocation
from scripts.validate_sector_shadow_account import audit_shadow_account

PROTOCOL = ROOT / "config/sector-shadow-account-v1.json"
PERIODS = {"short": 20, "medium": 60, "long": 120}
EXECUTION = AllocationExecution(review_sessions=1, initial_review=True, rebalance_band=.03,
                                relative_rebalance_band=.25, reinvest_on_settlement=True)


def prepare(reference):
    inputs = reference["input"]
    first = reference["coverage"]["full_pool_common_start"]
    days = [day for day in inputs["days"] if day >= first]
    codes = [board["code"] for board in inputs["boards"]]
    data = {"days": days, "prices": {board["code"]: [board["prices"][day] for day in days] for board in inputs["boards"]},
            "distributions": {code: [0.] * len(days) for code in codes}}
    indexes, sources = {day: index for index, day in enumerate(days)}, {}
    for period, value in reference["periods"].items():
        rows = {}
        for row in value["cases"]:
            learning = row["learning"]
            if not row["secondary"] or learning["status"] != "fitted":
                continue
            day = learning["as_of"]
            if (learning["latest_label_end"] >= day or learning["latest_ready_date"] > day
                    or row["fit_validation"]["status"] != "passed"):
                raise ValueError("Frozen shadow source contains unavailable training data")
            selections = {method: list(row["methods"][method]["selected_codes"]) for method in METHODS[:6]}
            if any(len(selected) > 10 or len(set(selected)) != len(selected) or not set(selected) <= set(codes)
                   for selected in selections.values()):
                raise ValueError("Frozen source has invalid sector identity or slots")
            rows[indexes[day]] = {"source_date": day, "selections": selections}
        if not rows or any(right - left != 5 for left, right in zip(sorted(rows), sorted(rows)[1:])):
            raise ValueError("Frozen weekly forecast source is incomplete")
        sources[period] = rows
    return data, sources


def schedule_at(sources, start, window, phase):
    dates = sorted(sources)
    position = bisect_left(dates, start + phase)
    if position == len(dates):
        raise ValueError("No frozen observation available after account entry")
    first = dates[position]
    scheduled = list(range(first, dates[-1] + 1, window))
    if any(index not in sources for index in scheduled):
        raise ValueError("Review cadence is not on a frozen past forecast date")
    return {index: sources[index] for index in scheduled}


def slices(account, episodes, calendar):
    dates = [row["date"] for row in account["equity_curve"]]
    result = {}
    for episode in episodes:
        requested = [day.isoformat() for day in calendar.sessions(date.fromisoformat(episode["start"]), date.fromisoformat(episode["end"]))]
        covered = [day for day in dates if episode["start"] <= day <= episode["end"]]
        if len(covered) < 20:
            result[episode["id"]] = {"status": "insufficient_account_history", "value": None}
        else:
            status = "complete" if covered[0] == requested[0] and covered[-1] == requested[-1] else "partial"
            result[episode["id"]] = {"status": status, "value": carry_slice(account, covered[0], covered[-1])}
    return result


def trial(data, sources, calendar, window, method, scenario, *, start, episodes):
    schedule = schedule_at(sources, start, window, scenario["phase"])
    allocator = SectorShadowAllocator(list(data["prices"]), schedule, method, repair=scenario["repair"])
    account = simulate_allocations(data, calendar, allocator, start=start, fee_bps=Decimal(scenario["fee"]),
        execution=replace(EXECUTION, execution_lag_sessions=scenario["lag"]), include_portfolio_state=True)
    ledger = validate_allocation(account, data, calendar)
    signal = audit_shadow_account(account, allocator.checks, data, schedule, method, repair=scenario["repair"])
    measured = metrics(account)
    measured["mean_market_exposure_pct"] = float(np.mean([float(row["market_value"]) / float(row["equity"]) for row in account["equity_curve"]]) * 100)
    measured["mean_receivables_pct"] = float(np.mean([float(row["receivables"]) / float(row["equity"]) for row in account["equity_curve"]]) * 100)
    dates = [row["date"] for row in account["equity_curve"]]
    annual = []
    for year in sorted({day[:4] for day in dates}):
        requested = calendar.sessions(date(int(year), 1, 1), date(int(year), 12, 31))
        annual.append({"year": year, "value": carry_slice(account, f"{year}-01-01", f"{year}-12-31"),
                       "partial": dates[0] > requested[0].isoformat() or dates[-1] < requested[-1].isoformat()})
    return {"metrics": measured, "ledger_validation": ledger, "signal_validation": signal,
            "first_scheduled_source": schedule[min(schedule)]["source_date"],
            "episodes": slices(account, episodes, calendar), "annual": annual,
            "account": account, "checks": allocator.checks}


def compare(trials):
    result = {}
    base, low = trials["return_top10"]["metrics"], trials["low_vol_top10"]["metrics"]
    for key, value in trials.items():
        metric = value["metrics"]
        if any(metric[field] != base[field] for field in ("start_date", "end_date")):
            raise ValueError("Shadow account comparison dates differ")
        result[key] = {"return_pct": metric["total_return_pct"], "drawdown_pct": metric["max_drawdown_pct"],
                       "minus_return_ranking_pp": metric["total_return_pct"] - base["total_return_pct"],
                       "minus_low_vol_pp": metric["total_return_pct"] - low["total_return_pct"]}
    return result


def compact(value):
    return {key: entry for key, entry in value.items() if key not in ("account", "checks")}


def run(reference, protocol):
    data, sources = prepare(reference)
    calendar = TradingCalendar(reference["calendar"])
    if calendar.validate_grid(data["days"], end=date.fromisoformat(data["days"][-1]))["status"] != "ready":
        raise ValueError("Sector shadow data grid incomplete")
    episodes = json.loads(reference["source_snapshots"]["config/market-regime-strategy-research-v1.json"])["episodes"]
    results, count = {}, 0
    for period, window in PERIODS.items():
        start = min(sources[period])
        scenarios = {}
        for scenario in protocol["scenarios"]:
            trials = {method: trial(data, sources[period], calendar, window, method, scenario, start=start, episodes=episodes)
                      for method in METHODS}
            count += len(trials)
            scenarios[scenario["id"]] = {"comparisons": compare(trials),
                "policies": trials if scenario["id"] == "fee10_default" else {method: compact(value) for method, value in trials.items()}}
            print(json.dumps({"period": period, "scenario": scenario["id"], "account_runs": count,
                              "maximin": scenarios[scenario["id"]]["comparisons"]["maximin_top10"]}), flush=True)
        fresh = {}
        for day in protocol["fresh_entry_dates"]:
            start = bisect_left(data["days"], day)
            scenario = protocol["scenarios"][0]
            trials = {method: trial(data, sources[period], calendar, window, method, scenario, start=start, episodes=episodes)
                      for method in METHODS}
            count += len(trials)
            fresh[day] = {"actual_start": data["days"][start], "comparisons": compare(trials),
                          "policies": {method: compact(value) for method, value in trials.items()}}
        results[period] = {"review_sessions": window, "main_start": data["days"][min(sources[period])],
                           "scenarios": scenarios, "fresh_entry": fresh}
    if count != 312:
        raise ValueError("Shadow research design did not execute all312accounts")
    return {"periods": results, "formal_account_runs": count, "data": data, "source_schedules": sources}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/sector-shadow-account-v1.json.gz")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Preserve prior sector shadow report")
    protocol = json.loads(PROTOCOL.read_text())
    expected = [(10, 0, 1, True), (0, 0, 1, True), (50, 0, 1, True), (100, 0, 1, True),
                (10, 5, 1, True), (10, 10, 1, True), (10, 15, 1, True), (10, 0, 2, True), (10, 0, 1, False)]
    if (protocol["periods"] != PERIODS or protocol["methods"] != list(METHODS) or protocol["initial_equity"] != 100000
            or [tuple(row[key] for key in ("fee", "phase", "lag", "repair")) for row in protocol["scenarios"]] != expected
            or protocol["scenarios"][0]["id"] != "fee10_default"
            or len({row["id"] for row in protocol["scenarios"]}) != 9
            or protocol["fresh_entry_dates"] != ["2024-07-29", "2025-01-02", "2025-07-01", "2026-01-05"]):
        raise ValueError("Frozen shadow-account design differs")
    path = ROOT / protocol["reference"]
    if sha256(path.read_bytes()).hexdigest() != protocol["reference_sha256"]:
        raise ValueError("Frozen ordinal source report changed")
    with gzip.open(path, "rt") as stream:
        reference = json.load(stream)
    for relative, expected in reference["source_hashes"].items():
        if sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Unrelated ordinal source changed: {relative}")
    paths = set(reference["source_hashes"]) | {"scripts/research_sector_shadow_accounts.py", "scripts/validate_sector_shadow_account.py",
        "backend/analysis/sector_shadow_account.py", "backend/analysis/fund_allocation.py", "backend/analysis/fund_holding_costs.py",
        "backend/analysis/allocation_checkpoint.py", "backend/analysis/strategy_risk.py", "scripts/validate_fund_allocation.py",
        "scripts/validate_allocation_checkpoint.py", "scripts/validate_trend_satellite.py", "scripts/search_fund_allocations.py",
        "scripts/research_market_regimes.py", str(PROTOCOL.relative_to(ROOT))}
    sources = {relative: (ROOT / relative).read_text() for relative in paths}
    result = run(reference, protocol)
    if any((ROOT / relative).read_text() != text for relative, text in sources.items()):
        raise ValueError("Shadow sources changed during research")
    report = {"version": protocol["version"], "generated_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol,
              "input_sha256": reference["input_sha256"], "calendar": reference["calendar"], **result,
              "source_snapshots": sources, "source_hashes": {relative: sha256(text.encode()).hexdigest() for relative, text in sources.items()},
              "investment_eligible": False, "production_ranking_changed": False,
              "limitations": reference["limitations"] + [protocol["scope"],
                  "No new predicted-volatility cap or stop;holding dates are research cadence,not user risk preference",
                  "Terminal receivables are included in wealth but not already received cash",
                  "Earlier market windows remain missing where a fitted account cannot exist;fresh accounts are not carry slices"]}
    with args.output.open("xb") as stream:
        with gzip.open(stream, "wt", encoding="utf-8") as compressed:
            json.dump(report, compressed, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output), "accounts": result["formal_account_runs"]}), flush=True)


if __name__ == "__main__":
    main()
