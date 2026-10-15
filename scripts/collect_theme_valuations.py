"""Resolve frozen funds to exact CSI indices and archive their public histories."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.collect_index_valuation import OSS, archive, parse_history
from scripts.collect_rotation_funds import ArchivedResponse
from scripts.import_related_market import CSI_INFO, CSI_PERF, _csi, _target_from_benchmark
from scripts.research_fund_rotation import INPUT


def collect(folder, *, cutoff, identity_only=False, cache_only_history=False, cache_only_assets=False):
    cache_only_history = cache_only_history or (folder / "access-stop.json").exists()
    source = json.loads(INPUT.read_text())
    raw, cache = {}, {}

    def opener(request, timeout):
        body, entry = archive(request.full_url, folder, request_body=request.data)
        raw[entry["path"]] = entry
        return ArchivedResponse(body, request.full_url, {"Content-Type": entry["content_type"] or ""})

    items = []
    for fund in source["items"]:
        if fund["role"] != "sector" or fund["theme"] == "黄金":
            continue
        benchmark = fund["source_profile_fields"]["业绩比较基准"]
        identity = _target_from_benchmark(benchmark)
        if identity is None or identity[1] != "95":
            raise ValueError("Theme benchmark must identify exactly one 95% index")
        profile = {"fund_code": fund["code"], "fund_name": fund["name"], "benchmark": benchmark,
                   "target": identity[0], "benchmark_weight_percent": identity[1]}
        result_path = folder / (fund["code"] + "-identity.json")
        if result_path.exists():
            saved = json.loads(result_path.read_text())
            if any(saved["relation"][key] != value for key, value in profile.items()):
                raise ValueError("Saved theme mapping differs from the frozen fund benchmark")
            item = saved
            for entry in item["identity_raw"]:
                body = (folder / entry["path"]).read_bytes()
                if sha256(body).hexdigest() != entry["sha256"]:
                    raise ValueError("Theme identity archive checksum differs")
                raw[entry["path"]] = entry
        else:
            before = set(raw)
            relation = _csi(profile, time.monotonic() + 120, opener, cache)
            if relation is None:
                raise ValueError(f"No unique official index matches {identity[0]}")
            info = cache["csi-info:" + relation["index_code"]]
            published = date.fromisoformat(info["publishDate"])
            if published > cutoff:
                raise ValueError("Theme index was not published by the research cutoff")
            item = {"fund_code": fund["code"], "theme": fund["theme"], "relation": relation, "official_info": info,
                    "index_publication_date": published.isoformat(), "identity_raw": [raw[key] for key in raw if key not in before]}
            result_path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        code = item["relation"]["index_code"]
        name = item["relation"]["index_name"]
        print(json.dumps({"fund": fund["code"], "theme": fund["theme"], "index": code, "name": name,
                          "published": item["index_publication_date"]}, ensure_ascii=False), flush=True)
        if item["official_info"].get("ifProtect") is True:
            item = item | {"status": "public_data_protection_flagged", "rows": [], "history_raw": [], "assets": {}}
        elif identity_only:
            item = item | {"status": "identity_only", "rows": [], "history_raw": [], "assets": {}}
        else:
            assets, asset_errors, history_raw, rows, missing_ranges = {}, {}, [], [], []
            for kind, suffix in (("factsheet", f"indices/detail/files/zh_CN/{code}factsheet.pdf"),
                                 ("indicator", f"file/autofile/indicator/{code}indicator.xls")):
                try:
                    _, entry = archive(OSS + suffix, folder, cache_only=cache_only_assets)
                except FileNotFoundError as exc:
                    asset_errors[kind] = {"url": OSS + suffix, "status": "not_collected", "error": str(exc)}
                    continue
                raw[entry["path"]] = entry
                assets[kind] = entry
            first = date.fromisoformat(item["index_publication_date"])
            for year in range(max(2009, first.year), cutoff.year + 1):
                start, end = max(first, date(year, 1, 1)), min(cutoff, date(year, 12, 31))
                url = CSI_PERF + "?" + urlencode({"indexCode": code, "startDate": start.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d")})
                try:
                    body, entry = archive(url, folder, cache_only=cache_only_history)
                except FileNotFoundError:
                    missing_ranges.append({"start": start.isoformat(), "end": end.isoformat(), "url": url,
                                           "status": "not_collected_access_stop_respected"})
                    continue
                raw[entry["path"]] = entry
                history_raw.append(entry)
                chunk = parse_history(body, code, start, end, expected_name=name)
                rows.extend(chunk)
            item = item | {"status": "partial_history_unverified_semantics" if missing_ranges else "collected_unverified_semantics",
                           "rows": rows, "history_raw": history_raw, "assets": assets, "asset_errors": asset_errors,
                           "missing_ranges": missing_ranges}
            print(json.dumps({"fund": fund["code"], "rows": len(rows), "null_valuations": sum(r["peg"] is None for r in rows)}), flush=True)
        items.append(item)
    if len(items) != 12 or len({x["relation"]["index_code"] for x in items}) != 12:
        raise ValueError("Frozen theme universe mapping is incomplete or duplicated")
    return {"items": items, "raw": list(raw.values()), "cutoff": cutoff.isoformat(), "new_history_requests_disabled": cache_only_history,
            "collected_at": datetime.now(timezone.utc).isoformat(), "fund_input_sha256": sha256(INPUT.read_bytes()).hexdigest(),
            "source_hashes": {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in
                              ("scripts/collect_theme_valuations.py", "scripts/collect_index_valuation.py", "scripts/import_related_market.py")},
            "limitations": ["Current official fund/index mapping, not a verified history of benchmark changes",
                            "Index data before official publication excluded; base date is not publication date",
                            "Current history download does not prove first-release or revision vintages",
                            "Candidate valuation semantics must be verified separately for each theme before testing"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runtime/verification/theme-valuations-20261013")
    parser.add_argument("--cutoff", type=date.fromisoformat, default=date(2026, 10, 12))
    parser.add_argument("--identity-only", action="store_true")
    parser.add_argument("--cache-only-history", action="store_true")
    parser.add_argument("--cache-only-assets", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = collect(args.output_dir, cutoff=args.cutoff, identity_only=args.identity_only,
                     cache_only_history=args.cache_only_history, cache_only_assets=args.cache_only_assets)
    output = args.output_dir / ("identity.json" if args.identity_only else "input.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output)}))


if __name__ == "__main__":
    main()
