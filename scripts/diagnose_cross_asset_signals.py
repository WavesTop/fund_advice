"""Describe future costed labels separately by class, not tradable strategy returns."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.cross_asset_trend import class_statistics, prepare_class_proxies
from backend.analysis.monthly_class_trend import completed_month_indices, monthly_class_signals
from backend.core.trading_calendar import TradingCalendar
from scripts.diagnose_trend_predictivity import rank_correlation, subsequent_return
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, fixed_cohorts, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.validate_cross_asset_trend import rebuild_signals

SOURCE = "https://mebfaber.com/2017/12/13/episode-86-quantitative-approach-tactical-asset-allocation/"
GATES = ("vote_two_thirds", "recent_not_down", "monthly_ma10")
RANK_FIELDS = ("rank_ic_raw_returns", "rank_ic_past_risk_units", "rank_ic_inverse_vol_past_risk_units",
               "rank_ic_risk_removed_raw_returns", "paired_rank_ic_increment_over_inverse_vol")


def describe(values):
    if not values:
        return {"observations": 0}
    return {"observations": len(values), "mean_return_pct": statistics.fmean(values),
            "median_return_pct": statistics.median(values), "positive_fraction": statistics.fmean(v > 0 for v in values),
            "worst_return_pct": min(values)}


def ranking_diagnostics(entries):
    names = list(entries)
    scores = [entries[n]["past_signal"]["normalised_return_score"] for n in names]
    risks = [entries[n]["past_signal"]["floored_annual_vol"] for n in names]
    raw = [entries[n]["forward_return_pct"] for n in names]
    units = [entries[n]["forward_return_past_risk_units"] for n in names]
    trend = rank_correlation(scores, units)
    baseline = rank_correlation([1 / v for v in risks], units)
    return {"rank_ic_raw_returns": rank_correlation(scores, raw), "rank_ic_past_risk_units": trend,
            "rank_ic_inverse_vol_past_risk_units": baseline,
            "rank_ic_risk_removed_raw_returns": rank_correlation([s * v for s, v in zip(scores, risks)], raw),
            "paired_rank_ic_increment_over_inverse_vol": trend - baseline if trend is not None and baseline is not None else None}


def label_row(data, index, horizon, calendar, month_indices):
    if index < 252 or index + 1 + horizon >= len(data["days"]):
        raise ValueError("Diagnostic needs matured forward labels and 252 past sessions")
    stats = class_statistics(data, index)
    monthly = monthly_class_signals(data, index, month_indices)
    entries = {}
    for name, codes in data["class_groups"].items():
        signal = stats["signals"][name]
        per_fund = {code: subsequent_return(data, code, index, horizon, calendar) for code in codes}
        # This future label is a fresh equal-money purchase, not a costless
        # daily rebalanced class proxy or a continuous investor account.
        future = statistics.fmean(per_fund.values())
        entries[name] = {"past_signal": signal, "monthly_signal": monthly[name], "fund_returns_pct": per_fund,
                         "forward_return_pct": future,
                         "forward_return_past_risk_units": future / 100 / (signal["floored_annual_vol"] * (horizon / 252) ** .5),
                         "gates": {"vote_two_thirds": signal["vote"] >= 2/3,
                                   "recent_not_down": signal["recent_state"] not in ("weakening", "down"),
                                   "monthly_ma10": monthly[name]["enabled"] if monthly[name]["known"] else None}}
    return {"signal_date": data["days"][index], "purchase_date": data["days"][index + 1],
            "sale_date": data["days"][index + 1 + horizon], "horizon": horizon, "classes": entries,
            **ranking_diagnostics(entries)}


def summaries(rows):
    classes = sorted({name for row in rows for name in row["classes"]})
    result = {"decision_dates": len(rows), "classes": {}}
    for name in classes:
        items = [(row["signal_date"], row["classes"][name]) for row in rows]
        gates = {}
        for gate in GATES:
            by_state = {state: [item["forward_return_pct"] for _, item in items if item["gates"][gate] is state]
                        for state in (True, False, None)}
            annual = defaultdict(lambda: {"enabled": [], "disabled": [], "unknown": []})
            for day, item in items:
                state = item["gates"][gate]
                key = "unknown" if state is None else "enabled" if state else "disabled"
                annual[day[:4]][key].append(item["forward_return_pct"])
            gates[gate] = {"enabled": describe(by_state[True]), "disabled": describe(by_state[False]),
                           "unknown": describe(by_state[None]),
                           "enabled_minus_disabled_pp": (statistics.fmean(by_state[True]) - statistics.fmean(by_state[False])
                                                          if by_state[True] and by_state[False] else None),
                           "by_year": {y: {k: describe(v) for k, v in bucket.items()} for y, bucket in sorted(annual.items())}}
        result["classes"][name] = {"unconditional": describe([item["forward_return_pct"] for _, item in items]), "gates": gates}
    for field in RANK_FIELDS:
        values = [row[field] for row in rows if row[field] is not None]
        result[field] = {"valid_dates": len(values), "mean": statistics.fmean(values) if values else None,
                         "positive_fraction": statistics.fmean(v > 0 for v in values) if values else None}
    return result


def audit_label_row(row, data, index, calendar):
    horizon = row["horizon"]
    if horizon not in (20, 60, 120) or index < 252 or index + 1 + horizon >= len(data["days"]):
        raise ValueError("Diagnostic maturity or horizon differs")
    purchase, sale = index + 1, index + 1 + horizon
    if [row[k] for k in ("signal_date", "purchase_date", "sale_date")] != [data["days"][i] for i in (index, purchase, sale)]:
        raise ValueError("Diagnostic execution dates differ")
    groups, signals = rebuild_signals(data, index)
    if set(row["classes"]) != set(groups):
        raise ValueError("Diagnostic classes differ")
    known = [i for i in range(index + 1) if calendar.shift(date.fromisoformat(data["days"][i]), 1).month
             != date.fromisoformat(data["days"][i]).month]
    for name, codes in groups.items():
        entry, expected = row["classes"][name], signals[name]
        actual = entry["past_signal"]
        for key in ("vote", "annual_vol60", "floored_annual_vol", "normalised_return_score"):
            if not math.isfinite(actual[key]) or abs(actual[key] - expected[key]) > 1e-9:
                raise ValueError("Diagnostic past signal differs from raw fund history")
        observed_returns = {int(k): v for k, v in actual["log_returns"].items()}
        if set(observed_returns) != set(expected["log_returns"]) or any(not math.isfinite(v) or abs(v - expected["log_returns"][k]) > 1e-10
                                                                       for k, v in observed_returns.items()):
            raise ValueError("Diagnostic momentum horizons differ")
        if actual["recent_state"] != expected["recent_state"]:
            raise ValueError("Diagnostic recent state differs")
        monthly = entry["monthly_signal"]
        if monthly["known"] != (len(known) >= 10) or monthly["observed_months"] != len(known):
            raise ValueError("Diagnostic completed-month availability differs")
        if len(known) >= 10:
            if any(not math.isfinite(monthly[k]) for k in ("price", "ma10", "bias")) or monthly["price"] <= 0 or monthly["ma10"] <= 0:
                raise ValueError("Diagnostic monthly numeric values invalid")
            wanted = set(known[-10:])
            cumulative, levels = 1., []
            for i in range(known[-1] + 1):
                if i:
                    cumulative *= 1 + math.fsum(data["marks"][c][i] / data["marks"][c][i - 1] - 1 for c in codes) / len(codes)
                if i in wanted:
                    levels.append(cumulative)
            average = statistics.fmean(levels)
            enabled = levels[-1] > average * (1 + 1e-12)
            if (monthly["reference_dates"] != [data["days"][i] for i in known[-10:]]
                    or monthly["source_date"] != data["days"][known[-1]] or monthly["enabled"] != enabled
                    or not math.isfinite(monthly["bias"]) or abs(monthly["bias"] - (levels[-1] / average - 1)) > 1e-10
                    or abs(monthly["price"] / levels[-1] - 1) > 1e-10 or abs(monthly["ma10"] / average - 1) > 1e-10):
                raise ValueError("Diagnostic monthly observation differs from past-only reconstruction")
        else:
            enabled = None
        gates = {"vote_two_thirds": expected["vote"] >= 2/3, "recent_not_down": expected["recent_state"] not in ("weakening", "down"),
                 "monthly_ma10": enabled}
        if entry["gates"] != gates or set(entry["fund_returns_pct"]) != set(codes):
            raise ValueError("Diagnostic gating or label membership differs")
        purchase_confirmation = calendar.shift(date.fromisoformat(data["days"][purchase]), 1)
        sale_confirmation = calendar.shift(date.fromisoformat(data["days"][sale]), 1)
        if (sale_confirmation - purchase_confirmation).days < 7:
            raise ValueError("Diagnostic label unexpectedly has short redemption")
        future = []
        for code in codes:
            proceeds = Decimal(str(data["prices"][code][sale])) * Decimal(".999")
            proceeds += sum((Decimal(str(v)) for v in data["distributions"][code][purchase + 1:sale + 1]), Decimal(0))
            cost = Decimal(str(data["prices"][code][purchase])) * Decimal("1.001")
            value = float((proceeds / cost - 1) * 100)
            if not math.isfinite(entry["fund_returns_pct"][code]) or abs(value - entry["fund_returns_pct"][code]) > 1e-10:
                raise ValueError("Diagnostic costed fund outcome differs")
            future.append(value)
        mean = statistics.fmean(future)
        if not math.isfinite(entry["forward_return_pct"]) or abs(mean - entry["forward_return_pct"]) > 1e-10:
            raise ValueError("Diagnostic class future outcome differs")
        risk_units = mean / 100 / (expected["floored_annual_vol"] * (horizon / 252) ** .5)
        if not math.isfinite(entry["forward_return_past_risk_units"]) or abs(risk_units - entry["forward_return_past_risk_units"]) > 1e-9:
            raise ValueError("Diagnostic future risk normalisation differs")
    rebuilt = {name: entry | {"past_signal": signals[name]} for name, entry in row["classes"].items()}
    for field, value in ranking_diagnostics(rebuilt).items():
        if (value is None) != (row[field] is None) or (value is not None and (not math.isfinite(row[field]) or abs(value - row[field]) > 1e-10)):
            raise ValueError("Diagnostic ranking outcome differs")


def run():
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, audit = load_funds(INPUT, calendar)
    groups = fixed_cohorts(funds)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    definitions = {"all13": groups["all_13_themes"] + list(bonds), "older9": groups["themes_available_2020"] + ["000016"],
                   "older4": groups["themes_available_2015"] + ["000016"], "long4": ["000217", "000051", "000008", "000016"]}
    results = {}
    for name, codes in definitions.items():
        data = prepare_class_proxies(prepare_local(funds, codes, calendar))
        month_indices = completed_month_indices(data["days"], calendar)
        horizons = {}
        for horizon in (20, 60, 120):
            # Purchase-to-sale interiors do not overlap within each horizon.
            rows = [label_row(data, i, horizon, calendar, month_indices)
                    for i in range(252, len(data["days"]) - horizon - 1, horizon)]
            dates = {day: i for i, day in enumerate(data["days"])}
            for row in rows:
                audit_label_row(row, data, dates[row["signal_date"]], calendar)
            horizons[str(horizon)] = {"summary": summaries(rows), "observations": rows,
                                     "independent_reconstruction": {"status": "passed", "dates": len(rows)}}
        results[name] = {"codes": codes, "class_groups": data["class_groups"], "horizons": horizons}
        print(json.dumps({"cohort": name, "summary": {h: {"dates": v["summary"]["decision_dates"],
              "mean_raw_rank_ic": v["summary"]["rank_ic_raw_returns"]["mean"],
              "monthly_gate_spread_pp": {c: info["gates"]["monthly_ma10"]["enabled_minus_disabled_pp"]
                                         for c, info in v["summary"]["classes"].items()}}
              for h, v in horizons.items()}}, ensure_ascii=False), flush=True)
    paths = ("scripts/diagnose_cross_asset_signals.py", "backend/analysis/monthly_class_trend.py",
             "backend/analysis/cross_asset_trend.py", "scripts/diagnose_trend_predictivity.py", "backend/analysis/sector_trend.py",
             "backend/analysis/fund_holding_costs.py", "scripts/research_fund_rotation.py", "scripts/research_local_trend_models.py",
             "backend/analysis/trend_risk_budget.py", "backend/analysis/fund_allocation_signals.py", "backend/core/trading_calendar.py")
    snapshots = {p: (ROOT / p).read_text() for p in paths}
    protocol = {"purpose": "Diagnostic only, no forward labels used for live signals or strategy threshold tuning",
                "horizons_sessions": [20, 60, 120], "decision_stride": "same as horizon, nonoverlapping interiors within horizon",
                "warmup_sessions": 252, "ordinary_fee_bps_each_side": 10, "short_redemption_floor_bps": 150,
                "label": "signal+1 buy, horizon sessions later redeem, equal-money per fund, dividends held as cash",
                "monthly_ma": "10 completed calendar month-end total-return proxy closes including latest, above by >1e-12 relative",
                "monthly_gate_source": SOURCE, "year_groups": "signal year, not outcome year",
                "limitations": ["Current survivor funds and previously viewed histories; no untouched OOS",
                                "Classes, pools and horizons correlated; monthly gates and year groups not independent experiments",
                                "Conditional future mean differences are not causal alpha or portfolio returns",
                                "Score and forward risk units share past-vol denominators; inverse-vol-only paired ranking control added after initial diagnostic",
                                "Risk-removed ranking cancels that denominator but is not a calibrated expected return or executable portfolio",
                                "Hypothetical fund fees, dividend availability and next-NAV execution remain assumptions",
                                "End data month is not complete merely because the data ends; no future price use"]}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "protocol": protocol,
            "fund_audit": audit + bond_audit, "source_snapshots": snapshots,
            "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()},
            "input_sha256": sha256(INPUT.read_bytes()).hexdigest(), "bond_input_sha256": sha256(BONDS.read_bytes()).hexdigest(),
            "calendar_sha256": sha256(CALENDAR.read_bytes()).hexdigest(), "calendar": json.loads(CALENDAR.read_text())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/cross-asset-signal-diagnostic-controls-v4.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))


if __name__ == "__main__":
    main()
