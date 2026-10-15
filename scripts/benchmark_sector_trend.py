"""Read-only local trend experiment with rolling, past-only signal windows."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.sector_trend import (
    METHOD_VERSION, midpoint_percentiles, original_proposal_score, trend_metrics, trend_score,
)
from backend.core.config import Settings
from backend.core.trading_calendar import get_calendar

WINDOWS = (20, 60, 120)
METHODS = ("return", "original_proposal", "trend", "trend_smoothed", "trend_gated")
UP_STATES = {"steady_up", "strengthening"}


def load_snapshot(path: Path) -> tuple[list[dict], dict]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        snapshot = dict(connection.execute("SELECT * FROM sector_heat_snapshot WHERE id=1").fetchone())
        members = [dict(row) for row in connection.execute("SELECT * FROM sector_heat_member ORDER BY code")]
        removed = {(row["source_id"], row["code"]) for row in connection.execute(
            "SELECT source_id,code FROM sector_removed_board WHERE universe_type='hot_board'")}
    excluded, boards, digest = Counter(), [], sha256()
    for member in members:
        reason = None
        if member["kind"] != "行业":
            reason = "non_industry"
        elif (member["source_id"], member["code"]) in removed:
            reason = "removed"
        elif member.get("history_source_id") != member["source_id"] or member["source_id"] != "sector_daily.ths":
            reason = "non_native_history"
        if reason:
            excluded[reason] += 1
            continue
        rows = json.loads(member["rows_json"])
        dates = [row["date"] for row in rows]
        if len(set(dates)) != len(dates):
            excluded["duplicate_dates"] += 1
            continue
        rows.sort(key=lambda row: row["date"])
        # An unfinished quote cannot stand in for a completed daily close.
        provisional = sum(bool(row.get("provisional")) for row in rows)
        rows = [row for row in rows if not row.get("provisional")]
        excluded["provisional_rows"] += provisional
        digest.update(json.dumps({"code": member["code"], "rows": rows}, sort_keys=True).encode())
        boards.append({"code": member["code"], "name": member["name"],
                       "prices": {row["date"]: float(row["close"]) if row["close"] is not None else float("nan") for row in rows},
                       "collection_error": member["collection_error"]})
    return boards, {"snapshot_as_of": snapshot["as_of"], "updated_at": snapshot["updated_at"],
                    "stored_members": len(members), "industry_members": len(boards),
                    "excluded": dict(excluded), "price_input_sha256": digest.hexdigest(),
                    "current_collection_errors": [b["code"] for b in boards if b["collection_error"]]}


def rank_at(boards: list[dict], days: list[str], end: int, window: int, *, current: bool = False) -> list[dict]:
    if end < window:
        return []
    grid = days[end - window:end + 1]
    entries = []
    for board in boards:
        if current and board["collection_error"]:
            continue
        if any(day not in board["prices"] for day in grid):
            continue
        try:
            metrics = trend_metrics([board["prices"][day] for day in grid])
        except ValueError:
            continue
        entries.append({"code": board["code"], "name": board["name"], **metrics})
    percentiles = midpoint_percentiles([e["return_pct"] for e in entries])
    slopes = midpoint_percentiles([e["daily_log_slope"] for e in entries])
    for entry, percentile, slope in zip(entries, percentiles, slopes):
        entry["relative_percentile"] = percentile
        entry["score"] = trend_score(entry, percentile)
        entry["original_score"] = original_proposal_score(entry, percentile or 0, slope or 0)
    return entries


def selections(entries: list[dict]) -> dict[str, list[dict]]:
    def top(key, candidates):
        return sorted(candidates, key=lambda e: (-e[key], e["code"]))[:10]
    return {"return": top("return_pct", entries), "original_proposal": top("original_score", entries),
            "trend": top("score", entries),
            "trend_smoothed": top("smoothed_score", entries),
            "trend_gated": top("score", [e for e in entries if e["score"] >= 60 and e["state"] in UP_STATES])}


def attach_smoothed_scores(entries: list[dict], previous: dict[str, float]) -> dict[str, float]:
    for entry in entries:
        entry["smoothed_score"] = .5 * entry["score"] + .5 * previous.get(entry["code"], entry["score"])
    return {e["code"]: e["smoothed_score"] for e in entries}


def future_metrics(prices: list[float]) -> tuple[float, float]:
    peak, drawdown = prices[0], 0.0
    for price in prices:
        peak = max(peak, price)
        drawdown = min(drawdown, price / peak - 1)
    return (prices[-1] / prices[0] - 1) * 100, drawdown * 100


def summarize(samples: list[dict]) -> dict:
    result = {}
    for window in WINDOWS:
        for forward in (5, 20):
            for split in ("development_2025", "validation_2026_h1", "validation_2026_h2", "all", "nonoverlap_20"):
                group = [s for s in samples if s["window"] == window and s["forward"] == forward
                         and (split == "all" or split == "nonoverlap_20" and s["nonoverlap"] or s["split"] == split)]
                metrics = {}
                for method in METHODS:
                    available = [s["methods"][method] for s in group if s["methods"][method]["count"]]
                    metrics[method] = {"dates": len(available), "possible_dates": len(group)}
                    for key in ("return_pct", "excess_pct", "positive_fraction", "beat_peer_fraction", "drawdown_pct", "count", "weakening_fraction"):
                        metrics[method][key] = round(statistics.fmean(s[key] for s in available), 4) if available else None
                paired = [s for s in group if s["methods"]["trend"]["count"] and s["methods"]["return"]["count"]]
                result[f"{window}/{forward}/{split}"] = {"methods": metrics,
                    "paired_trend_minus_return_pp": round(statistics.fmean(
                        s["methods"]["trend"]["return_pct"] - s["methods"]["return"]["return_pct"] for s in paired), 4) if paired else None}
    return result


def run(path: Path) -> dict:
    started = time.perf_counter()
    calendar = get_calendar()
    boards, inventory = load_snapshot(path)
    first = min(min(b["prices"]) for b in boards if b["prices"])
    stored_last = max(max(b["prices"]) for b in boards if b["prices"])
    completed = calendar.latest_completed(datetime.now(timezone.utc)).isoformat()
    last = min(stored_last, completed)
    days = [d.isoformat() for d in calendar.sessions(date.fromisoformat(first), date.fromisoformat(last))]
    lookup = {b["code"]: b for b in boards}
    coverage = {str(w): len(rank_at(boards, days, len(days) - 1, w, current=True)) for w in WINDOWS}
    full_grids = Counter()
    for board in boards:
        grid = sorted(d for d in board["prices"] if d <= last)
        quality = calendar.validate_grid(grid, end=date.fromisoformat(last))
        full_grids[quality["status"]] += 1
    samples, stability, latest, sensitivity = [], {}, {}, {}
    skipped_dates = 0
    for window in WINDOWS:
        previous = None
        smoothed = {}
        overlaps = {method: [] for method in METHODS}
        for end in range(window, len(days), 5):
            entries = rank_at(boards, days, end, window)
            if len(entries) < 10:
                skipped_dates += 1
                smoothed = {}
                continue
            smoothed = attach_smoothed_scores(entries, smoothed)
            selected = selections(entries)
            sets = {method: {e["code"] for e in selected[method]} for method in METHODS}
            if previous is not None and end - previous[0] == 5:
                for method in METHODS:
                    union = sets[method] | previous[1][method]
                    if union:
                        overlaps[method].append(len(sets[method] & previous[1][method]) / len(union))
            previous = (end, sets)
            for forward in (5, 20):
                if end + forward >= len(days):
                    continue
                future_grid = days[end:end + forward + 1]
                outcomes = {}
                for entry in entries:
                    board = lookup[entry["code"]]
                    if all(day in board["prices"] for day in future_grid):
                        prices = [board["prices"][day] for day in future_grid]
                        if all(p > 0 and p < float("inf") for p in prices):
                            outcomes[entry["code"]] = future_metrics(prices)
                # Skip the whole evaluation date instead of selecting on future availability.
                if len(outcomes) != len(entries):
                    skipped_dates += 1
                    continue
                peer_mean = statistics.fmean(value[0] for value in outcomes.values())
                if days[end + forward] < "2026-01-01":
                    split = "development_2025"
                elif days[end] >= "2026-01-01" and days[end + forward] < "2026-07-01":
                    split = "validation_2026_h1"
                elif days[end] >= "2026-07-01":
                    split = "validation_2026_h2"
                else:
                    split = "boundary_crossing"
                methods = {}
                for method, chosen in selected.items():
                    values = [outcomes[e["code"]] for e in chosen]
                    methods[method] = {"count": len(chosen)}
                    if values:
                        methods[method].update(
                            return_pct=statistics.fmean(v[0] for v in values),
                            excess_pct=statistics.fmean(v[0] - peer_mean for v in values),
                            positive_fraction=statistics.fmean(v[0] > 0 for v in values),
                            beat_peer_fraction=statistics.fmean(v[0] > peer_mean for v in values),
                            drawdown_pct=statistics.fmean(v[1] for v in values),
                            weakening_fraction=statistics.fmean(e["state"] == "weakening" for e in chosen))
                samples.append({"as_of": days[end], "window": window, "forward": forward,
                                "peer_count": len(entries), "peer_return_pct": peer_mean,
                                "split": split, "nonoverlap": (end - window) % 20 == 0, "methods": methods})
        stability[str(window)] = {method: {"comparisons": len(values),
            "mean_top10_jaccard": round(statistics.fmean(values), 4) if values else None} for method, values in overlaps.items()}
        entries = rank_at(boards, days, len(days) - 1, window, current=True)
        last_anchor = window + 5 * ((len(days) - 1 - window) // 5)
        if last_anchor == len(days) - 1:
            for entry in entries:
                entry["smoothed_score"] = smoothed.get(entry["code"], entry["score"])
        else:
            attach_smoothed_scores(entries, smoothed)
        returns = sorted(entries, key=lambda e: (-e["return_pct"], e["code"]))
        trends = sorted(entries, key=lambda e: (-e["score"], e["code"]))
        return_ranks = {e["code"]: i + 1 for i, e in enumerate(returns)}
        trend_ranks = {e["code"]: i + 1 for i, e in enumerate(trends)}
        for entry in entries:
            entry.update(return_rank=return_ranks[entry["code"]], trend_rank=trend_ranks[entry["code"]])
        latest[str(window)] = {"state_counts": dict(Counter(e["state"] for e in entries)),
                              "selections": selections(entries), "all": trends}
        base = {e["code"] for e in trends[:10]}
        sensitivity[str(window)] = {}
        for weight in (10, 20, 30):
            ranked = sorted(entries, key=lambda e: (-trend_score(e, e["relative_percentile"], relative_weight=weight), e["code"]))
            sensitivity[str(window)][str(weight)] = {"common_with_base_top10": len(base & {e["code"] for e in ranked[:10]}),
                                                    "codes": [e["code"] for e in ranked[:10]]}
    return {"method_version": METHOD_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
            "inventory": {**inventory, "first_date": first, "last_completed_date": last,
                          "calendar_version": calendar.version, "calendar_hash": calendar.hash,
                          "session_count": len(days), "full_grid_status": dict(full_grids), "latest_eligible": coverage},
            "protocol": {"stride_sessions": 5, "forward_sessions": [5, 20], "top_n": 10,
                         "score_smoothing": "0.5 current + 0.5 previous smoothed, five-session anchors",
                         "gated_score": 60, "gated_states": sorted(UP_STATES),
                         "parameters_optimized_for_future_returns": False,
                         "limitations": ["Current industry universe, not historical point-in-time membership",
                            "Historical rows use the current data vintage, not first-seen archived snapshots",
                            "All segments inspected during iteration; not a locked out-of-sample validation",
                            "Five-session anchors overlap; nonoverlap_20 is a separate descriptive check",
                            "Forward results are descriptive, without significance claims, fees or execution",
                            "No benchmark, volume, breadth, valuations or fundamentals in this experiment"]},
            "skipped_evaluation_dates": skipped_dates, "summary": summarize(samples),
            "stability": stability, "sensitivity": sensitivity, "latest": latest,
            "rolling_samples": samples, "elapsed_seconds": round(time.perf_counter() - started, 3)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Settings.from_env().database_path)
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/sector-trend-experiment.json")
    args = parser.parse_args()
    report = run(args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "inventory": report["inventory"],
                      "elapsed_seconds": report["elapsed_seconds"], "skipped": report["skipped_evaluation_dates"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
