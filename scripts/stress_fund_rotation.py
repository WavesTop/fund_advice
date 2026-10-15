"""Concentration, cost, confirmation and timing checks on the expanded funds."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.fund_holding_costs import FundHoldingCosts
from backend.core.trading_calendar import TradingCalendar
from scripts.backtest_sector_rotation import DEFAULT_POLICY, load_policy
from scripts.research_fund_rotation import CALENDAR, INPUT, cohort_frames, fixed_cohorts, load_funds, metrics_only, run_account
from scripts.validate_sector_rotation import validate_ledger
from scripts.optimize_sector_rotation import source_hashes


def candidate_policy():
    return replace(load_policy(DEFAULT_POLICY), score_mode="combined", entry_score=55,
                   soft_exit_min_calendar_days=30, allow_defensive_assets=True,
                   entry_ma_sessions=60, exit_ma_sessions=120, recent_return_sessions=30,
                   benchmark_ma_sessions=200, review_sessions=10, trailing_stop=.20)


def run() -> dict:
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, _ = load_funds(INPUT, calendar)
    groups = fixed_cohorts(funds)
    results = {}
    for group in ("all_13_themes", "themes_available_2020", "themes_available_2015"):
        data = cohort_frames(funds, groups[group], calendar)
        results[group] = {}
        for positions in (3, 5, 10):
            policy = replace(candidate_policy(), max_positions=min(positions, len(groups[group])))
            account = run_account(data, calendar, policy)
            check = validate_ledger(account, data["days"], data["prices"], calendar=calendar, distributions=data["distributions"])
            results[group][str(positions)] = {"metrics": metrics_only(account), "validation": check}
        print(json.dumps({"cohort": group, "position_counts": {p: (v["metrics"]["total_return_pct"], v["metrics"]["max_drawdown_pct"])
                                                               for p, v in results[group].items()}}, ensure_ascii=False), flush=True)
    data = cohort_frames(funds, groups["all_13_themes"], calendar)
    stress = {}
    for positions in (5, 10):
        policy = replace(candidate_policy(), max_positions=positions)
        scenarios = [(f"fee_{fee}", Decimal(fee), 0, FundHoldingCosts()) for fee in (10, 50, 100)]
        scenarios += [(f"phase_{offset}", Decimal(10), offset, FundHoldingCosts()) for offset in range(10)]
        scenarios += [(f"settlement_{lag}", Decimal(10), 0, FundHoldingCosts(settlement_lag_sessions=lag)) for lag in (3, 7)]
        scenarios += [("confirmation_2", Decimal(10), 0, FundHoldingCosts(confirmation_lag_sessions=2, settlement_lag_sessions=3))]
        stress[str(positions)] = {}
        for name, fee, offset, costs in scenarios:
            account = run_account(data, calendar, policy, fee_bps=fee, offset=offset, costs=costs)
            check = validate_ledger(account, data["days"], data["prices"], calendar=calendar, distributions=data["distributions"])
            stress[str(positions)][name] = {"metrics": metrics_only(account), "validation": check}
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "input_sha256": sha256(INPUT.read_bytes()).hexdigest(),
            "calendar_sha256": sha256(CALENDAR.read_bytes()).hexdigest(),
            "source_hashes": {**source_hashes(), **{path: sha256((ROOT / path).read_bytes()).hexdigest()
                              for path in ("scripts/stress_fund_rotation.py", "scripts/research_fund_rotation.py")}},
            "concentration": results, "stress": stress,
            "selection_scope": "Retrospective sensitivity, not an uninspected out-of-sample test"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/fund-rotation-fee-stress.json")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "stress": {p: {name: round(v["metrics"]["total_return_pct"], 3)
                                         for name, v in scenarios.items()} for p, scenarios in report["stress"].items()}}, indent=2))


if __name__ == "__main__":
    main()
