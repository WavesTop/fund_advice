"""Compare search CPU cost and optionally real THS full/quote-only collection."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import import_sector_heat as collector


def baseline_function(ref: str, name: str):
    source = subprocess.run(["git", "show", f"{ref}:scripts/import_sector_heat.py"], cwd=ROOT,
                            check=True, capture_output=True, text=True).stdout
    node = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = dict(vars(collector))
    exec(compile(ast.Module(body=[node], type_ignores=[]), "baseline", "exec"), namespace)
    return namespace[name]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default="00c59c3")
    parser.add_argument("--live-quotes", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "runtime" / "verification" / "sector-library-performance.json")
    args = parser.parse_args()
    sectors = json.loads((ROOT / "config" / "ths-search-sectors.json").read_text())["rows"]
    etfs = json.loads((ROOT / "config" / "ths-search-etfs.json").read_text())["rows"]
    queries = random.Random(20261012).sample([item["code"] for item in sectors + etfs] +
                                           [item["name"] for item in sectors + etfs], 200)
    old_search = baseline_function(args.baseline, "search_ths_boards")
    report = {"baseline": args.baseline, "measured_at": datetime.now(timezone.utc).isoformat(),
              "scope": "search_matching_with_same_frozen_catalog_no_network"}
    with patch.object(collector, "fetch_ths_standard_sector_directory", return_value={"rows": sectors}), \
         patch.object(collector, "fetch_ths_etf_directory", return_value=etfs):
        # The baseline function holds its own globals, so both use identical inputs.
        old_search.__globals__["fetch_ths_standard_sector_directory"] = collector.fetch_ths_standard_sector_directory
        old_search.__globals__["fetch_ths_etf_directory"] = collector.fetch_ths_etf_directory
        samples, identities = {}, {}
        for label, search in (("before", old_search), ("after", collector.search_ths_boards)):
            search(queries[0])
            times, selected = [], []
            for query in queries:
                start = time.perf_counter()
                result = search(query)
                times.append(1000 * (time.perf_counter() - start))
                selected.append(result["items"][0]["code"] if result["items"] else None)
            samples[label] = {"queries": len(times), "mean_ms": round(statistics.mean(times), 3),
                              "p95_ms": round(sorted(times)[int(len(times) * .95) - 1], 3),
                              "total_ms": round(sum(times), 2)}
            identities[label] = selected
        report["search"] = samples
        report["search"]["same_first_identity"] = identities["before"] == identities["after"]
    if args.live_quotes:
        old_collect = baseline_function(args.baseline, "collect_ths_board")
        comparisons = []
        for code in ("881273", "881107"):
            member = next(dict(item) for item in sectors if item["code"] == code)
            current = datetime.now(timezone.utc)
            member.update(heat_rank=1, heat_value="0", ranking_as_of=current.date().isoformat())
            current = datetime.now(timezone.utc)
            start = time.perf_counter()
            with patch.object(collector, "get_bytes", wraps=collector.get_bytes) as calls:
                old_collect.__globals__["get_bytes"] = calls
                baseline = old_collect(member, previous={}, current=current, attempted_at=current.isoformat(), throttle_seconds=0)
                before_calls = calls.call_count
            before_seconds = time.perf_counter() - start
            previous = {code: {**baseline, "membership": {"as_of": member["ranking_as_of"],
                         "members": baseline["constituents"], "fetched_at": baseline["constituent_updated_at"]}}}
            current = datetime.now(timezone.utc)
            start = time.perf_counter()
            with patch.object(collector, "get_bytes", wraps=collector.get_bytes) as calls:
                updated = collector.collect_ths_board(member, previous=previous, current=current,
                          attempted_at=current.isoformat(), throttle_seconds=0, market_only=True)
                after_calls = calls.call_count
            comparisons.append({"code": code, "name": member["name"], "before_seconds": round(before_seconds, 3),
                                "after_seconds": round(time.perf_counter() - start, 3),
                                "before_http_requests": before_calls, "after_http_requests": after_calls,
                                "before_error": baseline["collection_error"], "after_error": updated["collection_error"],
                                "as_of": updated["rows"][-1]["date"] if updated["rows"] else None,
                                "row_count": len(updated["rows"]), "quote_as_of": updated["rows"][-1].get("quote_as_of") if updated["rows"] else None})
            print(json.dumps(comparisons[-1], ensure_ascii=False), flush=True)
        report["live_quotes"] = comparisons
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return int(not report["search"]["same_first_identity"])


if __name__ == "__main__":
    raise SystemExit(main())
