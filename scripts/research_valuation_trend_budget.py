"""Conditional valuation/trend portfolios plus corrected-calendar controls."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime, timezone
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
from backend.analysis.trend_risk_budget import TrendBudgetPolicy, budget_statistics
from backend.analysis.valuation_trend_budget import POLICIES, ValuationHistory, valuation_budget_target
from backend.core.trading_calendar import TradingCalendar
from scripts.collect_index_valuation import parse_history
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import INPUT, fixed_cohorts, load_funds, rolling_summary
from scripts.research_local_trend_models import prepare_local
from scripts.research_trend_risk_budget import EXECUTION, replay as replay_price_budget
from scripts.search_fund_allocations import metrics
from scripts.validate_fund_allocation import validate_allocation

CALENDAR = ROOT / "config/research-exchange-calendar-2009-2026-v2.json"
OLD_CALENDAR = ROOT / "config/research-exchange-calendar-2009-2026.json"
VALUATION = ROOT / "runtime/verification/index-valuation-20261013/input.json"
EVIDENCE = VALUATION.parent / "semantic-evidence.json"
PROTOCOL = ROOT / "config/fund-valuation-trend-protocol-v1.json"
PREVIOUS = ROOT / "runtime/verification/trend-risk-budget-extended-v3.json.gz"


def audit_valuation_decision(entry, raw_maps, market_days, policy):
    state = entry["valuation"]
    if state["signal_date"] != entry["signal_date"] or policy not in POLICIES:
        raise ValueError("Valuation policy or signal date differs")
    lag = state["lag_sessions"]
    if not isinstance(lag, int) or isinstance(lag, bool) or lag < 1:
        raise ValueError("Invalid audited valuation lag")
    signal_index = market_days.index(entry["signal_date"])
    known = signal_index - lag
    percentiles = []
    for code, source in raw_maps.items():
        chosen = None
        for i in range(max(0, known - 3), known + 1):
            value = source.get(market_days[i])
            if value is not None:
                chosen = i
        expected = {"status": "unavailable", "quote_date": None, "value": None, "percentile": None,
                    "reference_count": 0, "reference_start": None, "reference_end": None, "coverage_fraction": None}
        if chosen is not None:
            prior_days = market_days[max(0, chosen - 1260):chosen]
            first = next((i for i, day in enumerate(prior_days) if source.get(day) is not None), len(prior_days))
            reference_days = prior_days[first:]
            reference = [source[day] for day in reference_days if source.get(day) is not None]
            fraction = len(reference) / len(reference_days) if reference_days else 0.
            value = source[market_days[chosen]]
            expected.update(status="insufficient_reference", quote_date=market_days[chosen], value=value,
                            reference_count=len(reference), reference_start=reference_days[0] if reference_days else None,
                            reference_end=reference_days[-1] if reference_days else None, coverage_fraction=fraction)
            if len(reference) >= 252 and fraction >= .95:
                percentile = (sum(v < value for v in reference) + sum(v <= value for v in reference)) / (2 * len(reference))
                expected.update(status="ready", percentile=percentile)
                percentiles.append(percentile)
        recorded = state["indices"][code]
        if set(recorded) != set(expected):
            raise ValueError("Valuation trace fields differ")
        for key, value in expected.items():
            observed = recorded[key]
            if isinstance(value, float):
                if observed is None or not math.isfinite(observed) or abs(observed - value) > 1e-10:
                    raise ValueError("Valuation trace cannot be reconstructed from past original rows")
            elif observed != value:
                raise ValueError("Valuation trace date/count/status differs")
    ready = len(percentiles) == 2
    expected_percentile = sum(percentiles) / 2 if ready else None
    if state["status"] != ("ready" if ready else "unavailable"):
        raise ValueError("Valuation context readiness was altered")
    if expected_percentile is None:
        if state["percentile"] is not None:
            raise ValueError("Unknown valuation context cannot have a percentile")
    elif not math.isfinite(state["percentile"]) or abs(state["percentile"] - expected_percentile) > 1e-10:
        raise ValueError("Combined valuation percentile was altered")
    quality = .35 + .25 * entry["quality"]
    desired = {"static50_vol10": .5, "static30_vol10": .3, "quality_vol10": quality, "value_hybrid_vol10": quality}.get(policy, .4)
    if ready:
        value_budget = .6 - .4 * expected_percentile
        if policy == "value_vol10":
            desired = value_budget
        elif policy == "value_hybrid_vol10":
            desired = (value_budget + quality) / 2
        elif policy == "value_confirmed_vol10":
            if expected_percentile <= .2 and entry["quality"] > 0:
                desired = .5
            elif expected_percentile >= .8 and entry["quality"] < 0:
                desired = .3
    if not math.isfinite(entry["proposed_stock_budget"]) or abs(desired - entry["proposed_stock_budget"]) > 1e-12:
        raise ValueError("Valuation budget proposal differs from the declared rule")


def replay(data, calendar, history, raw_maps, policy, *, start=120, end=None, phase=0, fee=Decimal(10), lag=1, execution=EXECUTION, detail=True):
    trace = []

    def allocate(index, held):
        targets, meta = valuation_budget_target(data, policy, index, held, data["valuation_states"][lag][index],
                                               stats=data["risk_stats"][index])
        trace.append(meta | {"targets": targets.weights})
        return targets

    account = simulate_allocations(data, calendar, allocate, start=start, end=end, phase=phase, fee_bps=fee, execution=execution)
    check = validate_allocation(account, data, calendar)
    by_date = {d: i for i, d in enumerate(data["days"])}
    for entry in trace:
        audit_valuation_decision(entry, raw_maps, history.days, policy)
        index = by_date[entry["signal_date"]]
        codes = list(data["prices"])
        daily = np.asarray([[np.log(data["marks"][c][i] / data["marks"][c][i - 1])
                             for i in range(index - 59, index + 1)] for c in codes])
        series = np.asarray([entry["targets"].get(c, 0) for c in codes]) @ daily
        volatility = float(np.std(series) * np.sqrt(252))
        if not math.isfinite(entry["projected_volatility"]) or abs(volatility - entry["projected_volatility"]) > 1e-10 or volatility > .1 + 1e-10:
            raise ValueError("Valuation portfolio risk fails raw-return reconstruction")
    result = {"metrics": metrics(account), "ledger_validation": check, "risk_validation": {"status": "passed", "decisions": len(trace)},
              "information_validation": {"status": "passed", "decisions": len(trace),
                                         "unknown_decisions": sum(e["valuation"]["status"] != "ready" for e in trace)}}
    if detail:
        result.update(account=account, signal_trace=trace)
    return result


def verify_input(payload):
    evidence = json.loads(EVIDENCE.read_text())
    if evidence["input_sha256"] != sha256(VALUATION.read_bytes()).hexdigest():
        raise ValueError("Valuation semantic evidence belongs to different input data")
    reconstructed = {code: [] for code in payload["indices"]}
    for entry in payload["raw"]:
        path = VALUATION.parent / entry["path"]
        body = path.read_bytes()
        if sha256(body).hexdigest() != entry["sha256"]:
            raise ValueError("Valuation original archive checksum differs")
        if "/perf/index-perf?" in entry["url"]:
            from urllib.parse import parse_qs, urlparse
            query = parse_qs(urlparse(entry["url"]).query)
            code = query["indexCode"][0]
            first = datetime.strptime(query["startDate"][0], "%Y%m%d").date()
            last = datetime.strptime(query["endDate"][0], "%Y%m%d").date()
            reconstructed[code].extend(parse_history(body, code, first, last))
    for code, rows in reconstructed.items():
        if sorted(rows, key=lambda r: r["date"]) != payload["indices"][code]["rows"]:
            raise ValueError("Valuation input was changed independently of original pages")
        check = evidence["checks"][code]
        if check["status"] != "provisional_dated_factsheet_match_not_complete_historical_method_verification":
            raise ValueError("Unrecognised valuation semantic evidence")
    return evidence


def run():
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    valuation = json.loads(VALUATION.read_text())
    evidence = verify_input(valuation)
    history = ValuationHistory(valuation["indices"], calendar, cutoff=valuation["cutoff"])
    raw_maps = {code: {r["date"]: r["peg"] for r in item["rows"] if r["date"] in history.day_indexes} for code, item in valuation["indices"].items()}
    funds, _ = load_funds(INPUT, calendar)
    groups = fixed_cohorts(funds)
    bonds, _ = load_funds(BONDS, calendar)
    if set(bonds) != {"000016", "006669", "009803"}:
        raise ValueError("Defensive universe incomplete")
    funds.update(bonds)
    definitions = {"all13": groups["all_13_themes"] + list(bonds), "older9": groups["themes_available_2020"] + ["000016"],
                   "older4": groups["themes_available_2015"] + ["000016"], "long4": ["000217", "000051", "000008", "000016"]}
    results, calendar_impact = {}, {}
    previous = json.load(gzip.open(PREVIOUS, "rt"))
    if previous["calendar_sha256"] != sha256(OLD_CALENDAR.read_bytes()).hexdigest():
        raise ValueError("Original calendar changed instead of being preserved")
    for name, codes in definitions.items():
        data = prepare_local(funds, codes, calendar)
        data["risk_stats"] = {i: budget_statistics(data, i) for i in range(120, len(data["days"]))}
        data["valuation_states"] = {lag: {i: history.state(data["days"][i], lag_sessions=lag) for i in range(120, len(data["days"]))}
                                    for lag in (1, 2, 5)}
        runs, annual, rolling, stress = {}, {}, {}, {}
        for policy in POLICIES:
            runs[policy] = replay(data, calendar, history, raw_maps, policy)
            for year in sorted({d[:4] for d in data["days"]}):
                indexes = [i for i, d in enumerate(data["days"]) if d.startswith(year)]
                if indexes[0] < 120 or len(indexes) < 20:
                    continue
                annual.setdefault(year, {})[policy] = replay(data, calendar, history, raw_maps, policy, start=indexes[0], end=indexes[-1], detail=False)
            stress[policy] = {f"valuation_lag{lag}": replay(data, calendar, history, raw_maps, policy, lag=lag, detail=False) for lag in (2, 5)}
            if name == "all13":
                rolling[policy] = {}
                for horizon in (20, 60, 120):
                    windows, checks = [], []
                    for start in range(120, len(data["days"]) - horizon, horizon):
                        item = replay(data, calendar, history, raw_maps, policy, start=start, end=start + horizon, detail=False,
                                      execution=replace(EXECUTION, deadline_entry_guard_days=7))
                        windows.append({"start_date": item["metrics"]["start_date"], "end_date": item["metrics"]["end_date"],
                                        "return_pct": item["metrics"]["total_return_pct"], "drawdown_pct": item["metrics"]["max_drawdown_pct"]})
                        checks.append({"ledger": item["ledger_validation"], "information": item["information_validation"], "risk": item["risk_validation"]})
                    rolling[policy][str(horizon)] = {"summary": rolling_summary(windows), "windows": windows, "validation": checks}
                scenarios = [(f"fee{f}", Decimal(f), 0, EXECUTION) for f in (50, 100)]
                scenarios += [(f"phase{p}", Decimal(10), p, EXECUTION) for p in (2, 5, 7)]
                scenarios += [("execution_lag2", Decimal(10), 0, replace(EXECUTION, execution_lag_sessions=2))]
                stress[policy].update({label: replay(data, calendar, history, raw_maps, policy, fee=fee, phase=p, execution=execution, detail=False)
                                       for label, fee, p, execution in scenarios})
        results[name] = {"codes": codes, "runs": runs, "annual": annual, "rolling": rolling, "stress": stress}
        old_metrics = previous["cohorts"][name]["runs"]["static40_vol10"]["metrics"]
        if name in ("older4", "long4"):
            old_calendar = TradingCalendar(json.loads(OLD_CALENDAR.read_text()))
            old_funds, _ = load_funds(INPUT, old_calendar)
            old_bonds, _ = load_funds(BONDS, old_calendar)
            old_funds.update(old_bonds)
            old_data = prepare_local(old_funds, codes, old_calendar)
            old_data["risk_stats"] = {i: budget_statistics(old_data, i) for i in range(120, len(old_data["days"]))}
            old_run = replay_price_budget(old_data, old_calendar, TrendBudgetPolicy("static40_vol10", "static40", .1), start=120, detail=False)
            if abs(old_run["metrics"]["total_return_pct"] - old_metrics["total_return_pct"]) > 1e-9:
                raise ValueError("Frozen original calendar control cannot be reproduced")
        else:
            old_run = {"metrics": old_metrics, "basis": "archived date-matched original control; no calendar difference within account or signal lookbacks"}
        calendar_impact[name] = {"old_control": old_run, "new_control": runs["static40_vol10"]["metrics"],
                                 "return_difference_pp": runs["static40_vol10"]["metrics"]["total_return_pct"] - old_metrics["total_return_pct"]}
        print(json.dumps({"cohort": name, "metrics": {p: [round(v["metrics"]["total_return_pct"], 3), round(v["metrics"]["max_drawdown_pct"], 3)]
                           for p, v in runs.items()}, "calendar_effect_pp": round(calendar_impact[name]["return_difference_pp"], 4)}), flush=True)
    paths = ("scripts/research_valuation_trend_budget.py", "scripts/collect_index_valuation.py", "scripts/verify_index_valuation.py",
             "backend/analysis/valuation_trend_budget.py", "backend/analysis/trend_risk_budget.py", "backend/analysis/fund_allocation.py",
             "backend/analysis/fund_holding_costs.py", "backend/analysis/sector_trend.py", "backend/analysis/strategy_risk.py",
             "backend/analysis/matured_trend_forecast.py", "scripts/research_local_trend_models.py", "scripts/research_calibrated_trends.py",
             "scripts/research_defensive_allocations.py", "scripts/research_fund_rotation.py", "scripts/validate_fund_allocation.py",
             "scripts/search_fund_allocations.py", "backend/core/trading_calendar.py", "scripts/research_trend_risk_budget.py")
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in paths}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "calendar_impact": calendar_impact,
            "valuation_input": valuation, "semantic_evidence": evidence, "valuation_coverage": history.coverage,
            "protocol": json.loads(PROTOCOL.read_text()), "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(),
            "calendar": json.loads(CALENDAR.read_text()), "calendar_sha256": sha256(CALENDAR.read_bytes()).hexdigest(),
            "previous_calendar": json.loads(OLD_CALENDAR.read_text()), "previous_report_sha256": sha256(PREVIOUS.read_bytes()).hexdigest(),
            "input_sha256": sha256(INPUT.read_bytes()).hexdigest(), "bond_input_sha256": sha256(BONDS.read_bytes()).hexdigest(),
            "valuation_input_sha256": sha256(VALUATION.read_bytes()).hexdigest(), "semantic_evidence_sha256": sha256(EVIDENCE.read_bytes()).hexdigest(),
            "source_hashes": {p: sha256(s.encode("utf-8")).hexdigest() for p, s in snapshots.items()}, "source_snapshots": snapshots}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/fund-valuation-trend-budget-audited-v2.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8", compresslevel=6) as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))


if __name__ == "__main__":
    main()
