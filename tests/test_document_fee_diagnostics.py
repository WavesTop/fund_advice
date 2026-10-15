from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import unittest

from backend.core.trading_calendar import TradingCalendar
from scripts.diagnose_document_fee_scenarios import EVIDENCE, document_rate, fixed_order_costs, load_evidence


class DocumentFeeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profiles = json.loads(EVIDENCE.read_text())["profiles"]
        cls.calendar = TradingCalendar(json.loads(Path("config/research-exchange-calendar-2009-2026-v2.json").read_text()))

    def rate(self, code, age):
        return document_rate(self.profiles, code, "SELL", age, allow_document_scenario=True)

    def test_document_boundaries_are_inclusive_at7and30_not_flat_rates(self):
        self.assertEqual([self.rate("000217", age) for age in (6, 7, 29, 30)], list(map(Decimal, (150, 10, 10, 0))))
        self.assertEqual([self.rate("007301", age) for age in (6, 7, 29, 30)], list(map(Decimal, (150, 0, 0, 0))))
        self.assertEqual(document_rate(self.profiles, "000217", "BUY", allow_document_scenario=True), 0)

    def test_unknown_or_parent_share_is_not_free(self):
        for code in ("000216", "007300", "unknown"):
            with self.assertRaises(LookupError):
                document_rate(self.profiles, code, "BUY", allow_document_scenario=True)

    def test_document_point_is_not_automatically_historical_truth(self):
        with self.assertRaises(ValueError):
            document_rate(self.profiles, "000217", "BUY")
        for age in (-1, True, 7., None):
            with self.assertRaises(ValueError):
                self.rate("000217", age)

    def test_uncovered_schedule_and_nonfinite_rate_rejected(self):
        profiles = deepcopy(self.profiles)
        profiles["000217"]["redemption_bands"].pop()
        with self.assertRaises(ValueError):
            document_rate(profiles, "000217", "SELL", 50, allow_document_scenario=True)
        profiles = deepcopy(self.profiles)
        profiles["000217"]["purchase_bps"] = "NaN"
        with self.assertRaises(ValueError):
            document_rate(profiles, "000217", "BUY", allow_document_scenario=True)
        profiles = deepcopy(self.profiles)
        profiles["000217"]["redemption_bands"][0]["until_days"] = None
        with self.assertRaises(ValueError):
            document_rate(profiles, "000217", "SELL", 5, allow_document_scenario=True)

    def test_missing_fees_do_not_produce_complete_cost_or_new_roi(self):
        trades = [{"code": "007301", "action": "BUY", "fee": "1", "gross_amount": "1000"},
                  {"code": "unknown", "action": "BUY", "fee": "1", "gross_amount": "1000"}]
        result = fixed_order_costs({"trades": trades}, self.profiles, self.calendar)
        self.assertEqual(result["unknown_products"], {"unknown": 1})
        self.assertIsNone(result["complete_document_cost"])
        self.assertIsNone(result["revised_portfolio_return_pct"])
        self.assertEqual(Decimal(result["fixed_gross_known_fee_difference"]), 1)

    def test_lot_fragments_are_aggregated_before_checking_minimum_order(self):
        start = date(2026, 10, 9)
        sold = self.calendar.shift(start, 2).isoformat()
        trades = [{"code": "000217", "action": "SELL", "fee": "0", "gross_amount": "1",
                   "units": ".6", "holding_calendar_days": 3, "purchase_date": start.isoformat(),
                   "execution_date": sold, "reason": "risk_exit"} for _ in range(2)]
        result = fixed_order_costs({"trades": trades}, self.profiles, self.calendar)
        self.assertEqual(result["potential_below_standard_min_redemption_orders"], [])
        self.assertEqual(result["potential_early_redemption_records"], [])
        split = deepcopy(trades)
        split[1]["reason"] = "terminal_liquidation"
        split_result = fixed_order_costs({"trades": split}, self.profiles, self.calendar)
        self.assertEqual(len(split_result["potential_below_standard_min_redemption_orders"]), 2)
        for trade in trades:
            trade["execution_date"] = self.calendar.shift(start, 1).isoformat()
        result = fixed_order_costs({"trades": trades}, self.profiles, self.calendar)
        self.assertEqual(len(result["potential_early_redemption_records"]), 2)

    @unittest.skipUnless(Path("runtime/verification/product-terms-20261014/manifest.json").exists(), "Local raw documents unavailable")
    def test_real_reviewed_documents_are_verified_without_granting_eligibility(self):
        evidence, observed = load_evidence()
        self.assertTrue(observed.startswith("2026-10-14"))
        self.assertEqual(len(evidence["reviewed_documents"]), 4)
        self.assertTrue(all(not p["investment_eligible"] for p in evidence["profiles"].values()))


if __name__ == "__main__":
    unittest.main()
