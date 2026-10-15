from copy import deepcopy
from datetime import date, timedelta
import json
import math
from pathlib import Path
from types import SimpleNamespace
import unittest

import numpy as np

from backend.analysis.matured_trend_forecast import MaturedForecaster, feature_vector
from backend.core.trading_calendar import TradingCalendar
from scripts.research_calibrated_trends import outcome_records, prepare
from scripts.research_local_trend_models import BlendedForecaster, ExpiringAllocator, audit_forecaster, plans, prepare_local


class LocalTrendModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "config/research-exchange-calendar-2009-2026.json"
        cls.calendar = TradingCalendar(json.loads(path.read_text()))

    def funds(self):
        days = [d.isoformat() for d in self.calendar.sessions(date(2023, 1, 1), date(2024, 11, 1))][:320]
        funds = {}
        for j, code in enumerate(("a", "b", "000217", "bond", "000051")):
            prices = [math.exp(.0003 * i + .02 * math.sin(i / (5 + j))) for i in range(len(days))]
            funds[code] = {"code": code, "daily_start": days[0], "last_date": days[-1],
                           "rows": {d: {"unit_nav": str(p)} for d, p in zip(days, prices)},
                           "distributions": dict.fromkeys(days, 0.), "theme": "黄金" if code == "000217" else "other",
                           "role": "defensive" if code == "bond" else "sector"}
        return funds, ["a", "b", "000217", "bond"]

    def test_restored_features_match_old_features_after_old_warmup(self):
        funds, codes = self.funds()
        local = prepare_local(funds, codes, self.calendar)
        legacy = prepare(funds, codes, self.calendar)
        for index in (252, 262, 280):
            for code in codes:
                np.testing.assert_allclose(local["vectors"][index][code], legacy["vectors"][index][code], rtol=0, atol=1e-14)

    def test_early_features_do_not_read_future_or_negative_indexes(self):
        funds, codes = self.funds()
        original = prepare_local(funds, codes, self.calendar)
        altered = deepcopy(funds)
        asof = original["days"][120]
        for fund in altered.values():
            for day, row in fund["rows"].items():
                if day > asof:
                    row["unit_nav"] = str(float(row["unit_nav"]) * 100)
        changed = prepare_local(altered, codes, self.calendar)
        for code in codes:
            self.assertEqual(original["vectors"][120][code], changed["vectors"][120][code])
        with self.assertRaises(ValueError):
            feature_vector(original, "a", 119)

    def test_added_samples_preserve_the_previous_training_grid(self):
        funds, codes = self.funds()
        data = prepare_local(funds, codes, self.calendar)
        old = outcome_records(data, self.calendar, 20)
        restored = outcome_records(data, self.calendar, 20, start=122)
        retained = [r for r in restored if r["signal_date"] >= data["days"][252]]
        self.assertEqual(old, retained)
        self.assertGreater(len(restored), len(old))

    def allocation_fixture(self):
        funds, codes = self.funds()
        data = prepare_local(funds, codes, self.calendar)
        state = {"high": True}
        model = SimpleNamespace(predict=lambda x: np.asarray([[.8, -5], [-.8, -5]]) if state["high"] else np.asarray([[.1, -5], [-.1, -5]]))
        teacher = SimpleNamespace(at=lambda day: (model, {"status": "fitted", "asof": day, "validation_rank_ic": .2}))
        return data, state, {20: teacher}

    def test_expiry_releases_a_weakening_satellite_but_not_before_deadline(self):
        data, state, teachers = self.allocation_fixture()
        allocator = ExpiringAllocator(data, "ridge20", teachers, expiry=20)
        allocator(252, set())
        self.assertEqual(allocator.active, ["a"])
        self.assertEqual(allocator.deadlines["a"], 272)
        state["high"] = False
        allocator(262, set())
        self.assertEqual(allocator.active, ["a"])
        result = allocator(272, set())
        self.assertEqual(allocator.active, [])
        self.assertAlmostEqual(result.weights["a"], .2)

    def test_strong_satellite_renews_without_changing_target_weight(self):
        data, _, teachers = self.allocation_fixture()
        allocator = ExpiringAllocator(data, "ridge20", teachers, expiry=20)
        first = allocator(252, set())
        renewed = allocator(272, set())
        self.assertEqual(first.weights, renewed.weights)
        self.assertEqual(allocator.deadlines["a"], 292)
        self.assertEqual(allocator.renewals[-1]["renewed"], ["a"])

    def records(self, multiplier=1):
        rows = []
        for i in range(50):
            day = date(2020, 1, 1) + timedelta(days=10 * i)
            for j, code in enumerate(("a", "b")):
                rows.append({"signal_date": day.isoformat(), "label_end": (day + timedelta(days=35)).isoformat(),
                             "code": code, "features": [float(j)], "outcomes": [multiplier * (j - .5), -j - 2]})
        return rows

    def test_blend_only_uses_available_mature_models(self):
        teacher, local = MaturedForecaster(self.records()), MaturedForecaster(self.records(-1))
        blend = BlendedForecaster(teacher, local)
        model, metadata = blend.at("2020-12-01")
        a, _ = teacher.at("2020-12-01")
        b, _ = local.at("2020-12-01")
        np.testing.assert_allclose(model.predict([[0], [1]]), (a.predict([[0], [1]]) + b.predict([[0], [1]])) / 2)
        self.assertEqual(audit_forecaster(blend, metadata)["status"], "passed")
        missing = BlendedForecaster(teacher, MaturedForecaster(self.records()[:10]))
        fallback, meta = missing.at("2020-12-01")
        self.assertEqual(meta["available_components"], ["teacher"])
        np.testing.assert_allclose(fallback.predict([[0]]), a.predict([[0]]))
        self.assertEqual(audit_forecaster(missing, meta)["components"]["local"]["status"], "not_fitted")
        changed = deepcopy(metadata)
        changed["components"]["local"]["asof"] = "2021-01-01"
        with self.assertRaises(ValueError):
            audit_forecaster(blend, changed)

    def test_duplicate_transfer_and_self_trials_are_not_counted_twice(self):
        self.assertEqual(len(plans()), 9)
        self.assertEqual(len(plans(transfer=True)), 14)


if __name__ == "__main__":
    unittest.main()
