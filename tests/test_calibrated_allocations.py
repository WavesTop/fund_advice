from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import numpy as np

from backend.core.trading_calendar import TradingCalendar
from scripts.research_calibrated_trends import CalibratedAllocator, outcome_records
from scripts.compare_defensive_risk_controls import passive_target


class CalibratedAllocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "config/research-exchange-calendar-2009-2026.json"
        cls.calendar = TradingCalendar(json.loads(path.read_text()))

    def data(self):
        codes = ("a", "b", "c", "bond", "000217")
        days = [d.isoformat() for d in self.calendar.sessions(date(2023, 1, 1), date(2024, 11, 1))][:280]
        marks = dict((code, [1.] * len(days)) for code in codes)
        for code in ("a", "b", "c"):
            marks[code][132] = .8
            marks[code][252] = 1.2
            marks[code][253] = 1.3
        marks["a"][232] = 1.25
        marks["a"][247] = 1.15
        frames = {}
        for index in range(252, 280):
            frames[index] = {"entries": [{"code": code, "price": marks[code][index], "ma120": 1.,
                                           "return5_pct": 2., "annual_vol60": .2, "defensive_asset": code == "000217"}
                                          for code in codes]}
        return {"days": days, "marks": marks, "prices": {code: [1.] * len(days) for code in codes},
                "distributions": {code: [0.] * len(days) for code in codes}, "frames": frames,
                "roles": {code: "defensive" if code == "bond" else "sector" for code in codes},
                "vectors": {i: {code: [float(j)] for j, code in enumerate(codes)} for i in range(252, 280)}}

    def fake_forecasters(self, ic=.2):
        model = SimpleNamespace(predict=lambda x: np.asarray([[.8, -3], [-.8, -12], [0., -15]])[:len(x)])
        forecaster = SimpleNamespace(at=lambda day: (model, {"status": "fitted", "asof": day, "validation_rank_ic": ic}))
        return {20: forecaster, 60: forecaster}

    def test_passive_holdings_do_not_count_as_pullback_entries(self):
        data = self.data()
        allocator = CalibratedAllocator(data, "pullback_resume", {})
        first = allocator(252, set(data["prices"]))
        self.assertEqual(allocator.active, ["a"])
        self.assertAlmostEqual(first.weights["a"], .2 / 3 + .2)
        self.assertAlmostEqual(first.weights["b"], .2 / 3)
        self.assertAlmostEqual(sum(first.weights.values()), 1)
        allocator(253, set(data["prices"]))
        self.assertEqual(allocator.active, ["a"])

    def test_validation_gate_reverts_to_base_and_forecast_does_not_change_core(self):
        data = self.data()
        blocked = CalibratedAllocator(data, "validated_ridge20", self.fake_forecasters(ic=-.1))
        result = blocked(252, set())
        self.assertAlmostEqual(result.weights["a"], .4 / 3)
        active = CalibratedAllocator(data, "ridge20", self.fake_forecasters())
        result = active(252, set())
        self.assertEqual(active.active, ["a"])
        self.assertEqual(result.weights["bond"], .4)
        self.assertEqual(result.weights["000217"], .2)

    def test_risk_forecast_redistributes_but_does_not_leverage_stock_budget(self):
        data = self.data()
        allocator = CalibratedAllocator(data, "drawdown60", self.fake_forecasters())
        result = allocator(252, set())
        self.assertGreater(result.weights["a"], result.weights["b"])
        self.assertAlmostEqual(sum(result.weights[c] for c in ("a", "b", "c")), .4)
        self.assertAlmostEqual(sum(result.weights.values()), 1)
        self.assertTrue(all(result.weights[c] <= .35 for c in ("a", "b", "c")))

    def test_common_teacher_relative_prediction_is_not_current_peer_alpha(self):
        data = self.data()
        model = SimpleNamespace(predict=lambda x: np.asarray([[.8, -5.]] * len(x)))
        teacher = SimpleNamespace(at=lambda day: (model, {"status": "fitted", "asof": day, "validation_rank_ic": .2}))
        allocator = CalibratedAllocator(data, "ridge20", {20: teacher})
        result = allocator(252, set())
        self.assertEqual(allocator.active, [])
        self.assertAlmostEqual(result.weights["a"], .4 / 3)
        self.assertTrue(all(abs(p["excess_return_pct"]) < 1e-10 for p in allocator.trace[-1]["predictions"].values()))

    def test_future_marks_do_not_change_current_pullback_target(self):
        original = self.data()
        altered = deepcopy(original)
        for values in altered["marks"].values():
            values[253:] = [100.] * len(values[253:])
        left = CalibratedAllocator(original, "pullback_resume", {})(252, set())
        right = CalibratedAllocator(altered, "pullback_resume", {})(252, set())
        self.assertEqual(left, right)

    def test_labels_exclude_purchase_day_dividend_and_use_maturity_end(self):
        data = self.data()
        data["distributions"]["a"][253] = .1
        data["distributions"]["a"][254] = .2
        records = outcome_records(data, self.calendar, 20)
        row = next(r for r in records if r["code"] == "a")
        self.assertEqual(row["signal_date"], data["days"][252])
        self.assertEqual(row["label_end"], data["days"][273])
        self.assertAlmostEqual(row["net_return_pct"], ((.999 + .2) / 1.001 - 1) * 100)
        self.assertAlmostEqual(sum(r["outcomes"][0] for r in records), 0)

    def test_passive_risk_control_scales_stock_and_gold_into_same_bonds(self):
        data = self.data()
        result = passive_target(data, 252, .6)
        self.assertAlmostEqual(sum(result.weights[c] for c in ("a", "b", "c")), .24)
        self.assertAlmostEqual(result.weights["000217"], .12)
        self.assertAlmostEqual(result.weights["bond"], .64)
        self.assertAlmostEqual(sum(result.weights.values()), 1)
        with self.assertRaises(ValueError):
            passive_target(data, 252, 1.1)


if __name__ == "__main__":
    unittest.main()
