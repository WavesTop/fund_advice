from copy import deepcopy
from dataclasses import asdict
from datetime import date
from decimal import Decimal
import gzip
from hashlib import sha256
import json
import unittest

from backend.analysis.fund_allocation import AllocationExecution, AllocationTargets, simulate_allocations
from backend.analysis.fund_holding_costs import FundHoldingCosts
from backend.analysis.trend_satellite import in_band
from backend.core.trading_calendar import TradingCalendar
from scripts.research_fund_rotation import CALENDAR
from scripts.research_satellite_low_fees import PROTOCOL, compact, validate_protocol, verify_fee_only
from scripts.export_strategy_trial import checked_metrics
from scripts.validate_fund_allocation import validate_allocation


class SatelliteLowFeeTests(unittest.TestCase):
    def engine_fixture(self):
        calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
        days = [day.isoformat() for day in calendar.sessions(date(2024, 7, 1), date(2024, 8, 30))]
        data = {"days": days, "prices": {code: [100.] * len(days) for code in ("one", "two")},
                "distributions": {code: [0.] * len(days) for code in ("one", "two")}}
        return calendar, data

    def fixture(self):
        baseline = {"holding_cost_policy": FundHoldingCosts().document(), "execution": asdict(AllocationExecution())}
        account = {**deepcopy(baseline), "fee_bps_each_side": "0", "trades": [
            {"action": "BUY", "fee": "0", "gross_amount": "1000"},
            {"action": "SELL", "fee": "15", "gross_amount": "1000", "holding_calendar_days": 6, "sell_fee_bps": "150"},
            {"action": "SELL", "fee": "0", "gross_amount": "1000", "holding_calendar_days": 7, "sell_fee_bps": "0"}]}
        return baseline, account

    def test_low_ordinary_fee_keeps_calendar_short_fee(self):
        baseline, account = self.fixture()
        verify_fee_only(account, baseline, 1)
        self.assertEqual(Decimal(account["trades"][1]["fee"]), 15)

    def test_removing_short_fee_or_altering_trade_cost_rejected(self):
        baseline, account = self.fixture()
        for field, value in (("sell_fee_bps", "0"), ("fee", "0")):
            modified = deepcopy(account)
            modified["trades"][1][field] = value
            with self.assertRaises(ValueError):
                verify_fee_only(modified, baseline, 1)

    def test_zero_settlement_confirmation_or_short_floor_rejected(self):
        baseline, account = self.fixture()
        for field in ("settlement_lag_sessions", "confirmation_lag_sessions", "short_redemption_bps"):
            modified = deepcopy(account)
            modified["holding_cost_policy"][field] = 0
            with self.assertRaises(ValueError):
                verify_fee_only(modified, baseline, 1)

    def test_only_declared_execution_lag_change_allowed(self):
        baseline, account = self.fixture()
        account["execution"]["execution_lag_sessions"] = 2
        verify_fee_only(account, baseline, 2)
        account["execution"]["minimum_holding_days"] = 0
        with self.assertRaises(ValueError):
            verify_fee_only(account, baseline, 2)

    def test_protocol_does_not_allow_best_pool_or_phase_selection(self):
        protocol = json.loads(PROTOCOL.read_text())
        validate_protocol(protocol)
        for field in ("scenarios", "cohorts", "policies", "fresh_entry_dates"):
            modified = deepcopy(protocol)
            modified[field].pop()
            with self.assertRaises(ValueError):
                validate_protocol(modified)
        for value in (True, 10):
            modified = deepcopy(protocol)
            modified["ordinary_fee_bps_each_side"] = value
            with self.assertRaises(ValueError):
                validate_protocol(modified)

    def test_compaction_retains_audits_but_not_duplicate_full_paths(self):
        value = {"account": {}, "daily_checks": [], "signal_trace": [], "ledger_validation": {"status": "passed"}, "metrics": {}}
        result = compact(value)
        self.assertEqual(set(result), {"ledger_validation", "metrics"})
        self.assertIn("account", value)

    def test_real_zero_ordinary_engine_still_charges_short_risk_exit(self):
        calendar, data = self.engine_fixture()
        execution = AllocationExecution(review_sessions=1, minimum_holding_days=30)
        account = simulate_allocations(data, calendar,
            lambda index, held: AllocationTargets({"one": 1}) if index == 0 else AllocationTargets({}, frozenset({"one"})),
            start=0, fee_bps=Decimal(0), execution=execution)
        baseline = {"holding_cost_policy": FundHoldingCosts().document(), "execution": asdict(execution)}
        verify_fee_only(account, baseline, 1)
        self.assertAlmostEqual(account["total_return_pct"], -1.5)
        sale = next(trade for trade in account["trades"] if trade["action"] == "SELL")
        self.assertEqual(sale["sell_fee_bps"], "150")
        sale_day = next(row for row in account["equity_curve"] if row["date"] == sale["execution_date"])
        self.assertGreater(Decimal(sale_day["receivables"]), 0)
        self.assertEqual(validate_allocation(account, data, calendar)["status"], "passed")

    def test_real_zero_ordinary_engine_does_not_spend_unsettled_redemption(self):
        calendar, data = self.engine_fixture()
        account = simulate_allocations(data, calendar, lambda index, held: {"one" if index < 10 else "two": 1},
            start=0, fee_bps=Decimal(0), execution=AllocationExecution(review_sessions=10))
        sale = next(trade for trade in account["trades"] if trade["action"] == "SELL")
        buy = next(trade for trade in account["trades"] if trade["action"] == "BUY" and trade["code"] == "two")
        self.assertGreater(buy["execution_date"], sale["settlement_date"])
        self.assertEqual(Decimal(account["total_fees"]), 0)
        self.assertAlmostEqual(account["total_return_pct"], 0)
        self.assertEqual(validate_allocation(account, data, calendar)["status"], "passed")

    @unittest.skipUnless((PROTOCOL.parents[1] / "runtime/verification/satellite-low-ordinary-fee-research-v1.json.gz").exists(), "Local frozen sensitivity unavailable")
    def test_frozen_real_runs_keep_friction_and_expose_discrete_band_fork(self):
        root = PROTOCOL.parents[1]
        path = root / "runtime/verification/satellite-low-ordinary-fee-research-v1.json.gz"
        self.assertEqual(sha256(path.read_bytes()).hexdigest(), "1be9c501c8b12392a5a9939104fb370c2c916e22b6893690f3df3eddda6f86d1")
        with gzip.open(path, "rt") as stream:
            report = json.load(stream)
        reference_path = root / report["protocol"]["reference"]
        self.assertEqual(sha256(reference_path.read_bytes()).hexdigest(), report["reference_sha256"])
        with gzip.open(reference_path, "rt") as stream:
            reference = json.load(stream)
        self.assertEqual((report["new_account_runs"], report["fresh_reused_main"]), (175, 5))
        for cohort, value in report["cohorts"].items():
            self.assertEqual(len(value["scenarios"]), 5)
            self.assertEqual(len(value["fresh_entry"]), 4)
            for scenario in value["scenarios"].values():
                self.assertEqual(len(scenario["episodes"]), 9)
                for run in scenario["policies"].values():
                    checked_metrics(run)
            for entry in value["fresh_entry"].values():
                self.assertEqual(entry["status"], "complete")
                for run in entry["policies"].values():
                    checked_metrics(run)
            for key, run in value["scenarios"]["default"]["policies"].items():
                verify_fee_only(run["account"], reference["cohorts"][cohort]["full"][key]["account"], 1)
        old = reference["cohorts"]["older9"]["full"]["fixed20"]["daily_checks"]
        new = report["cohorts"]["older9"]["scenarios"]["default"]["policies"]["fixed20"]["daily_checks"]
        signature = lambda row: (row["event"], row["action"], row["memory_after"]["candidate"],
                                 row["memory_after"]["funded_index"], row["memory_after"]["work"])
        before, after = next((a, b) for a, b in zip(old, new) if signature(a) != signature(b))
        self.assertEqual(before["signal_date"], "2021-08-10")
        self.assertEqual(before["signal_date"], after["signal_date"])
        self.assertEqual(before["scores"], after["scores"])
        self.assertEqual(before["own_votes"], after["own_votes"])
        self.assertFalse(in_band(before["current_fund_weights"], before["targets"]))
        self.assertTrue(in_band(after["current_fund_weights"], before["targets"]))
        self.assertEqual((before["action"], after["action"]), ("repair", "hold"))


if __name__ == "__main__":
    unittest.main()
