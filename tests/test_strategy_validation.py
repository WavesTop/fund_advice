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
from backend.analysis.strategy_portfolio import historical_strategy_report
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
