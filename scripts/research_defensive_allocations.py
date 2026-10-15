"""Risk-matched controls, real bond NAVs and staggered trend allocation sleeves."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.fund_allocation import AllocationExecution, AllocationTargets, simulate_allocations
from backend.analysis.fund_allocation_signals import AllocationPlan, allocation_at, extend_features
from backend.analysis.sector_trend import midpoint_percentiles, trend_metrics, trend_score
from backend.core.trading_calendar import TradingCalendar
from scripts.research_fund_rotation import CALENDAR, INPUT, cohort_frames, fixed_cohorts, load_funds, rolling_summary
from scripts.search_fund_allocations import metrics, run_plan
from scripts.validate_fund_allocation import validate_allocation

BONDS = ROOT / "runtime/verification/fund-allocation-defensive-20261013/input.json"
PLANS = ("static_40_40_20", "core_trend", "core_trend_equityvol8", "core_trend_tilt")


def separate_peer_scores(data, *, start=252):
    groups = {}
    for code in data["prices"]:
        group = "bond" if data["roles"][code] == "defensive" else "gold" if code == "000217" else "stock"
        groups.setdefault(group, []).append(code)
    if start < 120:
        raise ValueError("Peer scores require 120 past sessions")
    for index in range(start, len(data["days"])):
        entries = {e["code"]: e for e in data["frames"][index]["entries"]}
        for codes in groups.values():
            for key, window in (("short", 20), ("medium", 60), ("long", 120)):
                descriptors = [trend_metrics(data["marks"][code][index - window:index + 1]) for code in codes]
                ranks = midpoint_percentiles([m["return_pct"] for m in descriptors])
                for code, descriptor, rank in zip(codes, descriptors, ranks):
                    entries[code]["scores"][key] = trend_score(descriptor, rank)


def defensive_target(data, family, index, held):
    entries = {e["code"]: e for e in data["frames"][index]["entries"]}
    bonds = [code for code in entries if data["roles"][code] == "defensive"]
    gold = [code for code, e in entries.items() if e["defensive_asset"]]
    stocks = [code for code in entries if code not in bonds + gold]
    if family == "static_40_40_20":
        weights = {code: .4 / len(stocks) for code in stocks}
        weights.update({code: .4 / len(bonds) for code in bonds})
        weights.update({code: .2 / len(gold) for code in gold})
        return AllocationTargets(weights)
    eligible, quality = [], {}
    for code in stocks:
        e = entries[code]
        score = .2 * e["scores"]["short"] + .4 * e["scores"]["medium"] + .4 * e["scores"]["long"]
        ready = (score >= 45 and (e["price"] > e["ma120"] or e["return30_pct"] >= 0)) if code in held else (
            score >= 55 and e["price"] > e["ma60"] and e["return30_pct"] > 0)
        quality[code] = score
        if ready:
            eligible.append(code)
    ranked = sorted(eligible, key=lambda code: (-quality[code], code))
    selected = [code for code in ranked[:7] if code in held]
    selected = (selected + [code for code in ranked if code not in selected])[:5]
    raw = {code: 1 / max(.05, entries[code]["annual_vol60"]) for code in selected}
    weights = {code: .4 * weight / sum(raw.values()) for code, weight in raw.items()}
    if family == "core_trend_tilt":
        weights = {code: .2 / len(stocks) + .5 * weights.get(code, 0) for code in stocks}
    elif family == "core_trend_equityvol8" and weights:
        centered = {code: [value - statistics.fmean(entries[code]["daily60"]) for value in entries[code]["daily60"]]
                    for code in weights}
        vol = math.sqrt(statistics.fmean(sum(weight * centered[code][i] for code, weight in weights.items()) ** 2
                                       for i in range(60)) * 252)
        scale = min(1., .08 / max(.01, vol))
        weights = {code: weight * scale for code, weight in weights.items()}
    elif family not in ("core_trend", "core_trend_equityvol8"):
        raise ValueError("Unknown core allocation")
    weights = {code: min(.2, weight) for code, weight in weights.items()}
    for code in gold:
        e = entries[code]
        if e["price"] > e["ma200"] and e["returns"][126] > 0:
            weights[code] = .2 / len(gold)
    bond_eligible = [code for code in bonds if entries[code]["price"] > entries[code]["ma60"]
                     and entries[code]["return30_pct"] > 0]
    raw_bonds = {code: 1 / max(.005, entries[code]["annual_vol60"]) for code in bond_eligible}
    remainder = max(0., 1 - sum(weights.values()))
    for code, value in raw_bonds.items():
        weights[code] = min(.75, remainder * value / sum(raw_bonds.values()))
    risk_exits = frozenset(code for code in held if weights.get(code, 0) == 0 and
                          (code not in stocks or code not in eligible))
    return AllocationTargets(weights, risk_exits)


def core_account(data, calendar, family, *, start=252, end=None, phase=0, fee=Decimal(10), execution=AllocationExecution()):
    account = simulate_allocations(data, calendar, lambda i, held: defensive_target(data, family, i, held),
                                   start=start, end=end, phase=phase, fee_bps=fee, execution=execution)
    return account, validate_allocation(account, data, calendar)


def ensemble(data, calendar, family, *, start=252, end=None, fee=Decimal(10)):
    sleeves, checks = [], []
    for phase in (0, 5, 10, 15):
        account = simulate_allocations(data, calendar, lambda i, held: defensive_target(data, family, i, held),
                                       start=start, end=end, phase=phase, fee_bps=fee, initial=Decimal(25000))
        checks.append(validate_allocation(account, data, calendar))
        sleeves.append(account)
    # These are four cash-segregated sleeves, not netted orders in a single account.
    curve = []
    for points in zip(*(a["equity_curve"] for a in sleeves)):
        curve.append({"date": points[0]["date"], "equity": str(sum((Decimal(p["equity"]) for p in points), Decimal(0)))})
    peak, drawdown = Decimal(100000), Decimal(0)
    for point in curve:
        value = Decimal(point["equity"])
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    combined = {"start_date": sleeves[0]["start_date"], "end_date": sleeves[0]["end_date"], "initial_equity": "100000",
                "final_equity": curve[-1]["equity"], "total_return_pct": float((Decimal(curve[-1]["equity"]) / 100000 - 1) * 100),
                "max_drawdown_pct": float(drawdown * 100), "total_fees": str(sum((Decimal(a["total_fees"]) for a in sleeves), Decimal(0))),
                "trade_count": sum(a["trade_count"] for a in sleeves),
                "short_redemption_sales": sum(a["short_redemption_sales"] for a in sleeves), "equity_curve": curve}
    return {"metrics": metrics(combined), "sleeves": sleeves, "validation": checks}


def run():
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    required = ["000016", "006669", "009803"]
    if set(bonds) != set(required):
        raise ValueError(f"Defensive universe incomplete: {bond_audit}")
    groups = fixed_cohorts(funds)
    funds.update(bonds)
    selected_groups = {
        "all_13_with_bonds": groups["all_13_themes"] + required,
        "themes_2020_with_purebond": groups["themes_available_2020"] + ["000016"],
        "themes_2015_with_purebond": groups["themes_available_2015"] + ["000016"],
        "long_stock_gold_bond": ["000217", "000051", "000008", "000016"]}
    results = {}
    # Risk control on the original theme pools without changing their investment dates.
    cash_controls = {}
    for name in ("all_13_themes", "themes_available_2020", "themes_available_2015"):
        data = cohort_frames(funds, groups[name], calendar)
        extend_features(data)
        def fixed_cash(i, held):
            target = allocation_at(data, AllocationPlan("hold", "hold"), i, held)
            return AllocationTargets({code: .6 * weight for code, weight in target.weights.items()}) if target else None
        account = simulate_allocations(data, calendar, fixed_cash)
        cash_controls[name] = {"metrics": metrics(account), "validation": validate_allocation(account, data, calendar)}
    for name, codes in selected_groups.items():
        data = cohort_frames(funds, codes, calendar)
        data["roles"] = {code: funds[code]["role"] for code in codes}
        extend_features(data)
        separate_peer_scores(data)
        runs = {}
        for family in PLANS:
            account, check = core_account(data, calendar, family)
            runs[family] = {"metrics": metrics(account), "validation": check, "account": account}
        for family in ("core_trend", "core_trend_equityvol8"):
            runs[family + "_four_sleeves"] = ensemble(data, calendar, family)
        years = sorted({int(day[:4]) for day in data["days"]})
        annual = {}
        for year in years:
            indexes = [i for i, day in enumerate(data["days"]) if day.startswith(str(year))]
            if not indexes or indexes[0] < 252 or len(indexes) < 20:
                continue
            annual[str(year)] = {}
            for family in PLANS:
                account, check = core_account(data, calendar, family, start=indexes[0], end=indexes[-1])
                annual[str(year)][family] = {"metrics": metrics(account), "validation": check}
        rolling = {}
        if name == "all_13_with_bonds":
            for family in PLANS:
                rolling[family] = {}
                for horizon in (20, 60, 120):
                    rows, checks = [], []
                    for start in range(252, len(data["days"]) - horizon, horizon):
                        account, check = core_account(data, calendar, family, start=start, end=start + horizon,
                                                       execution=AllocationExecution(deadline_entry_guard_days=7))
                        rows.append({"start_date": account["start_date"], "end_date": account["end_date"],
                                     "return_pct": account["total_return_pct"], "drawdown_pct": account["max_drawdown_pct"]})
                        checks.append(check)
                    rolling[family][str(horizon)] = {"summary": rolling_summary(rows), "windows": rows, "validation": checks}
            for family in ("core_trend", "core_trend_equityvol8"):
                runs[family]["fee_stress"] = {}
                for fee in (50, 100):
                    account, check = core_account(data, calendar, family, fee=Decimal(fee))
                    runs[family]["fee_stress"][str(fee)] = {"metrics": metrics(account), "validation": check}
        results[name] = {"codes": codes, "runs": runs, "annual_fresh_accounts": annual, "rolling": rolling}
        print(json.dumps({"cohort": name, "metrics": {key: [round(v["metrics"]["total_return_pct"], 3),
                                                             round(v["metrics"]["max_drawdown_pct"], 3)] for key, v in runs.items()}}, ensure_ascii=False), flush=True)
    source_files = ("backend/analysis/fund_allocation.py", "backend/analysis/fund_allocation_signals.py",
                    "backend/analysis/fund_holding_costs.py", "scripts/research_defensive_allocations.py",
                    "scripts/search_fund_allocations.py", "scripts/validate_fund_allocation.py", "scripts/research_fund_rotation.py")
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "data_audit": audit + bond_audit,
            "source_hashes": {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in source_files},
            "input_sha256": sha256(INPUT.read_bytes()).hexdigest(), "bond_input_sha256": sha256(BONDS.read_bytes()).hexdigest(),
            "calendar_sha256": sha256(CALENDAR.read_bytes()).hexdigest(), "cash_controls": cash_controls, "cohorts": results,
            "protocol": {"selected_after_first_round": True, "families": list(PLANS),
                         "full_history_start": "252-session warmup, identical within each cohort",
                         "baseline": "fixed 40% stock / 40% bond / 20% gold monthly banded allocation",
                         "active_bond_allocation": "Unallocated equity/gold risk budget can enter positive-trend bonds; single bond cap 75%",
                         "peer_ranking": "Stocks, gold and bonds scored within separate asset classes; singleton uses own trend only",
                         "equity_vol_target_scope": "8% annualized equity-component forecast, not a guaranteed total-portfolio volatility",
                         "four_sleeves": "Cash-segregated quarters, review phases 0/5/10/15; no netting or pooled redemption cash",
                         "annual_scope": "Fixed-rule fresh calendar-year accounts, not compounded forward executable performance",
                         "limitations": ["Previously viewed histories; this round follows observation of first-round failures",
                                         "Ordinary fees are scenarios, not verified full product schedules",
                                         "Cash distributions inferred; registration and actual payment dates not verified",
                                         "Bond funds have credit/duration risk and are not guaranteed cash substitutes"]}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/fund-defensive-allocation-peer-v2.json")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
