"""Learn forward path persistence separately for sector opportunity horizons."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path

import numpy as np

from backend.analysis.sector_continuation import continuation_choices
from backend.analysis.sector_forecast import audit_fit, features, fit_period
from backend.analysis.sector_price_validation import summarize
from scripts.benchmark_sector_trend import rank_at
from scripts.research_sector_direction_screens import QUALITY_FIELDS, attach_quality, future_quality
from scripts.research_sector_forecasts import labels, rank_ic
from scripts.research_sector_horizons import PERIODS
from scripts.research_sector_rank_risk import comparison

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "config/sector-continuation-research-v1.json"
OLD = ("return_top10", "maximin_top10", "direction_return", "direction_low_vol", "state_return", "state_maximin", "state_low_vol")
NEW = ("continuation_top10", "direction_continuation", "state_continuation", "state_historical_continuation",
       "state_positive_continuation", "positive_count_state_return", "positive_count_state_low_vol", "positive_count_universe_return")
METHODS = (*OLD, *NEW)
ERRORS = ("persistent_rmse", "historical_persistent_rmse", "efficiency_rmse", "historical_efficiency_rmse",
          "persistent_rank_ic", "efficiency_rank_ic")


def retarget(records, boards, days, window):
    indexes = {day: index for index, day in enumerate(days)}
    result = []
    for record in records:
        first = indexes[record["signal_date"]] + 1
        quality = future_quality(boards, days, first, window)
        result.append({**record, "outcomes": [[float(quality[code]["persistent"]) - .5, quality[code]["efficiency"]]
                                             for code in record["codes"]]})
    return result


def audit_targets(records, original, boards, days, window):
    if len(records) != len(original):
        raise ValueError("Continuation training dates differ")
    indexes = {day: index for index, day in enumerate(days)}
    codes = [board["code"] for board in boards]
    for record, previous in zip(records, original):
        if ({key: value for key, value in record.items() if key != "outcomes"}
                != {key: value for key, value in previous.items() if key != "outcomes"}):
            raise ValueError("Continuation retargeting changed past features or availability")
        index = indexes[record["signal_date"]]
        if (record["codes"] != codes or (index - window) % 10
                or record["exit_date"] != days[index + window + 1]
                or record["ready_date"] != days[index + window + 2]):
            raise ValueError("Continuation label maturity or calendar grid differs")
        expected = []
        for board in boards:
            prices = [Decimal(str(board["prices"][day])) for day in days[index + 1:index + window + 2]]
            if len(prices) != window + 1 or any(not price.is_finite() or price <= 0 for price in prices):
                raise ValueError("Continuation label prices are incomplete")
            above = sum(2 if price > prices[0] else 1 if price == prices[0] else 0 for price in prices[1:])
            persistent = prices[-1] > prices[0] and above > window
            variation = math.fsum(abs(math.log(float(right / left))) for left, right in zip(prices, prices[1:]))
            efficiency = math.log(float(prices[-1] / prices[0])) / variation if variation > 1e-15 else 0.
            expected.append([float(persistent) - .5, efficiency])
        if not np.allclose(record["outcomes"], expected, rtol=0, atol=1e-12):
            raise ValueError("Continuation targets differ from independent price paths")
    return {"status": "passed", "dates": len(records), "observations": len(records) * len(codes)}


def summary(cases, costs):
    pairs = [("continuation_vs_old_maximin", "continuation_top10", "maximin_top10"),
             ("direction_vs_same_gate_return", "direction_continuation", "direction_return"),
             ("direction_vs_same_gate_low_vol", "direction_continuation", "direction_low_vol"),
             ("state_vs_same_gate_return", "state_continuation", "state_return"),
             ("state_vs_same_gate_low_vol", "state_continuation", "state_low_vol"),
             ("state_vs_same_gate_old_maximin", "state_continuation", "state_maximin"),
             ("state_vs_historical", "state_continuation", "state_historical_continuation"),
             ("positive_vs_count_state_return", "state_positive_continuation", "positive_count_state_return"),
             ("positive_vs_count_state_low_vol", "state_positive_continuation", "positive_count_state_low_vol"),
             ("positive_vs_count_universe_return", "state_positive_continuation", "positive_count_universe_return")]
    result = {"scheduled_dates": len(cases), "methods": {}, "paired": {}}
    for method in METHODS:
        values = [case["methods"][method] for case in cases]
        mature = [row for row in values if row["status"] == "matured"]
        result["methods"][method] = {**summarize(values, costs),
            **{field: float(np.mean([row[field] for row in mature])) if mature else None for field in QUALITY_FIELDS}}
    for name, left, right in pairs:
        pairs_at = [(case["methods"][left], case["methods"][right]) for case in cases
                    if case["methods"][left]["status"] == case["methods"][right]["status"] == "matured"]
        result["paired"][name] = {**comparison(cases, left, right),
            **{f"mean_delta_{field}": float(np.mean([a[field] - b[field] for a, b in pairs_at])) if pairs_at else None
               for field in QUALITY_FIELDS}}
    errors = [case["forecast_errors"] for case in cases if case["forecast_errors"] is not None]
    result["forecast_errors"] = {"dates": len(errors),
        **{field: float(np.mean([row[field] for row in errors if row[field] is not None]))
           if any(row[field] is not None for row in errors) else None for field in ERRORS}}
    return result


def run(reference, training, costs):
    boards, days = reference["input"]["boards"], reference["input"]["days"]
    if reference["input"] != training["input"] or reference["input_sha256"] != training["input_sha256"]:
        raise ValueError("Continuation data inputs differ")
    indexes, codes = {day: i for i, day in enumerate(days)}, [board["code"] for board in boards]
    episodes = json.loads(reference["source_snapshots"]["config/market-regime-strategy-research-v1.json"])["episodes"]
    output = {}
    for period, window in PERIODS.items():
        previous = training["periods"][period]
        records = retarget(previous["training_records"], boards, days, window)
        record_validation = audit_targets(records, previous["training_records"], boards, days, window)
        cases = []
        if len(reference["periods"][period]["cases"]) != len(previous["cases"]):
            raise ValueError("Continuation evaluation schedules differ")
        for at, (old, prior) in enumerate(zip(reference["periods"][period]["cases"], previous["cases"])):
            original = old["methods"]["return_top10"]
            as_of, index = original["signal_date"], indexes[original["signal_date"]]
            entries = rank_at(boards, days, index, window)
            model, historical, metadata = fit_period(records, as_of, codes)
            if any(metadata[key] != prior["learning"][key] for key in ("as_of", "status", "training_dates", "distinct_dates")):
                raise ValueError("Continuation model changed original training availability")
            validation = audit_fit(records, metadata, codes)
            row = {"primary": old["primary"], "secondary": old["secondary"], "as_of": as_of,
                   "learning": metadata, "fit_validation": validation, "forecast_errors": None,
                   "methods": {key: old["methods"][key] for key in OLD}}
            if model is None or len(entries) != len(codes):
                row["methods"].update({key: {**{field: original[field] for field in
                    ("signal_date", "entry_date", "exit_date", "eligible_count", "pool_count")},
                    "status": "not_fitted" if len(entries) == len(codes) else "signal_incomplete", "selected_codes": []} for key in NEW})
            else:
                mapped = {entry["code"]: entry for entry in entries}
                matrix = np.asarray([features(mapped[code]) for code in codes])
                predicted = model.predict(matrix)
                rebuilt = np.clip((matrix - metadata["means"]) / np.asarray(metadata["scales"]), -5, 5)
                rebuilt = rebuilt @ metadata["coefficients"] + metadata["intercept"]
                if not np.allclose(predicted, rebuilt, rtol=0, atol=1e-12):
                    raise ValueError("Continuation predictions differ from audited fit")
                forecasts = {code: value.tolist() for code, value in zip(codes, predicted)}
                history = {code: value.tolist() for code, value in zip(codes, historical)}
                chosen = continuation_choices(entries, forecasts, history)
                row["methods"].update(labels(boards, days, window, original, chosen))
                row.update(predictions=forecasts, historical_predictions=history,
                           selected_state_counts={method: dict(Counter(mapped[code]["state"] for code in selected)) for method, selected in chosen.items()})
                if original["exit_date"] is not None:
                    quality = future_quality(boards, days, index + 1, window)
                    attach_quality(row["methods"], quality)
                    actual = np.asarray([[float(quality[code]["persistent"]) - .5, quality[code]["efficiency"]] for code in codes])
                    row["forecast_errors"] = {"persistent_rmse": float(np.sqrt(np.mean((predicted[:, 0] - actual[:, 0]) ** 2))),
                        "historical_persistent_rmse": float(np.sqrt(np.mean((historical[:, 0] - actual[:, 0]) ** 2))),
                        "efficiency_rmse": float(np.sqrt(np.mean((predicted[:, 1] - actual[:, 1]) ** 2))),
                        "historical_efficiency_rmse": float(np.sqrt(np.mean((historical[:, 1] - actual[:, 1]) ** 2))),
                        "persistent_rank_ic": rank_ic(predicted[:, 0].tolist(), actual[:, 0].tolist()),
                        "efficiency_rank_ic": rank_ic(predicted[:, 1].tolist(), actual[:, 1].tolist())}
            cases.append(row)
            if at and at % 100 == 0:
                print(json.dumps({"period": period, "evaluated_dates": at + 1}), flush=True)
        grouped = {}
        for episode in episodes:
            eligible = [row for row in cases if row["primary"] and row["as_of"] >= episode["start"]
                        and row["methods"]["return_top10"]["exit_date"] is not None
                        and row["methods"]["return_top10"]["exit_date"] <= episode["end"]]
            grouped[episode["id"]] = {**{key: value for key, value in reference["periods"][period]["episodes"][episode["id"]].items() if key != "summary"},
                                      "summary": summary(eligible, costs)}
        latest = next(row for row in reversed(cases) if row["secondary"] and "predictions" in row)
        output[period] = {"lookback_sessions": window, "forward_sessions": window, "training_records": records,
                          "record_validation": record_validation, "cases": cases, "episodes": grouped,
                          "primary": summary([row for row in cases if row["primary"]], costs),
                          "secondary": summary([row for row in cases if row["secondary"]], costs),
                          "latest": {"as_of": latest["as_of"], "predictions": latest["predictions"],
                                     "methods": {key: value["selected_codes"] for key, value in latest["methods"].items()},
                                     "selected_state_counts": latest["selected_state_counts"]}}
        print(json.dumps({"period": period, "state_vs_return_primary": output[period]["primary"]["paired"]["state_vs_same_gate_return"],
                          "state_vs_return_secondary": output[period]["secondary"]["paired"]["state_vs_same_gate_return"]}), flush=True)
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/sector-continuation-research-v1.json.gz")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Preserve prior continuation study")
    protocol = json.loads(PROTOCOL.read_text())
    if (protocol["periods"] != PERIODS or protocol["top_n"] != 10 or protocol["controls"] != list(OLD)
            or protocol["new_methods"] != list(NEW) or protocol["cost_stress_bps"] != [0, 20, 50, 100]
            or protocol["targets"] != ["positive_terminal_and_majority_above_entry_minus_half", "signed_log_path_efficiency"]):
        raise ValueError("Registered continuation rules differ")
    reports = []
    for key in ("reference", "training_reference"):
        path = ROOT / protocol[key]
        if sha256(path.read_bytes()).hexdigest() != protocol[f"{key}_sha256"]:
            raise ValueError("Continuation source report digest differs")
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            reports.append(json.load(stream))
    reference, training = reports
    for relative, expected in reference["source_hashes"].items():
        if sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Continuation source dependency differs: {relative}")
    paths = set(reference["source_hashes"]) | {str(PROTOCOL.relative_to(ROOT)),
        "backend/analysis/sector_continuation.py", "scripts/research_sector_continuation.py"}
    sources = {path: (ROOT / path).read_text() for path in paths}
    result = run(reference, training, protocol["cost_stress_bps"])
    if any((ROOT / path).read_text() != text for path, text in sources.items()):
        raise ValueError("Continuation sources changed during study")
    report = {"version": protocol["version"], "generated_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol,
              "input": reference["input"], "input_sha256": reference["input_sha256"], "calendar": reference["calendar"],
              "coverage": reference["coverage"], "periods": result, "source_snapshots": sources,
              "source_hashes": {key: sha256(value.encode()).hexdigest() for key, value in sources.items()},
              "investment_eligible": False, "production_ranking_changed": False,
              "limitations": reference["limitations"] + [protocol["scope"], protocol["selection_history"],
                  "Linear binary-label forecasts are uncalibrated scores,not probabilities or future win rates",
                  "Persistence target uses gross prices,not fees or an absolute portfolio drawdown limit"]}
    with args.output.open("xb") as stream:
        with gzip.open(stream, "wt", encoding="utf-8") as compressed:
            json.dump(report, compressed, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output), "sha256": sha256(args.output.read_bytes()).hexdigest()}), flush=True)


if __name__ == "__main__":
    main()
