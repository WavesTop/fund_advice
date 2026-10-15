"""Expanded NAV cohorts, seven-day fees and short/medium/long rolling accounts."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.fund_holding_costs import FundHoldingCosts
from backend.analysis.sector_rotation import RotationPolicy, simulate_rotation
from backend.analysis.sector_trend import midpoint_percentiles, trend_metrics, trend_score
from backend.core.trading_calendar import TradingCalendar
from scripts.backtest_sector_rotation import DEFAULT_POLICY, load_policy
from scripts.optimize_sector_rotation import source_hashes
from scripts.validate_sector_rotation import validate_ledger

INPUT = ROOT / "runtime/verification/fund-rotation-expanded-20261013/input.json"
CALENDAR = ROOT / "config/research-exchange-calendar-2009-2026-v2.json"
MODES = ("short", "medium", "long")


def load_funds(path: Path, calendar: TradingCalendar) -> tuple[dict, list[dict]]:
    input_data = json.loads(path.read_text(encoding="utf-8"))
    funds, audit = {}, []
    for item in input_data["items"]:
        if item["status"] != "available":
            audit.append({"code": item["code"], "status": "source_failed", "error": item.get("error")})
            continue
        rows = {row["date"]: row for row in item["rows"] if calendar.is_session(date.fromisoformat(row["date"]))}
        expected = [day.isoformat() for day in calendar.sessions(date.fromisoformat(min(rows)), date.fromisoformat(max(rows)))]
        missing = [day for day in expected if day not in rows]
        daily_start = expected[expected.index(missing[-1]) + 1] if missing else expected[0]
        if missing and (date.fromisoformat(missing[-1]) - date.fromisoformat(expected[0])).days > 120:
            audit.append({"code": item["code"], "status": "blocked_internal_gap", "missing": missing})
            continue
        rows = {day: row for day, row in rows.items() if day >= daily_start}
        deltas, prior = {}, None
        for day, row in sorted(rows.items()):
            cumulative_cash = Decimal(row["accumulated_nav"]) - Decimal(row["unit_nav"])
            delta = cumulative_cash - prior if prior is not None else Decimal(0)
            deltas[day] = float(delta) if abs(delta) > Decimal(".0002") else 0.0
            prior = cumulative_cash
        negative = [day for day, value in deltas.items() if value < 0]
        state = ("blocked_legacy_graded_fund_semantics" if item["role"] == "historical_sector"
                 else "blocked_corporate_action_semantics" if negative else "ready")
        record = {"code": item["code"], "theme": item["theme"], "role": item["role"], "name": item["name"],
                  "daily_start": daily_start, "last_date": max(rows), "observations": len(rows),
                  "off_session_observations": len(item["rows"]) - len(expected) + len(missing),
                  "initial_missing_days": len(missing), "status": state,
                  "negative_accumulated_nav_adjustments": len(negative),
                  "implied_distributions": sum(value > 0 for value in deltas.values()),
                  "full_fee_schedule_status": item["fee_schedule_status"]}
        audit.append(record)
        if state == "ready":
            funds[item["code"]] = {**record, "rows": rows, "distributions": deltas}
    return funds, audit


def cohort_frames(funds: dict, codes: list[str], calendar: TradingCalendar) -> dict:
    selected = [funds[code] for code in codes]
    benchmark = funds["000051"]
    first = max(f["daily_start"] for f in selected + [benchmark])
    last = min(f["last_date"] for f in selected + [benchmark])
    days = [day.isoformat() for day in calendar.sessions(date.fromisoformat(first), date.fromisoformat(last))]
    prices, distributions, marks = {}, {}, {}
    for fund in selected:
        code = fund["code"]
        if any(day not in fund["rows"] for day in days):
            raise ValueError(f"{code}: incomplete cohort dates")
        values = [float(fund["rows"][day]["unit_nav"]) for day in days]
        cash = [fund["distributions"][day] for day in days]
        total_return_prices = [values[0]]
        for i in range(1, len(days)):
            total_return_prices.append(total_return_prices[-1] * (values[i] + cash[i]) / values[i - 1])
        prices[code], distributions[code], marks[code] = values, cash, total_return_prices
    benchmark_days = sorted(benchmark["rows"])
    benchmark_prices = [float(benchmark["rows"][day]["unit_nav"]) for day in benchmark_days]
    benchmark_indexes = {day: i for i, day in enumerate(benchmark_days)}
    frames = {}
    for index in range(120, len(days)):
        entries = []
        metrics = {code: {key: trend_metrics(values[index - window:index + 1])
                         for key, window in (("short", 20), ("medium", 60), ("long", 120))} for code, values in marks.items()}
        relative = {key: midpoint_percentiles([metrics[code][key]["return_pct"] for code in codes]) for key in MODES}
        for position, code in enumerate(codes):
            values = marks[code]
            daily = [math.log(values[i] / values[i - 1]) for i in range(index - 19, index + 1)]
            entries.append({"code": code, "price": values[index], "ma20": statistics.fmean(values[index - 19:index + 1]),
                            "ma60": statistics.fmean(values[index - 59:index + 1]),
                            "ma120": statistics.fmean(values[index - 119:index + 1]),
                            "return5_pct": (values[index] / values[index - 5] - 1) * 100,
                            "return15_pct": (values[index] / values[index - 15] - 1) * 100,
                            "return30_pct": (values[index] / values[index - 30] - 1) * 100,
                            "scores": {key: trend_score(metrics[code][key], relative[key][position]) for key in MODES},
                            "short_state": metrics[code]["short"]["state"], "volatility20": statistics.pstdev(daily),
                            "defensive_asset": funds[code]["theme"] == "黄金"})
        benchmark_index = benchmark_indexes[days[index]]
        benchmark_context = {f"benchmark_above_ma{window}": benchmark_prices[benchmark_index] > statistics.fmean(
                             benchmark_prices[benchmark_index - window + 1:benchmark_index + 1]) if benchmark_index >= window - 1 else None
                             for window in (60, 120, 200)}
        frames[index] = {"entries": entries, "context": {
            "breadth_above_ma60": statistics.fmean(entry["price"] > entry["ma60"] for entry in entries),
            **benchmark_context}}
    return {"days": days, "prices": prices, "distributions": distributions, "marks": marks, "frames": frames,
            "codes": codes, "first_date": first, "last_date": last}


def metrics_only(result: dict) -> dict:
    keys = ("policy", "holding_cost_policy", "start_date", "end_date", "total_return_pct", "max_drawdown_pct",
            "final_equity", "total_fees", "trade_count", "short_redemption_sales", "year_returns",
            "mean_market_exposure_pct", "distribution_income", "final_receivables")
    return {key: result[key] for key in keys}


def fixed_cohorts(funds: dict) -> dict[str, list[str]]:
    core = [code for code, fund in funds.items() if fund["role"] == "sector"]
    if len(core) != 13:
        raise ValueError("The fixed 13-theme universe is incomplete; do not shrink the denominator")
    return {"all_13_themes": core,
            "themes_available_2020": [code for code in core if funds[code]["daily_start"] < "2021-01-01"],
            "themes_available_2015": [code for code in core if funds[code]["daily_start"] < "2016-01-01"],
            "earliest_theme_gold": ["000217"], "earliest_benchmark_300": ["000051"],
            "earliest_benchmark_500": ["000008"]}


def run_account(data: dict, calendar: TradingCalendar, policy: RotationPolicy, *, start=120, end=None,
                fee_bps=Decimal(10), offset=0, costs=FundHoldingCosts()) -> dict:
    return simulate_rotation(data["prices"], data["days"], data["frames"], policy=policy, start=start, end=end,
                             fee_bps=fee_bps, offset=offset, holding_costs=costs, calendar=calendar,
                             distributions=data["distributions"], signal_prices=data["marks"])


def rolling_summary(rows: list[dict]) -> dict:
    return {"windows": len(rows), "mean_return_pct": statistics.fmean(r["return_pct"] for r in rows) if rows else None,
            "median_return_pct": statistics.median(r["return_pct"] for r in rows) if rows else None,
            "positive_fraction": statistics.fmean(r["return_pct"] > 1e-8 for r in rows) if rows else None,
            "zero_fraction": statistics.fmean(abs(r["return_pct"]) <= 1e-8 for r in rows) if rows else None,
            "worst_return_pct": min((r["return_pct"] for r in rows), default=None)}


def run(path: Path) -> dict:
    calendar = TradingCalendar(json.loads(CALENDAR.read_text(encoding="utf-8")))
    funds, audit = load_funds(path, calendar)
    cohorts = fixed_cohorts(funds)
    frozen = load_policy(DEFAULT_POLICY)
    policies = {"frozen_v1_with_fee": frozen,
                "soft_exit_7d": replace(frozen, soft_exit_min_calendar_days=7),
                "soft_exit_14d": replace(frozen, soft_exit_min_calendar_days=14),
                "review_10d": replace(frozen, soft_exit_min_calendar_days=7, review_sessions=10),
                "gold_defensive_7d": replace(frozen, soft_exit_min_calendar_days=7, allow_defensive_assets=True)}
    policies.update({
        "cost_aware_slow_exit": replace(frozen, soft_exit_min_calendar_days=14, allow_defensive_assets=True,
                                         entry_ma_sessions=60, exit_ma_sessions=60, recent_return_sessions=15,
                                         benchmark_ma_sessions=120, review_sessions=10, trailing_stop=.15),
        "cost_aware_200d_regime": replace(frozen, soft_exit_min_calendar_days=14, allow_defensive_assets=True,
                                           entry_ma_sessions=60, exit_ma_sessions=60, recent_return_sessions=15,
                                           benchmark_ma_sessions=200, review_sessions=10, trailing_stop=.15),
        "cost_aware_long_exit": replace(frozen, soft_exit_min_calendar_days=30, allow_defensive_assets=True,
                                         entry_ma_sessions=60, exit_ma_sessions=120, recent_return_sessions=30,
                                         benchmark_ma_sessions=200, review_sessions=20, trailing_stop=.20)})
    policies.update({
        "long_entry_55": replace(policies["cost_aware_long_exit"], entry_score=55),
        "long_review_10d": replace(policies["cost_aware_long_exit"], review_sessions=10),
        "long_entry55_review10": replace(policies["cost_aware_long_exit"], entry_score=55, review_sessions=10)})
    results, rolling, validation, rolling_by_plan = {}, {}, {}, {}
    for name, codes in cohorts.items():
        if not codes or any(code not in funds for code in codes):
            raise ValueError(f"Frozen cohort is not available: {name}")
        data = cohort_frames(funds, codes, calendar)
        print(json.dumps({"cohort": name, "funds": len(codes), "first": data["first_date"], "last": data["last_date"], "sessions": len(data["days"])}, ensure_ascii=False), flush=True)
        results[name] = {"codes": codes, "fund_count": len(codes), "first_date": data["first_date"], "last_date": data["last_date"], "plans": {}}
        for plan_name, prototype in policies.items():
            plans = {}
            for mode in ("combined", *MODES):
                policy = replace(prototype, score_mode=mode, max_positions=min(prototype.max_positions, len(codes)))
                account = run_account(data, calendar, policy)
                plans[mode] = metrics_only(account)
                validation[f"{name}/{plan_name}/{mode}"] = validate_ledger(account, data["days"], data["prices"], calendar=calendar, distributions=data["distributions"])
            results[name]["plans"][plan_name] = plans
        rolling[name] = {}
        rolling_by_plan[name] = {}
        for mode, horizon in (("short", 20), ("medium", 60), ("long", 120)):
            policy = replace(policies["gold_defensive_7d"], score_mode=mode, max_positions=min(10, len(codes)))
            rows = []
            for start in range(120, len(data["days"]) - horizon, horizon):
                account = run_account(data, calendar, policy, start=start, end=start + horizon)
                validation[f"{name}/legacy_rolling/{mode}/{start}"] = validate_ledger(
                    account, data["days"], data["prices"], calendar=calendar, distributions=data["distributions"])
                rows.append({"start_date": account["start_date"], "end_date": account["end_date"],
                             "return_pct": account["total_return_pct"], "drawdown_pct": account["max_drawdown_pct"],
                             "fees": account["total_fees"], "short_sales": account["short_redemption_sales"]})
            rolling[name][mode] = {"summary": rolling_summary(rows), "windows": rows}
        for plan_name in ("review_10d", "cost_aware_slow_exit", "cost_aware_200d_regime",
                          "cost_aware_long_exit", "long_entry_55", "long_review_10d", "long_entry55_review10"):
            rolling_by_plan[name][plan_name] = {}
            for horizon_name, horizon in (("short", 20), ("medium", 60), ("long", 120)):
                by_mode = {}
                for mode in ("combined", horizon_name):
                    policy = replace(policies[plan_name], score_mode=mode, max_positions=min(10, len(codes)),
                                     entry_min_days_to_liquidation=7)
                    rows = []
                    for start in range(120, len(data["days"]) - horizon, horizon):
                        account = run_account(data, calendar, policy, start=start, end=start + horizon)
                        validation[f"{name}/{plan_name}/rolling/{horizon_name}/{mode}/{start}"] = validate_ledger(
                            account, data["days"], data["prices"], calendar=calendar, distributions=data["distributions"])
                        rows.append({"start_date": account["start_date"], "end_date": account["end_date"],
                                     "return_pct": account["total_return_pct"], "drawdown_pct": account["max_drawdown_pct"],
                                     "fees": account["total_fees"], "short_sales": account["short_redemption_sales"]})
                    by_mode[mode] = {"summary": rolling_summary(rows), "windows": rows}
                rolling_by_plan[name][plan_name][horizon_name] = by_mode
        # Detailed ledgers for the representative expanded pool, including fee and timing stress.
        if name == "all_13_themes":
            chosen = replace(policies["gold_defensive_7d"], max_positions=min(10, len(codes)))
            detailed = {f"cost_{fee}": run_account(data, calendar, chosen, fee_bps=Decimal(fee)) for fee in (10, 50, 100)}
            detailed.update({f"phase_{phase}": run_account(data, calendar, chosen, offset=phase) for phase in range(5)})
            results[name]["detailed_stress"] = detailed
            for key, account in detailed.items():
                validation[f"{name}/{key}"] = validate_ledger(account, data["days"], data["prices"], calendar=calendar, distributions=data["distributions"])
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "input_sha256": sha256(path.read_bytes()).hexdigest(),
            "calendar_version": calendar.version, "calendar_sha256": sha256(CALENDAR.read_bytes()).hexdigest(),
            "source_hashes": {**source_hashes(), "scripts/research_fund_rotation.py": sha256(Path(__file__).read_bytes()).hexdigest()},
            "data_audit": audit, "cohorts": results, "rolling": rolling, "rolling_by_plan": rolling_by_plan,
            "ledger_validation": validation,
            "protocol": {"initial_equity": 100000, "short_fee_bps": 150, "short_holding_days": 7,
                         "confirmation_lag_sessions": 1, "redemption_settlement_lag_sessions": 2,
                         "rolling_horizons_sessions": [20, 60, 120], "rolling_stride": "equal to horizon; no overlapping holding windows",
                         "position_limit": "min(10, available fixed cohort size)",
                         "rolling_new_entry_guard": "only enter if at least 7 confirmed calendar days remain until the known investment deadline",
                         "deadline_scope": "used only for declared rolling finite-horizon accounts, not continuous-history plans",
                         "distribution_model": "positive cumulative-NAV minus unit-NAV differences; no inference across negative corporate-action changes",
                         "volume_signal": "not available for NAV and not fabricated",
                         "limitations": ["Current-survivor themes; historic fund subscription availability and publication times unknown",
                                         "1.5% floor retrospectively applied to all years as requested stress, not historical law reconstruction",
                                         "Ordinary buy/sell cost is a scenario, not verified complete product fee schedules",
                                         "Cash distributions inferred from NAV pairs; actual payment dates not independently verified",
                                         "The original index policy remains frozen; improved fund profiles were selected after inspecting expanded results",
                                         "No untouched holdout remains: all improved-profile comparisons are retrospective, not forward performance"]}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=INPUT)
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/fund-rotation-fee-validation.json")
    args = parser.parse_args()
    report = run(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "rolling": {key: {m: v["summary"] for m, v in modes.items()} for key, modes in report["rolling"].items()}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
