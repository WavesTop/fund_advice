from __future__ import annotations

from decimal import Decimal
import unittest

from backend.analysis.strategy_risk import summarize_curve_risk


def curve(values: list[str]) -> list[dict[str, str]]:
    return [
        {"date": f"2026-01-{index + 1:02d}", "equity": value}
        for index, value in enumerate(values)
    ]


class StrategyRiskTests(unittest.TestCase):
    def test_computes_observed_risk_metrics_and_underwater_duration(self):
        result = summarize_curve_risk(
            curve(["100", "110", "99", "99", "118.8"]),
            observations_per_year=Decimal("4"),
        )

        self.assertEqual(result["return_observations"], 4)
        self.assertEqual(result["observations_per_year"], "4")
        self.assertEqual(Decimal(str(result["annualized_return_pct"])), Decimal("18.8"))
        self.assertAlmostEqual(float(str(result["annualized_volatility_pct"])), 22.360679775, places=9)
        self.assertAlmostEqual(float(str(result["sharpe_ratio"])), 0.894427191, places=9)
        self.assertAlmostEqual(float(str(result["sortino_ratio"])), 2.0, places=9)
        self.assertAlmostEqual(float(str(result["calmar_ratio"])), 1.88, places=9)
        self.assertEqual(Decimal(str(result["max_drawdown_pct"])), Decimal("-10"))
        self.assertEqual(result["max_drawdown_duration_observations"], 2)
        self.assertEqual(Decimal(str(result["positive_return_observation_pct"])), Decimal("50"))
        self.assertEqual(Decimal(str(result["worst_observation_return_pct"])), Decimal("-10"))

    def test_returns_none_for_ratios_with_zero_denominators(self):
        flat = summarize_curve_risk(curve(["100", "100", "100"]))
        self.assertEqual(flat["annualized_return_pct"], "0")
        self.assertEqual(flat["annualized_volatility_pct"], "0")
        self.assertIsNone(flat["sharpe_ratio"])
        self.assertIsNone(flat["sortino_ratio"])
        self.assertIsNone(flat["calmar_ratio"])
        self.assertEqual(flat["max_drawdown_duration_observations"], 0)

    def test_uses_only_previous_and_current_observation(self):
        base = summarize_curve_risk(curve(["100", "110", "99"]), observations_per_year=Decimal("2"))
        extended = summarize_curve_risk(
            curve(["100", "110", "99", "198"]), observations_per_year=Decimal("2"),
        )
        self.assertEqual(base["worst_observation_return_pct"], extended["worst_observation_return_pct"])
        self.assertEqual(base["max_drawdown_pct"], extended["max_drawdown_pct"])

    def test_rejects_non_decimal_non_finite_nonpositive_and_unordered_values(self):
        bad_curves = (
            [{"date": "2026-01-01", "equity": "100"},
             {"date": "2026-01-02", "equity": "NaN"}],
            [{"date": "2026-01-01", "equity": "100"},
             {"date": "2026-01-02", "equity": "0"}],
            [{"date": "2026-01-02", "equity": "100"},
             {"date": "2026-01-01", "equity": "101"}],
            [{"date": "2026-01-01", "equity": "100"},
             {"date": "2026-01-02", "equity": 101.0}],
        )
        for item in bad_curves:
            with self.subTest(item=item):
                with self.assertRaises(ValueError):
                    summarize_curve_risk(item)
        with self.assertRaises(TypeError):
            summarize_curve_risk(curve(["100", "101"]), observations_per_year=244)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            summarize_curve_risk(
                curve(["100", "101"]), observations_per_year=Decimal("Infinity"),
            )


if __name__ == "__main__":
    unittest.main()
