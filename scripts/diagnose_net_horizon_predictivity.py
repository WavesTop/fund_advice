"""Matured, cash-settled net holding labels across fixed investment horizons."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, localcontext
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.class_return_forecast import CLASSES, MODES, class_features
from backend.analysis.cross_asset_trend import class_statistics, prepare_class_proxies
from backend.analysis.fund_allocation import AllocationExecution, simulate_allocations
from backend.analysis.fund_holding_costs import FundHoldingCosts
from backend.analysis.matured_trend_forecast import MaturedForecaster, audit_training_visibility
from backend.core.trading_calendar import TradingCalendar
from scripts.diagnose_trend_predictivity import subsequent_return
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
from scripts.research_local_trend_models import prepare_local
from scripts.research_market_regimes import effective_episode
from scripts.validate_class_return_forecast import rebuilt_features
from scripts.validate_fund_allocation import validate_allocation

HORIZONS = (3, 10, 20, 60, 120)
PROTOCOL = ROOT / "config/net-horizon-predictivity-v1.json"


def label_dates(data, index, horizon, calendar):
    if (isinstance(horizon, bool) or horizon not in HORIZONS or index < 252
            or index + horizon + 3 >= len(data["days"])):
        raise ValueError("Net horizon label requires a complete holding and settlement interval")
    entry, exit_index, settled = index + 1, index + horizon + 1, index + horizon + 3
    fee = FundHoldingCosts().redemption(data["days"][entry], data["days"][exit_index], Decimal(10), calendar)
    if fee["settlement_date"] != data["days"][settled]:
        raise ValueError("Shared NAV calendar does not match the exchange settlement calendar")
    return {"horizon_sessions": horizon, "purchase_date": data["days"][entry],
            "exit_date": data["days"][exit_index], "settlement_date": fee["settlement_date"],
            "holding_confirmation_days": fee["holding_calendar_days"], "exit_fee_bps": int(fee["sell_fee_bps"])}


def net_labels(data, index, horizon, calendar):
    dates = label_dates(data, index, horizon, calendar)
    values = {name: statistics.fmean(subsequent_return(data, c, index, horizon, calendar) / 100
                                     for c in data["class_groups"][name]) for name in CLASSES}
    return values, dates


def net_records(data, horizon, mode, calendar, *, feature_cache=None, label_cache=None):
    if mode not in MODES or horizon not in HORIZONS or isinstance(horizon, bool):
        raise ValueError("Unknown net horizon or forecast mode")
    rows = []
    for index in range(252, len(data["days"]) - horizon - 3, 10):
        key = (horizon, index)
        if label_cache is not None and key in label_cache:
            outcomes, dates = label_cache[key]
        else:
            outcomes, dates = net_labels(data, index, horizon, calendar)
            if label_cache is not None:
                label_cache[key] = outcomes, dates
        for name in CLASSES:
            feature_key = (mode, index, name)
            if feature_cache is not None and feature_key in feature_cache:
                features = feature_cache[feature_key]
            else:
                features = class_features(data, name, index, mode)
                if feature_cache is not None:
                    feature_cache[feature_key] = features
            rows.append({"signal_date": data["days"][index], "label_end": dates["settlement_date"],
                         "code": name, "features": features, "outcomes": [outcomes[name]], "net_label": dates})
    return rows


def audit_net_records(records, data, horizon, mode, calendar, *, feature_cache=None):
    if horizon not in HORIZONS or isinstance(horizon, bool) or mode not in MODES:
        raise ValueError("Invalid audited net horizon")
    wanted = {(data["days"][i], c): i for i in range(252, len(data["days"]) - horizon - 3, 10) for c in CLASSES}
    observed = {(r["signal_date"], r["code"]): r for r in records}
    if len(observed) != len(records) or set(observed) != set(wanted):
        raise ValueError("Net label records differ from the declared training grid")
    with localcontext() as ctx:
        ctx.prec = 40
        for key, index in wanted.items():
            row, name = observed[key], key[1]
            first, last = index + 1, index + horizon + 1
            purchase, exit_day = date.fromisoformat(data["days"][first]), date.fromisoformat(data["days"][last])
            holding = (calendar.shift(exit_day, 1) - calendar.shift(purchase, 1)).days
            fee = 150 if holding < 7 else 10
            settled = calendar.shift(exit_day, 2).isoformat()
            expected = {"horizon_sessions": horizon, "purchase_date": purchase.isoformat(), "exit_date": exit_day.isoformat(),
                        "settlement_date": settled, "holding_confirmation_days": holding, "exit_fee_bps": fee}
            if row["label_end"] != settled or row["net_label"] != expected:
                raise ValueError("Net holding label matured before cash settlement or used different fees")
            feature_key = (mode, index, name)
            if feature_cache is not None and feature_key in feature_cache:
                features = feature_cache[feature_key]
            else:
                features = rebuilt_features(data, name, index, mode)
                if feature_cache is not None:
                    feature_cache[feature_key] = features
            if not np.isfinite(row["features"]).all() or not np.allclose(row["features"], features, rtol=1e-8, atol=1e-9):
                raise ValueError("Net forecast features differ from independent past raw-data rebuild")
            members = data["class_groups"][name]
            wealth = Decimal(0)
            for code in members:
                units = Decimal(1) / len(members) / Decimal(str(data["prices"][code][first])) / Decimal("1.001")
                wealth += units * Decimal(str(data["prices"][code][last])) * (1 - Decimal(fee) / 10000)
                wealth += units * sum((Decimal(str(v)) for v in data["distributions"][code][first + 1:last + 1]), Decimal(0))
            if (len(row["outcomes"]) != 1 or not math.isfinite(row["outcomes"][0])
                    or abs(row["outcomes"][0] - float(wealth - 1)) > 1e-10):
                raise ValueError("Net label differs from purchased units, cash dividends and terminal redemption")
    return {"status": "passed", "records": len(records), "distinct_dates": len(wanted) // 3}


def ledger_probe(data, index, horizon, name, calendar):
    outcomes, dates = net_labels(data, index, horizon, calendar)
    members = data["class_groups"][name]
    account = simulate_allocations(data, calendar, lambda i, held: dict.fromkeys(members, 1 / len(members)),
        start=index, end=index + horizon + 1, fee_bps=Decimal(10), initial=Decimal(100000),
        execution=AllocationExecution(initial_review=True, review_sessions=len(data["days"]) + 1, reinvest_on_settlement=False))
    checked = validate_allocation(account, data, calendar)
    buys = {t["code"]: t for t in account["trades"] if t["action"] == "BUY"}
    if len(buys) != len(members) or set(buys) != set(members):
        raise ValueError("Holding probe did not purchase each class member exactly once")
    with localcontext() as ctx:
        ctx.prec = 40
        nominal = (Decimal(str(1 / len(members))) * 100000).quantize(Decimal(".01"), rounding=ROUND_DOWN)
        budgets = {c: Decimal(t["gross_amount"]) + Decimal(t["fee"]) for c, t in buys.items()}
        if any(not nominal - Decimal(".01000001") <= v <= nominal + Decimal(".00000001") for v in budgets.values()):
            raise ValueError("Holding probe budget differs by more than permitted final cent rounding")
        expected = Decimal(0)
        for code, budget in budgets.items():
            price = Decimal(str(data["prices"][code][index + 1]))
            per_unit = Decimal(str(data["prices"][code][index + horizon + 1])) * (1 - Decimal(dates["exit_fee_bps"]) / 10000)
            per_unit += sum((Decimal(str(v)) for v in data["distributions"][code][index + 2:index + horizon + 2]), Decimal(0))
            expected += budget / 100000 * (per_unit / (price * Decimal("1.001")) - 1) * 100
        capital = float(sum(budgets.values()) / 100000)
        fractions = {c: float(v / 100000) for c, v in budgets.items()}
        expected = float(expected)
    if abs(account["total_return_pct"] - expected) > 1e-8:
        raise ValueError("Unit net label does not match the cents-funded FIFO holding account")
    sells = [t for t in account["trades"] if t["action"] == "SELL"]
    if (len(sells) != len(members) or any(int(t["sell_fee_bps"]) != dates["exit_fee_bps"]
            or t["settlement_date"] != dates["settlement_date"] for t in sells)):
        raise ValueError("Holding probe terminal fee or settlement differs")
    return {"class": name, "signal_date": data["days"][index], **dates, "unit_net_label_pct": outcomes[name] * 100,
            "invested_initial_capital_fraction": capital, "per_fund_capital_fractions": fractions,
            "account_net_return_pct": account["total_return_pct"], "budget_adjusted_label_pct": expected,
            "ledger_validation": checked, "account": account}


def audit_prediction(records, learning, prediction, features, day):
    if (learning["asof"] != day or learning["window_start"] != (date.fromisoformat(day) - timedelta(days=1096)).isoformat()
            or learning["status"] == "fitted" and learning["penalty"] != 1
            or learning["status"] == "insufficient_history" and learning["minimum_dates"] != 24):
        raise ValueError("Net model fitting protocol or visible date differs")
    checked = audit_training_visibility(records, learning)
    if learning["status"] != "fitted":
        if prediction:
            raise ValueError("Unfitted net horizon must not emit invented predictions")
        return checked
    if learning["distinct_dates"] < 24 or set(prediction) != set(CLASSES):
        raise ValueError("Net model fitted too early or predicted different classes")
    z = np.clip((np.asarray(features) - np.asarray(learning["means"])) / np.asarray(learning["scales"]), -5, 5)
    expected = z @ np.asarray(learning["coefficients"]) + np.asarray(learning["intercept"])
    for i, name in enumerate(CLASSES):
        if not math.isfinite(prediction[name]) or abs(prediction[name] - max(-1., float(expected[i, 0]))) > 1e-9:
            raise ValueError("Net predictions do not follow the independently audited model")
    return checked


def describe(rows, *, both_eligible=False):
    fitted = [r for r in rows if all(r["learning"][m]["status"] == "fitted" for m in MODES)]
    selected = [r for r in fitted if not both_eligible or all(r["eligible"][c] for c in ("stock", "gold"))]
    modes = {}
    for mode in MODES:
        errors, correct, passing, eligible_actual = [], [], [], []
        for row in selected:
            actual = row["net_labels"]["stock"] - row["net_labels"]["gold"]
            predicted = row["predictions"][mode]["stock"] - row["predictions"][mode]["gold"]
            errors.append((actual - predicted) ** 2)
            if abs(actual) > 1e-12:
                correct.append((predicted > 0) == (actual > 0))
            for name in ("stock", "gold"):
                if row["eligible"][name]:
                    eligible_actual.append(row["net_labels"][name] > 0)
                    if row["predictions"][mode][name] > 0:
                        passing.append(row["net_labels"][name] > 0)
        modes[mode] = {"spread_rmse_pp": statistics.fmean(errors) ** .5 * 100 if errors else None,
                       "stock_gold_winner_accuracy": statistics.fmean(correct) if correct else None,
                       "winner_trials": len(correct), "positive_forecast_count": len(passing),
                       "positive_forecast_precision": statistics.fmean(passing) if passing else None,
                       "eligible_risky_labels": len(eligible_actual),
                       "eligible_unconditional_positive_fraction": statistics.fmean(eligible_actual) if eligible_actual else None}
    return {"evaluation_dates": len(rows), "both_models_fitted_dates": len(fitted), "selected_dates": len(selected), "modes": modes}


def evaluate(data, horizon, record_sets, calendar):
    models = {m: MaturedForecaster(rows) for m, rows in record_sets.items()}
    lookup = {m: {(r["signal_date"], r["code"]): r for r in rows} for m, rows in record_sets.items()}
    rows = []
    for index in range(252, len(data["days"]) - horizon - 3, max(10, horizon)):
        day = data["days"][index]
        outcomes, dates = net_labels(data, index, horizon, calendar)
        predicted, learning, audits = {}, {}, {}
        for mode, forecaster in models.items():
            model, metadata = forecaster.at(day)
            features = [lookup[mode][day, name]["features"] for name in CLASSES]
            predictions = {} if model is None else dict(zip(CLASSES, (max(-1., float(v)) for v in model.predict(features)[:, 0])))
            audits[mode] = audit_prediction(record_sets[mode], metadata, predictions, features, day)
            predicted[mode], learning[mode] = predictions, metadata
        rows.append({"signal_date": day, **dates, "net_labels": outcomes, "predictions": predicted, "learning": learning,
                     "model_audit": audits, "eligible": {name: data["cross_stats"][index]["signals"][name]["vote"] >= 2/3 for name in CLASSES}})
    return rows


def run():
    protocol = json.loads(PROTOCOL.read_text())
    reference_path = ROOT / protocol["reference"]
    reference = json.load(gzip.open(reference_path, "rt", encoding="utf-8"))
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Net horizon frozen input changed: {key}")
    for path, digest in reference["source_hashes"].items():
        if sha256((ROOT / path).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Net horizon reference source changed: {path}")
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, audit = load_funds(INPUT, calendar)
    bonds, bond_audit = load_funds(BONDS, calendar)
    funds.update(bonds)
    results = {}
    for name in protocol["cohorts"]:
        frozen = reference["cohorts"][name]
        data = prepare_class_proxies(prepare_local(funds, frozen["codes"], calendar))
        data["cross_stats"] = {i: class_statistics(data, i) for i in range(252, len(data["days"]))}
        features, labels, rebuilt = {}, {}, {}
        horizons = {}
        for horizon in protocol["horizons"]:
            record_sets = {m: net_records(data, horizon, m, calendar, feature_cache=features, label_cache=labels) for m in MODES}
            record_checks = {m: audit_net_records(rows, data, horizon, m, calendar, feature_cache=rebuilt) for m, rows in record_sets.items()}
            rows = evaluate(data, horizon, record_sets, calendar)
            indexes = sorted({i for h, i in labels if h == horizon})
            probes = {indexes[0], indexes[-1], indexes[len(indexes) // 2]}
            for fee in (10, 150):
                match = next((i for i in indexes if labels[horizon, i][1]["exit_fee_bps"] == fee), None)
                if match is not None:
                    probes.add(match)
            checks = [ledger_probe(data, i, horizon, group, calendar) for i in sorted(probes) for group in CLASSES]
            episodes = {}
            for episode in reference["regime_protocol"]["episodes"]:
                coverage = effective_episode(data["days"], episode)
                contained = [r for r in rows if episode["start"] <= r["signal_date"] and r["settlement_date"] <= episode["end"]]
                episodes[episode["id"]] = {"shared_history_coverage": coverage, "all": describe(contained),
                                           "both_eligible": describe(contained, both_eligible=True)}
            horizons[str(horizon)] = {"all": describe(rows), "both_eligible": describe(rows, both_eligible=True),
                "record_validation": record_checks, "training_records": record_sets, "evaluation_rows": rows,
                "ledger_probes": checks, "episodes": episodes}
        results[name] = {"codes": frozen["codes"], "horizons": horizons}
        print(json.dumps({"cohort": name, "net_horizon_diagnostics": {h: r["all"] for h, r in horizons.items()}}, ensure_ascii=False), flush=True)
    paths = set(reference["source_hashes"]) | {"scripts/diagnose_net_horizon_predictivity.py", "scripts/diagnose_trend_predictivity.py"}
    snapshots = {p: (ROOT / p).read_text(encoding="utf-8") for p in sorted(paths)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cohorts": results, "protocol": protocol,
            "regime_protocol": reference["regime_protocol"], "protocol_sha256": sha256(PROTOCOL.read_bytes()).hexdigest(),
            "source_snapshots": snapshots, "source_hashes": {p: sha256(s.encode()).hexdigest() for p, s in snapshots.items()},
            "reference_sha256": sha256(reference_path.read_bytes()).hexdigest(),
            "input_sha256": reference["input_sha256"], "bond_input_sha256": reference["bond_input_sha256"],
            "calendar_sha256": reference["calendar_sha256"], "fund_audit": audit + bond_audit}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/net-horizon-predictivity-v1.json.gz")
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output)}))


if __name__ == "__main__":
    main()
