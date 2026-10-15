"""Conditional partial-coverage theme valuation/price portfolios, with matched controls."""
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
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

from backend.analysis.fund_allocation import simulate_allocations
from backend.analysis.theme_value_tilt import POLICIES, ThemeValuationHistory, theme_tilt_target
from backend.analysis.trend_risk_budget import budget_statistics
from backend.core.trading_calendar import TradingCalendar
from scripts.collect_index_valuation import parse_history
from scripts.import_related_market import CSI_INFO, _normal, _target_from_benchmark
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, fixed_cohorts, load_funds, rolling_summary
from scripts.research_local_trend_models import prepare_local
from scripts.research_trend_risk_budget import EXECUTION
from scripts.search_fund_allocations import metrics
from scripts.validate_fund_allocation import validate_allocation
from scripts.validate_theme_value import audit_theme_decision

THEMES = ROOT / "runtime/verification/theme-valuations-20261013/input.json"
EVIDENCE = THEMES.parent / "semantic-evidence.json"
ACCESS_STOP = THEMES.parent / "access-stop.json"
PROTOCOL = ROOT / "config/fund-theme-value-protocol-v1.json"
PREVIOUS = ROOT / "runtime/verification/fund-valuation-trend-budget-audited-v2.json.gz"


def verify_input(payload, evidence):
    if payload["fund_input_sha256"] != sha256(INPUT.read_bytes()).hexdigest() or evidence["input_sha256"] != sha256(THEMES.read_bytes()).hexdigest():
        raise ValueError("Theme identity/semantics refer to different source data")
    originals = {}
    for raw in payload["raw"]:
        body = (THEMES.parent / raw["path"]).read_bytes()
        if sha256(body).hexdigest() != raw["sha256"]:
            raise ValueError("Theme archive checksum differs")
        originals[raw["url"]] = body
    funds = {x["code"]: x for x in json.loads(INPUT.read_text())["items"]}
    if len(payload["items"]) != 12 or set(evidence["checks"]) != {x["fund_code"] for x in payload["items"]}:
        raise ValueError("Theme universe or evidence is incomplete")
    for item in payload["items"]:
        fund = funds[item["fund_code"]]
        relation = item["relation"]
        code = relation["index_code"]
        benchmark = fund["source_profile_fields"]["业绩比较基准"]
        identity = _target_from_benchmark(benchmark)
        if identity is None or identity != (relation["target"], relation["benchmark_weight_percent"]) or relation["benchmark"] != benchmark:
            raise ValueError("Theme mapping differs from the original frozen fund benchmark")
        info = json.loads(originals[CSI_INFO + code])["data"]
        if (info["indexCode"] != code or info["indexFullNameCn"] != relation["index_name"]
                or info["publishDate"] != item["index_publication_date"]):
            raise ValueError("Theme official identity/publication was altered")
        aliases = {_normal(info["indexFullNameCn"]), _normal(info["indexShortNameCn"]), _normal(info["indexShortNameCn"] + "指数")}
        if _normal(identity[0]) not in aliases:
            raise ValueError("Theme official index is not the exact named benchmark")
        rebuilt = []
        for raw in item["history_raw"]:
            query = parse_qs(urlparse(raw["url"]).query)
            if query["indexCode"] != [code]:
                raise ValueError("Theme history request belongs to another index")
            start, end = (datetime.strptime(query[key][0], "%Y%m%d").date() for key in ("startDate", "endDate"))
            if start.isoformat() < item["index_publication_date"] or end.isoformat() > payload["cutoff"]:
                raise ValueError("Theme history includes unpublished or post-cutoff data")
            rebuilt.extend(parse_history(originals[raw["url"]], code, start, end, expected_name=relation["index_name"]))
        if rebuilt != item["rows"]:
            raise ValueError("Theme observations cannot be rebuilt from original archives")
        check = evidence["checks"][item["fund_code"]]
        if check["index_code"] != code:
            raise ValueError("Theme semantic cross-check has the wrong identity")
        if check["status"] == "provisional_dated_factsheet_match":
            quote = next(r for r in rebuilt if r["date"] == check["factsheet_date"])
            if check["api_value"] != quote["peg"] or abs(check["factsheet_value"] - quote["peg"]) > .005:
                raise ValueError("Theme positive field cross-check cannot be reconstructed")


def replay(data, calendar, history, raw_maps, policy, *, start=120, end=None, phase=0, lag=1, fee=Decimal(10), execution=EXECUTION, detail=True):
    trace = []

    def allocate(index, held):
        target, meta = theme_tilt_target(data, policy, index, held, data["theme_states"][lag][index], stats=data["risk_stats"][index])
        trace.append(meta | {"targets": target.weights})
        return target

    account = simulate_allocations(data, calendar, allocate, start=start, end=end, phase=phase, fee_bps=fee, execution=execution)
    check = validate_allocation(account, data, calendar)
    by_day = {d: i for i, d in enumerate(data["days"])}
    for entry in trace:
        index = by_day[entry["signal_date"]]
        audit_theme_decision(entry, data, index, history.mapping, raw_maps, history.history.days, policy)
        codes = list(data["prices"])
        daily = np.asarray([[np.log(data["marks"][c][i] / data["marks"][c][i - 1])
                             for i in range(index - 59, index + 1)] for c in codes])
        returns = np.asarray([entry["targets"].get(c, 0) for c in codes]) @ daily
        risk = float(np.std(returns) * np.sqrt(252))
        if not math.isfinite(risk) or not math.isfinite(entry["projected_volatility"]) or abs(risk - entry["projected_volatility"]) > 1e-10 or risk > .1 + 1e-10:
            raise ValueError("Theme risk fails independent past-return reconstruction")
    count = [sum(entry["theme_valuation"]["funds"][c]["status"] == "ready" for c in entry["stock_distribution"]) for entry in trace]
    result = {"metrics": metrics(account), "ledger_validation": check, "risk_validation": {"status": "passed", "decisions": len(trace)},
              "information_validation": {"status": "passed", "decisions": len(trace), "min_ready_themes": min(count, default=0),
                                         "max_ready_themes": max(count, default=0), "mean_ready_themes": sum(count) / len(count) if count else 0.}}
    if detail:
        result.update(account=account, signal_trace=trace)
    return result


def run():
    payload, evidence = json.loads(THEMES.read_text()), json.loads(EVIDENCE.read_text())
    verify_input(payload, evidence)
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    history = ThemeValuationHistory(payload, evidence, calendar)
    raw_maps = {item["relation"]["index_code"]: {r["date"]: r["peg"] for r in item["rows"] if r["date"] in history.history.day_indexes}
                if history.eligibility[item["fund_code"]]["positive_field_cross_check"] else {} for item in payload["items"]}
    funds, _ = load_funds(INPUT, calendar)
    groups = fixed_cohorts(funds)
    bonds, _ = load_funds(BONDS, calendar)
    if set(bonds) != {"000016", "006669", "009803"}:
        raise ValueError("Defensive universe incomplete")
    funds.update(bonds)
    definitions = {"all13": groups["all_13_themes"] + list(bonds), "older9": groups["themes_available_2020"] + ["000016"],
                   "older4": groups["themes_available_2015"] + ["000016"]}
    previous = json.load(gzip.open(PREVIOUS, "rt"))
    if previous["calendar_sha256"] != sha256(CALENDAR.read_bytes()).hexdigest():
        raise ValueError("Theme reference uses a different calendar version")
    results = {}
    for name, codes in definitions.items():
        data = prepare_local(funds, codes, calendar)
        data["risk_stats"] = {i: budget_statistics(data, i) for i in range(120, len(data["days"]))}
        data["theme_states"] = {lag: {i: history.state(data["days"][i], lag_sessions=lag) for i in range(120, len(data["days"]))}
                               for lag in (1, 2, 5)}
        runs, annual, rolling, stress = {}, {}, {}, {}
        for policy in POLICIES:
            runs[policy] = replay(data, calendar, history, raw_maps, policy)
            if policy == "equal40" and abs(runs[policy]["metrics"]["total_return_pct"] - previous["cohorts"][name]["runs"]["static40_vol10"]["metrics"]["total_return_pct"]) > 1e-9:
                raise ValueError("Theme control no longer reproduces the corrected equal-stock baseline")
            for year in sorted({d[:4] for d in data["days"]}):
                indexes = [i for i, d in enumerate(data["days"]) if d.startswith(year)]
                if indexes[0] < 120 or len(indexes) < 20:
                    continue
                annual.setdefault(year, {})[policy] = replay(data, calendar, history, raw_maps, policy, start=indexes[0], end=indexes[-1], detail=False)
            stress[policy] = {f"information_lag{lag}": replay(data, calendar, history, raw_maps, policy, lag=lag, detail=False) for lag in (2, 5)}
            if name == "all13":
                rolling[policy] = {}
                for horizon in (20, 60, 120):
                    rows, checks = [], []
                    for start in range(120, len(data["days"]) - horizon, horizon):
                        trial = replay(data, calendar, history, raw_maps, policy, start=start, end=start + horizon, detail=False,
                                       execution=replace(EXECUTION, deadline_entry_guard_days=7))
                        rows.append({"start_date": trial["metrics"]["start_date"], "end_date": trial["metrics"]["end_date"],
                                     "return_pct": trial["metrics"]["total_return_pct"], "drawdown_pct": trial["metrics"]["max_drawdown_pct"]})
                        checks.append({"ledger": trial["ledger_validation"], "information": trial["information_validation"], "risk": trial["risk_validation"]})
                    rolling[policy][str(horizon)] = {"summary": rolling_summary(rows), "windows": rows, "validation": checks}
                scenarios = [(f"fee{f}", Decimal(f), 0, EXECUTION) for f in (50, 100)]
                scenarios += [(f"phase{p}", Decimal(10), p, EXECUTION) for p in (2, 5, 7)]
                scenarios += [("execution_lag2", Decimal(10), 0, replace(EXECUTION, execution_lag_sessions=2))]
                stress[policy].update({label: replay(data, calendar, history, raw_maps, policy, fee=fee, phase=p, execution=execution, detail=False)
                                       for label, fee, p, execution in scenarios})
        results[name] = {"codes": codes, "runs": runs, "annual": annual, "rolling": rolling, "stress": stress}
        print(json.dumps({"cohort": name, "metrics": {p: [round(r["metrics"]["total_return_pct"], 3), round(r["metrics"]["max_drawdown_pct"], 3)]
                             for p, r in runs.items()}}, ensure_ascii=False), flush=True)
    paths = ("scripts/research_theme_value_tilts.py", "scripts/validate_theme_value.py", "scripts/collect_theme_valuations.py",
             "scripts/verify_theme_valuations.py", "scripts/collect_index_valuation.py", "scripts/import_related_market.py",
             "backend/analysis/theme_value_tilt.py", "backend/analysis/trend_risk_budget.py", "backend/analysis/valuation_trend_budget.py",
             "backend/analysis/fund_allocation.py", "backend/analysis/fund_holding_costs.py", "backend/analysis/fund_allocation_signals.py",
             "backend/analysis/sector_trend.py", "backend/analysis/strategy_risk.py", "scripts/research_local_trend_models.py",
             "scripts/research_calibrated_trends.py", "scripts/research_defensive_allocations.py", "scripts/research_fund_rotation.py",
             "scripts/search_fund_allocations.py", "scripts/validate_fund_allocation.py", "backend/core/trading_calendar.py",
             "backend/analysis/matured_trend_forecast.py", "scripts/research_trend_risk_budget.py")
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in paths}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "eligibility": history.eligibility,
            "valuation_coverage": history.history.coverage, "theme_input": payload, "semantic_evidence": evidence,
            "access_stop": json.loads(ACCESS_STOP.read_text()), "input_sha256": sha256(INPUT.read_bytes()).hexdigest(),
            "bond_input_sha256": sha256(BONDS.read_bytes()).hexdigest(), "calendar_sha256": sha256(CALENDAR.read_bytes()).hexdigest(),
            "theme_input_sha256": sha256(THEMES.read_bytes()).hexdigest(), "evidence_sha256": sha256(EVIDENCE.read_bytes()).hexdigest(),
            "protocol": json.loads(PROTOCOL.read_text()), "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(),
            "source_hashes": {p: sha256(s.encode("utf-8")).hexdigest() for p, s in snapshots.items()}, "source_snapshots": snapshots,
            "limitations": ["Only three of twelve themes have positive field cross-checks and complete collected history",
                            "Other fund positions are preserved, not dropped to select a favourable available universe",
                            "Current source coverage and current semantic documents can create retrospective selection bias",
                            "This conditional partial-coverage study cannot establish full-13-theme strategy effectiveness or future profit"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/fund-theme-value-partial-audited-v2.json.gz")
    args = parser.parse_args()
    result = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8", compresslevel=6) as output:
        json.dump(result, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))


if __name__ == "__main__":
    main()
