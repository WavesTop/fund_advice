from copy import deepcopy
import unittest

import numpy as np

from backend.analysis.class_return_forecast import CLASSES, class_features
from backend.analysis.matured_trend_forecast import MaturedForecaster
from scripts.diagnose_net_horizon_predictivity import (
    audit_net_records, audit_prediction, describe, label_dates, ledger_probe, net_labels, net_records,
)
from tests import test_class_return_forecast as fixtures


class NetHorizonPredictivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.ClassReturnForecastTests.setUpClass()
        cls.calendar = fixtures.ClassReturnForecastTests.calendar

    def data(self):
        return fixtures.ClassReturnForecastTests().data()

    def test_flat_prices_have_short_fee_floor_not_an_added_fee(self):
        data = self.data()
        for code in data["prices"]:
            data["prices"][code] = np.ones(len(data["days"]))
        at = next(i for i in range(252, 700) if label_dates(data, i, 3, self.calendar)["exit_fee_bps"] == 150)
        short, _ = net_labels(data, at, 3, self.calendar)
        normal, _ = net_labels(data, at, 20, self.calendar)
        self.assertAlmostEqual(short["stock"], .985/1.001 - 1)
        self.assertAlmostEqual(normal["stock"], .999/1.001 - 1)

    def test_cash_dividends_are_not_charged_terminal_sale_fees(self):
        data = self.data()
        for code in data["prices"]:
            data["prices"][code] = np.full(len(data["days"]), 2.)
            data["distributions"][code][253] = .8
            data["distributions"][code][254] = .1
            data["distributions"][code][273] = .4
            data["distributions"][code][274] = .9
        net, _ = net_labels(data, 252, 20, self.calendar)
        self.assertAlmostEqual(net["stock"], (2*.999 + .5)/(2*1.001) - 1)
        self.assertEqual(ledger_probe(data, 252, 20, "stock", self.calendar)["ledger_validation"]["status"], "passed")

    def test_labels_mature_at_settlement_not_exit_nav(self):
        data = self.data()
        rows = net_records(data, 20, "class_mean", self.calendar)
        self.assertEqual(rows[0]["net_label"]["exit_date"], data["days"][273])
        self.assertEqual(rows[0]["label_end"], data["days"][275])
        self.assertEqual(audit_net_records(rows, data, 20, "class_mean", self.calendar)["status"], "passed")

    def test_net_labels_and_past_features_are_independently_audited(self):
        data = self.data()
        rows = net_records(data, 3, "trend_ridge", self.calendar)
        self.assertEqual(audit_net_records(rows, data, 3, "trend_ridge", self.calendar)["status"], "passed")
        for field in ("outcomes", "features", "label_end", "fee"):
            changed = deepcopy(rows)
            if field in ("outcomes", "features"):
                changed[0][field][0] += .1
            elif field == "label_end":
                changed[0][field] = changed[0]["net_label"]["exit_date"]
            else:
                changed[0]["net_label"]["exit_fee_bps"] = 999
            with self.assertRaises(ValueError):
                audit_net_records(changed, data, 3, "trend_ridge", self.calendar)

    def test_twenty_fourth_sample_is_unavailable_until_cash_maturity(self):
        data = self.data()
        predictor = MaturedForecaster(net_records(data, 20, "class_mean", self.calendar))
        model, metadata = predictor.at(data["days"][503])
        self.assertIsNone(model)
        self.assertEqual(metadata["distinct_dates"], 23)
        model, metadata = predictor.at(data["days"][505])
        self.assertIsNotNone(model)
        self.assertEqual(metadata["distinct_dates"], 24)

    def test_unsettled_future_labels_cannot_change_earlier_fit(self):
        data = self.data()
        rows = net_records(data, 20, "trend_ridge", self.calendar)
        other = deepcopy(rows); day = data["days"][650]
        for row in other:
            if row["label_end"] > day:
                row["outcomes"] = [123.]
        left, lm = MaturedForecaster(rows).at(day)
        right, rm = MaturedForecaster(other).at(day)
        self.assertEqual(lm, rm)
        x = [class_features(data, c, 650, "trend_ridge") for c in CLASSES]
        np.testing.assert_array_equal(left.predict(x), right.predict(x))

    def test_predictions_follow_audited_matured_coefficients(self):
        data = self.data(); day = data["days"][650]
        rows = net_records(data, 20, "trend_ridge", self.calendar)
        model, learning = MaturedForecaster(rows).at(day)
        features = [class_features(data, c, 650, "trend_ridge") for c in CLASSES]
        prediction = dict(zip(CLASSES, (max(-1., float(v)) for v in model.predict(features)[:, 0])))
        self.assertEqual(audit_prediction(rows, learning, prediction, features, day)["status"], "passed")
        with self.assertRaises(ValueError):
            audit_prediction(rows, learning, prediction | {"stock": prediction["stock"] + .1}, features, day)

    def test_unit_label_matches_actual_cent_rounded_multi_fund_account(self):
        data = self.data()
        for code in ("stock2", "stock3"):
            data["prices"][code] = data["prices"]["stock"].copy()
            data["marks"][code] = data["marks"]["stock"].copy()
            data["distributions"][code] = list(data["distributions"]["stock"])
            data["roles"][code] = "sector"
            data["class_groups"]["stock"].append(code)
        probe = ledger_probe(data, 252, 20, "stock", self.calendar)
        self.assertLess(probe["invested_initial_capital_fraction"], 1.)
        self.assertEqual(len([t for t in probe["account"]["trades"] if t["action"] == "SELL"]), 3)

    def test_final_cent_budget_disparity_uses_each_actual_fund_amount(self):
        data = self.data()
        data["prices"]["stock"] = np.full(len(data["days"]), 1.7205)
        data["prices"]["stock"][273] = 1.6128
        data["prices"]["stock2"] = np.full(len(data["days"]), 1.028)
        data["prices"]["stock2"][273] = .998
        data["distributions"]["stock2"] = list(data["distributions"]["stock"])
        data["marks"]["stock2"] = data["prices"]["stock2"].copy()
        data["roles"]["stock2"] = "sector"
        data["class_groups"]["stock"].append("stock2")
        probe = ledger_probe(data, 252, 20, "stock", self.calendar)
        self.assertNotEqual(probe["per_fund_capital_fractions"]["stock"], probe["per_fund_capital_fractions"]["stock2"])
        self.assertAlmostEqual(probe["account_net_return_pct"], probe["budget_adjusted_label_pct"], places=10)

    def test_unsupported_and_incomplete_horizons_are_rejected(self):
        data = self.data()
        for horizon in (True, 0, 5, 121):
            with self.assertRaises(ValueError):
                net_records(data, horizon, "class_mean", self.calendar)
        with self.assertRaises(ValueError):
            label_dates(data, len(data["days"]) - 6, 3, self.calendar)
        with self.assertRaises(ValueError):
            label_dates(data, 251, 3, self.calendar)

    def test_missing_prediction_samples_are_not_zero_success(self):
        empty = describe([])
        self.assertEqual(empty["selected_dates"], 0)
        for metrics in empty["modes"].values():
            self.assertIsNone(metrics["positive_forecast_precision"])
            self.assertIsNone(metrics["stock_gold_winner_accuracy"])


if __name__ == "__main__":
    unittest.main()
