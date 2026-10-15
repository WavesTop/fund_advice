"""Export audited, frozen three-horizon research into the sector opportunity UI."""
import argparse
import gzip
from hashlib import sha256
import json
from pathlib import Path

import numpy as np

from backend.analysis.sector_forecast import audit_fit, features
from backend.analysis.sector_rank_risk import choices
from backend.analysis.sector_trend import midpoint_percentiles
from scripts.benchmark_sector_trend import rank_at
from scripts.export_strategy_trial import checked_metrics

ROOT = Path(__file__).resolve().parents[1]
REPORTS = {
    "rank": ("runtime/verification/sector-rank-risk-research-v1.json.gz",
             "78ba73766f907838b94a307c54969ce7f3f2ffe96af4292d211d3aeae1024d55"),
    "shadow": ("runtime/verification/sector-shadow-account-v1.json.gz",
               "1d780346171f2916089d274b9db737936aadb09d255880a8b3d1218cb0fd2321"),
}
OUTPUT = ROOT / "frontend/src/features/market/sector-research-snapshot.json"
METHODS = ("return_top10", "rank_top10", "maximin_top10", "low_vol_top10", "upward_quality")
CONTROLS = (*METHODS, "all_industries")
PERIODS = {"short": ("短期", "1 周至 1 个月", 20),
           "medium": ("中期", "1 至 3 个月", 60),
           "long": ("长期", "3 至 6 个月", 120)}
FIELDS = ("code", "name", "state", "return_pct", "fitted_return_pct", "r2", "up_day_ratio",
          "recent_fitted_return_pct", "recent_sessions", "max_drawdown_pct", "daily_volatility_pct",
          "fast_ma_bias_pct", "fast_slow_ma_spread_pct")


def verified_reports():
    result = {}
    for key, (relative, digest) in REPORTS.items():
        path = ROOT / relative
        if sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Frozen report digest changed: {key}")
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            report = json.load(stream)
        if report["investment_eligible"] or report["production_ranking_changed"]:
            raise ValueError("Research-only scope changed")
        for source, archived in report["source_snapshots"].items():
            if sha256(archived.encode()).hexdigest() != report["source_hashes"][source]:
                raise ValueError(f"Archived source digest changed: {source}")
        result[key] = report
    for relative in ("scripts/benchmark_sector_trend.py", "backend/analysis/sector_trend.py",
                     "backend/analysis/sector_forecast.py", "backend/analysis/sector_rank_risk.py"):
        if sha256((ROOT / relative).read_bytes()).hexdigest() != result["rank"]["source_hashes"][relative]:
            raise ValueError(f"Export calculation differs from archived source: {relative}")
    return result


def latest_common_cases(rank):
    available = {period: {case["learning"]["as_of"]: case for case in value["cases"]
                         if case["secondary"] and case["learning"]["status"] == "fitted"}
                 for period, value in rank["periods"].items()}
    common = set.intersection(*(set(available[period]) for period in PERIODS))
    if not common:
        raise ValueError("No common fitted research date")
    as_of = max(common)
    return as_of, {period: available[period][as_of] for period in PERIODS}


def current_period(rank, period, case, as_of):
    boards, days = rank["input"]["boards"], rank["input"]["days"]
    label, horizon, window = PERIODS[period]
    end = days.index(as_of)
    codes = [board["code"] for board in boards]
    entries = rank_at(boards, days, end, window, current=True)
    if len(entries) != len(codes) or any(board["collection_error"] for board in boards):
        raise ValueError("Frozen full peer pool is incomplete")
    prior, metadata = rank["periods"][period], case["learning"]
    if prior["record_validation"]["status"] != "passed" or case["fit_validation"]["status"] != "passed":
        raise ValueError("Candidate audit is incomplete")
    audit_fit(prior["training_records"], metadata, codes)
    matrix = np.asarray([features(entry) for entry in entries])
    z = np.clip((matrix - np.asarray(metadata["means"])) / np.asarray(metadata["scales"]), -5, 5)
    predicted = z @ np.asarray(metadata["coefficients"]) + np.asarray(metadata["intercept"])
    predicted -= predicted.mean(axis=0)
    saved = np.asarray([[case["predictions"][code][key] for key in ("return_rank", "safety_rank")]
                        for code in codes])
    if not np.allclose(predicted, saved, rtol=0, atol=1e-12):
        raise ValueError("Frozen predictions do not reproduce")
    selected = choices(entries, codes, predicted, np.zeros_like(predicted))
    selected["return_top10"] = [entry["code"] for entry in sorted(entries,
                                key=lambda entry: (-entry["return_pct"], entry["code"]))[:10]]
    for method in METHODS:
        original = case["methods"][method]
        if original["signal_date"] != as_of or selected[method] != original["selected_codes"]:
            raise ValueError(f"Frozen candidate selection differs: {period}/{method}")
    percentiles = [midpoint_percentiles(predicted[:, i].tolist()) for i in (0, 1)]
    rows = []
    for i, (entry, board) in enumerate(zip(entries, boards)):
        if entry["code"] != board["code"]:
            raise ValueError("Feature peer order differs")
        first = board["prices"][days[end - window]]
        rows.append({**{key: entry[key] for key in FIELDS},
                     "return_percentile": percentiles[0][i], "safety_percentile": percentiles[1][i],
                     "curve": [round((board["prices"][day] / first - 1) * 100, 8)
                               for day in days[end - window:end + 1]]})
    return {"id": period, "label": label, "horizon": horizon, "window": window,
            "observation_start": days[end - window], "as_of": as_of,
            "latest_label_end": metadata["latest_label_end"], "training_dates": metadata["distinct_dates"],
            "dates": days[end - window:end + 1], "selections": {key: selected[key] for key in METHODS},
            "rows": rows}


def account_evidence(shadow, period, current):
    source = shadow["periods"][period]
    matches = [row for row in shadow["source_schedules"][period].values()
               if row["source_date"] == current["as_of"]]
    if len(matches) != 1 or any(matches[0]["selections"][method] != current["selections"][method]
                                for method in METHODS):
        raise ValueError("Current candidates differ from account source schedule")

    def metrics(trial):
        return {"start": trial["metrics"]["start_date"], "end": trial["metrics"]["end_date"],
                **checked_metrics(trial)}

    scenarios = {key: {method: metrics(value["policies"][method]) for method in CONTROLS}
                 for key, value in source["scenarios"].items()}
    fresh = {key: {method: metrics(value["policies"][method]) for method in CONTROLS}
             for key, value in source["fresh_entry"].items()}
    runs = source["scenarios"]["fee10_default"]["policies"]
    episodes = json.loads(shadow["source_snapshots"]["config/market-regime-strategy-research-v1.json"])["episodes"]
    return {"start": source["main_start"], "scenarios": scenarios, "fresh": fresh,
            "annual": {method: runs[method]["annual"] for method in CONTROLS},
            "episodes": [{"id": episode["id"], "group": episode["evaluation_group"],
                          "start": episode["start"], "end": episode["end"],
                          "methods": {method: runs[method]["episodes"][episode["id"]] for method in CONTROLS}}
                         for episode in episodes]}


def project(reports):
    rank, shadow = reports["rank"], reports["shadow"]
    if rank["input_sha256"] != shadow["input_sha256"]:
        raise ValueError("Research inputs differ")
    as_of, cases = latest_common_cases(rank)
    periods = []
    for period in PERIODS:
        current = current_period(rank, period, cases[period], as_of)
        periods.append({**current, "evidence": account_evidence(shadow, period, current)})
    return {"version": "sector-opportunity-trial-v1", "as_of": as_of,
            "data_cutoff": rank["coverage"]["as_of"], "pool_count": len(rank["input"]["boards"]),
            "full_pool_start": rank["coverage"]["full_pool_common_start"],
            "investment_eligible": False, "production_ranking_changed": False,
            "sources": {key: {"path": relative, "sha256": digest} for key, (relative, digest) in REPORTS.items()},
            "periods": periods}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    snapshot = project(verified_reports())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "as_of": snapshot["as_of"],
                      "counts": {p["id"]: {m: len(v) for m, v in p["selections"].items()} for p in snapshot["periods"]}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
