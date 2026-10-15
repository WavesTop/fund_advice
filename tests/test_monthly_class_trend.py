from copy import deepcopy
from datetime import date
import json
import unittest

from backend.analysis.cross_asset_trend import prepare_class_proxies
from backend.analysis.monthly_class_trend import completed_month_indices, monthly_class_signals
from backend.core.trading_calendar import TradingCalendar
from scripts.diagnose_cross_asset_signals import audit_label_row, label_row, ranking_diagnostics, summaries
from scripts.research_fund_rotation import CALENDAR
from tests import test_macro_trend_budget as fixtures


class MonthlyClassTests(unittest.TestCase):
    def setUp(self):
        self.calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
        self.data = prepare_class_proxies(fixtures.fixture())
        self.months = completed_month_indices(self.data["days"], self.calendar)

    def test_partial_final_month_and_holiday_end_are_not_inferred(self):
        days = [d.isoformat() for d in self.calendar.sessions(date(2026, 1, 1), date(2026, 10, 12))]
        actual = [days[i] for i in completed_month_indices(days, self.calendar)]
        self.assertEqual(actual[-1], "2026-09-30")
        self.assertEqual(actual[1], "2026-02-27")
        self.assertTrue(all(not d.startswith("2026-10") for d in actual))

    def test_monthly_signal_waits_for_ten_completed_observations(self):
        at = self.months[8]
        self.assertFalse(monthly_class_signals(self.data, at, self.months)["gold"]["known"])
        ready = monthly_class_signals(self.data, self.months[9], self.months)["gold"]
        self.assertTrue(ready["known"])
        self.assertEqual(len(ready["reference_dates"]), 10)

    def test_future_suffix_cannot_change_completed_month_signal(self):
        at = self.months[10] + 1
        before = monthly_class_signals(self.data, at, self.months)
        changed = deepcopy(self.data)
        for values in changed["class_proxies"].values():
            values[at + 1:] *= 10
        self.assertEqual(before, monthly_class_signals(changed, at, self.months))
        for signal in before.values():
            self.assertLessEqual(signal["source_date"], self.data["days"][at])
            self.assertNotEqual(signal["source_date"], self.data["days"][at])

    def test_flat_prices_are_not_positive_trends(self):
        for values in self.data["class_proxies"].values():
            values[:] = 1.
        observed = monthly_class_signals(self.data, self.months[10], self.months)
        self.assertTrue(all(not v["enabled"] and v["bias"] == 0 for v in observed.values()))

    def test_future_outcome_changes_label_but_not_past_signals(self):
        at, horizon = 252, 20
        before = label_row(self.data, at, horizon, self.calendar, self.months)
        changed = deepcopy(self.data)
        for code in changed["prices"]:
            changed["prices"][code][at + 1 + horizon] *= 2
        after = label_row(changed, at, horizon, self.calendar, self.months)
        for name, entry in before["classes"].items():
            self.assertEqual(entry["past_signal"], after["classes"][name]["past_signal"])
            self.assertEqual(entry["monthly_signal"], after["classes"][name]["monthly_signal"])
            self.assertGreater(after["classes"][name]["forward_return_pct"], entry["forward_return_pct"])

    def test_unknown_is_separate_and_labels_must_have_matured(self):
        row = label_row(self.data, 252, 20, self.calendar, self.months)
        row["classes"]["stock"]["gates"]["monthly_ma10"] = None
        summary = summaries([row])["classes"]["stock"]["gates"]["monthly_ma10"]
        self.assertEqual(summary["unknown"]["observations"], 1)
        self.assertEqual(summary["disabled"]["observations"], 0)
        self.assertIsNone(summary["enabled_minus_disabled_pp"])
        with self.assertRaises(ValueError):
            label_row(self.data, len(self.data["days"]) - 10, 20, self.calendar, self.months)

    def test_independent_month_signal_and_costed_label_rebuild_rejects_tampering(self):
        row = label_row(self.data, 252, 20, self.calendar, self.months)
        audit_label_row(row, self.data, 252, self.calendar)
        bad = deepcopy(row)
        bad["classes"]["gold"]["forward_return_pct"] += 1
        with self.assertRaises(ValueError):
            audit_label_row(bad, self.data, 252, self.calendar)
        for field in ("price", "ma10", "bias"):
            bad = deepcopy(row)
            bad["classes"]["gold"]["monthly_signal"][field] = float("nan")
            with self.assertRaises(ValueError):
                audit_label_row(bad, self.data, 252, self.calendar)
        bad = deepcopy(row)
        bad["classes"]["gold"]["monthly_signal"]["source_date"] = self.data["days"][280]
        with self.assertRaises(ValueError):
            audit_label_row(bad, self.data, 252, self.calendar)

    def test_inverse_vol_control_can_explain_high_risk_unit_rank_without_trend(self):
        entries = {str(i): {"past_signal": {"normalised_return_score": 1 / vol, "floored_annual_vol": vol},
                           "forward_return_pct": 1., "forward_return_past_risk_units": .01 / vol}
                   for i, vol in enumerate((.005, .1, .2))}
        stats = ranking_diagnostics(entries)
        self.assertAlmostEqual(stats["rank_ic_past_risk_units"], 1.)
        self.assertAlmostEqual(stats["rank_ic_inverse_vol_past_risk_units"], 1.)
        self.assertEqual(stats["paired_rank_ic_increment_over_inverse_vol"], 0.)
        self.assertIsNone(stats["rank_ic_risk_removed_raw_returns"])


if __name__ == "__main__":
    unittest.main()
