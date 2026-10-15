"""Ordinary-fee-only counterfactuals without removing holding/cash friction."""
import argparse
from bisect import bisect_left
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.portfolio_increment import prepare_economic
from backend.analysis.trend_satellite import policies
from backend.core.trading_calendar import TradingCalendar
from scripts.diagnose_satellite_increment import checked_pair, factorial_returns
from scripts.export_strategy_trial import checked_metrics
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import carry_slice, effective_episode
from scripts.research_satellite_headroom import event_repair_policies
from scripts.research_trend_satellites import satellite_trial

PROTOCOL = ROOT / "config/satellite-low-ordinary-fee-research-v1.json"
POLICIES = ["matched_core", "fixed20", "event_core", "event_fixed20", "inverse20"]


def validate_protocol(protocol):
    expected = [{"id": "default", "phase": 0, "lag": 1},
                {"id": "phase5", "phase": 5, "lag": 1},
                {"id": "phase10", "phase": 10, "lag": 1},
                {"id": "phase15", "phase": 15, "lag": 1},
                {"id": "lag2", "phase": 0, "lag": 2}]
    if (protocol["ordinary_fee_bps_each_side"] != 0
            or isinstance(protocol["ordinary_fee_bps_each_side"], bool)
            or protocol["policies"] != POLICIES or protocol["scenarios"] != expected
            or protocol["cohorts"] != ["long4", "older4", "older9", "all13"]
            or protocol["fresh_entry_dates"] != ["2024-07-29", "2025-01-02", "2025-07-01", "2026-01-05"]):
        raise ValueError("Low-fee diagnostic must retain its entire fixed design")


def verify_fee_only(account, reference, lag):
    if Decimal(account["fee_bps_each_side"]) != 0:
        raise ValueError("Ordinary fee was not set to zero")
    if account["holding_cost_policy"] != reference["holding_cost_policy"]:
        raise ValueError("Ordinary-fee study changed holding or cash rules")
    expected = {**reference["execution"], "execution_lag_sessions": lag}
    if account["execution"] != expected:
        raise ValueError("Low-fee scenario changed unrelated execution settings")
    with localcontext() as context:
        context.prec = 40
        for trade in account["trades"]:
            if trade["action"] == "BUY":
                fee = Decimal(0)
            elif trade["action"] == "SELL":
                short = trade["holding_calendar_days"] < reference["holding_cost_policy"]["short_holding_days"]
                rate = Decimal(reference["holding_cost_policy"]["short_redemption_bps"] if short else 0)
                if Decimal(trade["sell_fee_bps"]) != rate:
                    raise ValueError("Low ordinary fee erased a short redemption floor")
                fee = Decimal(trade["gross_amount"]) * rate / 10000
            else:
                raise ValueError("Unknown low-fee trade action")
            if Decimal(trade["fee"]) != fee:
                raise ValueError("Ordinary-fee-only trade cost mismatch")


def paired_summary(runs):
    measured = {key: checked_metrics(runs[key]) for key in POLICIES}
    if len({(run["metrics"]["start_date"], run["metrics"]["end_date"]) for run in runs.values()}) != 1:
        raise ValueError("Low-fee comparison coverage mismatch")
    return {"immediate": checked_pair(runs["fixed20"], runs["matched_core"]),
            "event": checked_pair(runs["event_fixed20"], runs["event_core"]),
            "factorial": factorial_returns([measured[key]["total_return_pct"] for key in POLICIES])}


def compact(run):
    return {key: value for key, value in run.items() if key not in ("account", "daily_checks", "signal_trace")}


def run():
    protocol = json.loads(PROTOCOL.read_text())
    validate_protocol(protocol)
    path = ROOT / protocol["reference"]
    if sha256(path.read_bytes()).hexdigest() != protocol["reference_sha256"]:
        raise ValueError("Frozen low-fee reference changed")
    with gzip.open(path, "rt") as stream:
        reference = json.load(stream)
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Low-fee frozen data changed: {key}")
    for path, digest in reference["source_hashes"].items():
        if sha256((ROOT / path).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Shared source changed before low-fee replay: {path}")
    paths = set(reference["source_hashes"]) | {str(PROTOCOL.relative_to(ROOT)),
        "scripts/research_satellite_low_fees.py", "scripts/diagnose_satellite_increment.py",
        "scripts/export_strategy_trial.py"}
    snapshots = {path: (ROOT / path).read_text() for path in sorted(paths)}
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, fund_audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    definitions = {policy.name: policy for policy in policies()} | event_repair_policies() | {"inverse20": "inverse20"}
    results, executed, reused = {}, 0, 0
    for name in protocol["cohorts"]:
        frozen = reference["cohorts"][name]
        data = prepare_economic(prepare_local(funds, frozen["codes"], calendar))
        cache, scenarios, default = {}, {}, None
        for scenario in protocol["scenarios"]:
            trials = {}
            for key in POLICIES:
                trial = satellite_trial(data, calendar, definitions[key], phase=scenario["phase"],
                                        lag=scenario["lag"], fee=Decimal(0), cache=cache)
                verify_fee_only(trial["account"], frozen["full"][key]["account"], scenario["lag"])
                trials[key] = trial
                executed += 1
            episodes = {}
            for episode in reference["regime_protocol"]["episodes"]:
                coverage = effective_episode(data["days"], episode)
                episodes[episode["id"]] = {"coverage": coverage, "policies": {} if coverage["status"] == "insufficient_shared_history"
                    else {key: carry_slice(trial["account"], coverage["start_date"], coverage["end_date"])
                          for key, trial in trials.items()}}
            comparisons = paired_summary(trials)
            if scenario["id"] == "default":
                default = trials
            scenarios[scenario["id"]] = {"comparisons": comparisons, "episodes": episodes,
                "policies": trials if scenario["id"] == "default" else {key: compact(trial) for key, trial in trials.items()}}
            print(json.dumps({"cohort": name, "scenario": scenario["id"], "account_runs": executed,
                "immediate_increment_pp": comparisons["immediate"]["increment_pp"],
                "event_increment_pp": comparisons["event"]["increment_pp"]}), flush=True)
        fresh = {}
        for day in protocol["fresh_entry_dates"]:
            start = max(252, bisect_left(data["days"], day))
            if start >= len(data["days"]) - 21:
                fresh[day] = {"status": "insufficient_shared_history", "policies": {}, "comparisons": None}
                continue
            trials = {}
            for key in POLICIES:
                if start == 252:
                    trials[key] = default[key]
                    reused += 1
                else:
                    trials[key] = satellite_trial(data, calendar, definitions[key], start=start, fee=Decimal(0), cache=cache)
                    verify_fee_only(trials[key]["account"], frozen["full"][key]["account"], 1)
                    executed += 1
            fresh[day] = {"status": "partial" if day < data["days"][252] else "complete",
                          "actual_start": data["days"][start], "comparisons": paired_summary(trials),
                          "policies": {key: compact(trial) for key, trial in trials.items()}}
            print(json.dumps({"cohort": name, "fresh_entry": day, "account_runs": executed}), flush=True)
        results[name] = {"codes": frozen["codes"], "scenarios": scenarios, "fresh_entry": fresh,
                         "old_ordinary_fee_default": paired_summary(frozen["full"] | {})}
    for path, source in snapshots.items():
        if (ROOT / path).read_text() != source:
            raise ValueError(f"Research source changed during low-fee run: {path}")
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol,
            "protocol_sha256": sha256(snapshots[str(PROTOCOL.relative_to(ROOT))].encode()).hexdigest(),
            "reference_sha256": protocol["reference_sha256"],
            "source_snapshots": snapshots, "source_hashes": {path: sha256(source.encode()).hexdigest() for path, source in snapshots.items()},
            **{key: reference[key] for key in ("input_sha256", "bond_input_sha256", "calendar_sha256")},
            "cohorts": results, "new_account_runs": executed, "fresh_reused_main": reused,
            "fund_audit": fund_audit + bond_audit}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/satellite-low-ordinary-fee-research-v1.json.gz")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Preserve the previous low-fee research report")
    result = run()
    with args.output.open("xb") as stream:
        with gzip.open(stream, "wt", encoding="utf-8") as compressed:
            json.dump(result, compressed, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"output": str(args.output), "account_runs": result["new_account_runs"], "reused": result["fresh_reused_main"]}), flush=True)
