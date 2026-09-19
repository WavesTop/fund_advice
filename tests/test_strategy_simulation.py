from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, localcontext
import unittest

from backend.analysis.simulation import SimulationCosts, simulate_strategy
from backend.analysis.strategy import ObservedSessionCalendar, StrategyPolicy, evaluate_strategy
from backend.core.trading_calendar import get_calendar


CALENDAR = get_calendar()


def sessions(count=230):
    return CALENDAR.sessions(date(2025, 1, 2), date(2026, 9, 18))[:count]


def historical_scenario():
    """Rise, cool, resume and break: a coherent long-only market cycle."""
    prices = []
    price = Decimal("100")
    for index, session in enumerate(sessions()):
        if index > 0:
            if index <= 121:
                daily_return = Decimal("0.002")
            elif index <= 150:
                daily_return = Decimal("0.0005")
            elif index <= 205:
                daily_return = Decimal("0.003")
            else:
                daily_return = Decimal("-0.03")
            price *= Decimal(1) + daily_return
        prices.append({"date": session.isoformat(), "close": format(price, "f")})
    return prices


class StrategyTests(unittest.TestCase):
    def test_future_rows_cannot_change_decision(self):
        rows = historical_scenario()
        as_of = rows[130]["date"]
        expected = evaluate_strategy(rows[:131], as_of=as_of, current_weight="0.6", calendar=CALENDAR)
        altered = deepcopy(rows)
        for row in altered[131:]:
            row["close"] = "999999"
        actual = evaluate_strategy(altered, as_of=as_of, current_weight="0.6", calendar=CALENDAR)
        self.assertEqual(actual, expected)
        self.assertEqual(actual["metrics"]["used_through"], as_of)

    def test_incomplete_or_gapped_history_waits_instead_of_inventing_prices(self):
        rows = historical_scenario()[:120]
        decision = evaluate_strategy(rows, as_of=rows[-1]["date"], current_weight="0", calendar=CALENDAR)
        self.assertEqual(decision["action"], "WAIT")
        self.assertEqual(decision["reason_code"], "DATA_INSUFFICIENT")
        gapped = historical_scenario()[:130]
        gapped.pop(50)
        decision = evaluate_strategy(gapped, as_of=gapped[-1]["date"], current_weight="0", calendar=CALENDAR)
        self.assertEqual(decision["action"], "WAIT")
        self.assertIn("missing_sessions", decision["data_quality"]["errors"])

    def test_risk_exit_bypasses_cooldown(self):
        rows = historical_scenario()
        as_of = rows[-1]["date"]
        decision = evaluate_strategy(
            rows,
            as_of=as_of,
            current_weight="0.6",
            calendar=CALENDAR,
            last_trade_date=rows[-2]["date"],
        )
        self.assertEqual(decision["action"], "EXIT")
        self.assertTrue(decision["risk_exit"])
        self.assertEqual(decision["reason_code"], "RISK_EXIT")


class SimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = historical_scenario()
        cls.result = simulate_strategy(
            cls.rows,
            calendar=CALENDAR,
            initial_cash="100000",
            costs=SimulationCosts(buy_fee_bps=Decimal("10"), sell_fee_bps=Decimal("10")),
        )

    def test_full_market_cycle_produces_every_user_action(self):
        counts = self.result["action_counts"]
        for action in ("BUY", "ADD", "HOLD", "REDUCE", "EXIT", "WAIT"):
            with self.subTest(action=action):
                self.assertGreater(counts[action], 0)
        execution_counts = self.result["executed_action_counts"]
        for action in ("BUY", "ADD", "REDUCE", "EXIT"):
            with self.subTest(action=action):
                self.assertGreater(execution_counts[action], 0)
        self.assertTrue(any(row["reason_code"] == "COOLDOWN" for row in self.result["decisions"]))

    def test_decisions_execute_only_on_the_following_session(self):
        index_by_date = {row["date"]: index for index, row in enumerate(self.rows)}
        for trade in self.result["trades"]:
            self.assertEqual(index_by_date[trade["execution_date"]], index_by_date[trade["signal_date"]] + 1)

    def test_cash_units_and_equity_identity_never_break(self):
        with localcontext() as context:
            context.prec = self.result["decimal_precision"]
            for point in self.result["equity_curve"]:
                cash = Decimal(point["cash"])
                units = Decimal(point["units"])
                price = Decimal(point["price"])
                self.assertGreaterEqual(cash, 0)
                self.assertGreaterEqual(units, 0)
                self.assertEqual(cash + units * price, Decimal(point["equity"]))
            for trade in self.result["trades"]:
                self.assertGreaterEqual(Decimal(trade["cash_after"]), 0)
                self.assertGreaterEqual(Decimal(trade["units_after"]), 0)
                target = Decimal(trade["target_weight"])
                if target > 0:
                    actual = (Decimal(trade["units_after"]) * Decimal(trade["price"])
                              / Decimal(trade["equity_after"]))
                    self.assertLessEqual(abs(actual - target), Decimal("1e-25"))

    def test_four_decimal_nav_and_decimal_unit_tails_survive_repeated_rebalancing(self):
        rounded = [
            {"date": row["date"], "close": str(Decimal(row["close"]).quantize(Decimal("0.0001")))}
            for row in self.rows
        ]
        result = simulate_strategy(rounded, calendar=CALENDAR, initial_cash="100000.01")
        self.assertGreaterEqual(len(result["trades"]), 4)
        self.assertTrue(any(len(trade["units_after"].split(".")[-1]) > 8 for trade in result["trades"][:-1]))
        exits = [trade for trade in result["trades"] if trade["action"] == "EXIT"]
        self.assertTrue(exits)
        self.assertTrue(all(trade["units_after"] == "0" for trade in exits))
        self.assertTrue(all(Decimal(point["cash"]) >= 0 and Decimal(point["units"]) >= 0
                            for point in result["equity_curve"]))

    def test_reported_return_drawdown_fees_and_final_equity_recompute(self):
        with localcontext() as context:
            context.prec = self.result["decimal_precision"]
            curve = self.result["equity_curve"]
            initial = Decimal(self.result["initial_equity"])
            final = Decimal(curve[-1]["equity"])
            self.assertEqual(Decimal(self.result["final_equity"]), final)
            self.assertEqual(Decimal(self.result["profit_loss"]), final - initial)
            self.assertEqual(Decimal(self.result["total_return_pct"]), (final / initial - 1) * 100)
            peak = Decimal(curve[0]["equity"])
            drawdowns = []
            for point in curve:
                equity = Decimal(point["equity"])
                peak = max(peak, equity)
                drawdowns.append((equity / peak - 1) * 100)
            self.assertEqual(Decimal(self.result["max_drawdown_pct"]), min(drawdowns))
            self.assertEqual(
                Decimal(self.result["total_fees"]),
                sum((Decimal(trade["fee"]) for trade in self.result["trades"]), Decimal(0)),
            )
        self.assertEqual(self.result["strategy_policy"]["strong_target"], "0.60")
        self.assertEqual(self.result["cost_policy"]["buy_fee_bps"], "10")
        self.assertEqual(self.result["calendar_hash"], CALENDAR.hash)

    def test_flat_short_history_stays_cash_with_a_constant_curve(self):
        rows = [{"date": value.isoformat(), "close": "100"} for value in sessions(40)]
        result = simulate_strategy(rows, calendar=CALENDAR, initial_cash="12345.67")
        self.assertEqual(result["trades"], [])
        self.assertEqual({point["equity"] for point in result["equity_curve"]}, {"12345.67"})
        self.assertEqual(result["total_return_pct"], "0")
        self.assertEqual(result["max_drawdown_pct"], "0")

    def test_policy_and_inputs_fail_closed(self):
        rows = historical_scenario()
        with self.assertRaisesRegex(ValueError, "当前仓位"):
            evaluate_strategy(rows, as_of=rows[-1]["date"], current_weight="1.1", calendar=CALENDAR)
        with self.assertRaisesRegex(ValueError, "价格序列"):
            simulate_strategy(list(reversed(rows)), calendar=CALENDAR, initial_cash="1000")
        with self.assertRaisesRegex(ValueError, "观察窗口"):
            evaluate_strategy(
                rows,
                as_of=rows[-1]["date"],
                current_weight="0",
                calendar=CALENDAR,
                policy=StrategyPolicy(short_window=60, medium_window=20),
            )

    def test_explicit_observed_sessions_support_old_real_series_without_inferring_calendar(self):
        observed = []
        current = date(2020, 1, 2)
        while len(observed) < 150:
            # This fixture stands in for dates already observed in a real NAV
            # series.  The production constructor itself performs no weekday
            # or holiday inference.
            if current.weekday() < 5:
                observed.append(current.isoformat())
            current += timedelta(days=1)
        calendar = ObservedSessionCalendar(observed, source="fixture common actual NAV dates")
        rows = [{"date": value, "close": str(100 + index)} for index, value in enumerate(observed)]
        result = simulate_strategy(rows, calendar=calendar, initial_cash="10000")
        self.assertEqual(result["calendar_basis"], "explicit_common_observation_dates")
        self.assertEqual(result["calendar_document"]["coverage_start"], observed[0])
        self.assertIn("无法识别", result["calendar_document"]["limitations"][1])

        as_of = observed[130]
        expected = evaluate_strategy(rows[:131], as_of=as_of, current_weight="0", calendar=calendar)
        altered = deepcopy(rows)
        for row in altered[131:]:
            row["close"] = "1"
        self.assertEqual(
            evaluate_strategy(altered, as_of=as_of, current_weight="0", calendar=calendar),
            expected,
        )
        missing = rows.copy()
        missing.pop(50)
        with self.assertRaisesRegex(ValueError, "missing_observed_sessions"):
            simulate_strategy(missing, calendar=calendar, initial_cash="10000")


if __name__ == "__main__":
    unittest.main()
