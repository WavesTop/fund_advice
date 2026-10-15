"""Hypothetical industry-index rotation; no actual fund execution is claimed."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_DOWN, localcontext
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.sector_trend import METHOD_VERSION
from backend.analysis.strategy_portfolio import _benchmark
from backend.analysis.strategy_risk import summarize_curve_risk
from backend.core.config import Settings
from backend.core.trading_calendar import get_calendar
from scripts.benchmark_sector_trend import UP_STATES, WINDOWS, load_snapshot, rank_at

METHODS = ("return", "trend", "gated_slots", "gated_full", "all_industries", "gated_exposure_benchmark")


def signals(boards: list[dict], days: list[str], window: int, start: int, *, offset: int = 0) -> dict[int, list[dict]]:
    return {end + 1: rank_at(boards, days, end, window)
            for end in range(start + offset, len(days) - 1, 20)}


def choose(entries: list[dict], method: str, gate_threshold: float = 60) -> list[dict]:
    if method in ("all_industries", "gated_exposure_benchmark"):
        return sorted(entries, key=lambda e: e["code"])
    if method.startswith("gated"):
        entries = [e for e in entries if e["score"] >= gate_threshold and e["state"] in UP_STATES]
    key = "return_pct" if method == "return" else "score"
    return sorted(entries, key=lambda e: (-e[key], e["code"]))[:10]


def simulate(boards: list[dict], days: list[str], schedule: dict[int, list[dict]], *,
             method: str, start: int, initial: Decimal = Decimal("100000"), fee_bps: Decimal = Decimal("10"),
             gate_threshold: float = 60) -> dict:
    if method not in METHODS or not 0 <= fee_bps < 10000 or initial <= 0:
        raise ValueError("Invalid simulation settings")
    lookup = {board["code"]: board for board in boards}
    events = sorted(schedule)
    if not events or events[0] <= start or events[-1] >= len(days):
        raise ValueError("Signals must execute after the baseline and before the data ends")
    rate = fee_bps / 10000
    equity, total_fees = initial, Decimal(0)
    curve = [{"date": day, "equity": str(initial), "cash": str(initial), "market_value": "0"}
             for day in days[start:events[0]]]
    cycles = []
    with localcontext() as context:
        context.prec = 40
        for i, entry_day in enumerate(events):
            exit_day = events[i + 1] if i + 1 < len(events) else len(days) - 1
            selected = choose(schedule[entry_day], method, gate_threshold)
            slots = 10 if method in ("return", "trend", "gated_slots") else len(selected)
            before = equity
            allocation = before / slots if slots else Decimal(0)
            if method == "gated_exposure_benchmark":
                fraction = Decimal(len(choose(schedule[entry_day], "gated_slots", gate_threshold))) / 10
                allocation = before * fraction / slots if slots else Decimal(0)
                if fraction == 0:
                    selected = []
            allocation = allocation.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            allocations = {e["code"]: allocation for e in selected}
            reserved = before - sum(allocations.values(), Decimal(0))
            rows = {code: [{"date": day, "close": str(lookup[code]["prices"][day])}
                          for day in days[entry_day:exit_day + 1]] for code in allocations}
            if rows:
                holding_curve, buy_fees = _benchmark(rows, allocations, fee_bps)
            else:
                holding_curve, buy_fees = ([{"date": day, "equity": "0", "cash": "0", "market_value": "0"}
                                            for day in days[entry_day:exit_day + 1]], Decimal(0))
            points = []
            for point in holding_curve:
                cash = reserved + Decimal(point["cash"])
                market = Decimal(point["market_value"])
                points.append({"date": point["date"], "equity": str(cash + market),
                               "cash": str(cash), "market_value": str(market)})
            sale_fees = Decimal(points[-1]["market_value"]) * rate
            equity = Decimal(points[-1]["equity"]) - sale_fees
            points[-1].update(equity=str(equity), cash=str(equity), market_value="0")
            if curve and curve[-1]["date"] == points[0]["date"]:
                curve.pop()
            curve.extend(points)
            total_fees += buy_fees + sale_fees
            cycles.append({"signal_date": days[entry_day - 1], "entry_date": days[entry_day],
                           "exit_date": days[exit_day], "sessions_held": exit_day - entry_day,
                           "capital_before": str(before), "capital_after": str(equity),
                           "return_pct": float((equity / before - 1) * 100),
                           "buy_fees": str(buy_fees), "sell_fees": str(sale_fees),
                           "reserved_cash": str(reserved), "selected_count": len(selected),
                           "selected": [{"code": e["code"], "name": e["name"], "score": e["score"],
                                         "state": e["state"], "allocation": str(allocation),
                                         "entry_close": rows[e["code"]][0]["close"]} for e in selected]})
        peak = initial
        for point in curve:
            value = Decimal(point["equity"])
            peak = max(peak, value)
            point.update(return_pct=str((value / initial - 1) * 100), drawdown_pct=str((value / peak - 1) * 100))
        years = []
        for year in sorted({p["date"][:4] for p in curve}):
            indexes = [i for i, p in enumerate(curve) if p["date"].startswith(year)]
            base = curve[max(0, indexes[0] - 1)]
            last = curve[indexes[-1]]
            years.append({"year": year, "base_date": base["date"], "end_date": last["date"],
                          "return_pct": float((Decimal(last["equity"]) / Decimal(base["equity"]) - 1) * 100)})
        invested = [c for c in cycles if c["selected_count"]]
        return {"method": method, "fee_bps_each_side": float(fee_bps), "start_date": curve[0]["date"],
                "first_execution_date": days[events[0]], "end_date": curve[-1]["date"],
                "initial_equity": str(initial), "final_equity": str(equity),
                "total_return_pct": float((equity / initial - 1) * 100), "total_fees": str(total_fees),
                "max_drawdown_pct": float(summarize_curve_risk(curve)["max_drawdown_pct"]),
                "cycles": len(cycles), "invested_cycles": len(invested),
                "profitable_invested_cycles_fraction": statistics.fmean(c["return_pct"] > 0 for c in invested) if invested else None,
                "mean_market_exposure_pct": statistics.fmean(float(Decimal(p["market_value"]) / Decimal(p["equity"]) * 100) for p in curve),
                "year_returns": years, "holding_cycles": cycles, "equity_curve": curve}


def run(path: Path) -> dict:
    boards, inventory = load_snapshot(path)
    calendar = get_calendar()
    first = min(min(b["prices"]) for b in boards)
    last = min(max(max(b["prices"]) for b in boards), calendar.latest_completed(datetime.now(timezone.utc)).isoformat())
    days = [d.isoformat() for d in calendar.sessions(date.fromisoformat(first), date.fromisoformat(last))]
    for board in boards:
        if calendar.validate_grid(sorted(d for d in board["prices"] if d <= last), end=date.fromisoformat(last))["status"] != "ready":
            raise ValueError(f"{board['code']} has a missing or invalid session grid; backtest stopped")
        if any(not 0 < board["prices"][day] < float("inf") for day in days):
            raise ValueError(f"{board['code']} has an invalid price; backtest stopped")
    start = max(WINDOWS)
    results, phase_checks = {}, {}
    for window in WINDOWS:
        schedule = signals(boards, days, window, start)
        results[str(window)] = {str(fee): {method: simulate(boards, days, schedule, method=method, start=start, fee_bps=Decimal(fee))
                                           for method in METHODS} for fee in (0, 10, 50)}
        phase_checks[str(window)] = {}
        for offset in (0, 5, 10, 15):
            schedule = signals(boards, days, window, start, offset=offset)
            phase_checks[str(window)][str(offset)] = {
                method: simulate(boards, days, schedule, method=method, start=start)["total_return_pct"]
                for method in ("return", "trend", "gated_slots", "gated_full")}
    return {"method_version": METHOD_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
            "inventory": inventory, "protocol": {"signal_baseline": days[start], "last_date": last,
                "rebalance_sessions": 20, "execution_lag_sessions": 1, "execution_price": "next_session_close",
                "initial_capital": 100000, "top_n": 10, "gate": "score >= 60 and steady_up/strengthening",
                "gated_slots": "10% per candidate; unused slots stay cash", "gated_full": "fully invest equally in available candidates",
                "rotation": "full liquidation and re-entry every 20 sessions, including overlapping names",
                "final_position": "liquidate at final close; include the last partial holding cycle",
                "cash_interest": 0, "benchmark": "all 86 industries, equally weighted and rotated on the same dates",
                "allocation_rounding": "round each allocation down to cents; residual remains cash",
                "exposure_benchmark": "all industries with the same target invested fraction as gated_slots at each rotation",
                "fee_scenarios_bps_each_side": [0, 10, 50], "phase_offsets_sessions": [0, 5, 10, 15],
                "limitations": ["Hypothetical nontradable industry indices, not actual ETF or fund NAVs",
                    "Price returns only; dividends, tracking differences, taxes and actual product expenses not modelled",
                    "Current universe and data vintage; no historical first-seen snapshots",
                    "Rules developed with this history already inspected; not out-of-sample",
                    "Execution lag avoids same-close signal trading but does not prove realizable fills",
                    "Fees are illustrative assumptions; no intraday slippage or liquidity model"]},
            "results": results, "phase_sensitivity": phase_checks}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Settings.from_env().database_path)
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/sector-trend-investment-backtest.json")
    args = parser.parse_args()
    report = run(args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "protocol": report["protocol"],
        "results": {w: {m: {k: result[k] for k in ("total_return_pct", "max_drawdown_pct", "final_equity", "year_returns", "mean_market_exposure_pct")}
                           for m, result in costs["10"].items()} for w, costs in report["results"].items()},
        "phase_sensitivity": report["phase_sensitivity"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
