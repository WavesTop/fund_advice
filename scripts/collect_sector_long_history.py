"""Extend the exact opportunity-sector pool without changing production data."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.trading_calendar import TradingCalendar
from scripts.collect_sector_validation_history import collect_year, parse_annual, RequestPacer

PROTOCOL = ROOT / "config/sector-long-history-v1.json"


def extend(board, annual, *, calendar, as_of):
    original = dict(board["prices"])
    overlap = {day: value for day, value in original.items() if day.startswith("2025-")}
    fetched = {row["date"]: row["close"] for row in annual.get(2025, [])}
    if not overlap or any(day not in fetched or Decimal(str(value)) != Decimal(fetched[day]) for day, value in overlap.items()):
        return original, {"status": "retained_original", "error": "2025 overlap absent or changed", "overlap_checked": 0}
    prices, years = dict(original), []
    for year in range(2024, 2016, -1):
        rows = annual.get(year, [])
        expected = [day.isoformat() for day in calendar.sessions(date(year, 1, 1), date(year, 12, 31))]
        if not rows or [row["date"] for row in rows] != expected:
            break
        prices.update({row["date"]: float(row["close"]) for row in rows})
        years.append(year)
    grid = sorted(prices)
    if calendar.validate_grid(grid, end=as_of)["status"] != "ready":
        raise ValueError("Extended sector dates are not contiguous")
    return prices, {"status": "extended" if years else "retained_original", "error": None,
                    "overlap_checked": len(overlap), "prepended_years": sorted(years),
                    "first_date": grid[0], "rows": len(grid)}


def collect(output):
    protocol = json.loads(PROTOCOL.read_text())
    if (protocol["years"] != list(range(2017, 2026)) or protocol["pool_count"] != 86
            or protocol["overlap_year"] != 2025 or protocol["workers"] != 2
            or protocol["minimum_interval_seconds"] != .5 or protocol["timeout_seconds"] != 15
            or protocol["attempts_per_url"] != 1):
        raise ValueError("Extended sector protocol differs from implemented design")
    base_path = ROOT / protocol["base_report"]
    if sha256(base_path.read_bytes()).hexdigest() != protocol["base_report_sha256"]:
        raise ValueError("Frozen sector base changed")
    base = json.loads(base_path.read_text())
    boards = base["input"]["boards"]
    if len(boards) != 86 or len({board["code"] for board in boards}) != 86:
        raise ValueError("Frozen86sector pool differs")
    as_of = date.fromisoformat(base["protocol"]["as_of"])
    paths = ["scripts/collect_sector_long_history.py", "scripts/collect_sector_validation_history.py",
             "backend/core/trading_calendar.py", "backend/integrations/http_transport.py",
             str(PROTOCOL.relative_to(ROOT)), protocol["calendar"]]
    sources = {path: (ROOT / path).read_text() for path in paths}
    calendar = TradingCalendar(json.loads(sources[protocol["calendar"]]))
    for board in boards:
        if calendar.validate_grid(sorted(board["prices"]), end=as_of)["status"] != "ready":
            raise ValueError("Original sector grid invalid")
    probe_path = ROOT / protocol["probe_manifest"]
    if sha256(probe_path.read_bytes()).hexdigest() != protocol["probe_sha256"]:
        raise ValueError("Probe provenance changed")
    probes = {row["year"]: row for row in json.loads(probe_path.read_text())}
    output.mkdir(parents=True, exist_ok=False)
    pacer, outcomes = RequestPacer(.5), {}
    started = datetime.now(timezone.utc).isoformat()

    def cached(code, year):
        if code != "881101" or year not in probes:
            return None
        record = probes[year]
        expected = f"https://d.10jqka.com.cn/v4/line/bk_{code}/01/{year}.js"
        if record["status"] != "available" or record["http_status"] != 200 or record["url"] != expected:
            raise ValueError("Probe identity/status differs")
        raw = (probe_path.parent / record["raw_path"]).read_bytes()
        if sha256(raw).hexdigest() != record["sha256"] or len(raw) != record["bytes"]:
            raise ValueError("Probe response changed")
        rows = parse_annual(raw, code=code, year=year, as_of=as_of, calendar=calendar)
        target = output / record["raw_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return rows, {**record, "reused_probe": True, "probe_manifest": protocol["probe_manifest"]}

    def job(board):
        annual, requests = {}, []
        for year in protocol["years"]:
            result = cached(board["code"], year)
            rows, record = result if result is not None else collect_year(
                code=board["code"], year=year, directory=output, timeout=15,
                pacer=pacer, as_of=as_of, calendar=calendar)
            requests.append(record)
            annual[year] = rows
        prices, meta = extend(board, annual, calendar=calendar, as_of=as_of)
        return prices, {"code": board["code"], "name": board["name"], **meta, "requests": requests}

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {pool.submit(job, board): board["code"] for board in boards}
        for future in as_completed(jobs):
            outcomes[jobs[future]] = future.result()
            if len(outcomes) % 10 == 0 or len(outcomes) == len(boards):
                print(json.dumps({"verified_boards": len(outcomes), "pool_count": len(boards)}), flush=True)
    result = deepcopy(base["input"])
    for board in result["boards"]:
        board["prices"] = outcomes[board["code"]][0]
    first = min(min(board["prices"]) for board in result["boards"])
    common = max(min(board["prices"]) for board in result["boards"])
    result["days"] = [day.isoformat() for day in calendar.sessions(date.fromisoformat(first), as_of)]
    records = [outcomes[board["code"]][1] for board in boards]
    requests = [request for record in records for request in record["requests"]]
    if any((ROOT / path).read_text() != source for path, source in sources.items()):
        raise ValueError("Sector collection sources changed while running")
    metadata = {"version": protocol["version"], "protocol": protocol, "started_at": started,
                "completed_at": datetime.now(timezone.utc).isoformat(), "calendar": calendar.document,
                "source_snapshots": sources, "source_hashes": {path: sha256(source.encode()).hexdigest() for path, source in sources.items()},
                "pool_count": len(boards), "annual_requests": len(requests),
                "network_requests": sum(not row.get("reused_probe", False) for row in requests),
                "valid_annual_responses": sum(row["status"] == "available" for row in requests),
                "extended_boards": sum(row["status"] == "extended" for row in records),
                "full_pool_common_start": common, "as_of": as_of.isoformat(),
                "coverage_by_year": {str(year): sum(any(day.startswith(f"{year}-") for day in board["prices"])
                                                       for board in result["boards"]) for year in range(2017, 2027)},
                "boards": records, "input_sha256": sha256(json.dumps(result, sort_keys=True, allow_nan=False).encode()).hexdigest(),
                "production_modified": False, "investment_eligible": False,
                "limitations": ["Current86industry survivor pool and current historical vintage, not historical membership",
                    "Full-pool missing dates remain missing; no removal of failed boards or silent backward fill",
                    "Price history only, no historical constituents, dividends, valuations or executable funds"]}
    for name, value in (("input.json", result), ("collection.json", metadata)):
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({key: metadata[key] for key in ("pool_count", "annual_requests", "network_requests",
                      "valid_annual_responses", "extended_boards", "full_pool_common_start", "coverage_by_year")}), flush=True)
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/sector-long-history-v1")
    collect(parser.parse_args().output)
