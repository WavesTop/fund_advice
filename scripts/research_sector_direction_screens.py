"""Separate current direction eligibility from three-horizon model ordering."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path

import numpy as np

from backend.analysis.sector_direction_screen import DIRECTION_FIELDS, GATES, SORTS, gate_members, screened_choices
from backend.analysis.sector_price_validation import summarize
from scripts.benchmark_sector_trend import rank_at
from scripts.research_sector_forecasts import labels
from scripts.research_sector_horizons import PERIODS
from scripts.research_sector_rank_risk import comparison

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "config/sector-direction-screen-research-v1.json"
OLD = ("return_top10", "rank_top10", "maximin_top10", "low_vol_top10", "upward_quality")
NEW = tuple(f"{gate}_{sort}" for gate in GATES for sort in SORTS) + tuple(f"{gate}_count_return" for gate in GATES)
METHODS = (*OLD, *NEW)
QUALITY_FIELDS = ("selected_above_entry_occupancy_pct", "selected_signed_efficiency", "selected_persistent_up_fraction_pct")


def future_quality(boards, days, first, window):
    grid = days[first:first + window + 1]
    if len(grid) != window + 1 or any(any(day not in board["prices"] for day in grid) for board in boards):
        raise ValueError("Future direction labels require the full outcome grid")
    paths = np.asarray([[board["prices"][day] for day in grid] for board in boards])
    if not np.isfinite(paths).all() or (paths <= 0).any():
        raise ValueError("Invalid future direction price")
    # Entry itself is excluded; equal closes contribute half an observation.
    occupancy = ((paths[:, 1:] > paths[:, :1]) + .5 * (paths[:, 1:] == paths[:, :1])).mean(axis=1)
    logs = np.log(paths / paths[:, :1])
    variation = np.abs(np.diff(logs, axis=1)).sum(axis=1)
    efficiency = np.divide(logs[:, -1], variation, out=np.zeros(len(boards)), where=variation > 1e-15)
    persistent = (paths[:, -1] > paths[:, 0]) & (occupancy > .5)
    return {board["code"]: {"occupancy": float(occupancy[i]), "efficiency": float(efficiency[i]),
                              "persistent": bool(persistent[i])} for i, board in enumerate(boards)}


def attach_quality(cases, outcomes):
    for case in cases.values():
        if case["status"] != "matured":
            continue
        points = [outcomes[code] for code in case["selected_codes"]]
        case.update(selected_above_entry_occupancy_pct=float(np.mean([point["occupancy"] for point in points])) * 100,
                    selected_signed_efficiency=float(np.mean([point["efficiency"] for point in points])),
                    selected_persistent_up_fraction_pct=float(np.mean([point["persistent"] for point in points])) * 100)


def summary(cases, costs):
    comparisons = [(f"{gate}_{sort}_vs_same_gate_return", f"{gate}_{sort}", f"{gate}_return")
                   for gate in GATES for sort in ("rank", "maximin", "low_vol")]
    comparisons += [(f"{gate}_return_vs_count_return", f"{gate}_return", f"{gate}_count_return") for gate in GATES]
    comparisons += [(f"{gate}_maximin_vs_old_maximin", f"{gate}_maximin", "maximin_top10") for gate in GATES]
    result = {"scheduled_dates": len(cases), "methods": {}, "paired": {}}
    for method in METHODS:
        values = [case["methods"][method] for case in cases]
        mature = [value for value in values if value["status"] == "matured"]
        result["methods"][method] = {**summarize(values, costs),
            **{field: float(np.mean([value[field] for value in mature])) if mature else None for field in QUALITY_FIELDS}}
    for name, left, right in comparisons:
        paired = [(case["methods"][left], case["methods"][right]) for case in cases
                  if case["methods"][left]["status"] == case["methods"][right]["status"] == "matured"]
        result["paired"][name] = {**comparison(cases, left, right),
            **{f"mean_delta_{field}": float(np.mean([a[field] - b[field] for a, b in paired])) if paired else None
               for field in QUALITY_FIELDS}}
    return result


def diagnostic(entries, predictions, methods):
    by_code = {entry["code"]: entry for entry in entries}
    gates = gate_members(entries)
    old_eligible = {entry["code"] for entry in entries if entry["return_pct"] > 0 and entry["recent_fitted_return_pct"] > 0}
    predicted_positive = {code for code, value in predictions.items() if value["return_rank"] > 0 and value["safety_rank"] > 0}
    return {"pool_state_counts": dict(Counter(entry["state"] for entry in entries)),
            "positive_observed_return_count": sum(entry["return_pct"] > 0 for entry in entries),
            "old_price_gate_count": len(old_eligible), "old_model_gate_count": len(predicted_positive),
            "old_joint_gate_count": len(old_eligible & predicted_positive),
            "old_price_only_codes": sorted(old_eligible - predicted_positive),
            "gate_codes": gates,
            "selected_state_counts": {method: dict(Counter(by_code[code]["state"] for code in value["selected_codes"]))
                                      for method, value in methods.items()}}


def run(reference, costs):
    boards, days = reference["input"]["boards"], reference["input"]["days"]
    indexes = {day: i for i, day in enumerate(days)}
    episodes = json.loads(reference["source_snapshots"]["config/market-regime-strategy-research-v1.json"])["episodes"]
    output = {}
    for period, window in PERIODS.items():
        cases = []
        for at, old in enumerate(reference["periods"][period]["cases"]):
            original = old["methods"]["return_top10"]
            index = indexes[original["signal_date"]]
            entries = rank_at(boards, days, index, window)
            full = len(entries) == len(boards)
            fitted = full and old["learning"]["status"] == "fitted"
            row = {"primary": old["primary"], "secondary": old["secondary"],
                   "as_of": original["signal_date"], "learning_status": old["learning"]["status"],
                   "methods": {method: dict(old["methods"][method]) for method in OLD}, "diagnostic": None}
            if fitted:
                if old["fit_validation"]["status"] != "passed":
                    raise ValueError("Archived sector model fit audit incomplete")
                gates, selected = screened_choices(entries, old["predictions"])
                row["methods"].update(labels(boards, days, window, original, selected))
                row["diagnostic"] = diagnostic(entries, old["predictions"], row["methods"])
                if any(len(selected[f"{gate}_{sort}"]) != min(10, len(gates[gate])) for gate in GATES for sort in SORTS):
                    raise ValueError("Same gate controls have unequal candidate counts")
            else:
                row["methods"].update({method: {**{key: original[key] for key in
                    ("signal_date", "entry_date", "exit_date", "eligible_count", "pool_count")},
                    "status": "not_fitted" if full else "signal_incomplete", "selected_codes": []} for method in NEW})
            if full and original["exit_date"] is not None:
                outcomes = future_quality(boards, days, index + 1, window)
                attach_quality(row["methods"], outcomes)
            cases.append(row)
            if at and at % 100 == 0:
                print(json.dumps({"period": period, "evaluated_dates": at + 1}), flush=True)
        grouped = {}
        for episode in episodes:
            eligible = [row for row in cases if row["primary"] and row["as_of"] >= episode["start"]
                        and row["methods"]["return_top10"]["exit_date"] is not None
                        and row["methods"]["return_top10"]["exit_date"] <= episode["end"]]
            prior = reference["periods"][period]["episodes"][episode["id"]]
            grouped[episode["id"]] = {**{key: value for key, value in prior.items() if key != "summary"},
                                      "summary": summary(eligible, costs)}
        latest = next(row for row in reversed(cases) if row["secondary"] and row["diagnostic"] is not None)
        current = rank_at(boards, days, indexes[latest["as_of"]], window, current=True)
        output[period] = {"lookback_sessions": window, "forward_sessions": window,
                          "primary": summary([row for row in cases if row["primary"]], costs),
                          "secondary": summary([row for row in cases if row["secondary"]], costs),
                          "episodes": grouped, "cases": cases,
                          "latest": {"as_of": latest["as_of"], "diagnostic": latest["diagnostic"],
                                     "methods": {key: value["selected_codes"] for key, value in latest["methods"].items()},
                                     "entries": current}}
        print(json.dumps({"period": period, "primary": output[period]["primary"]["paired"]}), flush=True)
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/sector-direction-screen-research-v1.json.gz")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Preserve prior direction-screen study")
    protocol = json.loads(PROTOCOL.read_text())
    if (protocol["periods"] != PERIODS or protocol["top_n"] != 10 or list(protocol["gates"]) != list(GATES)
            or protocol["sorts"] != list(SORTS) or protocol["cost_stress_bps"] != [0, 20, 50, 100]
            or protocol["controls"] != [*OLD, "direction_count_return", "state_count_return"]):
        raise ValueError("Frozen direction-screen protocol differs")
    source = ROOT / protocol["reference"]
    if sha256(source.read_bytes()).hexdigest() != protocol["reference_sha256"]:
        raise ValueError("Archived sector ranking study changed")
    with gzip.open(source, "rt", encoding="utf-8") as stream:
        reference = json.load(stream)
    for relative, expected in reference["source_hashes"].items():
        if sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Sector calculation dependency changed: {relative}")
    paths = set(reference["source_hashes"]) | {str(PROTOCOL.relative_to(ROOT)),
        "backend/analysis/sector_direction_screen.py", "scripts/research_sector_direction_screens.py"}
    sources = {path: (ROOT / path).read_text() for path in paths}
    result = run(reference, protocol["cost_stress_bps"])
    if any((ROOT / path).read_text() != text for path, text in sources.items()):
        raise ValueError("Direction-screen sources changed during study")
    report = {"version": protocol["version"], "generated_at": datetime.now(timezone.utc).isoformat(),
              "protocol": protocol, "input": reference["input"], "input_sha256": reference["input_sha256"],
              "calendar": reference["calendar"], "coverage": reference["coverage"], "periods": result,
              "source_snapshots": sources, "source_hashes": {key: sha256(value.encode()).hexdigest() for key, value in sources.items()},
              "investment_eligible": False, "production_ranking_changed": False,
              "limitations": reference["limitations"] + [protocol["scope"], protocol["selection_history"],
                  "Added future path quality labels are retrospective and never used to select current candidates",
                  "Gate-only controls use same full-pool fitted dates;pair summaries still condition on nonempty mature lists",
                  "Fixed cost haircuts do not simulate orders,cash,settlement,contracts or real fund returns"]}
    with args.output.open("xb") as stream:
        with gzip.open(stream, "wt", encoding="utf-8") as compressed:
            json.dump(report, compressed, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output), "sha256": sha256(args.output.read_bytes()).hexdigest()}), flush=True)


if __name__ == "__main__":
    main()
