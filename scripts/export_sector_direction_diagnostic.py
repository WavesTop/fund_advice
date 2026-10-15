"""Add a provenance-checked price-direction diagnostic without replacing V1 results."""
import argparse
import gzip
from hashlib import sha256
import json
from pathlib import Path

from backend.analysis.sector_direction_screen import screened_choices
from scripts.benchmark_sector_trend import rank_at

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "runtime/verification/sector-direction-screen-research-v1.json.gz"
REPORT_SHA256 = "822e4751e3dae36e12d2fb208951dddea60cbb0d53707664fcaf5034c7973bd0"
SNAPSHOT = ROOT / "frontend/src/features/market/sector-research-snapshot.json"
OUTPUT = ROOT / "frontend/src/features/market/sector-direction-diagnostic.json"


def verified_report():
    if sha256(REPORT.read_bytes()).hexdigest() != REPORT_SHA256:
        raise ValueError("Frozen direction study changed")
    with gzip.open(REPORT, "rt", encoding="utf-8") as stream:
        report = json.load(stream)
    for path, source in report["source_snapshots"].items():
        if sha256(source.encode()).hexdigest() != report["source_hashes"][path]:
            raise ValueError("Direction study source digest differs")
    for path in ("backend/analysis/sector_direction_screen.py", "scripts/benchmark_sector_trend.py", "backend/analysis/sector_trend.py"):
        if sha256((ROOT / path).read_bytes()).hexdigest() != report["source_hashes"][path]:
            raise ValueError("Direction calculation differs from archived source")
    return report


def project(report, snapshot, reference):
    if report["investment_eligible"] or report["production_ranking_changed"]:
        raise ValueError("Direction study is not research-only")
    if (report["protocol"]["reference_sha256"] != snapshot["sources"]["rank"]["sha256"]
            or report["input_sha256"] != reference["input_sha256"]
            or report["coverage"]["as_of"] != snapshot["data_cutoff"]
            or report["input"] != reference["input"]):
        raise ValueError("Direction study differs from the original trial sources")
    boards, days = report["input"]["boards"], report["input"]["days"]
    periods = {}
    for previous in snapshot["periods"]:
        period = previous["id"]
        source = report["periods"][period]
        latest = source["latest"]
        if latest["as_of"] != snapshot["as_of"] or previous["as_of"] != snapshot["as_of"] or previous["window"] != source["lookback_sessions"]:
            raise ValueError("Direction diagnostic would mix candidate dates or windows")
        cases = [row for row in reference["periods"][period]["cases"] if row["secondary"] and row["learning"]["as_of"] == latest["as_of"]]
        if len(cases) != 1:
            raise ValueError("Direction diagnostic lacks a unique archived forecast")
        entries = rank_at(boards, days, days.index(latest["as_of"]), previous["window"], current=True)
        if len(entries) != snapshot["pool_count"]:
            raise ValueError("Direction diagnostic has incomplete current prices")
        gates, chosen = screened_choices(entries, cases[0]["predictions"])
        if gates != latest["diagnostic"]["gate_codes"] or any(chosen[key] != latest["methods"][key] for key in chosen):
            raise ValueError("Direction diagnostic does not reproduce its selections")
        for method, codes in previous["selections"].items():
            if codes != latest["methods"][method]:
                raise ValueError("Direction diagnostic would overwrite an original trial selection")
        if {row["code"]: row["state"] for row in entries} != {row["code"]: row["state"] for row in previous["rows"]}:
            raise ValueError("Direction states differ from original trial")
        evidence = {}
        for clock in ("primary", "secondary"):
            summary = source[clock]
            evidence[clock] = {"method": summary["methods"]["state_return"],
                               "vs_count_return": summary["paired"]["state_return_vs_count_return"]}
        diagnostic = latest["diagnostic"]
        periods[period] = {"state_codes": sorted(gates["state"]), "direction_codes": sorted(gates["direction"]),
                           **{key: diagnostic[key] for key in ("old_price_gate_count", "old_model_gate_count", "old_joint_gate_count")},
                           "evidence": evidence}
    return {"version": report["version"], "as_of": snapshot["as_of"], "pool_count": snapshot["pool_count"],
            "source": {"path": str(REPORT.relative_to(ROOT)), "sha256": REPORT_SHA256},
            "investment_eligible": False, "periods": periods}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    report = verified_report()
    reference = ROOT / report["protocol"]["reference"]
    if sha256(reference.read_bytes()).hexdigest() != report["protocol"]["reference_sha256"]:
        raise ValueError("Original ranking study changed")
    with gzip.open(reference, "rt", encoding="utf-8") as stream:
        original = json.load(stream)
    output = project(report, json.loads(SNAPSHOT.read_text()), original)
    args.output.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "as_of": output["as_of"],
                      "state_counts": {key: len(value["state_codes"]) for key, value in output["periods"].items()}}))


if __name__ == "__main__":
    main()
