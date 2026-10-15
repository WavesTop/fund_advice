"""Match sector opportunity lookbacks to their own future evaluation horizons."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.sector_price_validation import label_case, summarize
from backend.core.config import Settings
from backend.core.trading_calendar import get_calendar
from scripts.benchmark_sector_trend import load_snapshot, rank_at

PROTOCOL = ROOT / "config/sector-horizon-research-v1.json"
PERIODS = {"short": 20, "medium": 60, "long": 120}
METHODS = ("return_top10", "trend_top10", "trend_gated", "state_gated_return", "matched_count_return")


def select(entries):
    def top(pool, key, count=10):
        return sorted(pool, key=lambda row: (-row[key], row["code"]))[:count]
    gated = [row for row in entries if row["score"] >= 60 and row["state"] in ("steady_up", "strengthening")]
    return {"return_top10": top(entries, "return_pct"), "trend_top10": top(entries, "score"),
            "trend_gated": top(gated, "score"), "state_gated_return": top(gated, "return_pct"),
            "matched_count_return": top(entries, "return_pct", min(10, len(gated)))}


def evaluate(boards, days, calendar, window, index):
    entries = rank_at(boards, days, index, window)
    chosen = select(entries)
    common = {"signal_date": days[index], "entry_date": days[index + 1] if index + 1 < len(days) else None,
              "exit_date": days[index + 1 + window] if index + 1 + window < len(days) else None,
              "eligible_count": len(entries), "pool_count": len(boards)}
    grid = days[index - window:index + 1]
    valid = calendar.validate_grid(grid, end=date.fromisoformat(days[index]), minimum=window + 1)
    status = "signal_incomplete" if len(entries) != len(boards) or valid["status"] != "ready" else None
    future_days = days[index + 1:index + window + 2]
    prices = [{"code": board["code"], "rows": [{"date": day, "close": board["prices"][day]}
              for day in future_days if day in board["prices"]]} for board in boards]
    cases = {}
    for method, selected in chosen.items():
        row = {**common, "selected_codes": [item["code"] for item in selected]}
        if status:
            row["status"] = status
        elif common["exit_date"] is None:
            row["status"] = "not_matured" if selected else "no_selection"
        else:
            row.update(label_case(prices, row["selected_codes"], future_days))
        cases[method] = row
    return cases


def paired(cases, left, right):
    rows = [(row["methods"][left], row["methods"][right]) for row in cases
            if row["methods"][left]["status"] == row["methods"][right]["status"] == "matured"]
    deltas = [a["selected_return_pct"] - b["selected_return_pct"] for a, b in rows]
    return {"paired_dates": len(rows), "mean_delta_pp": sum(deltas) / len(deltas) if deltas else None,
            "positive_dates": sum(value > 1e-12 for value in deltas),
            "negative_dates": sum(value < -1e-12 for value in deltas),
            "same_selection_dates": sum(set(a["selected_codes"]) == set(b["selected_codes"]) for a, b in rows)}


def summarize_group(cases, costs):
    return {"scheduled_dates": len(cases),
            "methods": {method: summarize([row["methods"][method] for row in cases], costs) for method in METHODS},
            "paired": {name: paired(cases, left, right) for name, left, right in (
                ("trend_vs_return", "trend_top10", "return_top10"),
                ("gated_vs_count_matched_return", "trend_gated", "matched_count_return"),
                ("gated_score_vs_same_gate_return", "trend_gated", "state_gated_return"))}}


def run(boards, days, calendar, episodes, costs):
    result = {}
    for period, window in PERIODS.items():
        cases = []
        for index in range(window, len(days)):
            primary = (index - window) % (window + 1) == 0
            secondary = (index - window) % 5 == 0
            if not primary and not secondary:
                continue
            cases.append({"primary": primary, "secondary": secondary,
                          "methods": evaluate(boards, days, calendar, window, index)})
        primary = [row for row in cases if row["primary"]]
        latest = rank_at(boards, days, len(days) - 1, window, current=True)
        current = select(latest) if len(latest) == len(boards) else {key: [] for key in METHODS}
        result[period] = {"lookback_sessions": window, "forward_sessions": window,
                         "primary": summarize_group(primary, costs),
                         "secondary": summarize_group([row for row in cases if row["secondary"]], costs),
                         "episodes": {}, "cases": cases,
                         "latest": {"as_of": days[-1], "eligible_count": len(latest),
                                    "state_counts": dict(Counter(row["state"] for row in latest)),
                                    "candidate_count": len(current["trend_gated"]),
                                    "candidates": current["trend_gated"]}}
        for episode in episodes:
            overlaps = days[-1] >= episode["start"] and days[0] <= episode["end"]
            requested = calendar.sessions(date.fromisoformat(episode["start"]), date.fromisoformat(episode["end"])) if overlaps else []
            covered = bool(requested) and days[0] <= requested[0].isoformat() and days[-1] >= requested[-1].isoformat()
            contained = [row for row in primary if row["methods"]["return_top10"]["signal_date"] >= episode["start"]
                         and row["methods"]["return_top10"]["exit_date"] is not None
                         and row["methods"]["return_top10"]["exit_date"] <= episode["end"]]
            result[period]["episodes"][episode["id"]] = {
                "calendar_coverage": "complete" if covered else "partial" if overlaps else "insufficient_history",
                "summary": summarize_group(contained, costs)}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Settings.from_env().database_path)
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/sector-horizon-research-v1.json")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Preserve previous matched-horizon report")
    protocol = json.loads(PROTOCOL.read_text())
    if (protocol["periods"] != PERIODS or protocol["methods"] != list(METHODS) or protocol["score_floor"] != 60
            or protocol["top_n"] != 10 or protocol["states"] != ["steady_up", "strengthening"]
            or protocol["entry"] != "next_session_close" or protocol["exit"] != "entry_plus_period_sessions_close"
            or protocol["primary"] != "first_window_then_stride_horizon_plus_one"
            or protocol["secondary"] != "five_session_signal_stride_overlapping_descriptive"
            or protocol["coverage"] != "complete_frozen_pool_at_signal_and_outcome_no_future_filtering"):
        raise ValueError("Protocol differs from implemented rules")
    paths = ["scripts/research_sector_horizons.py", "scripts/benchmark_sector_trend.py",
             "backend/analysis/sector_trend.py", "backend/analysis/sector_price_validation.py",
             "backend/analysis/sector_status.py", "backend/analysis/sector_strength.py",
             "config/sector-horizon-research-v1.json", "config/market-regime-strategy-research-v1.json"]
    sources = {path: (ROOT / path).read_text() for path in paths}
    boards, inventory = load_snapshot(args.database)
    if inventory["price_input_sha256"] != protocol["price_input_sha256"]:
        raise ValueError("Frozen sector input differs")
    calendar = get_calendar()
    first = min(min(board["prices"]) for board in boards)
    last = protocol["as_of"]
    days = [day.isoformat() for day in calendar.sessions(date.fromisoformat(first), date.fromisoformat(last))]
    for board in boards:
        quality = calendar.validate_grid(sorted(board["prices"]), end=date.fromisoformat(last))
        if quality["status"] != "ready":
            raise ValueError(f"Sector full grid invalid: {board['code']}")
    episodes = json.loads(sources["config/market-regime-strategy-research-v1.json"])["episodes"]
    periods = run(boards, days, calendar, episodes, protocol["cost_stress_bps"])
    if any((ROOT / path).read_text() != source for path, source in sources.items()):
        raise ValueError("Matched-horizon source changed during research")
    report = {"version": protocol["version"], "generated_at": datetime.now(timezone.utc).isoformat(),
              "protocol": protocol, "inventory": inventory, "calendar": calendar.document,
              "source_snapshots": sources, "source_hashes": {path: sha256(text.encode()).hexdigest() for path, text in sources.items()},
              "input": {"boards": boards, "days": days}, "periods": periods,
              "parsed_input_sha256": sha256(json.dumps({"boards": boards, "days": days}, sort_keys=True, allow_nan=False).encode()).hexdigest(),
              "investment_eligible": False, "production_ranking_changed": False,
              "limitations": ["Current survivor industries and current historical price vintage, repeatedly studied",
                  "Price baskets, not fund or ETF net returns; no compounding of batches or annualization",
                  "Primary holding intervals do not overlap but dates and industries remain dependent",
                  "Same-gate and count-matched controls are conditional diagnostics, not executable strategy returns",
                  "Insufficient early bull/bear sector history is not replaced with a different fund universe"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output), "inventory": inventory,
                      "primary": {key: {"paired": value["primary"]["paired"],
                          "gated": value["primary"]["methods"]["trend_gated"]} for key, value in periods.items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
