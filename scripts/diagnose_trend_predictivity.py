"""Explore score ordering versus subsequent costed returns, not trading forecasts."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.fund_holding_costs import FundHoldingCosts
from backend.analysis.sector_trend import midpoint_percentiles
from backend.core.trading_calendar import TradingCalendar
from scripts.research_fund_rotation import CALENDAR, INPUT, cohort_frames, fixed_cohorts, load_funds


def rank_correlation(scores, returns):
    left, right = midpoint_percentiles(scores), midpoint_percentiles(returns)
    if len(left) < 2 or statistics.pstdev(left) < 1e-12 or statistics.pstdev(right) < 1e-12:
        return None
    mx, my = statistics.fmean(left), statistics.fmean(right)
    return statistics.fmean((x - mx) * (y - my) for x, y in zip(left, right)) / (statistics.pstdev(left) * statistics.pstdev(right))


def subsequent_return(data, code, signal_index, horizon, calendar, fee_bps=Decimal(10)):
    first, last = signal_index + 1, signal_index + 1 + horizon
    prices = data["prices"][code]
    dividends = data["distributions"][code]
    fee = FundHoldingCosts().redemption(data["days"][first], data["days"][last], fee_bps, calendar)
    # Per purchased unit: cash dividends are held, not notionally reinvested.
    proceeds = Decimal(str(prices[last])) * (1 - Decimal(fee["sell_fee_bps"]) / 10000)
    proceeds += sum((Decimal(str(v)) for v in dividends[first + 1:last + 1]), Decimal(0))
    cost = Decimal(str(prices[first])) * (1 + fee_bps / 10000)
    return float((proceeds / cost - 1) * 100)


def run():
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, _ = load_funds(INPUT, calendar)
    groups = fixed_cohorts(funds)
    output = {}
    for name in ("all_13_themes", "themes_available_2020", "themes_available_2015"):
        data = cohort_frames(funds, groups[name], calendar)
        horizons = {}
        for horizon in (20, 60, 120):
            rows = []
            for index in range(120, len(data["days"]) - horizon - 1, 20):
                entries = data["frames"][index]["entries"]
                values = [subsequent_return(data, e["code"], index, horizon, calendar) for e in entries]
                scored = {"fast_combined": [.5 * e["scores"]["short"] + .3 * e["scores"]["medium"] + .2 * e["scores"]["long"]
                                            for e in entries],
                          "slow_combined": [.2 * e["scores"]["short"] + .4 * e["scores"]["medium"] + .4 * e["scores"]["long"]
                                            for e in entries]}
                rows.append({"signal_date": data["days"][index], "purchase_date": data["days"][index + 1],
                             "sale_date": data["days"][index + 1 + horizon], "returns_pct": values,
                             "codes": [e["code"] for e in entries], "scores": scored,
                             "rank_correlations": {key: rank_correlation(scores, values) for key, scores in scored.items()}})
            summaries = {}
            for mode in ("fast_combined", "slow_combined"):
                bins = defaultdict(list)
                for row in rows:
                    for score, ret in zip(row["scores"][mode], row["returns_pct"]):
                        bins[min(90, int(score // 10) * 10)].append((row["signal_date"], ret))
                correlations = [r["rank_correlations"][mode] for r in rows if r["rank_correlations"][mode] is not None]
                summaries[mode] = {"decision_dates": len(rows), "mean_rank_correlation": statistics.fmean(correlations) if correlations else None,
                                   "positive_rank_correlation_fraction": statistics.fmean(v > 0 for v in correlations) if correlations else None,
                                   "score_bins": {str(key): {"asset_date_observations": len(items),
                                                           "distinct_dates": len({d for d, _ in items}),
                                                           "mean_return_pct": statistics.fmean(v for _, v in items),
                                                           "median_return_pct": statistics.median(v for _, v in items),
                                                           "positive_fraction": statistics.fmean(v > 0 for _, v in items)}
                                                  for key, items in sorted(bins.items())}}
            horizons[str(horizon)] = {"summaries": summaries, "observations": rows}
        output[name] = horizons
        print(json.dumps({"cohort": name, "mean_rank_correlations": {h: {mode: s["mean_rank_correlation"]
                                           for mode, s in v["summaries"].items()} for h, v in horizons.items()}}), flush=True)
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": output,
            "input_sha256": sha256(INPUT.read_bytes()).hexdigest(),
            "source_hashes": {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in
                              ("scripts/diagnose_trend_predictivity.py", "scripts/research_fund_rotation.py",
                               "backend/analysis/sector_trend.py", "backend/analysis/fund_holding_costs.py")},
            "protocol": {"purpose": "Historical diagnostic only; future labels are never used as contemporaneous signals",
                         "fee_bps_each_side": 10, "purchase": "next-session NAV", "sale": "horizon sessions after purchase",
                         "score_bin_size": 10, "decision_stride_sessions": 20,
                         "limitations": ["60/120-session future outcomes overlap; assets and cohorts are correlated",
                                         "Already-viewed current-survivor histories; no significance or calibrated forward probabilities claimed",
                                         "Mean rank correlation is descriptive, not evidence of executable portfolio alpha",
                                         "Dividends and ordinary fee schedules remain research assumptions"]}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/trend-predictivity-v1.json")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
