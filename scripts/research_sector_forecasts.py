"""Fixed past-only sector forecasts for each opportunity horizon."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.sector_forecast import FEATURES, audit_fit, features, fit_period, predictions
from backend.analysis.sector_price_validation import label_case, summarize
from backend.analysis.sector_trend import midpoint_percentiles
from scripts.benchmark_sector_trend import rank_at
from scripts.research_sector_horizons import PERIODS, paired, select

PROTOCOL = ROOT / "config/sector-forecast-research-v1.json"
METHODS = ("return_top10", "trend_top10", "trend_gated", "historical_mean_top10", "ridge_top10",
           "ridge_gated", "ridge_positive", "positive_count_return")


def training_records(boards, days, window, entries_at):
    codes = [board["code"] for board in boards]
    records = []
    for index in range(window, len(days) - window - 2, 10):
        entries = entries_at(index)
        if len(entries) != len(codes):
            continue
        first, last = index + 1, index + 1 + window
        grid = days[first:last + 1]
        if any(any(day not in board["prices"] for day in grid) for board in boards):
            continue
        absolute = np.asarray([board["prices"][days[last]] / board["prices"][days[first]] - 1 for board in boards])
        entries = {entry["code"]: entry for entry in entries}
        records.append({"signal_date": days[index], "exit_date": days[last], "ready_date": days[last + 1],
                        "codes": codes, "features": [features(entries[code]) for code in codes],
                        "outcomes": np.column_stack((absolute, absolute - absolute.mean())).tolist()})
    return records


def audit_records(records, boards, days, window):
    indexes = {day: index for index, day in enumerate(days)}
    codes = [board["code"] for board in boards]
    for record in records:
        index = indexes[record["signal_date"]]
        if ((index - window) % 10 or record["exit_date"] != days[index + window + 1]
                or record["ready_date"] != days[index + window + 2] or record["codes"] != codes):
            raise ValueError("Sector training label has a wrong horizon/maturity/grid")
        absolute = [board["prices"][record["exit_date"]] / board["prices"][days[index + 1]] - 1 for board in boards]
        average = sum(absolute) / len(absolute)
        expected = [[value, value - average] for value in absolute]
        if not np.allclose(expected, record["outcomes"], rtol=0, atol=1e-12):
            raise ValueError("Sector training outcome differs from raw prices")
    return {"status": "passed", "dates": len(records), "observations": len(records) * len(codes)}


def forecast_choices(entries, codes, predicted, mean):
    def top(values, allowed=None, count=10):
        return sorted((code for code in codes if allowed is None or code in allowed),
                      key=lambda code: (-values[codes.index(code), 1], code))[:count]
    gated = {entry["code"] for entry in entries if entry["score"] >= 60 and entry["state"] in ("steady_up", "strengthening")}
    positive = {code for code, value in zip(codes, predicted) if value[0] > .002 and value[1] > 0}
    chosen = {"historical_mean_top10": top(mean), "ridge_top10": top(predicted),
              "ridge_gated": top(predicted, gated), "ridge_positive": top(predicted, positive)}
    chosen["positive_count_return"] = [entry["code"] for entry in sorted(entries,
        key=lambda entry: (-entry["return_pct"], entry["code"]))[:len(chosen["ridge_positive"])]]
    return chosen


def labels(boards, days, window, original, chosen):
    common = {key: original[key] for key in ("signal_date", "entry_date", "exit_date", "eligible_count", "pool_count")}
    if common["eligible_count"] != len(boards):
        return {method: {**common, "selected_codes": codes, "status": "signal_incomplete"} for method, codes in chosen.items()}
    if common["exit_date"] is None:
        return {method: {**common, "selected_codes": codes, "status": "not_matured" if codes else "no_selection"}
                for method, codes in chosen.items()}
    first = days.index(common["entry_date"])
    grid = days[first:first + window + 1]
    rows = [{"code": board["code"], "rows": [{"date": day, "close": board["prices"][day]}
             for day in grid if day in board["prices"]]} for board in boards]
    return {method: {**common, "selected_codes": codes, **label_case(rows, codes, grid)} for method, codes in chosen.items()}


def group_summary(cases, costs):
    result = {"methods": {method: summarize([row["methods"][method] for row in cases], costs) for method in METHODS},
              "paired": {key: paired(cases, left, right) for key, left, right in (
                  ("ridge_vs_return", "ridge_top10", "return_top10"),
                  ("ridge_vs_trend", "ridge_top10", "trend_top10"),
                  ("ridge_vs_mean", "ridge_top10", "historical_mean_top10"),
                  ("ridge_same_gate_vs_trend", "ridge_gated", "trend_gated"),
                  ("positive_vs_count_return", "ridge_positive", "positive_count_return"))}}
    errors = [row["forecast_errors"] for row in cases if row.get("forecast_errors")]
    result["forecast_errors"] = {"dates": len(errors), **{key: float(np.mean([row[key] for row in errors if row[key] is not None]))
        if any(row[key] is not None for row in errors) else None for key in
        ("ridge_excess_rmse_pp", "mean_excess_rmse_pp", "ridge_rank_ic", "mean_rank_ic")}}
    return result


def rank_ic(predicted, actual):
    a, b = midpoint_percentiles(predicted), midpoint_percentiles(actual)
    return float(np.corrcoef(a, b)[0, 1]) if np.std(a) > 1e-12 and np.std(b) > 1e-12 else None


def run(reference):
    boards, days = reference["input"]["boards"], reference["input"]["days"]
    codes = [board["code"] for board in boards]
    costs = [0, 20, 50, 100]
    output = {}
    for period, window in PERIODS.items():
        cache = {}

        def entries_at(index):
            if index not in cache:
                cache[index] = rank_at(boards, days, index, window)
            return cache[index]

        records = training_records(boards, days, window, entries_at)
        record_validation = audit_records(records, boards, days, window)
        cases = []
        for frozen in reference["periods"][period]["cases"]:
            original = frozen["methods"]["return_top10"]
            index = days.index(original["signal_date"])
            entries = entries_at(index)
            selected = select(entries)
            for method in ("return_top10", "trend_top10", "trend_gated"):
                if [row["code"] for row in selected[method]] != frozen["methods"][method]["selected_codes"]:
                    raise ValueError("Forecast extension changed existing sector choices")
            model, mean, metadata = fit_period(records, days[index], codes)
            validation = audit_fit(records, metadata, codes)
            row = {"primary": frozen["primary"], "secondary": frozen["secondary"],
                   "methods": {method: frozen["methods"][method] for method in ("return_top10", "trend_top10", "trend_gated")},
                   "learning": metadata, "fit_validation": validation, "forecast_errors": None}
            if len(entries) != len(codes) or model is None:
                status = "signal_incomplete" if len(entries) != len(codes) else "not_fitted"
                row["methods"].update({method: {**{key: original[key] for key in
                    ("signal_date", "entry_date", "exit_date", "eligible_count", "pool_count")},
                    "selected_codes": [], "status": status} for method in METHODS[3:]})
            else:
                mapped = {entry["code"]: entry for entry in entries}
                matrix = [features(mapped[code]) for code in codes]
                predicted = predictions(model, matrix)
                manual = np.clip((np.asarray(matrix) - np.asarray(metadata["means"])) / np.asarray(metadata["scales"]), -5, 5)
                rebuilt = manual @ np.asarray(metadata["coefficients"]) + np.asarray(metadata["intercept"])
                rebuilt[:, 1] -= rebuilt[:, 1].mean()
                if not np.allclose(predicted, rebuilt, rtol=0, atol=1e-12):
                    raise ValueError("Sector predictions differ from audited fit")
                chosen = forecast_choices(entries, codes, predicted, mean)
                row["methods"].update(labels(boards, days, window, original, chosen))
                row["predictions"] = {code: {"absolute": float(value[0]), "excess": float(value[1]),
                    "historical_mean_absolute": float(mean[i, 0]), "historical_mean_excess": float(mean[i, 1])}
                    for i, (code, value) in enumerate(zip(codes, predicted))}
                if original["exit_date"] is not None:
                    actual = np.asarray([board["prices"][original["exit_date"]] / board["prices"][original["entry_date"]] - 1 for board in boards])
                    excess = actual - actual.mean()
                    row["forecast_errors"] = {"ridge_excess_rmse_pp": float(np.sqrt(np.mean((predicted[:, 1] - excess) ** 2)) * 100),
                        "mean_excess_rmse_pp": float(np.sqrt(np.mean((mean[:, 1] - excess) ** 2)) * 100),
                        "ridge_rank_ic": rank_ic(predicted[:, 1].tolist(), excess.tolist()),
                        "mean_rank_ic": rank_ic(mean[:, 1].tolist(), excess.tolist())}
            cases.append(row)
        episodes = {}
        regime = json.loads(reference["source_snapshots"]["config/market-regime-strategy-research-v1.json"])["episodes"]
        for episode in regime:
            eligible = [row for row in cases if row["primary"] and row["methods"]["return_top10"]["signal_date"] >= episode["start"]
                        and row["methods"]["return_top10"]["exit_date"] is not None
                        and row["methods"]["return_top10"]["exit_date"] <= episode["end"]]
            episodes[episode["id"]] = {**{key: value for key, value in reference["periods"][period]["episodes"][episode["id"]].items()
                                          if key != "summary"}, "summary": group_summary(eligible, costs)}
        output[period] = {"lookback_sessions": window, "forward_sessions": window, "training_records": records,
                          "record_validation": record_validation,
                          "primary": group_summary([row for row in cases if row["primary"]], costs),
                          "secondary": group_summary([row for row in cases if row["secondary"]], costs),
                          "episodes": episodes, "cases": cases}
        print(json.dumps({"period": period, "training_dates": len(records), "primary": output[period]["primary"]["paired"]}), flush=True)
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/sector-forecast-research-v1.json.gz")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Preserve prior sector forecast study")
    protocol = json.loads(PROTOCOL.read_text())
    if (protocol["periods"] != PERIODS or protocol["methods"] != list(METHODS) or protocol["features"] != list(FEATURES)
            or (protocol["training_stride"], protocol["minimum_dates"], protocol["training_calendar_days"], protocol["penalty"]) != (10, 24, 1096, 1)
            or protocol["top_n"] != 10 or protocol["positive_absolute_floor"] != .002 or protocol["positive_excess_floor"] != 0
            or protocol["label_ready"] != "one_exchange_session_after_exit_close"
            or protocol["targets"] != ["absolute_price_return", "same_date_equal_weight_pool_excess"]):
        raise ValueError("Frozen sector forecast rules differ")
    path = ROOT / protocol["reference"]
    if sha256(path.read_bytes()).hexdigest() != protocol["reference_sha256"]:
        raise ValueError("Frozen sector reference changed")
    with gzip.open(path, "rt") as stream:
        reference = json.load(stream)
    for relative, expected in reference["source_hashes"].items():
        if sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Existing sector source changed: {relative}")
    paths = set(reference["source_hashes"]) | {"backend/analysis/sector_forecast.py", "backend/analysis/matured_trend_forecast.py",
        "scripts/research_sector_forecasts.py", str(PROTOCOL.relative_to(ROOT))}
    sources = {relative: (ROOT / relative).read_text() for relative in paths}
    periods = run(reference)
    if any((ROOT / relative).read_text() != text for relative, text in sources.items()):
        raise ValueError("Sector forecast sources changed during run")
    report = {"version": protocol["version"], "generated_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol,
              "input": reference["input"], "input_sha256": reference["input_sha256"], "calendar": reference["calendar"],
              "coverage": reference["coverage"], "source_snapshots": sources,
              "source_hashes": {relative: sha256(text.encode()).hexdigest() for relative, text in sources.items()},
              "periods": periods, "investment_eligible": False, "production_ranking_changed": False,
              "limitations": reference["limitations"] + [protocol["interpretation"],
                  "Cross-sectional rows,overlapping training labels and evaluation dates are dependent,not independent trades",
                  "Predicted excess>0 does not imply absolute profit; positive filter uses a fixed20bp proxy,not real fund costs"]}
    with args.output.open("xb") as stream:
        with gzip.open(stream, "wt", encoding="utf-8") as compressed:
            json.dump(report, compressed, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"output": str(args.output)}), flush=True)


if __name__ == "__main__":
    main()
