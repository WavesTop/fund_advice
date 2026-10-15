"""Replay the frozen fund candidate and validate every account independently."""
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
from scripts.backtest_sector_rotation import load_policy
from scripts.optimize_sector_rotation import source_hashes
from scripts.research_fund_rotation import CALENDAR, INPUT, cohort_frames, fixed_cohorts, load_funds, metrics_only, rolling_summary, run_account
from scripts.validate_sector_rotation import validate_ledger

POLICY = ROOT / "config/fund-rotation-cost-aware-v1.json"


def replay(input_path=INPUT, policy_path=POLICY) -> dict:
    document = json.loads(policy_path.read_text())
    policy = load_policy(policy_path)
    costs = FundHoldingCosts(**document["holding_costs"])
    costs.validate()
    fee = Decimal(document["ordinary_fee_bps_each_side"])
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, audit = load_funds(input_path, calendar)
    results, checks = {}, {}
    for name, codes in fixed_cohorts(funds).items():
        data = cohort_frames(funds, codes, calendar)
        adapted = replace(policy, max_positions=min(policy.max_positions, len(codes)))
        account = run_account(data, calendar, adapted, costs=costs, fee_bps=fee)
        checks[name] = validate_ledger(account, data["days"], data["prices"], calendar=calendar,
                                      distributions=data["distributions"])
        rolling = {}
        finite = replace(adapted, entry_min_days_to_liquidation=document["finite_horizon_entry_guard_calendar_days"])
        for horizon in (20, 60, 120):
            rows = []
            for start in range(120, len(data["days"]) - horizon, horizon):
                window = run_account(data, calendar, finite, start=start, end=start + horizon, costs=costs, fee_bps=fee)
                checks[f"{name}/{horizon}/{start}"] = validate_ledger(
                    window, data["days"], data["prices"], calendar=calendar, distributions=data["distributions"])
                rows.append({"start_date": window["start_date"], "end_date": window["end_date"],
                             "return_pct": window["total_return_pct"], "drawdown_pct": window["max_drawdown_pct"]})
            rolling[str(horizon)] = {"summary": rolling_summary(rows), "windows": rows}
        results[name] = {"codes": codes, "account": account, "rolling": rolling}
        print(json.dumps({"cohort": name, "metrics": metrics_only(account)}, ensure_ascii=False), flush=True)
    hashes = {**source_hashes(), **{path: sha256((ROOT / path).read_bytes()).hexdigest() for path in
              ("scripts/replay_fund_rotation.py", "scripts/research_fund_rotation.py")}}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "policy_sha256": sha256(policy_path.read_bytes()).hexdigest(),
            "input_sha256": sha256(input_path.read_bytes()).hexdigest(), "calendar_sha256": sha256(CALENDAR.read_bytes()).hexdigest(),
            "source_hashes": hashes, "data_audit": audit, "cohorts": results, "ledger_validation": checks,
            "limitations": document["limits"], "selection_scope": document["status"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=INPUT)
    parser.add_argument("--policy", type=Path, default=POLICY)
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/fund-rotation-frozen-replay.json")
    args = parser.parse_args()
    report = replay(args.input, args.policy)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "validated_accounts": len(report["ledger_validation"])}))


if __name__ == "__main__":
    main()
