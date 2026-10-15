"""Matched, nonliquidating action probes from real incumbent account states."""
from datetime import datetime, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.fund_allocation import AllocationExecution, AllocationTargets, simulate_allocations
from backend.analysis.matured_trend_forecast import MaturedForecaster
from backend.analysis.portfolio_increment import MODES, initial_target, prepare_economic, safety_target
from backend.analysis.trend_risk_budget import projected_volatility
from backend.core.trading_calendar import TradingCalendar
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import carry_slice, effective_episode
from scripts.research_portfolio_increments import economic_trial
from scripts.validate_allocation_checkpoint import validate_checkpoint_lineage
from scripts.validate_fund_allocation import validate_allocation

PROTOCOL = ROOT / "config/holding-action-diagnostic-v1.json"
EXECUTION = AllocationExecution(review_sessions=1, initial_review=True, rebalance_band=.03,
                                relative_rebalance_band=.25, reinvest_on_settlement=True)


class ContinuingCore:
    def __init__(self, data, start=252):
        self.data, self.start = data, start

    def __call__(self, index, held, state):
        if (index - self.start) % 20 == 0:
            weights, _ = initial_target(self.data, index)
            failed = frozenset(c for c in held if self.data["economic_stats"][index]["votes"][c] < 2/3)
            return AllocationTargets(weights, failed, enforce_risk_exits=True)
        weights, _, exits = safety_target(self.data, index, state)
        return None if weights is None else AllocationTargets(weights, frozenset(exits), enforce_risk_exits=True)


def adjustment_target(data, index, state, candidate):
    current = dict(state.weights)
    stats = data["economic_stats"][index]
    failed = {c for c, w in current.items() if w > 0 and stats["votes"][c] < 2/3}
    target = {c: 0. if c in failed else w for c, w in current.items()}
    stocks = data["class_groups"]["stock"]
    if candidate != "cash" and (candidate not in stocks or stats["votes"][candidate] < 2/3):
        raise ValueError("Adjustment candidate is not currently eligible")
    donors = [c for c in stocks if c != candidate and target[c] > 0]
    available = sum(target[c] for c in donors)
    wanted = .1 if candidate == "cash" else min(.1, max(0., .35 - target[candidate]))
    moved = min(wanted, available)
    for code in donors:
        target[code] *= 1 - moved/available
    if candidate != "cash":
        addition = moved if available else min(wanted, max(0., .4 - sum(target[c] for c in stocks)), max(0., 1 - sum(target.values())))
        target[candidate] += addition
    risk = projected_volatility(stats["risk"], target)
    scale = min(1., .1/risk) if risk else 1.
    target = {c: w*scale for c, w in target.items()}
    exits = failed | ({c for c, w in current.items() if w > 0} if scale < 1 else set())
    return AllocationTargets(target, frozenset(exits), enforce_risk_exits=True)


class HoldingAction:
    def __init__(self, data, candidate=None, staged=False):
        self.data, self.candidate, self.requested = data, candidate, False
        self.action_date = None
        self.staged, self.intent = staged, None

    def __call__(self, index, held, state):
        if self.candidate is not None and not self.requested:
            self.requested = True
            self.action_date = state.signal_date
            # A pre-existing queued order can postpone the fork's first callback.
            if self.candidate != "cash" and self.data["economic_stats"][index]["votes"][self.candidate] < 2/3:
                return None
            target = adjustment_target(self.data, index, state, self.candidate)
            if self.staged and self.candidate != "cash":
                self.intent = dict(target.weights)
            return target
        weights, _, exits = safety_target(self.data, index, state)
        if self.intent is not None:
            if self.data["economic_stats"][index]["votes"][self.candidate] < 2/3:
                self.intent = None
            else:
                for c in self.intent:
                    if self.data["economic_stats"][index]["votes"][c] < 2/3:
                        self.intent[c] = 0.
                risk = projected_volatility(self.data["economic_stats"][index]["risk"], self.intent)
                scale = min(1., .1/risk) if risk else 1.
                target = {c: w*scale for c, w in self.intent.items()}
                tolerance = min(.03, target[self.candidate]*.25)
                if abs(state.weights[self.candidate] - target[self.candidate]) <= tolerance:
                    self.intent = None
                elif weights is None:
                    return AllocationTargets(target)
        return None if weights is None else AllocationTargets(weights, frozenset(exits), enforce_risk_exits=True)


def summary(rows):
    eligible = [r for r in rows if r["ranked"] is not None]
    return {"samples": len(rows), "eligible_samples": len(eligible),
            "mean_ranked_increment_pp": statistics.fmean(r["ranked_increment_pp"] for r in eligible) if eligible else None,
            "mean_equal_eligible_increment_pp": statistics.fmean(r["equal_eligible_increment_pp"] for r in eligible) if eligible else None,
            "positive_ranked_samples": sum(r["ranked_increment_pp"] > 1e-8 for r in eligible),
            "ranked_better_than_equal_samples": sum(r["ranked_increment_pp"] > r["equal_eligible_increment_pp"] + 1e-8 for r in eligible),
            "one_shot_ranked_filled_samples": sum(r["trials"][r["ranked"]]["candidate_buy_executions"] > 0 for r in eligible),
            "mean_staged_ranked_increment_pp": statistics.fmean(r["staged_ranked_increment_pp"] for r in eligible) if eligible else None,
            "mean_staged_equal_increment_pp": statistics.fmean(r["staged_equal_increment_pp"] for r in eligible) if eligible else None,
            "positive_staged_ranked_samples": sum(r["staged_ranked_increment_pp"] > 1e-8 for r in eligible),
            "staged_ranked_filled_samples": sum(r["trials"][f"staged:{r['ranked']}"]["candidate_buy_executions"] > 0 for r in eligible)}


def run():
    protocol = json.loads(PROTOCOL.read_text())
    reference_path = ROOT / protocol["reference"]
    reference = json.load(gzip.open(reference_path, "rt"))
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Frozen holding-action input changed: {key}")
    allowed = {"backend/analysis/fund_allocation.py", "scripts/validate_fund_allocation.py"}
    changed = {}
    for path, digest in reference["source_hashes"].items():
        actual = sha256((ROOT/path).read_bytes()).hexdigest()
        if actual != digest:
            if path not in allowed:
                raise ValueError(f"Unrelated holding-action source changed: {path}")
            changed[path] = {"reference": digest, "current": actual}
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, audits = load_funds(INPUT, calendar)
    bonds, bond_audits = load_funds(BONDS, calendar)
    funds.update(bonds)
    results, compatible = {}, 0
    for name in protocol["cohorts"]:
        frozen = reference["cohorts"][name]
        data = prepare_economic(prepare_local(funds, frozen["codes"], calendar))
        data["increment_records"] = frozen["training_records"]
        data["increment_forecasters"] = {m: MaturedForecaster(rows) for m, rows in frozen["training_records"].items()}
        data["increment_model_audit_cache"] = {m: {} for m in MODES}
        for policy, old in frozen["full"].items():
            new = economic_trial(data, calendar, policy)
            if new["metrics"] != old["metrics"] or new["account"] != old["account"]:
                raise ValueError(f"Default engine changed prior account: {name}/{policy}")
            compatible += 1
        indices = list(range(protocol["first_sample_index"], len(data["days"]) - max(protocol["horizons_sessions"]) - 1,
                             protocol["sample_stride_sessions"]))
        parent = simulate_allocations(data, calendar, ContinuingCore(data), execution=EXECUTION,
                                      include_portfolio_state=True, liquidate_at_end=False, capture_state_indices=frozenset(indices))
        parent_audit = validate_allocation(parent, data, calendar)
        samples, resumptions = [], []
        for index in indices:
            seed = parent["account_checkpoints"][data["days"][index]]
            validate_checkpoint_lineage(seed, parent, data, calendar)
            if index in (indices[0], indices[len(indices)//2], indices[-1]):
                resumed = simulate_allocations(data, calendar, ContinuingCore(data), start=index, execution=EXECUTION,
                    include_portfolio_state=True, initial_state=seed, liquidate_at_end=False)
                if (resumed["equity_curve"] != parent["equity_curve"][index-252:]
                        or resumed["final_state"] != parent["final_state"]
                        or resumed["trades"] != [t for t in parent["trades"] if t["execution_date"] >= data["days"][index]]):
                    raise ValueError("Real-data checkpoint continuation differs from full account")
                resumptions.append(validate_allocation(resumed, data, calendar))
            eligible = [c for c in data["class_groups"]["stock"] if data["economic_stats"][index]["votes"][c] >= 2/3]
            entries = {r["code"]: r for r in data["frames"][index]["entries"]}
            ranked = min(eligible, key=lambda c: (-entries[c]["scores"]["medium"], c)) if eligible else None
            for horizon in protocol["horizons_sessions"]:
                end = index + horizon + 1
                trials = {}
                actions = [(None, False), ("cash", False), *[(c, False) for c in eligible], *[(c, True) for c in eligible]]
                for candidate, staged in actions:
                    allocate = HoldingAction(data, candidate, staged)
                    account = simulate_allocations(data, calendar, allocate, start=index, end=end, execution=EXECUTION,
                        initial_state=seed, include_portfolio_state=True, liquidate_at_end=False, resume_review=True)
                    audited = validate_allocation(account, data, calendar)
                    key = "hold" if candidate is None else f"staged:{candidate}" if staged else candidate
                    inherited = {q["origin_decision"]["signal_date"] for q in seed["queued_orders"]}
                    fills = sum(t["action"] == "BUY" and t["code"] == candidate and t["signal_date"] not in inherited for t in account["trades"])
                    trials[key] = {"account": account, "ledger_validation": audited, "action_date": allocate.action_date,
                                   "candidate_buy_executions": fills}
                hold = trials["hold"]["account"]["total_return_pct"]
                increments = {c: t["account"]["total_return_pct"] - hold for c, t in trials.items() if c != "hold"}
                samples.append({"signal_date": data["days"][index], "horizon_sessions": horizon,
                    "mark_date": data["days"][end], "seed": seed, "eligible": eligible, "ranked": ranked,
                    "hold_return_pct": hold, "increments_pp": increments,
                    "ranked_increment_pp": increments[ranked] if ranked else 0.,
                    "equal_eligible_increment_pp": statistics.fmean(increments[c] for c in eligible) if eligible else 0.,
                    "staged_ranked_increment_pp": increments[f"staged:{ranked}"] if ranked else 0.,
                    "staged_equal_increment_pp": statistics.fmean(increments[f"staged:{c}"] for c in eligible) if eligible else 0.,
                    "hindsight_best_increment_pp": max([0., *increments.values()]), "trials": trials})
        episodes = {}
        for episode in reference["regime_protocol"]["episodes"]:
            coverage = effective_episode(data["days"], episode)
            episodes[episode["id"]] = {"coverage": coverage}
            if coverage["status"] != "insufficient_shared_history":
                episodes[episode["id"]]["parent_carry"] = carry_slice(parent, coverage["start_date"], coverage["end_date"])
            for horizon in protocol["horizons_sessions"]:
                # Include only complete within-regime action windows, not a bull entry and bear exit.
                subset = [r for r in samples if r["horizon_sessions"] == horizon
                          and episode["start"] <= r["signal_date"] and r["mark_date"] <= episode["end"]]
                episodes[episode["id"]][f"actions{horizon}"] = summary(subset)
        results[name] = {"codes": frozen["codes"], "parent": parent, "parent_ledger_validation": parent_audit,
                         "continuation_validations": resumptions, "samples": samples, "episodes": episodes,
                         "summaries": {str(h): summary([r for r in samples if r["horizon_sessions"] == h]) for h in protocol["horizons_sessions"]}}
        print(json.dumps({"cohort": name, "parent_return_pct": parent["total_return_pct"],
                          "summaries": results[name]["summaries"]}, ensure_ascii=False), flush=True)
    paths = set(reference["source_hashes"]) | {"backend/analysis/allocation_checkpoint.py",
        "scripts/validate_allocation_checkpoint.py", "scripts/diagnose_holding_actions.py"}
    snapshots = {p: (ROOT/p).read_text() for p in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol, "cohorts": results,
            "unchanged_previous_main_accounts": compatible, "allowed_source_changes": changed,
            "source_snapshots": snapshots, "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()},
            "reference_sha256": sha256(reference_path.read_bytes()).hexdigest(), "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(),
            **{k: reference[k] for k in ("input_sha256", "bond_input_sha256", "calendar_sha256")}, "fund_audit": audits + bond_audits}


if __name__ == "__main__":
    report = run()
    output = ROOT / "runtime/verification/holding-action-diagnostic-v1.json.gz"
    with gzip.open(output, "wt", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"output": str(output)}))
