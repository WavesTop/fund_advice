"""Pullback entry and matured-label forecasts on a fixed stock/bond/gold core."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import gzip
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

from backend.analysis.fund_allocation import AllocationExecution, AllocationTargets, simulate_allocations
from backend.analysis.fund_allocation_signals import _capped_weights, extend_features
from backend.analysis.matured_trend_forecast import FEATURE_NAMES, MaturedForecaster, audit_training_visibility, feature_vector
from scripts.diagnose_trend_predictivity import subsequent_return
from scripts.research_defensive_allocations import BONDS, separate_peer_scores
from scripts.research_fund_rotation import CALENDAR, INPUT, cohort_frames, fixed_cohorts, load_funds, rolling_summary
from scripts.search_fund_allocations import metrics
from scripts.validate_fund_allocation import validate_allocation
from backend.core.trading_calendar import TradingCalendar

PROTOCOL = ROOT / "config/fund-calibrated-strategy-protocol-v2.json"
EXECUTION = AllocationExecution(review_sessions=10, rebalance_band=.03)
FAMILIES = tuple(json.loads(PROTOCOL.read_text())["strategies"])


def prepare(funds, codes, calendar):
    data = cohort_frames(funds, codes, calendar)
    data["roles"] = {code: funds[code]["role"] for code in codes}
    extend_features(data)
    separate_peer_scores(data)
    data["vectors"] = {i: {e["code"]: feature_vector(data, e["code"], i) for e in frame["entries"]}
                       for i, frame in data["frames"].items() if i >= 252}
    return data


def outcome_records(data, calendar, horizon, *, start=252, stride=10):
    if start < 120 or stride < 1 or horizon < 1:
        raise ValueError("Invalid outcome sampling window")
    records = []
    stocks = [code for code in data["prices"] if data["roles"][code] != "defensive" and code != "000217"]
    for index in range(start, len(data["days"]) - horizon - 1, stride):
        returns = {code: subsequent_return(data, code, index, horizon, calendar) for code in stocks}
        mean = statistics.fmean(returns.values())
        first, last = index + 1, index + 1 + horizon
        for code in stocks:
            prices, dividends = data["prices"][code], data["distributions"][code]
            cost = prices[first] * 1.001
            peak, drawdown, cash = cost, 0., 0.
            for position in range(first, last + 1):
                if position > first:
                    cash += dividends[position]
                wealth = prices[position] * (.999 if position == last else 1) + cash
                peak = max(peak, wealth)
                drawdown = min(drawdown, wealth / peak - 1)
            records.append({"signal_date": data["days"][index], "label_end": data["days"][last], "code": code,
                            "features": data["vectors"][index][code], "outcomes": [returns[code] - mean, drawdown * 100],
                            "net_return_pct": returns[code]})
    return records


class CalibratedAllocator:
    def __init__(self, data, family, forecasters, *, fee=Decimal(10)):
        self.data, self.family, self.forecasters = data, family, forecasters
        self.hurdle = .2 + 2 * float(fee) / 100
        self.active, self.trace = [], []

    def __call__(self, index, held):
        data = self.data
        entries = {e["code"]: e for e in data["frames"][index]["entries"]}
        bonds = [code for code in entries if data["roles"][code] == "defensive"]
        gold = [code for code, e in entries.items() if e["defensive_asset"]]
        stocks = [code for code in entries if code not in bonds + gold]
        if not stocks or not bonds or not gold:
            raise ValueError("Core budget requires all three asset classes")
        stock_budget, bond_budget, gold_budget = (.24, .64, .12) if self.family == "static_24_64_12" else (.4, .4, .2)
        if self.family.startswith("static_"):
            stock_weights = dict.fromkeys(stocks, stock_budget / len(stocks))
        elif self.family.startswith("pullback_"):
            marks = data["marks"]
            long_up = {code: entries[code]["price"] > entries[code]["ma120"]
                       and marks[code][index] > marks[code][index - 120] for code in stocks}
            # Satellite membership is decision state; passive core holdings are not entry signals.
            self.active = [code for code in self.active if long_up[code]]
            candidates, quality = [], {}
            for code in stocks:
                ret20 = marks[code][index] / marks[code][index - 20] - 1
                if code not in self.active and long_up[code] and -.08 <= ret20 < 0:
                    ready = entries[code]["return5_pct"] > 0 if self.family == "pullback_resume" else True
                    if ready:
                        candidates.append(code)
                        quality[code] = -ret20 / max(.01, entries[code]["annual_vol60"] * math.sqrt(20 / 252))
            self.active = (self.active + sorted(candidates, key=lambda c: (-quality[c], c)))[:3]
            stock_weights = self._satellite(stocks)
        else:
            horizon = 60 if self.family in ("ridge60", "drawdown60") else 20
            model, meta = self.forecasters[horizon].at(data["days"][index])
            self.trace.append({"asof": data["days"][index], "horizon": horizon, "metadata": meta})
            allowed = model is not None and (self.family != "validated_ridge20" or
                                             meta["validation_rank_ic"] is not None and meta["validation_rank_ic"] > .03)
            if not allowed:
                self.active = []
                stock_weights = dict.fromkeys(stocks, stock_budget / len(stocks))
            else:
                predicted = model.predict([data["vectors"][index][code] for code in stocks])
                # Transfer labels use the teacher mean; switches need current-peer excess.
                current_mean = float(predicted[:, 0].mean())
                alpha = {code: float(predicted[i, 0]) - current_mean for i, code in enumerate(stocks)}
                risk = {code: max(3., min(80., abs(float(predicted[i, 1])))) for i, code in enumerate(stocks)}
                self.trace[-1]["predictions"] = {code: {"excess_return_pct": alpha[code],
                                                     "teacher_relative_excess_return_pct": float(predicted[i, 0]),
                                                     "drawdown_magnitude_pct": risk[code]}
                                                for i, code in enumerate(stocks)}
                if self.family == "drawdown60":
                    raw = {code: 1 / risk[code] for code in stocks}
                    stock_weights = {code: .4 * weight for code, weight in _capped_weights(raw, .35 / .4).items()}
                else:
                    self.active = [code for code in self.active if alpha[code] >= -self.hurdle]
                    eligible = [code for code in stocks if code not in self.active and alpha[code] > self.hurdle]
                    quality = {code: alpha[code] / risk[code] if self.family == "ridge_risk20" else alpha[code] for code in stocks}
                    self.active = (self.active + sorted(eligible, key=lambda c: (-quality[c], c)))[:3]
                    stock_weights = self._satellite(stocks)
        weights = dict(stock_weights)
        weights.update({code: bond_budget / len(bonds) for code in bonds})
        weights.update({code: gold_budget / len(gold) for code in gold})
        return AllocationTargets(weights)

    def _satellite(self, stocks):
        if not self.active:
            return dict.fromkeys(stocks, .4 / len(stocks))
        return {code: .2 / len(stocks) + (.2 / len(self.active) if code in self.active else 0) for code in stocks}


def replay(data, calendar, family, forecasters, *, start=252, end=None, fee=Decimal(10), phase=0, execution=EXECUTION):
    allocator = CalibratedAllocator(data, family, forecasters, fee=fee)
    account = simulate_allocations(data, calendar, allocator, start=start, end=end, fee_bps=fee, phase=phase, execution=execution)
    check = validate_allocation(account, data, calendar)
    trace = allocator.trace
    audits = {}
    for item in trace:
        key = f"{item['horizon']}/{item['asof']}"
        if key not in audits:
            audits[key] = audit_training_visibility(forecasters[item["horizon"]].records, item["metadata"])
    return {"metrics": metrics(account), "account": account, "ledger_validation": check,
            "forecast_trace": trace, "forecast_validation": audits}


def run():
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, _ = load_funds(INPUT, calendar)
    groups = fixed_cohorts(funds)
    bonds, bond_audit = load_funds(BONDS, calendar)
    if set(bonds) != {"000016", "006669", "009803"}:
        raise ValueError(f"Bond universe is incomplete: {bond_audit}")
    funds.update(bonds)
    definitions = {
        "all_13_with_bonds": groups["all_13_themes"] + ["000016", "006669", "009803"],
        "themes_2020_with_purebond": groups["themes_available_2020"] + ["000016"],
        "themes_2015_with_purebond": groups["themes_available_2015"] + ["000016"],
        "long_stock_gold_bond": ["000217", "000051", "000008", "000016"]}
    prepared = {name: prepare(funds, codes, calendar) for name, codes in definitions.items()}
    teacher_names = {name: "themes_2020_with_purebond" if name == "all_13_with_bonds" else name for name in definitions}
    learning = {name: {h: outcome_records(data, calendar, h) for h in (20, 60)} for name, data in prepared.items()
                if name != "all_13_with_bonds"}
    forecasters = {name: {h: MaturedForecaster(rows) for h, rows in horizons.items()} for name, horizons in learning.items()}
    results = {}
    for name, data in prepared.items():
        teacher = forecasters[teacher_names[name]]
        runs = {family: replay(data, calendar, family, teacher) for family in FAMILIES}
        annual = {}
        for year in sorted({day[:4] for day in data["days"]}):
            indexes = [i for i, day in enumerate(data["days"]) if day.startswith(year)]
            if indexes[0] < 252 or len(indexes) < 20:
                continue
            annual[year] = {family: replay(data, calendar, family, teacher, start=indexes[0], end=indexes[-1]) for family in FAMILIES}
        rolling = {}
        stress = {}
        if name == "all_13_with_bonds":
            for family in FAMILIES:
                rolling[family] = {}
                for horizon in (20, 60, 120):
                    rows, checks = [], []
                    for start in range(252, len(data["days"]) - horizon, horizon):
                        result = replay(data, calendar, family, teacher, start=start, end=start + horizon,
                                        execution=replace(EXECUTION, deadline_entry_guard_days=7))
                        rows.append({"start_date": result["metrics"]["start_date"], "end_date": result["metrics"]["end_date"],
                                     "return_pct": result["metrics"]["total_return_pct"], "drawdown_pct": result["metrics"]["max_drawdown_pct"]})
                        checks.append({"ledger": result["ledger_validation"], "forecast": result["forecast_validation"]})
                    rolling[family][str(horizon)] = {"summary": rolling_summary(rows), "windows": rows, "validation": checks}
                scenarios = [(f"fee_{fee}", Decimal(fee), 0, EXECUTION) for fee in (50, 100)]
                scenarios += [(f"phase_{p}", Decimal(10), p, EXECUTION) for p in (2, 5, 7)]
                scenarios += [("execution_delay2", Decimal(10), 0, replace(EXECUTION, execution_lag_sessions=2))]
                stress[family] = {label: replay(data, calendar, family, teacher, fee=fee, phase=p, execution=execution)
                                  for label, fee, p, execution in scenarios}
        results[name] = {"codes": definitions[name], "teacher": teacher_names[name], "runs": runs,
                         "annual_fresh_accounts": annual, "rolling": rolling, "stress": stress}
        print(json.dumps({"cohort": name, "metrics": {key: [round(v["metrics"]["total_return_pct"], 3),
                                                           round(v["metrics"]["max_drawdown_pct"], 3)] for key, v in runs.items()}}, ensure_ascii=False), flush=True)
    sources = ("backend/analysis/matured_trend_forecast.py", "backend/analysis/fund_allocation.py",
               "backend/analysis/fund_allocation_signals.py", "backend/analysis/fund_holding_costs.py",
               "backend/analysis/sector_trend.py", "backend/analysis/strategy_risk.py", "backend/core/trading_calendar.py",
               "scripts/research_calibrated_trends.py", "scripts/research_defensive_allocations.py",
               "scripts/research_fund_rotation.py", "scripts/diagnose_trend_predictivity.py", "scripts/validate_fund_allocation.py")
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "protocol": json.loads(PROTOCOL.read_text()),
            "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(), "input_sha256": sha256(INPUT.read_bytes()).hexdigest(),
            "bond_input_sha256": sha256(BONDS.read_bytes()).hexdigest(), "calendar_sha256": sha256(CALENDAR.read_bytes()).hexdigest(),
            "numpy_version": np.__version__, "source_hashes": {p: sha256((ROOT / p).read_bytes()).hexdigest() for p in sources},
            "feature_names": list(FEATURE_NAMES), "learning_records": learning, "cohorts": results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/fund-calibrated-trends-peer-v3.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.suffix == ".gz":
        with gzip.open(args.output, "wt", encoding="utf-8", compresslevel=6) as output:
            json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    else:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
