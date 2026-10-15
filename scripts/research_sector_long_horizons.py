"""Recheck archived sector histories before the fixed three-horizon evaluation."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.trading_calendar import TradingCalendar
from scripts.collect_sector_validation_history import parse_annual
from scripts.research_sector_horizons import PERIODS, run

PROTOCOL = ROOT / "config/sector-horizon-long-v1.json"


def checked_annual(directory, record, code, *, calendar, as_of):
    year = record["year"]
    expected = f"raw/{code}/{year}.js"
    if record["url"] != f"https://d.10jqka.com.cn/v4/line/bk_{code}/01/{year}.js":
        raise ValueError("Annual archive URL differs from original sector identity")
    if record["raw_path"] is None:
        if record["status"] == "available":
            raise ValueError("Available annual response has no archive")
        return []
    if record["raw_path"] != expected:
        raise ValueError("Annual archive path differs")
    raw = (directory / expected).read_bytes()
    if sha256(raw).hexdigest() != record["sha256"] or len(raw) != record["bytes"]:
        raise ValueError("Annual response bytes changed")
    if record["status"] != "available":
        return []
    if record["http_status"] != 200:
        raise ValueError("Available annual response lacks HTTP200")
    rows = parse_annual(raw, code=code, year=year, as_of=as_of, calendar=calendar)
    if len(rows) != record["row_count"] or rows[0]["date"] != record["start"] or rows[-1]["date"] != record["end"]:
        raise ValueError("Annual response coverage metadata differs")
    return rows


def reconstruct_board(original, annual):
    prices = dict(original["prices"])
    overlap = {day: price for day, price in prices.items() if day.startswith("2025-")}
    fetched = {row["date"]: row["close"] for row in annual.get(2025, [])}
    if not overlap or any(day not in fetched or Decimal(str(value)) != Decimal(fetched[day]) for day, value in overlap.items()):
        return prices
    for year in reversed(range(2017, 2025)):
        if not annual.get(year):
            break
        prices.update({row["date"]: float(row["close"]) for row in annual[year]})
    return prices


def verify_collection(directory, base):
    metadata = json.loads((directory / "collection.json").read_text())
    snapshot = json.loads((directory / "input.json").read_text())
    actual = sha256(json.dumps(snapshot, sort_keys=True, allow_nan=False).encode()).hexdigest()
    if actual != metadata["input_sha256"]:
        raise ValueError("Published extended prices changed")
    original = base["input"]["boards"]
    codes = [board["code"] for board in original]
    if ([board["code"] for board in snapshot["boards"]] != codes
            or [board["code"] for board in metadata["boards"]] != codes or len(codes) != 86):
        raise ValueError("Extended collection dropped or reordered frozen sectors")
    calendar = TradingCalendar(metadata["calendar"])
    as_of = date.fromisoformat(base["protocol"]["as_of"])
    checked, count = 0, 0
    for board, published, record in zip(original, snapshot["boards"], metadata["boards"]):
        requests = record["requests"]
        if [item["year"] for item in requests] != list(range(2017, 2026)):
            raise ValueError("Annual archive does not preserve every scheduled year")
        annual = {item["year"]: checked_annual(directory, item, board["code"], calendar=calendar, as_of=as_of)
                  for item in requests}
        expected = reconstruct_board(board, annual)
        if expected != published["prices"] or any(published[key] != board[key] for key in board if key != "prices"):
            raise ValueError("Published sector history differs from raw archive reconstruction")
        if calendar.validate_grid(sorted(expected), end=as_of)["status"] != "ready":
            raise ValueError("Extended sector history has a gap")
        checked += sum(bool(rows) for rows in annual.values())
        count += len(board["prices"])
    first = min(min(board["prices"]) for board in snapshot["boards"])
    days = [day.isoformat() for day in calendar.sessions(date.fromisoformat(first), as_of)]
    if days != snapshot["days"] or checked != metadata["valid_annual_responses"]:
        raise ValueError("Extended collection counts or date axis differ")
    return snapshot, calendar, {"status": "passed", "reconstructed_boards": len(codes),
                                 "validated_annual_archives": checked, "original_prices_preserved": count}


def attach_pool_coverage(periods, inputs, calendar, episodes):
    common_start = max(min(board["prices"]) for board in inputs["boards"])
    days = inputs["days"]
    first_common = days.index(common_start)

    def coverage(first, requested):
        if first is None or not requested or first > requested[-1] or days[-1] < requested[0]:
            return "insufficient_history"
        return "complete" if first <= requested[0] and days[-1] >= requested[-1] else "partial"

    for period, window in PERIODS.items():
        first_signal = days[first_common + window] if first_common + window < len(days) else None
        for episode in episodes:
            requested = [day.isoformat() for day in calendar.sessions(
                date.fromisoformat(episode["start"]), date.fromisoformat(episode["end"]))]
            row = periods[period]["episodes"][episode["id"]]
            row["calendar_axis_coverage"] = row["calendar_coverage"]
            row["calendar_coverage"] = coverage(common_start, requested)
            row["full_pool_signal_coverage"] = coverage(first_signal, requested)
            row["first_possible_full_pool_signal"] = first_signal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path, default=PROTOCOL)
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/sector-horizon-long-v1.json.gz")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Preserve previous long-sector report")
    protocol_path = args.protocol.resolve()
    protocol = json.loads(protocol_path.read_text())
    base_path = ROOT / protocol["base_report"]
    if sha256(base_path.read_bytes()).hexdigest() != protocol["base_report_sha256"] or protocol["periods"] != PERIODS:
        raise ValueError("Frozen matched-horizon design differs")
    base = json.loads(base_path.read_text())
    for path, expected in base["source_hashes"].items():
        if sha256((ROOT / path).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Existing horizon algorithm changed: {path}")
    directory = ROOT / protocol["collection"]
    if protocol.get("collection_manifest_sha256") and sha256((directory / "collection.json").read_bytes()).hexdigest() != protocol["collection_manifest_sha256"]:
        raise ValueError("Pinned recovered collection changed")
    manifest = json.loads((directory / "collection.json").read_text())
    archived_protocol = manifest["source_snapshots"]["config/sector-long-history-v1.json"]
    if sha256(archived_protocol.encode()).hexdigest() != protocol["collection_protocol_sha256"]:
        raise ValueError("Sector collection protocol differs")
    for path, text in manifest["source_snapshots"].items():
        if sha256(text.encode()).hexdigest() != manifest["source_hashes"][path]:
            raise ValueError("Collection source archive changed")
    paths = set(base["source_hashes"]) | {"scripts/research_sector_long_horizons.py", str(protocol_path.relative_to(ROOT)),
                                          "backend/core/trading_calendar.py"}
    sources = {path: (ROOT / path).read_text() for path in paths}
    inputs, calendar, validation = verify_collection(directory, base)
    episodes = json.loads(sources["config/market-regime-strategy-research-v1.json"])["episodes"]
    costs = base["protocol"]["cost_stress_bps"]
    compatibility = run(base["input"]["boards"], base["input"]["days"], TradingCalendar(base["calendar"]), episodes, costs)
    if compatibility != base["periods"]:
        raise ValueError("Existing matched-horizon report changed")
    periods = run(inputs["boards"], inputs["days"], calendar, episodes, costs)
    attach_pool_coverage(periods, inputs, calendar, episodes)
    if any((ROOT / path).read_text() != source for path, source in sources.items()):
        raise ValueError("Long-sector analysis source changed during run")
    report = {"version": protocol["version"], "generated_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol,
              "collection_sha256": sha256((directory / "collection.json").read_bytes()).hexdigest(),
              "input_sha256": manifest["input_sha256"], "input": inputs, "calendar": calendar.document,
              "source_snapshots": sources, "source_hashes": {path: sha256(text.encode()).hexdigest() for path, text in sources.items()},
              "archive_validation": validation, "old_report_compatibility": "all_periods_cases_metrics_exact",
              "coverage": {key: manifest[key] for key in ("pool_count", "extended_boards", "full_pool_common_start", "as_of", "coverage_by_year")},
              "periods": periods, "investment_eligible": False, "production_ranking_changed": False,
              "limitations": base["limitations"] + [protocol["interpretation"], protocol["failed_history"]]}
    with args.output.open("xb") as stream:
        with gzip.open(stream, "wt", encoding="utf-8") as compressed:
            json.dump(report, compressed, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output), "archive_validation": validation, "coverage": report["coverage"],
                      "primary": {key: value["primary"]["paired"] for key, value in periods.items()}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
