"""Archive CSI public history; keep field semantics and publication limits explicit."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
import time
from urllib.parse import urlparse, urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.import_related_market import CSI_PERF

INDICES = {"000300": "沪深300指数", "000905": "中证小盘500指数"}
OSS = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"


def archive(url, folder, *, interval=.5, request_body=None, cache_only=False):
    if urlparse(url).hostname not in ("www.csindex.com.cn", "oss-ch.csindex.com.cn"):
        raise ValueError("Only public CSI pages are permitted")
    folder.mkdir(parents=True, exist_ok=True)
    key = sha256(url.encode() if request_body is None else url.encode() + b"\n" + request_body).hexdigest()
    path, metadata = folder / key, folder / (key + ".json")
    if path.exists() and metadata.exists():
        body = path.read_bytes()
        entry = json.loads(metadata.read_text())
        if entry["url"] != url or entry["sha256"] != sha256(body).hexdigest():
            raise ValueError("Archive identity/checksum mismatch")
        return body, entry
    if cache_only:
        raise FileNotFoundError("No archived response; new history requests are disabled")
    time.sleep(interval)
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.csindex.com.cn/"}
    if request_body is not None:
        headers["Content-Type"] = "application/json"
    with urlopen(Request(url, data=request_body, headers=headers), timeout=35) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError("Unexpected public response status/identity")
        body = response.read(8 * 1024 * 1024 + 1)
        if len(body) > 8 * 1024 * 1024:
            raise ValueError("Response exceeds archive limit")
        content_type = response.headers.get("Content-Type")
    entry = {"url": url, "path": key, "sha256": sha256(body).hexdigest(), "bytes": len(body),
             "content_type": content_type, "fetched_at": datetime.now(timezone.utc).isoformat()}
    if request_body is not None:
        entry["request_body"] = json.loads(request_body)
    path.write_bytes(body)
    metadata.write_text(json.dumps(entry, indent=2))
    return body, entry


def parse_history(body, code, start, end, *, expected_name=None):
    payload = json.loads(body)
    if str(payload.get("code")) != "200" or payload.get("success") is not True or not isinstance(payload.get("data"), list):
        raise ValueError("CSI history did not report successful structured data")
    rows = []
    for record in payload["data"]:
        if not isinstance(record, dict) or record.get("indexCode") != code or record.get("indexNameCnAll") != (expected_name or INDICES[code]):
            raise ValueError("CSI history identity differs from the requested index")
        stamp = record.get("tradeDate")
        if not isinstance(stamp, str) or len(stamp) != 8 or not stamp.isdigit():
            raise ValueError("CSI row has a noncanonical date")
        day = datetime.strptime(stamp, "%Y%m%d").date()
        if not start <= day <= end:
            raise ValueError("CSI row lies outside the requested dates")
        close, value = record.get("close"), record.get("peg")
        if isinstance(close, bool) or close is None or not math.isfinite(float(close)) or float(close) <= 0:
            raise ValueError("CSI history has invalid closing value")
        if value is not None and (isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0):
            raise ValueError("CSI history has invalid candidate valuation")
        rows.append({"date": day.isoformat(), "close": float(close), "peg": float(value) if value is not None else None})
    if len({r["date"] for r in rows}) != len(rows):
        raise ValueError("Duplicate CSI history dates")
    return sorted(rows, key=lambda r: r["date"])


def collect(folder, cutoff, *, metadata_only=False):
    raw, indices = [], {}
    for code in INDICES:
        assets = {}
        for kind, suffix in (("indicator", f"file/autofile/indicator/{code}indicator.xls"),
                             ("factsheet", f"indices/detail/files/zh_CN/{code}factsheet.pdf")):
            body, entry = archive(OSS + suffix, folder)
            raw.append(entry)
            assets[kind] = entry
            print(json.dumps({"index": code, "asset": kind, "bytes": len(body), "magic": body[:12].hex()}), flush=True)
        rows = []
        if not metadata_only:
            for year in range(2009, cutoff.year + 1):
                start, end = date(year, 1, 1), min(date(year, 12, 31), cutoff)
                url = CSI_PERF + "?" + urlencode({"indexCode": code, "startDate": start.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d")})
                body, entry = archive(url, folder)
                raw.append(entry)
                chunk = parse_history(body, code, start, end)
                rows.extend(chunk)
                print(json.dumps({"index": code, "year": year, "rows": len(chunk)}), flush=True)
        indices[code] = {"name": INDICES[code], "rows": rows, "assets": assets, "semantics_status": "not_yet_verified"}
    return {"indices": indices, "raw": raw, "cutoff": cutoff.isoformat(), "collected_at": datetime.now(timezone.utc).isoformat(),
            "collector_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
            "limitations": ["peg field identity needs primary-source cross-check before treating it as P/E",
                            "Current history download is not an independently preserved publication vintage",
                            "Non-session rows and missing/null values require explicit filtering; no interpolation",
                            "Do not apply industry calculation rules to index valuation without confirmation"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runtime/verification/index-valuation-20261013")
    parser.add_argument("--cutoff", type=date.fromisoformat, default=date(2026, 10, 12))
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()
    result = collect(args.output_dir, args.cutoff, metadata_only=args.metadata_only)
    path = args.output_dir / ("metadata.json" if args.metadata_only else "input.json")
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(path)}))


if __name__ == "__main__":
    main()
