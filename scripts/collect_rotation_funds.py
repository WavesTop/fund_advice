"""Archive a fixed public fund universe without importing into the daily DB."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.config import Settings
from scripts.fund_nav_ths import USER_AGENT, fetch_ths_fund_nav, parse_profile


class ArchivedResponse(BytesIO):
    def __init__(self, body: bytes, url: str, headers: dict):
        super().__init__(body)
        self.url, self.headers = url, headers

    def geturl(self):
        return self.url

    def getcode(self):
        return 200


def collect(code: str, expected: dict, folder: Path, *, throttle=None) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    cache, raw_manifest = {}, []

    def opener(request: Request, timeout: float):
        url = request.full_url
        if url not in cache:
            if throttle:
                throttle()
            with urlopen(request, timeout=timeout) as response:
                if response.geturl() != url or response.getcode() != 200:
                    raise ValueError("Unexpected response identity/status")
                body = response.read(16 * 1024 * 1024 + 1)
                if len(body) > 16 * 1024 * 1024:
                    raise ValueError("Response exceeds archive limit")
                headers = dict(response.headers)
            target = folder / ("profile.html" if url.endswith("interduce.html") else url.rsplit("/", 1)[-1])
            target.write_bytes(body)
            cache[url] = (body, headers)
            raw_manifest.append({"url": url, "path": str(target.relative_to(folder.parent)), "sha256": sha256(body).hexdigest(), "bytes": len(body)})
        body, headers = cache[url]
        return ArchivedResponse(body, url, headers)

    profile_url = f"https://fund.10jqka.com.cn/{code}/interduce.html"
    with opener(Request(profile_url, headers={"User-Agent": USER_AGENT, "Accept": "*/*",
                                              "Referer": f"https://fund.10jqka.com.cn/{code}/"}), 20) as response:
        soup = BeautifulSoup(response.read(), "html.parser")
    box = soup.select_one("ul.g-dialog")
    if box is None:
        raise ValueError("Missing fund identity box")
    identity_fields = {}
    for item in box.select("li"):
        key, value = item.select_one("span.key"), item.select_one("span.value")
        if key is not None and value is not None:
            identity_fields[key.get_text(strip=True)] = value.get_text(strip=True)
    short_name = identity_fields.get("基金简称", "")
    if identity_fields.get("基金代码") != code or not short_name:
        raise ValueError("Fund code differs from the frozen universe")
    full_name = identity_fields.get("基金全称", "")
    if expected["name"].endswith(("A", "C")) and not full_name.endswith(expected["name"][-1]):
        raise ValueError("Fund share class does not match the local catalog")
    parsed = parse_profile(cache[profile_url][0], code, short_name, expected["fund_type"])
    fetched = fetch_ths_fund_nav(code, short_name, "1900-01-01", date.today().isoformat(),
                                timeout=45, opener=opener, expected_fund_type=expected["fund_type"])
    return {**expected, "identity": parsed, "source_full_name": full_name, "source_profile_fields": identity_fields,
            "rows": fetched["rows"], "source_evidence": fetched["source_evidence"], "raw": raw_manifest,
            "fee_schedule_status": "not_verified_from_product_prospectus", "status": "available"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", type=Path, default=ROOT / "config/fund-rotation-expanded-universe-v1.json")
    parser.add_argument("--database", type=Path, default=Settings.from_env().database_path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runtime/verification/fund-rotation-expanded-20261013")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--throttle-seconds", type=float, default=2)
    args = parser.parse_args()
    definition = json.loads(args.universe.read_text(encoding="utf-8"))
    with closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        catalog = {}
        for item in definition["funds"]:
            row = connection.execute("SELECT code,name,fund_type FROM fund_catalog_projection WHERE code=?", (item["code"],)).fetchone()
            if row is None:
                raise ValueError(f"Missing catalog identity: {item['code']}")
            catalog[item["code"]] = {**dict(row), **item}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    items = []
    last_request = 0.0

    def throttle():
        nonlocal last_request
        time.sleep(max(0, args.throttle_seconds - (time.monotonic() - last_request)))
        last_request = time.monotonic()

    for item in definition["funds"]:
        code = item["code"]
        result_path = args.output_dir / code / "result.json"
        cached = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else None
        if cached and not (args.retry_failed and cached["status"] == "failed"):
            result = cached
        else:
            if cached:
                archive = result_path.parent / "previous-failure.json"
                if not archive.exists():
                    archive.write_text(json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                result = collect(code, catalog[code], args.output_dir / code, throttle=throttle)
            except Exception as exc:
                result = {**catalog[code], "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        items.append(result)
        print(json.dumps({"code": code, "theme": item["theme"], "status": result["status"], "rows": len(result.get("rows", [])),
                          "first": result.get("rows", [{}])[0].get("date"), "last": result.get("rows", [{}])[-1].get("date"),
                          "error": result.get("error")}, ensure_ascii=False), flush=True)
    report = {"universe": definition, "universe_sha256": sha256(args.universe.read_bytes()).hexdigest(),
              "collected_at": datetime.now(timezone.utc).isoformat(), "items": items,
              "limitations": ["Current-survivor theme selection, not point-in-time popularity",
                              "Current product fee schedules and historical NAV publication times are not verified"]}
    (args.output_dir / "input.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(any(item["status"] != "available" for item in items))


if __name__ == "__main__":
    raise SystemExit(main())
