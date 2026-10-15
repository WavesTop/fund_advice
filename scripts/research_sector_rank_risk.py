"""Retarget the frozen sector learner to ordinal return and path-risk labels."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.sector_forecast import audit_fit, features, fit_period
from backend.analysis.sector_price_validation import summarize
from backend.analysis.sector_rank_risk import centered_predictions, choices, path_outcomes, rank_targets
from scripts.benchmark_sector_trend import rank_at
from scripts.research_sector_forecasts import audit_records, labels, rank_ic
from scripts.research_sector_horizons import PERIODS, paired

PROTOCOL = ROOT / "config/sector-rank-risk-research-v1.json"
METHODS = ("return_top10", "magnitude_ridge_top10", "trend_gated", "low_vol_top10", "historical_rank_top10",
           "rank_top10", "safety_top10", "maximin_top10", "upward_quality", "upward_count_return")


def retarget(records, boards, days, window):
    result = deepcopy(records)
    indexes = {day: index for index, day in enumerate(days)}
    for record in result:
        end = indexes[record["exit_date"]]
        grid = days[end - window:end + 1]
        outcomes = [path_outcomes([board["prices"][day] for day in grid]) for board in boards]
        record["outcomes"] = rank_targets([row[0] for row in outcomes], [row[1] for row in outcomes]).tolist()
    return result


def audit_targets(records, original, boards, days, window):
    audit_records(original, boards, days, window)
    indexes = {day: index for index, day in enumerate(days)}
    if len(records) != len(original):
        raise ValueError("Ordinal training dates differ")
    for record, old in zip(records, original):
        if {key: value for key, value in record.items() if key != "outcomes"} != {key: value for key, value in old.items() if key != "outcomes"}:
            raise ValueError("Ordinal retargeting changed past features or maturity")
        first = indexes[record["signal_date"]] + 1
        paths = [path_outcomes([board["prices"][day] for day in days[first:first + window + 1]]) for board in boards]
        # Count ties directly rather than use the producer's percentile helper.
        expected = [[(sum(value[j] < point[j] for value in paths)
                      + (sum(value[j] == point[j] for value in paths) - 1) / 2) / (len(paths) - 1) - .5
                     for j in (0, 1)] for point in paths]
        if not np.allclose(expected, record["outcomes"], rtol=0, atol=1e-12):
            raise ValueError("Ordinal training ranks differ from saved price paths")
    return {"status": "passed", "dates": len(records), "observations": len(records) * len(boards)}


def comparison(cases, left, right):
    result = paired(cases, left, right)
    values = [(row["methods"][left], row["methods"][right]) for row in cases
              if row["methods"][left]["status"] == row["methods"][right]["status"] == "matured"]
    result["mean_drawdown_magnitude_delta_pp"] = (float(np.mean([abs(a["selected_max_drawdown_pct"]) - abs(b["selected_max_drawdown_pct"])
                                                for a, b in values])) if values else None)
    result["joint_return_and_drawdown_improvements"] = sum(a["selected_return_pct"] > b["selected_return_pct"] + 1e-12
        and abs(a["selected_max_drawdown_pct"]) < abs(b["selected_max_drawdown_pct"]) - 1e-12 for a, b in values)
    return result


def summary(cases):
    result = {"methods": {method: summarize([row["methods"][method] for row in cases], [0, 20, 50, 100]) for method in METHODS},
            "paired": {name: comparison(cases, left, right) for name, left, right in (
                ("rank_vs_return", "rank_top10", "return_top10"),
                ("rank_vs_magnitude", "rank_top10", "magnitude_ridge_top10"),
                ("rank_vs_historical_rank", "rank_top10", "historical_rank_top10"),
                ("safety_vs_low_vol", "safety_top10", "low_vol_top10"),
                ("maximin_vs_return", "maximin_top10", "return_top10"),
                ("maximin_vs_low_vol", "maximin_top10", "low_vol_top10"),
                ("upward_vs_count_return", "upward_quality", "upward_count_return"))}}
    known = [row["ordinal_ic"] for row in cases if row.get("ordinal_ic") is not None]
    result["ordinal_ic"] = {"dates": len(known), **{key: float(np.mean([row[key] for row in known if row[key] is not None]))
        if any(row[key] is not None for row in known) else None for key in ("return", "safety")}}
    return result


def run(reference):
    boards, days = reference["input"]["boards"], reference["input"]["days"]
    codes, indexes = [board["code"] for board in boards], {day: i for i, day in enumerate(days)}
    regime = json.loads(reference["source_snapshots"]["config/market-regime-strategy-research-v1.json"])["episodes"]
    result = {}
    for period, window in PERIODS.items():
        prior = reference["periods"][period]
        records = retarget(prior["training_records"], boards, days, window)
        record_validation = audit_targets(records, prior["training_records"], boards, days, window)
        cases = []
        for old in prior["cases"]:
            original = old["methods"]["return_top10"]
            entries = rank_at(boards, days, indexes[original["signal_date"]], window)
            model, historical, metadata = fit_period(records, original["signal_date"], codes)
            if any(metadata[field] != old["learning"][field] for field in ("training_dates", "distinct_dates", "status", "as_of")):
                raise ValueError("Ordinal model changed training availability")
            validation = audit_fit(records, metadata, codes)
            row = {"primary": old["primary"], "secondary": old["secondary"], "learning": metadata,
                   "fit_validation": validation, "ordinal_ic": None, "methods": {"return_top10": original,
                    "magnitude_ridge_top10": old["methods"]["ridge_top10"], "trend_gated": old["methods"]["trend_gated"]}}
            low_vol = [entry["code"] for entry in sorted(entries, key=lambda entry: (entry["daily_volatility_pct"], entry["code"]))[:10]]
            row["methods"].update(labels(boards, days, window, original, {"low_vol_top10": low_vol}))
            if model is None or len(entries) != len(codes):
                status = "signal_incomplete" if len(entries) != len(codes) else "not_fitted"
                row["methods"].update({method: {**{key: original[key] for key in
                    ("signal_date", "entry_date", "exit_date", "eligible_count", "pool_count")},
                    "selected_codes": [], "status": status} for method in METHODS[4:]})
            else:
                mapped = {entry["code"]: entry for entry in entries}
                matrix = [features(mapped[code]) for code in codes]
                predicted = centered_predictions(model, matrix)
                z = np.clip((np.asarray(matrix) - np.asarray(metadata["means"])) / np.asarray(metadata["scales"]), -5, 5)
                rebuilt = z @ np.asarray(metadata["coefficients"]) + np.asarray(metadata["intercept"])
                rebuilt -= rebuilt.mean(axis=0)
                if not np.allclose(predicted, rebuilt, rtol=0, atol=1e-12):
                    raise ValueError("Ordinal predictions differ from audited parameters")
                chosen = choices(entries, codes, predicted, historical)
                row["methods"].update(labels(boards, days, window, original, chosen))
                row["predictions"] = {code: {"return_rank": float(value[0]), "safety_rank": float(value[1])}
                                      for code, value in zip(codes, predicted)}
                if original["exit_date"] is not None:
                    first = indexes[original["entry_date"]]
                    paths = [path_outcomes([board["prices"][day] for day in days[first:first + window + 1]]) for board in boards]
                    actual = rank_targets([point[0] for point in paths], [point[1] for point in paths])
                    row["ordinal_ic"] = {"return": rank_ic(predicted[:, 0].tolist(), actual[:, 0].tolist()),
                                         "safety": rank_ic(predicted[:, 1].tolist(), actual[:, 1].tolist())}
            cases.append(row)
        episodes = {}
        for episode in regime:
            selected = [row for row in cases if row["primary"] and row["learning"]["as_of"] >= episode["start"]
                        and row["methods"]["return_top10"]["exit_date"] is not None
                        and row["methods"]["return_top10"]["exit_date"] <= episode["end"]]
            episodes[episode["id"]] = {**{key: value for key, value in prior["episodes"][episode["id"]].items() if key != "summary"},
                                       "summary": summary(selected)}
        result[period] = {"lookback_sessions": window, "forward_sessions": window, "training_records": records,
                          "record_validation": record_validation, "cases": cases, "episodes": episodes,
                          "primary": summary([row for row in cases if row["primary"]]),
                          "secondary": summary([row for row in cases if row["secondary"]])}
        print(json.dumps({"period": period, "primary": result[period]["primary"]["paired"]}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/sector-rank-risk-research-v1.json.gz")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Preserve previous ordinal sector report")
    protocol = json.loads(PROTOCOL.read_text())
    if (protocol["periods"] != PERIODS or protocol["methods"] != list(METHODS) or protocol["top_n"] != 10
            or protocol["targets"] != ["centered_midrank_forward_return", "centered_midrank_forward_max_drawdown"]):
        raise ValueError("Ordinal protocol differs from implemented rules")
    path = ROOT / protocol["reference"]
    if sha256(path.read_bytes()).hexdigest() != protocol["reference_sha256"]:
        raise ValueError("Frozen magnitude study changed")
    with gzip.open(path, "rt") as stream:
        reference = json.load(stream)
    for relative, expected in reference["source_hashes"].items():
        if sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Existing forecast source changed: {relative}")
    paths = set(reference["source_hashes"]) | {"backend/analysis/sector_rank_risk.py", "scripts/research_sector_rank_risk.py",
                                             str(PROTOCOL.relative_to(ROOT))}
    sources = {relative: (ROOT / relative).read_text() for relative in paths}
    periods = run(reference)
    if any((ROOT / relative).read_text() != text for relative, text in sources.items()):
        raise ValueError("Ordinal sources changed during study")
    report = {"version": protocol["version"], "generated_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol,
              "input": reference["input"], "input_sha256": reference["input_sha256"], "calendar": reference["calendar"],
              "coverage": reference["coverage"], "periods": periods, "source_snapshots": sources,
              "source_hashes": {relative: sha256(text.encode()).hexdigest() for relative, text in sources.items()},
              "investment_eligible": False, "production_ranking_changed": False,
              "limitations": reference["limitations"] + [protocol["scope"],
                  "Ordinal ridge regression is not pairwise learning-to-rank or a calibrated probability model",
                  "Individual sector drawdown labels do not set portfolio risk or the user's tolerated loss"]}
    with args.output.open("xb") as stream:
        with gzip.open(stream, "wt", encoding="utf-8") as compressed:
            json.dump(report, compressed, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}), flush=True)


if __name__ == "__main__":
    main()
