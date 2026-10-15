from copy import deepcopy
import unittest

from backend.analysis.class_selection_events import selection_event_indices
from backend.analysis.cross_asset_trend import class_statistics, prepare_class_proxies
from scripts.research_selection_event_reviews import audit_event_sets
from tests import test_macro_trend_budget as fixtures


class ClassSelectionEventTests(unittest.TestCase):
    def synthetic(self):
        data = {"days": list(range(256)), "cross_stats": {}}
        for i in range(252, 256):
            signals = {c: {"vote": 1., "normalised_return_score": score, "floored_annual_vol": vol, "recent_state": "steady_up"}
                       for c, score, vol in (("stock", 2., .2), ("gold", 1., .1), ("bond", .5, .01))}
            signals["gold"]["normalised_return_score"] = 3. if i >= 254 else 1.
            signals["stock"]["floored_annual_vol"] += (i - 252) * .01
            if i == 255:
                signals["bond"]["vote"] = 0.
            data["cross_stats"][i] = {"signal_index": i, "fund_risk": {"signal_index": i}, "signals": signals}
        return data

    def test_membership_changes_not_continuously_changing_weights_trigger_review(self):
        data = self.synthetic()
        self.assertEqual(selection_event_indices(data, "dual_leader60"), frozenset({254, 255}))
        self.assertEqual(selection_event_indices(data, "inverse_vol_cap60"), frozenset())
        self.assertEqual(selection_event_indices(data, "fixed_cash_equal"), frozenset())

    def test_bad_warmup_and_mismatched_cache_date_are_rejected(self):
        data = self.synthetic()
        with self.assertRaises(ValueError):
            selection_event_indices(data, "dual_return60", start=251)
        data["cross_stats"][254]["fund_risk"]["signal_index"] = 255
        with self.assertRaises(ValueError):
            selection_event_indices(data, "dual_return60")

    def test_future_suffix_cannot_change_past_events_and_raw_reconstruction_rejects_tampering(self):
        data = prepare_class_proxies(fixtures.fixture())
        data["cross_stats"] = {i: class_statistics(data, i) for i in range(252, len(data["days"]))}
        policies = ("dual_return60", "dual_leader60", "fixed_cash_equal")
        events = {p: selection_event_indices(data, p) for p in policies}
        audit_event_sets(data, policies, events)
        damaged = events | {"dual_return60": events["dual_return60"] ^ frozenset({253})}
        with self.assertRaises(ValueError):
            audit_event_sets(data, policies, damaged)
        changed = deepcopy(data)
        for marks in changed["marks"].values():
            for i in range(281, len(marks)):
                marks[i] *= 10
        prepare_class_proxies(changed)
        changed["cross_stats"] = {i: class_statistics(changed, i) for i in range(252, len(changed["days"]))}
        for p in policies:
            self.assertEqual({i for i in events[p] if i <= 280}, {i for i in selection_event_indices(changed, p) if i <= 280})


if __name__ == "__main__":
    unittest.main()
