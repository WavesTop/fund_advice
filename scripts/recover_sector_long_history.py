"""One bounded transport-only recovery, preserving the original collection."""
from copy import deepcopy
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.trading_calendar import TradingCalendar
from scripts.collect_sector_long_history import extend
from scripts.collect_sector_validation_history import collect_year, RequestPacer
from scripts.research_sector_long_horizons import checked_annual


def main():
    path = ROOT / "config/sector-long-history-recovery-v1.json"
    protocol = json.loads(path.read_text())
    original = ROOT / protocol["original"]
    raw_manifest = (original / "collection.json").read_bytes()
    if sha256(raw_manifest).hexdigest() != protocol["original_manifest_sha256"]:
        raise ValueError("Original sector collection changed")
    metadata = json.loads(raw_manifest)
    retries = [(board["code"], item["year"]) for board in metadata["boards"] for item in board["requests"]
               if item["status"] != "available" and item["http_status"] in (502, 504)]
    if (len(retries) != 23 or protocol["maximum_requests"] != 23 or protocol["retry_http_statuses"] != [502, 504]
            or protocol["workers"] != 1 or protocol["minimum_interval_seconds"] != 1):
        raise ValueError("Recovery differs from frozen transport-only design")
    base_path = ROOT / metadata["protocol"]["base_report"]
    if sha256(base_path.read_bytes()).hexdigest() != metadata["protocol"]["base_report_sha256"]:
        raise ValueError("Original sector pool changed")
    base = json.loads(base_path.read_text())
    calendar = TradingCalendar(metadata["calendar"])
    as_of = date.fromisoformat(metadata["as_of"])
    output = ROOT / "runtime/verification/sector-long-history-recovered-v1"
    output.mkdir(exist_ok=False)
    sources = dict(metadata["source_snapshots"])
    for relative in ("scripts/recover_sector_long_history.py", "config/sector-long-history-recovery-v1.json",
                     "scripts/research_sector_long_horizons.py"):
        sources[relative] = (ROOT / relative).read_text()
    result, records, requests = deepcopy(base["input"]), [], []
    pacer = RequestPacer(1.)
    recovered = 0
    for board, previous in zip(result["boards"], metadata["boards"]):
        annual, current = {}, deepcopy(previous)
        for index, record in enumerate(current["requests"]):
            if (board["code"], record["year"]) in retries:
                rows, new = collect_year(code=board["code"], year=record["year"], directory=output,
                                          timeout=15, pacer=pacer, as_of=as_of, calendar=calendar)
                new["previous_attempt"] = {"manifest": str(original.relative_to(ROOT) / "collection.json"), **record}
                current["requests"][index] = new
                recovered += new["status"] == "available"
                requests.append(new)
                print(json.dumps({"attempted": len(requests), "recovered": recovered}), flush=True)
            else:
                rows = checked_annual(original, record, board["code"], calendar=calendar, as_of=as_of)
                if record["raw_path"]:
                    target = output / record["raw_path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original / record["raw_path"], target)
            annual[record["year"]] = rows
        board["prices"], publication = extend(board, annual, calendar=calendar, as_of=as_of)
        current.update(publication)
        records.append(current)
    first = min(min(board["prices"]) for board in result["boards"])
    result["days"] = [day.isoformat() for day in calendar.sessions(date.fromisoformat(first), as_of)]
    if (original / "collection.json").read_bytes() != raw_manifest:
        raise ValueError("Original collection changed during recovery")
    for relative in ("scripts/recover_sector_long_history.py", "config/sector-long-history-recovery-v1.json",
                     "scripts/research_sector_long_horizons.py"):
        if (ROOT / relative).read_text() != sources[relative]:
            raise ValueError("Recovery source changed during execution")
    merged = deepcopy(metadata)
    merged.update(version=protocol["version"], recovery_protocol=protocol,
                  completed_at=datetime.now(timezone.utc).isoformat(), boards=records,
                  source_snapshots=sources, source_hashes={key: sha256(value.encode()).hexdigest() for key, value in sources.items()},
                  previous_network_requests=metadata["network_requests"], network_requests=len(requests),
                  recovered_annual_responses=recovered,
                  valid_annual_responses=sum(item["status"] == "available" for row in records for item in row["requests"]),
                  full_pool_common_start=max(min(board["prices"]) for board in result["boards"]),
                  extended_boards=sum(row["status"] == "extended" for row in records),
                  coverage_by_year={str(year): sum(any(day.startswith(f"{year}-") for day in board["prices"])
                                                    for board in result["boards"]) for year in range(2017, 2027)},
                  input_sha256=sha256(json.dumps(result, sort_keys=True, allow_nan=False).encode()).hexdigest())
    for name, value in (("input.json", result), ("collection.json", merged)):
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"requests": len(requests), "recovered": recovered,
                      "common_start": merged["full_pool_common_start"]}), flush=True)


if __name__ == "__main__":
    main()
