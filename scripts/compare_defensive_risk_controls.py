"""Audit the prior defensive candidate against passive risky-exposure controls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.fund_allocation import AllocationTargets, simulate_allocations
from backend.analysis.fund_allocation_signals import extend_features
from backend.core.trading_calendar import TradingCalendar
from scripts.research_defensive_allocations import BONDS, defensive_target, ensemble, separate_peer_scores
from scripts.research_fund_rotation import CALENDAR, INPUT, cohort_frames, fixed_cohorts, load_funds
from scripts.search_fund_allocations import metrics
from scripts.validate_fund_allocation import validate_allocation


def passive_target(data, index, risky_scale):
    if not 0 < risky_scale <= 1:
        raise ValueError("Invalid passive risk scale")
    base = defensive_target(data, "static_40_40_20", index, set()).weights
    bonds = [code for code in base if data["roles"][code] == "defensive"]
    weights = {code: weight * risky_scale for code, weight in base.items() if code not in bonds}
    weights.update(dict.fromkeys(bonds, (1 - sum(weights.values())) / len(bonds)))
    return AllocationTargets(weights)


def run():
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, _ = load_funds(INPUT, calendar)
    groups = fixed_cohorts(funds)
    bonds, _ = load_funds(BONDS, calendar)
    if set(bonds) != {"000016", "006669", "009803"}:
        raise ValueError("Defensive products incomplete")
    funds.update(bonds)
    definitions = {"all_13_with_bonds": groups["all_13_themes"] + list(bonds),
                   "themes_2020_with_purebond": groups["themes_available_2020"] + ["000016"],
                   "themes_2015_with_purebond": groups["themes_available_2015"] + ["000016"],
                   "long_stock_gold_bond": ["000217", "000051", "000008", "000016"]}
    output = {}
    for name, codes in definitions.items():
        data = cohort_frames(funds, codes, calendar)
        data["roles"] = {code: funds[code]["role"] for code in codes}
        extend_features(data)
        separate_peer_scores(data)
        candidate = ensemble(data, calendar, "core_trend_equityvol8")
        passive = {}
        for scale in (.4, .6, .8, 1.):
            account = simulate_allocations(data, calendar, lambda i, held: passive_target(data, i, scale))
            check = validate_allocation(account, data, calendar)
            passive[str(scale)] = {"metrics": metrics(account), "validation": check, "account": account}
        output[name] = {"candidate": candidate, "passive_risky_scales": passive}
        print(json.dumps({"cohort": name, "candidate": [candidate["metrics"]["total_return_pct"], candidate["metrics"]["max_drawdown_pct"]],
                          "passive": {scale: [v["metrics"]["total_return_pct"], v["metrics"]["max_drawdown_pct"]]
                                      for scale, v in passive.items()}}, ensure_ascii=False), flush=True)
    paths = ("scripts/compare_defensive_risk_controls.py", "scripts/research_defensive_allocations.py",
             "backend/analysis/fund_allocation.py", "backend/analysis/fund_holding_costs.py", "scripts/validate_fund_allocation.py")
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": output,
            "input_sha256": sha256(INPUT.read_bytes()).hexdigest(), "bond_input_sha256": sha256(BONDS.read_bytes()).hexdigest(),
            "source_hashes": {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in paths},
            "protocol": {"common_review_sessions": 20, "common_rebalance_band": .05,
                         "passive_scale": "Scale the 40% stock and 20% gold components; allocate the remainder to the same bonds",
                         "candidate": "Previously observed four-sleeve trend/risk candidate, not selected using this new control",
                         "risk_match_scope": "Discrete sensitivity grid; no claim that realized risks match exactly or are known in advance",
                         "limitations": ["Previously viewed histories, fee/distribution scenarios", "Candidate has staggered sleeves; passive has one account"]}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/defensive-passive-risk-controls-v1.json")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
