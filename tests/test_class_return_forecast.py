from copy import deepcopy
from datetime import date
import json
import math
from pathlib import Path
import unittest

import numpy as np

from backend.analysis.class_return_forecast import (
    CLASSES, ClassForecastAllocator, capital_payoff_choice, class_features,
    class_holding_return, class_records,
)
from backend.analysis.cross_asset_trend import class_statistics, prepare_class_proxies
from backend.analysis.fund_allocation import AllocationState
from backend.analysis.matured_trend_forecast import MaturedForecaster
from backend.core.trading_calendar import TradingCalendar
from scripts.research_class_return_forecasts import forecast_trial
from scripts.research_local_trend_models import prepare_local
from scripts.validate_class_return_forecast import audit_class_records, audit_forecast_choice


class ClassReturnForecastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "config/research-exchange-calendar-2009-2026-v2.json"
        cls.calendar = TradingCalendar(json.loads(path.read_text()))
        days = [d.isoformat() for d in cls.calendar.sessions(date(2020, 1, 1), date(2025, 1, 1))][:820]
        funds = {}
        for j, code in enumerate(("stock", "000217", "bond", "000051")):
            prices = [math.exp(.0002 * i + .035 * math.sin(i / (14 + j))) for i in range(len(days))]
            funds[code] = {"code": code, "daily_start": days[0], "last_date": days[-1],
                           "rows": {d: {"unit_nav": str(p)} for d, p in zip(days, prices)},
                           "distributions": dict.fromkeys(days, 0.),
                           "theme": "黄金" if code == "000217" else "other",
                           "role": "defensive" if code == "bond" else "sector"}
        cls.base = prepare_class_proxies(prepare_local(funds, ["stock", "000217", "bond"], cls.calendar))
        cls.base["cross_stats"] = {i: class_statistics(cls.base, i) for i in range(252, len(days))}
        cls.base["class_records"] = {m: class_records(cls.base, m) for m in ("class_mean", "trend_ridge")}

    def data(self):
        data = deepcopy(self.base)
        data["class_forecasters"] = {m: MaturedForecaster(r) for m, r in data["class_records"].items()}
        data["training_audit_cache"] = {m: {} for m in data["class_records"]}
        return data

    def decision(self, mode, index=650):
        data = self.data()
        allocator = ClassForecastAllocator(data, mode, data["class_records"][mode])
        state = AllocationState(index, data["days"][index], dict.fromkeys(data["prices"], 0.))
        target = allocator(index, set(), state)
        return data, allocator, target

    def test_next_nav_fixed_units_and_dividend_boundary(self):
        data = self.data()
        for code in data["prices"]:
            data["prices"][code] = np.full(820, 2.)
            data["distributions"][code][253] = .8
            data["distributions"][code][254] = .1
            data["distributions"][code][313] = .3
            data["distributions"][code][314] = .9
        self.assertAlmostEqual(class_holding_return(data, "stock", 252), .2)
        for index in (251, 759):
            with self.assertRaises(ValueError):
                class_holding_return(data, "stock", index)

    def test_record_grid_features_and_independent_labels(self):
        data = self.data()
        for mode, length in (("class_mean", 3), ("trend_ridge", 13)):
            self.assertEqual(len(class_features(data, "gold", 252, mode)), length)
            self.assertEqual(audit_class_records(data, data["class_records"][mode], mode)["status"], "passed")
            damaged = deepcopy(data["class_records"][mode])
            damaged[0]["outcomes"][0] += .01
            with self.assertRaises(ValueError):
                audit_class_records(data, damaged, mode)
        with self.assertRaises(ValueError):
            class_features(data, "stock", 251, "trend_ridge")

    def test_future_features_and_immature_outcomes_cannot_change_past_decision(self):
        original, allocator, target = self.decision("trend_ridge")
        changed = self.data()
        asof = changed["days"][650]
        for code in changed["marks"]:
            changed["marks"][code][651:] *= 100
        for row in changed["class_records"]["trend_ridge"]:
            if row["label_end"] > asof:
                row["outcomes"] = [123.]
        other = ClassForecastAllocator(changed, "trend_ridge", changed["class_records"]["trend_ridge"])
        state = AllocationState(650, asof, dict.fromkeys(changed["prices"], 0.))
        self.assertEqual(target, other(650, set(), state))
        self.assertEqual(allocator.trace, other.trace)
        for name in CLASSES:
            self.assertEqual(class_features(original, name, 650, "trend_ridge"), class_features(changed, name, 650, "trend_ridge"))

    def test_fitted_models_and_cash_scaled_choices_are_audited(self):
        for mode in ("class_mean", "trend_ridge"):
            data, allocator, target = self.decision(mode)
            entry = allocator.trace[0]
            self.assertEqual(entry["forecast_choice"]["learning"]["status"], "fitted")
            self.assertLessEqual(sum(target.weights.values()), 1 + 1e-12)
            self.assertLessEqual(entry["projected_volatility"], .1 + 1e-12)
            audit_forecast_choice(entry, data, 650, mode, data["class_records"][mode])

    def test_unfitted_fallback_is_explicit(self):
        data, allocator, _ = self.decision("trend_ridge", 252)
        entry = allocator.trace[0]
        self.assertEqual(entry["forecast_choice"]["chosen"], "raw_fallback")
        self.assertEqual(entry["forecast_choice"]["predicted_gross_returns"], {})
        audit_forecast_choice(entry, data, 252, "trend_ridge", data["class_records"]["trend_ridge"])

    def test_payoff_uses_risk_scaled_capital_and_costs(self):
        data = self.data()
        stats = deepcopy(data["cross_stats"][650])
        for c in CLASSES:
            stats["signals"][c]["vote"] = 1.
            stats["signals"][c]["floored_annual_vol"] = .1
        codes = stats["fund_risk"]["codes"]
        stats["fund_risk"]["covariance"] = np.diag([{"stock": .4, "000217": .1, "bond": .005}[c] ** 2 for c in codes])
        current = dict.fromkeys(codes, 0.)
        _, chosen, rows = capital_payoff_choice(data, stats, current, {"stock": .06, "gold": .04, "bond": 0.}, 10.)
        self.assertNotEqual(chosen, "stock")
        self.assertGreater(rows["gold"]["capital_payoff_proxy"], rows["stock"]["capital_payoff_proxy"])
        stats["fund_risk"]["covariance"] = np.diag([.0001] * len(codes))
        current.update(stock=.6, bond=.4)
        forecast = {"stock": .01, "gold": .0101, "bond": 0.}
        self.assertEqual(capital_payoff_choice(data, stats, current, forecast, 0.)[1], "gold")
        self.assertEqual(capital_payoff_choice(data, stats, current, forecast, 10.)[1], "stock")
        self.assertEqual(capital_payoff_choice(data, stats, dict.fromkeys(codes, 0.), dict.fromkeys(CLASSES, 0.), 0.)[1], "cash")

    def test_metadata_tampering_is_rejected(self):
        data, allocator, _ = self.decision("trend_ridge")
        for field in ("prediction", "coefficient", "date", "candidate", "chosen"):
            entry = deepcopy(allocator.trace[0])
            choice = entry["forecast_choice"]
            if field == "prediction":
                choice["predicted_gross_returns"]["stock"] += .1
            elif field == "coefficient":
                choice["learning"]["coefficients"][0][0] += .1
            elif field == "date":
                choice["learning"]["asof"] = data["days"][651]
            elif field == "candidate":
                choice["candidates"]["cash"]["capital_payoff_proxy"] += .1
            else:
                choice["chosen"] = "invented"
            with self.assertRaises(ValueError):
                audit_forecast_choice(entry, data, 650, "trend_ridge", data["class_records"]["trend_ridge"])

    def test_shared_cache_must_use_same_records(self):
        data = self.data()
        records = deepcopy(data["class_records"]["trend_ridge"])
        records[0]["outcomes"][0] += .1
        with self.assertRaises(ValueError):
            ClassForecastAllocator(data, "trend_ridge", records, forecaster=data["class_forecasters"]["trend_ridge"])

    def test_actual_account_checks_fifo_funding_risk_and_forecast(self):
        for mode in ("class_mean", "trend_ridge"):
            result = forecast_trial(self.data(), self.calendar, mode, 5, start=620, end=680)
            self.assertEqual(result["ledger_validation"]["status"], "passed")
            self.assertEqual(result["current_holdings_validation"]["status"], "passed")
            self.assertGreater(result["forecast_validation"]["fitted_decisions"], 0)


if __name__ == "__main__":
    unittest.main()
