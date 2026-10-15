"""Compare parameters, unused market data, and flexible rotation rules locally."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.sector_rotation import RotationPolicy, VERSION, simulate_rotation
from backend.analysis.sector_trend import trend_score
from backend.core.config import Settings
from backend.core.trading_calendar import get_calendar
from scripts.backtest_sector_trend import signals, simulate
from scripts.benchmark_sector_trend import load_snapshot, rank_at


def prepare(path: Path) -> tuple[list[dict], list[str], dict, dict]:
    boards, inventory = load_snapshot(path)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        updated = connection.execute("SELECT updated_at FROM sector_heat_snapshot WHERE id=1").fetchone()[0]
        if updated != inventory["updated_at"]:
            raise ValueError("Sector snapshot changed during loading")
        volumes = {r["code"]: {p["date"]: float(p["volume"]) for p in json.loads(r["rows_json"])}
                   for r in connection.execute("SELECT code,rows_json FROM sector_heat_member WHERE kind='行业'")}
        benchmark = {r["date"]: float(r["close"]) for r in connection.execute(
            "SELECT date,close FROM market_index_daily WHERE index_code='000300' ORDER BY date")}
        fundamental = [dict(r) for r in connection.execute(
            "SELECT subject_key,min(observed_at) first_seen,count(*) observations FROM sector_fundamental_observation GROUP BY subject_key")]
    calendar = get_calendar()
    first = max(min(min(b["prices"]) for b in boards), "2025-01-02")
    last = min(max(benchmark), min(max(b["prices"]) for b in boards),
               calendar.latest_completed(datetime.now(timezone.utc)).isoformat())
    days = [d.isoformat() for d in calendar.sessions(date.fromisoformat(first), date.fromisoformat(last))]
    prices = {}
    for board in boards:
        if any(day not in board["prices"] or day not in volumes[board["code"]] for day in days):
            raise ValueError(f"{board['code']}: incomplete price/volume grid")
        prices[board["code"]] = [board["prices"][day] for day in days]
        if any(not 0 < value < float('inf') for value in prices[board["code"]]):
            raise ValueError(f"{board['code']}: invalid closing prices")
    if any(day not in benchmark for day in days):
        raise ValueError("HS300 does not share the industry observation grid")
    frames = {}
    for end in range(120, len(days)):
        ranked = {key: {entry["code"]: entry for entry in rank_at(boards, days, end, window)}
                  for key, window in (("short", 20), ("medium", 60), ("long", 120))}
        entries = []
        for board in boards:
            code = board["code"]
            p = prices[code]
            if any(code not in ranked[key] for key in ranked):
                raise ValueError(f"{code}: invalid trend feature window")
            vol = [volumes[code][day] for day in days[end - 19:end + 1]]
            if any(not 0 < value < float('inf') for value in vol):
                raise ValueError(f"{code}: invalid volume")
            daily = [math.log(p[i] / p[i - 1]) for i in range(end - 19, end + 1)]
            entries.append({"code": code, "name": board["name"], "price": p[end],
                            "ma20": statistics.fmean(p[end - 19:end + 1]),
                            "ma60": statistics.fmean(p[end - 59:end + 1]),
                            "return5_pct": (p[end] / p[end - 5] - 1) * 100,
                            "volatility20": statistics.pstdev(daily),
                            "volume_ratio5_20": statistics.fmean(vol[-5:]) / statistics.fmean(vol),
                            "up_volume_ratio20": sum(v for v, r in zip(vol, daily) if r > 0) / sum(vol),
                            "scores": {key: ranked[key][code]["score"] for key in ranked},
                            "short_state": ranked["short"][code]["state"],
                            "period_metrics": {key: ranked[key][code] for key in ranked}})
        frames[end] = {"entries": entries, "context": {
            "breadth_above_ma60": statistics.fmean(e["price"] > e["ma60"] for e in entries),
            "benchmark_above_ma60": benchmark[days[end]] > statistics.fmean(benchmark[day] for day in days[end - 59:end + 1])}}
    audit = {**inventory, "common_first_date": first, "common_last_date": last,
             "benchmark_code": "000300", "benchmark_last_date": max(benchmark),
             "benchmark_input_sha256": sha256(json.dumps({day: benchmark[day] for day in days}, sort_keys=True).encode()).hexdigest(),
             "calendar_version": calendar.version, "calendar_hash": calendar.hash,
             "volume_positive_points": sum(len(days) for _ in boards),
             "fundamental_first_seen": min(r["first_seen"] for r in fundamental) if fundamental else None,
             "fundamental_subject_sources": dict(Counter(r["subject_key"].split(':')[1] for r in fundamental)),
             "fundamental_historical_usage": "Excluded: archived observations start after most simulation dates",
             "known_missing": ["point-in-time financial/valuation observations covering 2025-2026H1",
                               "complete industry-to-tradable-fund mapping and actual executions",
                               "constituent breadth/flow with historical publication times"]}
    return boards, days, frames, {"prices": prices, "benchmark_prices": {day: benchmark[day] for day in days}, "audit": audit}


def compact(result: dict) -> dict:
    return {key: value for key, value in result.items() if key not in ("equity_curve", "trades", "decisions", "holding_cycles")}


def source_hashes() -> dict[str, str]:
    files = ["backend/analysis/sector_rotation.py", "backend/analysis/sector_trend.py",
             "backend/analysis/fund_holding_costs.py",
             "backend/analysis/simulation.py", "backend/analysis/strategy_portfolio.py",
             "backend/analysis/strategy_risk.py", "scripts/optimize_sector_rotation.py",
             "scripts/backtest_sector_trend.py", "scripts/benchmark_sector_trend.py",
             "scripts/backtest_sector_rotation.py", "scripts/validate_sector_rotation.py",
             "config/sector-rotation-experiment-v1.json"]
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in files}


def policies() -> dict[str, RotationPolicy]:
    base = RotationPolicy()
    return {"retention_only": replace(base, review_sessions=20, daily_exits=False, ma_exit=False, trailing_stop=0),
            "daily_score_exit": replace(base, review_sessions=20, ma_exit=False, trailing_stop=0),
            "daily_exit": replace(base, review_sessions=20, trailing_stop=0),
            "five_day_review": replace(base, trailing_stop=0),
            "trailing_stop": base,
            "breadth_regime": replace(base, regime="breadth"),
            "benchmark_regime": replace(base, regime="benchmark"),
            "both_regimes": replace(base, regime="both"),
            "volume_confirmation": replace(base, volume_gate=True),
            "volatility_sizing": replace(base, volatility_sizing=True),
            "complete": replace(base, regime="benchmark", volume_gate=True, volatility_sizing=True),
            "confirmed_exits": replace(base, exit_confirm_sessions=2),
            "benchmark_exit": replace(base, regime="benchmark", regime_exit=True),
            "breadth_exit": replace(base, regime="breadth", regime_exit=True),
            "confirmed_entry_state": replace(base, entry_state_gate=True),
            "confirmed_state_risk_sizing": replace(base, entry_state_gate=True, volatility_sizing=True),
            "higher_exit_score": replace(base, exit_score=55)}


def run(path: Path) -> dict:
    boards, days, frames, data = prepare(path)
    prices, start = data["prices"], 120
    training_end = max(i for i, day in enumerate(days) if day < "2026-01-01")
    all_policies = policies()
    for positions in (5, 8, 10):
        for stop in (.08, .10, .12):
            if positions == 10 and stop == .10:
                continue
            all_policies[f"risk_grid_{positions}_{int(stop * 100)}"] = replace(
                all_policies["benchmark_exit"], max_positions=positions, trailing_stop=stop)
    # The limited grid is declared before evaluating later-period returns.
    for entry in (55, 60, 65):
        for mode in ("short", "combined", "long"):
            for review in (5, 10):
                key = f"grid_{mode}_{entry}_{review}"
                all_policies[key] = RotationPolicy(score_mode=mode, entry_score=entry, review_sessions=review,
                                                   regime="benchmark", volatility_sizing=True)
    training = {name: compact(simulate_rotation(prices, days, frames, policy=policy, start=start, end=training_end))
                for name, policy in all_policies.items()}
    qualified = [name for name, policy in all_policies.items()
                 if policy.daily_exits and policy.review_sessions <= 10 and policy.trailing_stop > 0]
    ordered = sorted(qualified, key=lambda name: (-(training[name]["total_return_pct"]
                     + .5 * training[name]["max_drawdown_pct"]), name))
    training_selected_name = ordered[0]
    ablations = {name: compact(simulate_rotation(prices, days, frames, policy=policy, start=start))
                 for name, policy in all_policies.items()}
    validation_start = max(i for i, day in enumerate(days) if day < "2026-01-01")
    candidate_validation = {name: compact(simulate_rotation(prices, days, frames, policy=policy, start=validation_start))
                            for name, policy in all_policies.items()}
    retrospective_qualified = [name for name in qualified
                               if training[name]["total_return_pct"] > 0
                               and candidate_validation[name]["total_return_pct"] > 0
                               and ablations[name]["total_return_pct"] > 0
                               and ablations[name]["max_drawdown_pct"] >= -15]
    if not retrospective_qualified:
        raise ValueError("No positive-return candidate passes the declared historical checks")
    selected_name = max(retrospective_qualified, key=lambda name: (ablations[name]["total_return_pct"], name))
    selected = all_policies[selected_name]
    baselines, parameter_only = {}, {}
    benchmark_board = {"code": "000300", "name": "沪深300", "prices": data["benchmark_prices"]}
    buy_hold_benchmark = compact(simulate([benchmark_board], days,
        {start + 1: [{"code": "000300", "name": "沪深300", "score": 0, "state": "benchmark"}]},
        method="all_industries", start=start))
    for key, window in (("short", 20), ("medium", 60), ("long", 120)):
        schedule = signals(boards, days, window, start)
        baselines[key] = {method: compact(simulate(boards, days, schedule, method=method, start=start))
                          for method in ("return", "trend", "gated_slots", "all_industries")}
        for weight in (10, 20, 30):
            for threshold in (55, 60, 65):
                changed = {index: [{**e, "score": trend_score(e, e["relative_percentile"], relative_weight=weight)
                                    } for e in entries] for index, entries in schedule.items()}
                name = f"{key}_relative{weight}_entry{threshold}"
                parameter_only[name] = compact(simulate(boards, days, changed, method="gated_slots", start=start, gate_threshold=threshold))
    selected_runs = {str(fee): simulate_rotation(prices, days, frames, policy=selected, start=start, fee_bps=Decimal(fee)) for fee in (0, 10, 50)}
    validation = simulate_rotation(prices, days, frames, policy=selected, start=validation_start)
    phases = {str(offset): simulate_rotation(prices, days, frames, policy=selected, start=start, offset=offset)
              for offset in (0, 1, 2, 3, 4)}
    half_end = max(i for i, day in enumerate(days) if day < "2026-07-01")
    validation_segments = {
        "2026_h1": simulate_rotation(prices, days, frames, policy=selected, start=validation_start, end=half_end),
        "2026_h2": simulate_rotation(prices, days, frames, policy=selected, start=half_end)}
    return {"version": VERSION, "generated_at": datetime.now(timezone.utc).isoformat(), "data_audit": data["audit"],
            "source_hashes": source_hashes(),
            "protocol": {"initial_equity": 100000, "baseline_date": days[start], "end_date": days[-1],
                         "training_end": days[training_end], "training_selection_rule": "max(training_return_pct + 0.5 * training_max_drawdown_pct)",
                         "selection_rule": "retrospective max full-history return, positive training and 2026 fresh account, full-history drawdown <= 15%",
                         "candidate_count": len(all_policies), "parameter_only_count": len(parameter_only),
                         "next_session_close_execution": True,
                         "selection_constraints": "daily exit checks, review at most 10 sessions, trailing stop enabled",
                         "constraint_origin": "added after first-pass diagnostic; first-pass train winner failed later periods",
                         "same_day_stop_fills": False, "cash_interest": 0,
                         "limitations": ["Retrospective current universe and data vintage; not executable ETF returns",
                                         "Entire history already inspected in earlier experiments; later-period checks are not pristine out-of-sample",
                                         "Recommended candidate selected using historical validation; historical gains are selection-biased",
                                         "The earlier training-only winner is retained separately and is not replaced in the evidence"]},
            "training": training, "selected_name": selected_name, "selected_policy": asdict(selected),
            "training_selected_name": training_selected_name,
            "training_selected_validation": candidate_validation[training_selected_name],
            "ablations": ablations, "candidate_validation": candidate_validation,
            "baselines": baselines, "buy_hold_benchmark": buy_hold_benchmark, "parameter_only": parameter_only,
            "selected_runs": selected_runs, "validation_fresh_account": validation,
            "validation_segments": validation_segments, "phase_sensitivity": phases}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Settings.from_env().database_path)
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/sector-rotation-optimization.json")
    args = parser.parse_args()
    report = run(args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    keys = ("total_return_pct", "max_drawdown_pct", "final_equity", "total_fees", "trade_count")
    print(json.dumps({"output": str(args.output.resolve()), "selected": report["selected_name"],
                      "policy": report["selected_policy"], "selected_runs": {k: {key: v[key] for key in keys} for k, v in report["selected_runs"].items()},
                      "validation": {key: report["validation_fresh_account"][key] for key in keys}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
