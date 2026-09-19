from copy import deepcopy
from datetime import date
from decimal import Decimal, localcontext
import json
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.analysis.simulation import SimulationCosts, simulate_strategy
from backend.analysis.strategy import StrategyPolicy, policy_document
from backend.analysis.strategy_economics import (
    build_exposure_matched_curve,
    build_sleeve_exposure_matched_curve,
    summarize_exposure,
    summarize_holding_cycles,
)
from backend.analysis.strategy_portfolio import historical_strategy_report
from backend.analysis.strategy_risk import summarize_curve_risk
from backend.analysis.strategy_validation import validate_strategy_report
from backend.core.trading_calendar import get_calendar
from scripts.validate_strategy import main as cli_main


CALENDAR = get_calendar()


def scenario(count=330):
    sessions = CALENDAR.sessions(date(2025, 1, 2), date(2026, 9, 18))[:count]
    rows, price = [], Decimal("100")
    for index, session in enumerate(sessions):
        if index > 0:
            if index <= 121:
                change = Decimal("0.002")
            elif index <= 150:
                change = Decimal("0.0005")
            elif index <= 205:
                change = Decimal("0.003")
            else:
                change = Decimal("-0.03")
            price *= Decimal(1) + change
        rows.append({"date": session.isoformat(), "close": format(price, "f")})
    return rows


def valid_report():
    rows = scenario()
    simulation = simulate_strategy(
        rows, calendar=CALENDAR, initial_cash="100000",
        costs=SimulationCosts(buy_fee_bps=Decimal("10"), sell_fee_bps=Decimal("10")),
    )
    trades = [{**trade, "code": "510300", "name": "沪深300ETF"} for trade in simulation["trades"]]
    observations = len(rows)
    with localcontext() as context:
        context.prec = simulation["decimal_precision"]
        years = max(Decimal(observations) / Decimal("244"), Decimal(1))
        trades_per_fund_year = format(Decimal(len(trades)) / years, "f")
    return {
        "version": "integrated-fund-strategy-v1",
        "status": "completed",
        "strategy_version": simulation["strategy_version"],
        "generated_at": "2026-05-20T00:00:00.000000Z",
        "start_date": rows[0]["date"],
        "end_date": rows[-1]["date"],
        "calendar_basis": simulation["calendar_version"],
        "calendar_document": {
            "coverage_start": rows[0]["date"], "coverage_end": rows[-1]["date"],
            "session_count": observations,
        },
        "protocol": {"initial_cash": "100000", "buy_fee_bps": "10", "sell_fee_bps": "10",
                     "mode": "historical_replay", "decimal_precision": simulation["decimal_precision"]},
        "universe": {"items": [{"code": "510300", "name": "沪深300ETF"}]},
        "input_identity": {"hash": "a" * 64, "common_observations": observations},
        "curves": [
            {"id": "strategy", "name": "综合策略", "points": deepcopy(simulation["equity_curve"])},
            {"id": "benchmark", "name": "基准", "points": deepcopy(simulation["equity_curve"])},
            {"id": "fund:510300", "name": "基金分账户", "points": deepcopy(simulation["equity_curve"])},
        ],
        "metrics": {
            "initial_equity": simulation["initial_equity"],
            "final_equity": simulation["final_equity"],
            "total_return_pct": simulation["total_return_pct"],
            "benchmark_return_pct": simulation["total_return_pct"],
            "excess_return_pct": "0",
            "max_drawdown_pct": simulation["max_drawdown_pct"],
            "total_fees": simulation["total_fees"],
            "trade_count": len(trades),
            "trades_per_fund_year": trades_per_fund_year,
        },
        "action_summary": simulation["action_counts"],
        "executed_action_summary": simulation["executed_action_counts"],
        "trades": trades,
    }


def v3_report():
    report = valid_report()
    report["version"] = "integrated-fund-strategy-v3"
    report["protocol"].update({
        "point_in_time_observations": True,
        "portfolio_engine": "shared_live_portfolio_engine",
        "execution_terms": "product_specific_versioned_terms",
        "validation_mode": "walk_forward_holdout",
    })
    report["universe"].update({"selection_temporality": "point_in_time"})
    report["universe"]["items"][0].update({
        "excluded_non_session_dates": [],
        "data_eligibility": {"performance_blockers": []},
        "relation_eligibility": {"performance_blockers": []},
    })
    report["observed_date_audit"] = {
        "policy": "weekend_period_end_disclosures_are_not_execution_sessions",
        "excluded_non_session_observations": 0,
        "excluded_non_session_dates": [],
    }
    report["investment_eligibility"] = {
        "status": "ready", "investment_operations_enabled": True, "blockers": [],
    }
    strategy = next(curve["points"] for curve in report["curves"] if curve["id"] == "strategy")
    benchmark = next(curve["points"] for curve in report["curves"] if curve["id"] == "benchmark")
    exposure_matched = build_exposure_matched_curve(strategy, benchmark)
    initial = Decimal(report["protocol"]["initial_cash"])
    cash = [{
        "date": point["date"], "cash": format(initial, "f"), "market_value": "0",
        "equity": format(initial, "f"), "return_pct": "0", "drawdown_pct": "0",
    } for point in strategy]
    report["curves"].extend([
        {"id": "exposure_matched", "name": "同敞口等权无成本对照", "points": exposure_matched},
        {"id": "cash", "name": "现金对照", "points": cash},
    ])
    report["exposure_summary"] = summarize_exposure(strategy)
    report["holding_cycles"] = summarize_holding_cycles(
        report["trades"], [point["date"] for point in strategy],
    )
    exposure_return = Decimal(exposure_matched[-1]["return_pct"])
    strategy_return = Decimal(report["metrics"]["total_return_pct"])
    with localcontext() as context:
        context.prec = report["protocol"]["decimal_precision"]
        report["metrics"].update({
            "exposure_matched_return_pct": format(exposure_return, "f"),
            "exposure_timing_excess_pct": format(strategy_return - exposure_return, "f"),
        })
    base_final = Decimal(report["metrics"]["final_equity"])
    base_fees = Decimal(report["metrics"]["total_fees"])
    trade_count = report["metrics"]["trade_count"]

    def stress(fee_bps: str, final: Decimal, fees: Decimal, requested: bool) -> dict:
        with localcontext() as context:
            context.prec = report["protocol"]["decimal_precision"]
            return {
                "buy_fee_bps": fee_bps, "sell_fee_bps": fee_bps,
                "is_requested_scenario": requested,
                "final_equity": format(final, "f"),
                "total_return_pct": format((final / initial - 1) * 100, "f"),
                "total_fees": format(fees, "f"), "trade_count": trade_count,
            }

    report["cost_stress"] = [
        stress("0", base_final + Decimal("1000"), Decimal(0), False),
        stress("10", base_final, base_fees, True),
        stress("50", base_final - Decimal("2500"), base_fees * 5, False),
        stress("100", base_final - Decimal("6000"), base_fees * 10, False),
    ]
    with localcontext() as context:
        context.prec = report["protocol"]["decimal_precision"]
        zero_cost_return = Decimal(report["cost_stress"][0]["total_return_pct"])
        report["economic_attribution"] = {
            "cash_return_pct": "0",
            "market_exposure_return_pct": format(exposure_return, "f"),
            "signal_and_execution_before_cost_pct": format(
                zero_cost_return - exposure_return, "f"
            ),
            "modeled_transaction_cost_drag_pct": format(
                strategy_return - zero_cost_return, "f"
            ),
            "strategy_total_return_pct": format(strategy_return, "f"),
            "identity": (
                "market_exposure_return_pct + signal_and_execution_before_cost_pct + "
                "modeled_transaction_cost_drag_pct = strategy_total_return_pct"
            ),
        }
    return report


def v4_report():
    report = v3_report()
    report["version"] = "integrated-fund-strategy-v4"
    report["protocol"]["strategy_policy"] = policy_document(StrategyPolicy())
    report["universe"]["items"][0]["catalog_eligibility"] = {"performance_blockers": []}
    return report


def v5_report():
    report = v4_report()
    report["version"] = "integrated-fund-strategy-v5"
    report["protocol"]["benchmarks"] = [
        "cash_zero_return",
        "same_funds_equal_weight_buy_and_hold",
        "same_prior_observation_fund_sleeve_exposure_no_cost",
    ]
    report["investment_eligibility"] = {
        "status": "blocked",
        "investment_operations_enabled": False,
        "blockers": sorted({
            "immutable_source_evidence_chain_missing",
            "point_in_time_universe_missing",
            "point_in_time_observation_revisions_missing",
            "verified_historical_trading_calendar_missing",
            "stable_investable_universe_policy_missing",
            "sector_research_signal_not_integrated",
            "risk_free_cash_return_series_missing",
            "shared_live_portfolio_engine_not_used",
            "atomic_pretrade_revalidation_missing",
            "product_specific_execution_terms_missing",
            "walk_forward_holdout_not_completed",
        }),
    }
    strategy = next(curve["points"] for curve in report["curves"] if curve["id"] == "strategy")
    report["risk_quality"] = summarize_curve_risk(strategy)
    fund = next(curve["points"] for curve in report["curves"] if curve["id"] == "fund:510300")
    matched = build_sleeve_exposure_matched_curve(strategy, {"510300": fund})
    exposure_curve = next(curve for curve in report["curves"] if curve["id"] == "exposure_matched")
    exposure_curve["name"] = "逐基金同敞口无成本对照"
    exposure_curve["points"] = matched
    exposure_return = Decimal(matched[-1]["return_pct"])
    strategy_return = Decimal(report["metrics"]["total_return_pct"])
    with localcontext() as context:
        context.prec = report["protocol"]["decimal_precision"]
        report["metrics"]["exposure_matched_return_pct"] = format(exposure_return, "f")
        report["metrics"]["exposure_timing_excess_pct"] = format(
            strategy_return - exposure_return, "f",
        )
    stress_rows = []
    for fee in ("0", "10", "50", "100"):
        simulation = simulate_strategy(
            scenario(), calendar=CALENDAR, initial_cash="100000",
            costs=SimulationCosts(
                buy_fee_bps=Decimal(fee), sell_fee_bps=Decimal(fee),
            ),
            policy=StrategyPolicy(),
        )
        stress_rows.append({
            "buy_fee_bps": fee,
            "sell_fee_bps": fee,
            "is_requested_scenario": fee == "10",
            "final_equity": simulation["final_equity"],
            "total_return_pct": simulation["total_return_pct"],
            "total_fees": simulation["total_fees"],
            "trade_count": len(simulation["trades"]),
        })
    report["cost_stress"] = stress_rows
    zero_cost = next(
        row for row in report["cost_stress"]
        if row["buy_fee_bps"] == "0" and row["sell_fee_bps"] == "0"
    )
    zero_cost_return = Decimal(zero_cost["total_return_pct"])
    with localcontext() as context:
        context.prec = report["protocol"]["decimal_precision"]
        report["economic_attribution"].update({
            "market_exposure_return_pct": format(exposure_return, "f"),
            "signal_and_execution_before_cost_pct": format(
                zero_cost_return - exposure_return, "f",
            ),
            "modeled_transaction_cost_drag_pct": format(
                strategy_return - zero_cost_return, "f",
            ),
            "strategy_total_return_pct": format(strategy_return, "f"),
        })
    return report


def rewrite_curve(points, loss_pct):
    last = len(points) - 1
    for index, point in enumerate(points):
        loss = loss_pct * Decimal(index) / Decimal(last)
        equity = Decimal("100000") * (Decimal(1) - loss)
        return_pct = (equity / Decimal("100000") - 1) * 100
        point.pop("price", None)
        point.pop("units", None)
        point.update({"cash": format(equity, "f"), "market_value": "0", "equity": format(equity, "f"),
                      "return_pct": format(return_pct, "f"), "drawdown_pct": format(return_pct, "f")})


class StrategyValidationTests(unittest.TestCase):
    def test_complete_multi_environment_report_passes_and_is_deterministic(self):
        report = valid_report()
        result = validate_strategy_report(report)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["blocking_errors"], [])
        self.assertEqual(result["review_findings"], [])
        self.assertEqual(result["coverage"]["common_observations"], 330)
        self.assertIsNone(result["coverage"]["date_intersection_audit"])
        self.assertEqual(len(result["performance"]["calendar_years"]), 2)
        second_year = result["performance"]["calendar_years"][1]
        strategy_points = next(
            curve["points"] for curve in report["curves"] if curve["id"] == "strategy"
        )
        prior_year_end = next(
            point for point in reversed(strategy_points)
            if point["date"] < second_year["strategy"]["start_date"]
        )
        current_year_end = next(
            point for point in reversed(strategy_points)
            if point["date"] <= second_year["strategy"]["end_date"]
        )
        expected_return = (
            Decimal(current_year_end["equity"]) / Decimal(prior_year_end["equity"]) - 1
        ) * 100
        self.assertEqual(second_year["strategy"]["return_base_date"], prior_year_end["date"])
        self.assertEqual(Decimal(second_year["strategy"]["return_pct"]), expected_return)
        self.assertEqual([row["segment"] for row in result["performance"]["time_segments"]],
                         ["earlier", "later"])
        self.assertEqual(result["validation_hash"], validate_strategy_report(deepcopy(report))["validation_hash"])
        self.assertTrue(all(result["actions"]["executed_counts"][action] > 0
                            for action in ("BUY", "ADD", "REDUCE", "EXIT")))

    def test_source_date_gap_is_reported_and_the_audit_is_reproducible(self):
        rows = scenario()
        missing_date = rows[160]["date"]
        sources = [rows, [row for row in rows if row["date"] != missing_date]]
        items = []
        for index, source_rows in enumerate(sources, 1):
            code = f"51030{index}"
            items.append({
                "code": code,
                "name": f"审计基金{index}",
                "index_code": f"00030{index}",
                "index_name": f"审计指数{index}",
                "relation_source_id": f"relation-{index}",
                "relation_evidence_url": f"https://example.test/{index}",
                "relation_verified_at": "2026-05-20T00:00:00Z",
                "series_kind": "nav",
                "series_source_id": f"series-{index}",
                "series_policy_version": "test-series-v1",
                "series_updated_at": "2026-05-20T00:00:00Z",
                "catalog_source_id": "catalog-manager-documents",
                "catalog_eligibility": {"performance_blockers": []},
                "data_eligibility": {"performance_blockers": []},
                "relation_eligibility": {"performance_blockers": []},
                "excluded_non_session_dates": [],
                "rows": deepcopy(source_rows),
                "observation_count": len(source_rows),
                "first_date": source_rows[0]["date"],
                "last_date": source_rows[-1]["date"],
            })
        universe = {
            "version": "integrated-fund-strategy-v2",
            "generated_at": "2026-05-20T00:00:00Z",
            "items": items,
            "excluded": [],
            "selection": "test_fixed_sources",
            "selection_temporality": "point_in_time_fixture",
        }
        with patch("backend.analysis.strategy_portfolio.eligible_fund_universe", return_value=universe):
            report = historical_strategy_report(object())  # type: ignore[arg-type]

        audit = report["date_intersection_audit"]
        self.assertEqual(audit["union_observations"], len(rows))
        self.assertEqual(audit["common_observations"], len(rows) - 1)
        self.assertEqual(audit["intersection_excluded_dates"], [missing_date])
        by_code = {item["code"]: item for item in audit["sources"]}
        self.assertEqual(by_code["510301"]["missing_dates"], [])
        self.assertEqual(by_code["510301"]["excluded_by_intersection_dates"], [missing_date])
        self.assertEqual(by_code["510302"]["missing_dates"], [missing_date])
        self.assertEqual(by_code["510302"]["excluded_by_intersection_dates"], [])
        self.assertNotIn(missing_date, [point["date"] for point in report["curves"][0]["points"]])

        result = validate_strategy_report(report)
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["checks"]["data_integrity"], "passed")
        self.assertEqual(result["checks"]["history_coverage"], "review_required")
        self.assertIn("source_date_gap", {item["code"] for item in result["review_findings"]})
        self.assertEqual(
            result["coverage"]["date_intersection_audit"]["intersection_excluded_dates"],
            [missing_date],
        )

        tampered = deepcopy(report)
        tampered["date_intersection_audit"]["sources"][1]["missing_observations"] = 0
        blocked = validate_strategy_report(tampered)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn(
            "date_intersection_count_mismatch",
            {item["code"] for item in blocked["blocking_errors"]},
        )

    def test_negative_return_is_performance_review_not_data_failure(self):
        report = valid_report()
        for curve in report["curves"]:
            if curve["id"] in {"strategy", "benchmark"}:
                rewrite_curve(
                    curve["points"],
                    Decimal("0.10") if curve["id"] == "strategy" else Decimal("0.05"),
                )
        report["metrics"].update({
            "final_equity": "90000", "total_return_pct": "-10", "benchmark_return_pct": "-5",
            "excess_return_pct": "-5", "max_drawdown_pct": "-10",
        })
        result = validate_strategy_report(report)
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["checks"]["data_integrity"], "passed")
        codes = {item["code"] for item in result["review_findings"]}
        self.assertIn("negative_strategy_return", codes)
        self.assertIn("strategy_underperformed_benchmark", codes)
        self.assertEqual(Decimal(result["performance"]["comparison"]["strategy_return_pct"]), Decimal("-10"))

    def test_nan_negative_balance_and_duplicate_dates_are_blocked(self):
        mutations = []
        nan = valid_report()
        nan["curves"][0]["points"][10]["cash"] = "NaN"
        mutations.append((nan, "non_finite_decimal"))
        negative = valid_report()
        point = negative["curves"][0]["points"][10]
        point["cash"] = "-1"
        point["market_value"] = format(Decimal(point["equity"]) + 1, "f")
        mutations.append((negative, "negative_cash"))
        duplicate = valid_report()
        duplicate["curves"][0]["points"][1]["date"] = duplicate["curves"][0]["points"][0]["date"]
        mutations.append((duplicate, "duplicate_curve_dates"))
        for report, expected in mutations:
            with self.subTest(expected=expected):
                result = validate_strategy_report(report)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(expected, {item["code"] for item in result["blocking_errors"]})

    def test_no_trades_and_single_action_require_review(self):
        no_trades = valid_report()
        observations = no_trades["input_identity"]["common_observations"]
        no_trades["trades"] = []
        no_trades["executed_action_summary"] = {action: 0 for action in ("BUY", "ADD", "REDUCE", "EXIT")}
        no_trades["action_summary"] = {"BUY": 0, "ADD": 0, "HOLD": observations // 2,
                                       "REDUCE": 0, "EXIT": 0, "WAIT": observations - observations // 2}
        for curve in no_trades["curves"]:
            if curve["id"] not in {"strategy", "fund:510300"}:
                continue
            for point in curve["points"]:
                point.update({"cash": "100000", "units": "0", "market_value": "0",
                              "equity": "100000", "return_pct": "0", "drawdown_pct": "0"})
        benchmark_return = Decimal(no_trades["metrics"]["benchmark_return_pct"])
        with localcontext() as context:
            context.prec = no_trades["protocol"]["decimal_precision"]
            no_trade_excess = format(-benchmark_return, "f")
        no_trades["metrics"].update({
            "final_equity": "100000", "total_return_pct": "0",
            "excess_return_pct": no_trade_excess, "max_drawdown_pct": "0",
            "trade_count": 0, "total_fees": "0", "trades_per_fund_year": "0",
        })
        result = validate_strategy_report(no_trades)
        self.assertEqual(result["status"], "review_required")
        self.assertIn("strategy_never_trades", {item["code"] for item in result["review_findings"]})

        single = valid_report()
        single["trades"] = [next(trade for trade in single["trades"] if trade["action"] == "BUY")]
        buy = single["trades"][0]
        fund_curve = next(curve for curve in single["curves"] if curve["id"] == "fund:510300")
        with localcontext() as context:
            context.prec = single["protocol"]["decimal_precision"]
            cash = Decimal(buy["cash_after"])
            units = Decimal(buy["units_after"])
            initial = Decimal(fund_curve["points"][0]["equity"])
            peak = initial
            for point in fund_curve["points"]:
                if point["date"] >= buy["execution_date"]:
                    market = units * Decimal(point["price"])
                    equity = cash + market
                    peak = max(peak, equity)
                    point.update({
                        "cash": format(cash, "f"), "units": format(units, "f"),
                        "market_value": format(market, "f"), "equity": format(equity, "f"),
                        "return_pct": format((equity / initial - 1) * 100, "f"),
                        "drawdown_pct": format((equity / peak - 1) * 100, "f"),
                    })
        strategy_curve = next(curve for curve in single["curves"] if curve["id"] == "strategy")
        strategy_curve["points"] = deepcopy(fund_curve["points"])
        single["executed_action_summary"] = {
            action: sum(trade["action"] == action for trade in single["trades"])
            for action in ("BUY", "ADD", "REDUCE", "EXIT")
        }
        single["action_summary"] = {
            "BUY": 1, "ADD": 0, "HOLD": observations - 1,
            "REDUCE": 0, "EXIT": 0, "WAIT": 0,
        }
        with localcontext() as context:
            context.prec = single["protocol"]["decimal_precision"]
            fees = sum((Decimal(trade["fee"]) for trade in single["trades"]), Decimal(0))
            years = Decimal(330) / Decimal(244)
            final = Decimal(strategy_curve["points"][-1]["equity"])
            strategy_return = (final / initial - 1) * 100
            benchmark_return = Decimal(single["metrics"]["benchmark_return_pct"])
            single_metrics = {
                "final_equity": format(final, "f"), "total_return_pct": format(strategy_return, "f"),
                "excess_return_pct": format(strategy_return - benchmark_return, "f"),
                "max_drawdown_pct": min(
                    (point["drawdown_pct"] for point in strategy_curve["points"]), key=Decimal
                ),
                "trade_count": len(single["trades"]), "total_fees": format(fees, "f"),
                "trades_per_fund_year": format(Decimal(len(single["trades"])) / years, "f"),
            }
        single["metrics"].update({
            **single_metrics,
        })
        result = validate_strategy_report(single)
        self.assertEqual(result["status"], "review_required")
        self.assertIn("single_executed_action", {item["code"] for item in result["review_findings"]})

    def test_trade_amount_fee_timing_and_fund_state_tampering_are_blocked(self):
        mutations = []

        gross = valid_report()
        gross["trades"][0]["gross_amount"] = format(
            Decimal(gross["trades"][0]["gross_amount"]) + Decimal("1"), "f"
        )
        mutations.append((gross, "trade_gross_units_mismatch"))

        fee = valid_report()
        fee["trades"][0]["fee"] = format(Decimal(fee["trades"][0]["fee"]) + Decimal("1"), "f")
        mutations.append((fee, "trade_fee_mismatch"))

        delayed = valid_report()
        dates = [point["date"] for point in delayed["curves"][0]["points"]]
        first_trade = delayed["trades"][0]
        first_trade["execution_date"] = dates[dates.index(first_trade["signal_date"]) + 2]
        mutations.append((delayed, "trade_not_next_observation"))

        post_state = valid_report()
        post_state["trades"][0]["cash_after"] = format(
            Decimal(post_state["trades"][0]["cash_after"]) + Decimal("1"), "f"
        )
        post_state["trades"][0]["equity_after"] = format(
            Decimal(post_state["trades"][0]["equity_after"]) + Decimal("1"), "f"
        )
        mutations.append((post_state, "trade_post_state_curve_mismatch"))

        unreported = valid_report()
        fund_points = next(
            curve["points"] for curve in unreported["curves"] if curve["id"] == "fund:510300"
        )
        trade_dates = {trade["execution_date"] for trade in unreported["trades"]}
        point = next(
            point for index, point in enumerate(fund_points)
            if index > 0 and point["date"] not in trade_dates
        )
        point["cash"] = format(Decimal(point["cash"]) + Decimal("1"), "f")
        point["equity"] = format(Decimal(point["equity"]) + Decimal("1"), "f")
        mutations.append((unreported, "unreported_fund_state_change"))

        for report, expected in mutations:
            with self.subTest(expected=expected):
                result = validate_strategy_report(report)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(expected, {item["code"] for item in result["blocking_errors"]})

    def test_exit_must_zero_units_and_signal_precedes_execution(self):
        report = valid_report()
        exit_trade = next(trade for trade in report["trades"] if trade["action"] == "EXIT")
        exit_trade["units_after"] = "0.1"
        exit_trade["equity_after"] = format(
            Decimal(exit_trade["cash_after"]) + Decimal("0.1") * Decimal(exit_trade["price"]), "f"
        )
        report["trades"][0]["execution_date"] = report["trades"][0]["signal_date"]
        result = validate_strategy_report(report)
        codes = {item["code"] for item in result["blocking_errors"]}
        self.assertEqual(result["status"], "blocked")
        self.assertIn("exit_units_not_zero", codes)
        self.assertIn("trade_not_after_signal", codes)

    def test_v3_recomputes_economic_evidence_and_surfaces_prudence_findings(self):
        report = v3_report()
        result = validate_strategy_report(report)
        self.assertNotEqual(result["status"], "blocked")
        self.assertEqual(result["checks"]["data_integrity"], "passed")
        self.assertEqual(
            Decimal(result["economic"]["exposure_summary"]["average_exposure"]),
            Decimal(report["exposure_summary"]["average_exposure"]),
        )
        self.assertEqual(result["economic"]["holding_cycles"], report["holding_cycles"])
        codes = {item["code"] for item in result["review_findings"]}
        self.assertIn("cost_sensitive", codes)
        self.assertTrue(
            {"low_market_exposure", "underperformed_exposure_matched",
             "short_holding_period"}.intersection(codes)
        )

    def test_v3_requires_economic_curves_and_evidence(self):
        report = v3_report()
        report["curves"] = [
            curve for curve in report["curves"] if curve["id"] not in {"cash", "exposure_matched"}
        ]
        report.pop("cost_stress")
        report.pop("economic_attribution")
        result = validate_strategy_report(report)
        codes = {item["code"] for item in result["blocking_errors"]}
        self.assertEqual(result["status"], "blocked")
        self.assertIn("required_curve_missing", codes)
        self.assertIn("cost_stress_missing", codes)
        self.assertIn("economic_attribution_missing", codes)

    def test_v3_economic_tampering_is_blocked(self):
        mutations = []

        exposure_curve = v3_report()
        point = next(curve for curve in exposure_curve["curves"]
                     if curve["id"] == "exposure_matched")["points"][8]
        point["strategy_exposure_pct"] = format(
            Decimal(point["strategy_exposure_pct"]) + Decimal("1"), "f"
        )
        mutations.append((exposure_curve, "exposure_matched_not_reproducible"))

        cash_curve = v3_report()
        point = next(curve for curve in cash_curve["curves"] if curve["id"] == "cash")["points"][8]
        point["cash"] = "99999"
        point["equity"] = "99999"
        point["return_pct"] = "-0.001"
        point["drawdown_pct"] = "-0.001"
        mutations.append((cash_curve, "cash_curve_not_constant"))

        exposure_summary = v3_report()
        exposure_summary["exposure_summary"]["full_cash_observation_days"] += 1
        mutations.append((exposure_summary, "exposure_summary_not_reproducible"))

        holding_cycles = v3_report()
        holding_cycles["holding_cycles"]["completed_cycle_count"] += 1
        mutations.append((holding_cycles, "holding_cycles_not_reproducible"))

        cost_stress = v3_report()
        requested = next(item for item in cost_stress["cost_stress"]
                         if item["is_requested_scenario"])
        requested["total_fees"] = format(Decimal(requested["total_fees"]) + 1, "f")
        mutations.append((cost_stress, "cost_stress_requested_result_mismatch"))

        metric = v3_report()
        metric["metrics"]["exposure_timing_excess_pct"] = format(
            Decimal(metric["metrics"]["exposure_timing_excess_pct"]) + 1, "f"
        )
        mutations.append((metric, "metric_not_reproducible"))

        attribution = v3_report()
        attribution["economic_attribution"]["modeled_transaction_cost_drag_pct"] = "0"
        mutations.append((attribution, "economic_attribution_not_reproducible"))

        for report, expected in mutations:
            with self.subTest(expected=expected):
                result = validate_strategy_report(report)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(expected, {item["code"] for item in result["blocking_errors"]})

    def test_v4_replays_signals_and_rejects_target_or_policy_tampering(self):
        report = v4_report()
        result = validate_strategy_report(report)
        self.assertNotEqual(result["status"], "blocked")
        self.assertEqual(result["checks"]["data_integrity"], "passed")

        target = deepcopy(report)
        target["trades"][0]["target_weight"] = "0.999999"
        target_result = validate_strategy_report(target)
        target_codes = {item["code"] for item in target_result["blocking_errors"]}
        self.assertEqual(target_result["status"], "blocked")
        self.assertTrue({"trade_target_weight_not_reached", "strategy_target_weight_mismatch"}
                        .intersection(target_codes))

        signal = deepcopy(report)
        signal["trades"][0]["signal_action"] = "EXIT"
        signal_result = validate_strategy_report(signal)
        self.assertIn(
            "strategy_signal_action_mismatch",
            {item["code"] for item in signal_result["blocking_errors"]},
        )

        policy = deepcopy(report)
        policy["protocol"]["strategy_policy"]["strong_target"] = "0.95"
        policy_result = validate_strategy_report(policy)
        self.assertIn(
            "strategy_policy_not_reproducible",
            {item["code"] for item in policy_result["blocking_errors"]},
        )

    def test_v5_rebuilds_fund_level_exposure_and_blocks_attribution_tampering(self):
        report = v5_report()
        result = validate_strategy_report(report)
        self.assertNotEqual(result["status"], "blocked")
        self.assertEqual(result["checks"]["data_integrity"], "passed")

        contribution = deepcopy(report)
        point = next(curve for curve in contribution["curves"]
                     if curve["id"] == "exposure_matched")["points"][130]
        point["sleeve_contributions"][0]["prior_strategy_weight"] = format(
            Decimal(point["sleeve_contributions"][0]["prior_strategy_weight"])
            + Decimal("0.01"),
            "f",
        )
        contribution_result = validate_strategy_report(contribution)
        self.assertIn(
            "exposure_sleeve_contributions_not_reproducible",
            {item["code"] for item in contribution_result["blocking_errors"]},
        )

        cash_return = deepcopy(report)
        point = next(curve for curve in cash_return["curves"]
                     if curve["id"] == "exposure_matched")["points"][130]
        point["cash_daily_return_pct"] = "0.01"
        cash_result = validate_strategy_report(cash_return)
        self.assertIn(
            "exposure_matched_not_reproducible",
            {item["code"] for item in cash_result["blocking_errors"]},
        )

        protocol = deepcopy(report)
        protocol["protocol"]["benchmarks"][-1] = "same_daily_exposure_equal_weight_no_cost"
        protocol_result = validate_strategy_report(protocol)
        self.assertIn(
            "exposure_benchmark_protocol_mismatch",
            {item["code"] for item in protocol_result["blocking_errors"]},
        )

        cost = deepcopy(report)
        zero = next(item for item in cost["cost_stress"]
                    if item["buy_fee_bps"] == "0" and item["sell_fee_bps"] == "0")
        initial = Decimal(cost["protocol"]["initial_cash"])
        zero["final_equity"] = format(Decimal(zero["final_equity"]) + Decimal("500"), "f")
        zero["total_return_pct"] = format(
            (Decimal(zero["final_equity"]) / initial - 1) * 100, "f",
        )
        exposure_return = Decimal(cost["economic_attribution"]["market_exposure_return_pct"])
        strategy_return = Decimal(cost["metrics"]["total_return_pct"])
        zero_return = Decimal(zero["total_return_pct"])
        cost["economic_attribution"]["signal_and_execution_before_cost_pct"] = format(
            zero_return - exposure_return, "f",
        )
        cost["economic_attribution"]["modeled_transaction_cost_drag_pct"] = format(
            strategy_return - zero_return, "f",
        )
        cost_result = validate_strategy_report(cost)
        self.assertIn(
            "cost_stress_not_independently_reproducible",
            {item["code"] for item in cost_result["blocking_errors"]},
        )

    def test_v5_recomputes_risk_quality_and_blocks_tampering(self):
        report = v5_report()
        result = validate_strategy_report(report)
        self.assertNotEqual(result["status"], "blocked")
        self.assertEqual(result["risk_quality"]["version"], report["risk_quality"]["version"])
        self.assertEqual(
            Decimal(str(result["risk_quality"]["annualized_return_pct"])),
            Decimal(str(report["risk_quality"]["annualized_return_pct"])),
        )

        for field, value in (
            ("annualized_return_pct", "999"),
            ("sharpe_ratio", "999"),
            ("max_drawdown_duration_observations", 999),
            ("observations_per_year", "365"),
        ):
            with self.subTest(field=field):
                tampered = deepcopy(report)
                tampered["risk_quality"][field] = value
                validation = validate_strategy_report(tampered)
                self.assertEqual(validation["status"], "blocked")
                self.assertIn(
                    "risk_quality_not_reproducible",
                    {item["code"] for item in validation["blocking_errors"]},
                )

        missing = deepcopy(report)
        missing.pop("risk_quality")
        validation = validate_strategy_report(missing)
        self.assertIn(
            "risk_quality_missing",
            {item["code"] for item in validation["blocking_errors"]},
        )

    def test_cli_outputs_json_without_validation_storage(self):
        report = valid_report()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "validation.json"
            with patch("scripts.validate_strategy.historical_strategy_report", return_value=report), \
                 patch("sys.stdout", StringIO()):
                self.assertEqual(cli_main(["--database", str(Path(directory) / "unused.sqlite3"),
                                           "--output", str(output)]), 0)
            saved = json.loads(output.read_text())
            self.assertEqual(saved["status"], "passed")
            self.assertEqual(list(Path(directory).glob("*.sqlite3")), [])
            with patch("scripts.validate_strategy.historical_strategy_report", return_value=report), \
                 patch("sys.stderr", StringIO()):
                self.assertEqual(cli_main(["--output", str(output)]), 2)


if __name__ == "__main__":
    unittest.main()
