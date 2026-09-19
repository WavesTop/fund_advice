from copy import deepcopy
from decimal import Decimal, localcontext
import unittest

from backend.analysis.strategy_economics import (
    DECIMAL_PRECISION,
    build_exposure_matched_curve,
    build_sleeve_exposure_matched_curve,
    summarize_exposure,
    summarize_holding_cycles,
)


def strategy_curve():
    return [
        {"date": "2026-01-05", "equity": "100", "cash": "100", "market_value": "0"},
        {"date": "2026-01-06", "equity": "100", "cash": "0", "market_value": "100"},
        {"date": "2026-01-07", "equity": "110", "cash": "55", "market_value": "55"},
        {"date": "2026-01-08", "equity": "105", "cash": "105", "market_value": "0"},
    ]


def benchmark_curve():
    return [
        {"date": "2026-01-05", "equity": "100"},
        {"date": "2026-01-06", "equity": "110"},
        {"date": "2026-01-07", "equity": "121"},
        {"date": "2026-01-08", "equity": "108.9"},
    ]


class ExposureMatchedCurveTests(unittest.TestCase):
    def test_uses_previous_observation_exposure_without_lookahead(self):
        curve = build_exposure_matched_curve(strategy_curve(), benchmark_curve())

        # The benchmark gains 10% on day two, but the strategy only becomes
        # invested at that day's close.  The counterfactual cannot receive it.
        self.assertEqual(curve[1]["equity"], "100")
        # The next 10% gain receives the prior close's 100% exposure.
        self.assertEqual(curve[2]["equity"], "110")
        # The final -10% return receives only the prior close's 50% exposure.
        self.assertEqual(curve[3]["equity"], "104.5")
        self.assertEqual(curve[1]["strategy_exposure"], "1")
        self.assertEqual(curve[2]["strategy_exposure_pct"], "50")

        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            for point in curve:
                self.assertEqual(
                    Decimal(point["equity"]),
                    Decimal(point["cash"]) + Decimal(point["market_value"]),
                )
                self.assertGreater(Decimal(point["equity"]), 0)

    def test_is_independent_of_callers_decimal_precision(self):
        with localcontext() as context:
            context.prec = 9
            low = build_exposure_matched_curve(strategy_curve(), benchmark_curve())
        with localcontext() as context:
            context.prec = 64
            high = build_exposure_matched_curve(strategy_curve(), benchmark_curve())
        self.assertEqual(low, high)

    def test_rejects_date_mismatch_and_invalid_assets(self):
        mismatched = benchmark_curve()
        mismatched[3]["date"] = "2026-01-09"
        with self.assertRaisesRegex(ValueError, "日期必须逐日完全对齐"):
            build_exposure_matched_curve(strategy_curve(), mismatched)

        broken_identity = strategy_curve()
        broken_identity[0]["cash"] = "99"
        with self.assertRaisesRegex(ValueError, "资产恒等式"):
            build_exposure_matched_curve(broken_identity, benchmark_curve())

        nonpositive = benchmark_curve()
        nonpositive[1]["equity"] = "0"
        with self.assertRaisesRegex(ValueError, "必须大于0"):
            build_exposure_matched_curve(strategy_curve(), nonpositive)

    def test_rejects_unordered_and_non_decimal_input(self):
        unordered = strategy_curve()
        unordered[1], unordered[2] = unordered[2], unordered[1]
        with self.assertRaisesRegex(ValueError, "严格递增"):
            build_exposure_matched_curve(unordered, benchmark_curve())

        numeric = strategy_curve()
        numeric[0]["equity"] = 100
        with self.assertRaisesRegex(ValueError, "十进制字符串"):
            build_exposure_matched_curve(numeric, benchmark_curve())


class SleeveExposureMatchedCurveTests(unittest.TestCase):
    def setUp(self):
        self.strategy = [
            {"date": "2026-01-05", "equity": "200", "cash": "100", "market_value": "100"},
            {"date": "2026-01-06", "equity": "210", "cash": "50", "market_value": "160"},
            {"date": "2026-01-07", "equity": "209", "cash": "50", "market_value": "159"},
        ]
        self.sleeves = {
            "A": [
                {"date": "2026-01-05", "price": "100", "equity": "100", "cash": "0", "market_value": "100"},
                {"date": "2026-01-06", "price": "110", "equity": "110", "cash": "0", "market_value": "110"},
                {"date": "2026-01-07", "price": "99", "equity": "99", "cash": "0", "market_value": "99"},
            ],
            "B": [
                {"date": "2026-01-05", "price": "100", "equity": "100", "cash": "100", "market_value": "0"},
                {"date": "2026-01-06", "price": "90", "equity": "100", "cash": "50", "market_value": "50"},
                {"date": "2026-01-07", "price": "108", "equity": "110", "cash": "50", "market_value": "60"},
            ],
        }

    def test_preserves_fund_selection_and_uses_lagged_sleeve_weights(self):
        curve = build_sleeve_exposure_matched_curve(self.strategy, self.sleeves)

        # A gained 10% while B lost 10%.  An equal-weight asset return would be
        # zero, but only A was held at the prior close, so the matched account
        # correctly earns 5% on its 50% A weight.
        self.assertEqual(curve[1]["equity"], "210")
        self.assertEqual(curve[1]["matched_daily_return_pct"], "5")
        self.assertEqual(curve[1]["cash_daily_return_pct"], "0")
        self.assertEqual(curve[1]["sleeve_contributions"], [
            {"code": "A", "prior_strategy_weight": "0.5",
             "asset_daily_return_pct": "10", "contribution_pct": "5"},
            {"code": "B", "prior_strategy_weight": "0",
             "asset_daily_return_pct": "-10", "contribution_pct": "0"},
        ])

        # On the next interval A loses 11 while B earns 10 on the exact prior
        # strategy notionals.  The counterfactual therefore moves from 210 to
        # 209, with no return assigned to the remaining cash.
        self.assertEqual(curve[2]["equity"], "209")
        self.assertEqual(curve[2]["cash_daily_return_pct"], "0")
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            self.assertEqual(
                sum((Decimal(item["contribution_pct"])
                     for item in curve[2]["sleeve_contributions"]), Decimal(0)),
                Decimal(curve[2]["matched_daily_return_pct"]),
            )

    def test_is_independent_of_callers_decimal_precision(self):
        with localcontext() as context:
            context.prec = 9
            low = build_sleeve_exposure_matched_curve(self.strategy, self.sleeves)
        with localcontext() as context:
            context.prec = 64
            high = build_sleeve_exposure_matched_curve(self.strategy, self.sleeves)
        self.assertEqual(low, high)

    def test_rejects_unreconciled_or_incomplete_sleeve_evidence(self):
        unreconciled = deepcopy(self.strategy)
        unreconciled[1]["market_value"] = "159"
        unreconciled[1]["cash"] = "51"
        with self.assertRaisesRegex(ValueError, "不能由基金分账户"):
            build_sleeve_exposure_matched_curve(unreconciled, self.sleeves)

        missing_price = deepcopy(self.sleeves)
        missing_price["A"][1].pop("price")
        with self.assertRaisesRegex(ValueError, "十进制字符串"):
            build_sleeve_exposure_matched_curve(self.strategy, missing_price)

        wrong_date = deepcopy(self.sleeves)
        wrong_date["B"][2]["date"] = "2026-01-08"
        with self.assertRaisesRegex(ValueError, "逐日完全对齐"):
            build_sleeve_exposure_matched_curve(self.strategy, wrong_date)


class ExposureSummaryTests(unittest.TestCase):
    def test_reports_average_median_and_low_exposure_days(self):
        summary = summarize_exposure(strategy_curve())
        self.assertEqual(summary, {
            "observation_count": 4,
            "average_exposure": "0.375",
            "average_exposure_pct": "37.5",
            "median_exposure": "0.25",
            "median_exposure_pct": "25",
            "full_cash_observation_days": 2,
            "below_10_percent_exposure_days": 2,
            "invested_observation_days": 2,
            "active_exposure_return_observations": 2,
            "positive_active_exposure_return_observations": 1,
            "positive_active_exposure_return_pct": "50",
        })

    def test_rejects_empty_curve(self):
        with self.assertRaisesRegex(ValueError, "不能为空"):
            summarize_exposure([])


class HoldingCycleSummaryTests(unittest.TestCase):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]

    def test_counts_completed_cycles_on_observation_grid(self):
        trades = [
            {"code": "A", "action": "BUY", "execution_date": "2026-01-01"},
            {"code": "B", "action": "BUY", "execution_date": "2026-01-02"},
            {"code": "A", "action": "ADD", "execution_date": "2026-01-02"},
            {"code": "A", "action": "EXIT", "execution_date": "2026-01-03"},
            {"code": "C", "action": "BUY", "execution_date": "2026-01-03"},
            {"code": "B", "action": "REDUCE", "execution_date": "2026-01-05"},
            {"code": "B", "action": "EXIT", "execution_date": "2026-01-07"},
        ]
        summary = summarize_holding_cycles(trades, self.dates)

        self.assertEqual(summary["completed_cycle_count"], 2)
        self.assertEqual(summary["open_cycle_count"], 1)
        self.assertEqual(summary["median_holding_observation_days"], "3.5")
        self.assertEqual(summary["cycles_lte_10_observation_days"], 2)
        self.assertEqual(summary["cycles_lte_20_observation_days"], 2)
        self.assertEqual(
            [cycle["holding_observations"] for cycle in summary["completed_cycles"]],
            [2, 5],
        )
        self.assertEqual(summary["open_cycles"], [{"code": "C", "entry_date": "2026-01-03"}])

    def test_empty_trades_has_no_synthetic_holding_period(self):
        summary = summarize_holding_cycles([], self.dates)
        self.assertEqual(summary["trade_count"], 0)
        self.assertEqual(summary["completed_cycle_count"], 0)
        self.assertEqual(summary["median_holding_observation_days"], None)
        self.assertEqual(summary["completed_cycles"], [])
        self.assertEqual(summary["open_cycles"], [])

    def test_rejects_misaligned_or_impossible_trade_sequences(self):
        with self.assertRaisesRegex(ValueError, "不在观察日期"):
            summarize_holding_cycles(
                [{"code": "A", "action": "BUY", "execution_date": "2026-01-09"}],
                self.dates,
            )
        with self.assertRaisesRegex(ValueError, "没有已开启"):
            summarize_holding_cycles(
                [{"code": "A", "action": "EXIT", "execution_date": "2026-01-01"}],
                self.dates,
            )
        with self.assertRaisesRegex(ValueError, "再次出现BUY"):
            summarize_holding_cycles([
                {"code": "A", "action": "BUY", "execution_date": "2026-01-01"},
                {"code": "A", "action": "BUY", "execution_date": "2026-01-02"},
            ], self.dates)
        with self.assertRaisesRegex(ValueError, "非递减"):
            summarize_holding_cycles([
                {"code": "A", "action": "BUY", "execution_date": "2026-01-02"},
                {"code": "A", "action": "EXIT", "execution_date": "2026-01-01"},
            ], self.dates)

    def test_rejects_unordered_observation_dates_and_unknown_action(self):
        dates = deepcopy(self.dates)
        dates[0], dates[1] = dates[1], dates[0]
        with self.assertRaisesRegex(ValueError, "严格递增"):
            summarize_holding_cycles([], dates)
        with self.assertRaisesRegex(ValueError, "必须是BUY"):
            summarize_holding_cycles(
                [{"code": "A", "action": "HOLD", "execution_date": "2026-01-01"}],
                self.dates,
            )


if __name__ == "__main__":
    unittest.main()
