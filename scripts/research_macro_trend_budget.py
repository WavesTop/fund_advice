"""Test dated LPR budget overlays against matched passive and price-only controls."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

from backend.analysis.fund_allocation import simulate_allocations
from backend.analysis.macro_trend_budget import LPRSeries, POLICIES, macro_budget_target
from backend.analysis.trend_risk_budget import budget_statistics
from backend.core.trading_calendar import TradingCalendar
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, fixed_cohorts, load_funds, rolling_summary
from scripts.research_local_trend_models import prepare_local
from scripts.research_trend_risk_budget import EXECUTION
from scripts.collect_lpr_announcements import parse_announcement
from scripts.search_fund_allocations import metrics
from scripts.validate_fund_allocation import validate_allocation

MACRO = ROOT / "runtime/verification/lpr-announcements-20261013/input.json"
PROTOCOL = ROOT / "config/fund-macro-trend-protocol-v1.json"


def audit_macro_decision(entry, rows, policy):
    state = entry["macro"]
    if state["signal_date"] != entry["signal_date"] or state["lookback_natural_days"] != 90:
        raise ValueError("Macro decision date/lookback mismatch")
    day = date.fromisoformat(entry["signal_date"])
    lag = state["lag_days"]
    if not isinstance(lag, int) or isinstance(lag, bool) or lag < 1:
        raise ValueError("Invalid audited publication lag")
    selected = []
    for cutoff in (day, day - timedelta(days=90)):
        visible = []
        for row in rows:
            announced = date.fromisoformat(row["announcement_date"])
            published = datetime.fromisoformat(row["displayed_published_at"]).date()
            if max(announced, published) + timedelta(days=lag) <= cutoff:
                visible.append(row)
        selected.append(max(visible, key=lambda r: r["announcement_date"], default=None))
    for key, row in zip(("current", "prior"), selected):
        expected = None if row is None else {"announcement_date": row["announcement_date"],
                                            "visible_date": max(row["announcement_date"], row["displayed_published_at"][:10]),
                                            "level_pct": (row["lpr_1y_pct"] + row["lpr_5y_pct"]) / 2,
                                            "source_url": row["source_url"]}
        if state[key] != expected:
            raise ValueError("Macro decision used an unavailable or different rate quote")
    ready = all(row is not None for row in selected)
    change = ((selected[0]["lpr_1y_pct"] + selected[0]["lpr_5y_pct"] - selected[1]["lpr_1y_pct"] - selected[1]["lpr_5y_pct"]) / 2
              if ready else None)
    easing = ready and change < -1e-10
    if state["status"] != ("ready" if ready else "insufficient_history") or state["recent_easing"] != easing:
        raise ValueError("Macro regime cannot be rebuilt from dated original quotes")
    recorded = state["change_percentage_points"]
    if (change is None) != (recorded is None) or (change is not None and (not math.isfinite(recorded) or abs(change - recorded) > 1e-10)):
        raise ValueError("Macro rate change was altered")
    expected_budget = .4
    if policy in ("static50_vol10", "static30_vol10"):
        expected_budget = .5 if policy == "static50_vol10" else .3
    elif policy == "quality_vol10":
        expected_budget = .35 + .25 * entry["quality"]
    elif easing:
        if policy == "easing_vol10" or (policy == "easing_confirmed_vol10" and entry["quality"] > 0):
            expected_budget = .5
        elif policy == "easing_caution_vol10" and entry["quality"] < 0:
            expected_budget = .3
    if policy not in POLICIES or not math.isfinite(entry["proposed_stock_budget"]) or abs(entry["proposed_stock_budget"] - expected_budget) > 1e-12:
        raise ValueError("Macro policy proposal differs from its declared rule")


def replay(data, calendar, policy, rows, *, start, end=None, phase=0, fee=Decimal(10), lag_days=1, execution=EXECUTION, detail=True):
    trace = []

    def allocate(index, held):
        targets, meta = macro_budget_target(data, policy, index, held, data["macro_states"][lag_days][index],
                                           stats=data["risk_stats"][index])
        trace.append(meta | {"targets": targets.weights})
        return targets

    account = simulate_allocations(data, calendar, allocate, start=start, end=end, phase=phase, fee_bps=fee, execution=execution)
    check = validate_allocation(account, data, calendar)
    indexes = {d: i for i, d in enumerate(data["days"])}
    for entry in trace:
        audit_macro_decision(entry, rows, policy)
        index = indexes[entry["signal_date"]]
        codes = list(data["prices"])
        daily = np.asarray([[np.log(data["marks"][c][i] / data["marks"][c][i - 1])
                             for i in range(index - 59, index + 1)] for c in codes])
        target_returns = np.asarray([entry["targets"].get(c, 0) for c in codes]) @ daily
        rebuilt = float(np.std(target_returns) * np.sqrt(252))
        if not math.isfinite(entry["projected_volatility"]) or abs(rebuilt - entry["projected_volatility"]) > 1e-10 or rebuilt > .10 + 1e-10:
            raise ValueError("Forecast risk fails independent raw-return reconstruction")
    result = {"metrics": metrics(account), "ledger_validation": check,
              "information_validation": {"status": "passed", "decisions": len(trace),
                                         "easing_decisions": sum(t["macro"]["recent_easing"] for t in trace),
                                         "unknown_decisions": sum(t["macro"]["status"] != "ready" for t in trace)},
              "risk_validation": {"status": "passed", "decisions": len(trace)}}
    if detail:
        result.update(account=account, signal_trace=trace)
    return result


def run():
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    macro = json.loads(MACRO.read_text())
    rows = macro["rows"]
    for raw in macro["raw"]:
        path = MACRO.parent / raw["path"]
        if sha256(path.read_bytes()).hexdigest() != raw["sha256"]:
            raise ValueError("LPR original archive checksum mismatch")
    originals = {r["url"]: MACRO.parent / r["path"] for r in macro["raw"]}
    for row in rows:
        if parse_announcement(originals[row["source_url"]].read_bytes(), row["source_url"]) != row:
            raise ValueError("LPR parsed quote differs from its original archived page")
    series = LPRSeries(rows)
    funds, _ = load_funds(INPUT, calendar)
    groups = fixed_cohorts(funds)
    bonds, _ = load_funds(BONDS, calendar)
    if set(bonds) != {"000016", "006669", "009803"}:
        raise ValueError("Defensive universe incomplete")
    funds.update(bonds)
    definitions = {"all13": groups["all_13_themes"] + list(bonds), "older9": groups["themes_available_2020"] + ["000016"],
                   "older4": groups["themes_available_2015"] + ["000016"], "long4": ["000217", "000051", "000008", "000016"]}
    results = {}
    for name, codes in definitions.items():
        data = prepare_local(funds, codes, calendar)
        data["macro_states"] = {lag: {i: series.state(data["days"][i], lag_days=lag) for i in range(120, len(data["days"]))}
                                for lag in (1, 7, 30)}
        start = next(i for i in range(120, len(data["days"])) if data["macro_states"][1][i]["status"] == "ready")
        data["risk_stats"] = {i: budget_statistics(data, i) for i in range(start, len(data["days"]))}
        runs, annual, rolling, stress = {}, {}, {}, {}
        for policy in POLICIES:
            runs[policy] = replay(data, calendar, policy, rows, start=start)
            for year in sorted({d[:4] for d in data["days"]}):
                indexes = [i for i, d in enumerate(data["days"]) if d.startswith(year)]
                if indexes[0] < start or len(indexes) < 20:
                    continue
                annual.setdefault(year, {})[policy] = replay(data, calendar, policy, rows, start=indexes[0], end=indexes[-1], detail=False)
            stress[policy] = {f"publication_lag{lag}": replay(data, calendar, policy, rows, start=start, lag_days=lag, detail=False)
                              for lag in (7, 30)}
            if name == "all13":
                rolling[policy] = {}
                for horizon in (20, 60, 120):
                    windows, checks = [], []
                    for entry in range(start, len(data["days"]) - horizon, horizon):
                        result = replay(data, calendar, policy, rows, start=entry, end=entry + horizon, detail=False,
                                        execution=replace(EXECUTION, deadline_entry_guard_days=7))
                        windows.append({"start_date": result["metrics"]["start_date"], "end_date": result["metrics"]["end_date"],
                                        "return_pct": result["metrics"]["total_return_pct"], "drawdown_pct": result["metrics"]["max_drawdown_pct"]})
                        checks.append({"ledger": result["ledger_validation"], "information": result["information_validation"],
                                       "risk": result["risk_validation"]})
                    rolling[policy][str(horizon)] = {"summary": rolling_summary(windows), "windows": windows, "validation": checks}
                scenarios = [(f"fee{f}", Decimal(f), 0, EXECUTION) for f in (50, 100)]
                scenarios += [(f"phase{p}", Decimal(10), p, EXECUTION) for p in (2, 5, 7)]
                scenarios += [("execution_lag2", Decimal(10), 0, replace(EXECUTION, execution_lag_sessions=2))]
                stress[policy].update({label: replay(data, calendar, policy, rows, start=start, fee=fee, phase=p, execution=execution, detail=False)
                                       for label, fee, p, execution in scenarios})
        results[name] = {"codes": codes, "account_start_sessions": start, "runs": runs, "annual": annual, "rolling": rolling, "stress": stress}
        print(json.dumps({"cohort": name, "metrics": {p: [round(r["metrics"]["total_return_pct"], 3),
                            round(r["metrics"]["max_drawdown_pct"], 3)] for p, r in runs.items()}}, ensure_ascii=False), flush=True)
    paths = ("scripts/research_macro_trend_budget.py", "scripts/collect_lpr_announcements.py", "backend/analysis/macro_trend_budget.py",
             "backend/analysis/trend_risk_budget.py", "backend/analysis/fund_allocation.py", "backend/analysis/fund_holding_costs.py",
             "backend/analysis/sector_trend.py", "backend/analysis/strategy_risk.py", "backend/analysis/matured_trend_forecast.py",
             "scripts/research_local_trend_models.py", "scripts/research_calibrated_trends.py", "scripts/research_defensive_allocations.py",
             "scripts/research_fund_rotation.py", "scripts/validate_fund_allocation.py", "scripts/search_fund_allocations.py", "backend/core/trading_calendar.py")
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in paths}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "macro_input": macro,
            "protocol": json.loads(PROTOCOL.read_text()), "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(),
            "input_sha256": sha256(INPUT.read_bytes()).hexdigest(), "bond_input_sha256": sha256(BONDS.read_bytes()).hexdigest(),
            "calendar_sha256": sha256(CALENDAR.read_bytes()).hexdigest(), "macro_input_sha256": sha256(MACRO.read_bytes()).hexdigest(),
            "source_hashes": {p: sha256(s.encode("utf-8")).hexdigest() for p, s in snapshots.items()}, "source_snapshots": snapshots}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/fund-macro-trend-budget-controlled-v2.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8", compresslevel=6) as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
