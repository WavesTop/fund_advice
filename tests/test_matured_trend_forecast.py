from copy import deepcopy
from datetime import date, timedelta
import unittest

import numpy as np

from backend.analysis.matured_trend_forecast import MaturedForecaster, audit_training_visibility, fit_ridge


class MaturedForecastTests(unittest.TestCase):
    def records(self):
        rows = []
        for i in range(50):
            start = date(2020, 1, 1) + timedelta(days=i * 10)
            for j, code in enumerate(("one", "two", "three")):
                x = j + .1 * i
                rows.append({"signal_date": start.isoformat(), "label_end": (start + timedelta(days=35)).isoformat(),
                             "code": code, "features": [x, x * x, float(i % 2)], "outcomes": [x - 1, -.5 * x - 2]})
        return rows

    def test_ridge_regularizes_collinear_and_constant_features(self):
        model = fit_ridge([[1, 2, 5], [2, 4, 5], [3, 6, 5]], [[1, -2], [2, -3], [3, -4]])
        self.assertTrue(np.isfinite(model.predict([[2, 4, 5]])).all())
        self.assertAlmostEqual(model.predict([[2, 4, 5]])[0, 0], 2)
        with self.assertRaises(ValueError):
            fit_ridge([[float("nan")]], [[0]])

    def test_outer_and_inner_outcomes_have_matured(self):
        forecaster = MaturedForecaster(self.records())
        model, metadata = forecaster.at("2020-12-01")
        self.assertIsNotNone(model)
        self.assertLessEqual(metadata["latest_label_end"], "2020-12-01")
        self.assertLess(metadata["inner_training_latest_label_end"], metadata["validation_start"])
        self.assertEqual(audit_training_visibility(forecaster.records, metadata)["status"], "passed")

    def test_future_labels_and_features_do_not_change_current_forecast(self):
        original, changed = self.records(), self.records()
        for record in changed:
            if record["label_end"] > "2020-12-01":
                record["features"] = [1000, 1000, 1000]
                record["outcomes"] = [1000, -1000]
        left, left_meta = MaturedForecaster(original).at("2020-12-01")
        right, right_meta = MaturedForecaster(changed).at("2020-12-01")
        self.assertEqual(left_meta, right_meta)
        np.testing.assert_allclose(left.predict([[1, 1, 0]]), right.predict([[1, 1, 0]]))

    def test_date_cache_cannot_reuse_a_later_fit_in_same_month(self):
        forecaster = MaturedForecaster(self.records())
        forecaster.at("2020-12-20")
        _, earlier = forecaster.at("2020-12-01")
        _, independent = MaturedForecaster(self.records()).at("2020-12-01")
        self.assertEqual(earlier, independent)

    def test_insufficient_history_is_explicit_and_audit_rejects_tampering(self):
        forecaster = MaturedForecaster(self.records())
        model, metadata = forecaster.at("2020-04-01")
        self.assertIsNone(model)
        self.assertEqual(metadata["status"], "insufficient_history")
        self.assertEqual(audit_training_visibility(forecaster.records, metadata)["status"], "not_fitted")
        _, full = forecaster.at("2020-12-01")
        changed = deepcopy(full)
        changed["observations"] += 1
        with self.assertRaises(ValueError):
            audit_training_visibility(forecaster.records, changed)
        changed = deepcopy(full)
        changed["validation_start"] = changed["asof"]
        with self.assertRaises(ValueError):
            audit_training_visibility(forecaster.records, changed)
        changed = deepcopy(full)
        changed["validation_rank_ic"] = -1
        with self.assertRaises(ValueError):
            audit_training_visibility(forecaster.records, changed)
        changed = deepcopy(full)
        changed["coefficients"][0][0] = float("nan")
        with self.assertRaises(ValueError):
            audit_training_visibility(forecaster.records, changed)
        changed = deepcopy(full)
        changed["coefficients"][0][0] += 1
        with self.assertRaises(ValueError):
            audit_training_visibility(forecaster.records, changed)


if __name__ == "__main__":
    unittest.main()
