from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, localcontext
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import unittest

import numpy as np

from backend.analysis.portfolio_increment import prepare_economic
from backend.analysis.trend_satellite import SatellitePolicy, TrendSatelliteAllocator, tilted, tilted_basket
from scripts.research_local_trend_models import prepare_local
from scripts.research_trend_satellites import satellite_trial
from scripts.research_satellite_baskets import PROTOCOL
from scripts.export_strategy_trial import checked_metrics
from scripts.validate_trend_satellite import audit_satellite, rebuild_executed_weights
from tests import test_trend_satellite as fixtures


class BasketStateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SatelliteStateTests()
        self.fixture.setUp()
        self.base = {"a": .075, "b": .075, "c": .075, "d": .075, "gold": .3, "bond": .4}
        self.fixture.base = self.base
        data = self.fixture.data
        data["class_groups"]["stock"] = ["a", "b", "c", "d"]
        for code in ("c", "d"):
            data["prices"][code] = [100.] * 400
            data["marks"][code] = [100.] * 400
        for index, stats in data["economic_stats"].items():
            stats["votes"].update(c=1., d=1.)
            stats["risk"]["codes"] = list(self.base)
            stats["risk"]["covariance"] = np.zeros((6, 6))
            for code, score in (("c", 65.), ("d", 50.)):
                data["frames"][index]["entries"].append({"code": code, "scores": {"medium": score}, "annual_vol60": .1})

    def allocator(self, size=2):
        return TrendSatelliteAllocator(self.fixture.data, SatellitePolicy("basket", "fixed20", basket_size=size))

    def test_equal_transfer_preserves_stock_total_and_defensive_raw_targets(self):
        target = tilted_basket(self.base, ["a", "b", "c", "d"], ["a", "b"])
        self.assertAlmostEqual(target["a"], .125)
        self.assertAlmostEqual(target["b"], .125)
        self.assertAlmostEqual(target["c"], .025)
        self.assertAlmostEqual(target["d"], .025)
        self.assertAlmostEqual(sum(target[c] for c in ("a", "b", "c", "d")), .3)
        self.assertEqual((target["gold"], target["bond"]), (.3, .4))

    def test_caps_donor_capacity_and_single_member_fallback(self):
        raw = {"a": .34, "b": .10, "c": .02, "gold": .14, "bond": .4}
        target = tilted_basket(raw, ["a", "b", "c"], ["a", "b"])
        self.assertAlmostEqual(target["a"], .35)
        self.assertAlmostEqual(target["b"], .11)
        self.assertAlmostEqual(target["c"], 0)
        self.assertEqual(tilted_basket(raw, ["a", "b", "c"], ["a"]), tilted(raw, ["a", "b", "c"], "a"))
        self.assertEqual(tilted_basket(raw, ["a", "b", "c"], ["a", "b", "c"]), raw)
        for members in ([], ["a", "a"], ["gold"], "a"):
            with self.assertRaises(ValueError):
                tilted_basket(raw, ["a", "b", "c"], members)

    def test_all_members_must_be_funded_before_shared_lease_starts(self):
        allocator = self.allocator()
        target = self.fixture.call(allocator, 252)
        self.assertEqual(allocator.memory.members, ["a", "b"])
        partial = {**target.weights, "b": .075}
        self.fixture.call(allocator, 253, partial)
        self.assertIsNone(allocator.memory.funded_index)
        self.fixture.call(allocator, 256, target.weights)
        self.assertEqual(allocator.memory.funded_index, 256)
        self.fixture.call(allocator, 275, target.weights)
        self.assertIsNotNone(allocator.memory.candidate)
        exit_target = self.fixture.call(allocator, 276, target.weights)
        self.assertEqual(allocator.checks[-1]["event"], "expiry")
        self.assertEqual(allocator.memory.members, [])
        self.assertEqual(exit_target.weights, self.base)

    def test_nonleader_failure_withdraws_basket_but_only_failed_code_is_urgent(self):
        allocator = self.allocator()
        target = self.fixture.call(allocator, 252)
        self.fixture.call(allocator, 253, target.weights)
        self.fixture.data["economic_stats"][254]["votes"]["b"] = 0
        reduced = self.fixture.call(allocator, 254, target.weights)
        self.assertEqual(allocator.checks[-1]["event"], "absolute_failure")
        self.assertEqual(reduced.risk_exits, frozenset({"b"}))
        self.assertEqual(reduced.weights, self.base)
        self.assertIsNone(allocator.memory.candidate)

    def test_timeout_and_actual_risk_priority_are_unchanged(self):
        allocator = self.allocator(3)
        self.fixture.call(allocator, 252)
        self.fixture.call(allocator, 262)
        self.assertEqual(allocator.checks[-1]["event"], "funding_timeout")
        allocator = self.allocator()
        target = self.fixture.call(allocator, 252)
        self.fixture.call(allocator, 253, target.weights)
        self.fixture.data["economic_stats"][254]["risk"]["covariance"] = np.eye(6)
        urgent = self.fixture.call(allocator, 254, target.weights)
        self.assertEqual(allocator.checks[-1]["action"], "risk")
        self.assertEqual(urgent.risk_exits, frozenset(self.base))
        self.assertTrue(urgent.enforce_risk_exits)

    def test_single_eligible_stock_keeps_old_target_and_future_scores_do_not_change_entry(self):
        for row in self.fixture.data["frames"][252]["entries"]:
            if row["code"] != "a":
                row["scores"]["medium"] = 0
        allocator = self.allocator(3)
        target = self.fixture.call(allocator, 252)
        self.assertEqual(allocator.memory.members, ["a"])
        old = TrendSatelliteAllocator(self.fixture.data, SatellitePolicy("one", "fixed20"))
        self.assertEqual(target, self.fixture.call(old, 252))
        self.fixture.data["frames"][399]["entries"][0]["scores"]["medium"] = 0
        repeated = self.allocator(3)
        self.assertEqual(target, self.fixture.call(repeated, 252))
        self.assertEqual(allocator.checks, repeated.checks)

    def test_invalid_sizes_and_unsupported_basket_mixes_rejected(self):
        for size in (0, 4, True, 2.):
            with self.assertRaises(ValueError):
                SatellitePolicy("bad", "fixed20", basket_size=size).validate()
        base = SatellitePolicy("basket", "fixed20", basket_size=2)
        for changes in ({"exit_rule": "price"}, {"random_seed": 1}, {"market_gate": "competition"},
                        {"repair_scope": "separate"}, {"sizing": "headroom"}, {"risk_repair": "event"}):
            with self.assertRaises(ValueError):
                replace(base, **changes).validate()


class BasketReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SatelliteReplayTests.setUpClass()
        cls.calendar = fixtures.SatelliteReplayTests.calendar
        days = fixtures.SatelliteReplayTests.data["days"]
        funds = {}
        for code, slope in (("a", .0007), ("b", .0006), ("c", .0005), ("d", .0004), ("000217", .0003), ("bond", .00007), ("000051", .0005)):
            funds[code] = {"code": code, "daily_start": days[0], "last_date": days[-1],
                "rows": {day: {"unit_nav": str(math.exp(slope * i))} for i, day in enumerate(days)},
                "distributions": dict.fromkeys(days, 0.), "theme": "黄金" if code == "000217" else "other",
                "role": "defensive" if code == "bond" else "sector"}
        cls.data = prepare_economic(prepare_local(funds, ["a", "b", "c", "d", "000217", "bond"], cls.calendar))

    def test_real_two_and_three_member_entries_fees_and_delays_are_audited(self):
        for size in (2, 3):
            for fee, phase, lag in ((10, 0, 1), (0, 5, 2)):
                trial = satellite_trial(self.data, self.calendar,
                    SatellitePolicy("basket", "fixed20", basket_size=size), fee=Decimal(fee), phase=phase, lag=lag)
                entries = [row for row in trial["daily_checks"] if row["event"] == "entry"]
                self.assertTrue(entries)
                self.assertTrue(all(len(row["memory_after"]["members"]) == size for row in entries))
                self.assertEqual(trial["ledger_validation"]["status"], "passed")
                self.assertEqual(trial["signal_validation"]["status"], "passed")

    def test_forged_member_and_funding_baseline_are_rejected(self):
        policy = SatellitePolicy("basket", "fixed20", basket_size=3)
        trial = satellite_trial(self.data, self.calendar, policy)
        for field in ("members", "entry_bases"):
            checks = deepcopy(trial["daily_checks"])
            row = next(row for row in checks if row["event"] == "entry")
            if field == "members":
                row["memory_after"][field].append("bond")
            else:
                row["memory_after"][field]["a"] = 99
            with self.assertRaises(ValueError):
                audit_satellite(trial["account"], checks, self.data, policy)

    @unittest.skipUnless((PROTOCOL.parents[1] / "runtime/verification/satellite-basket-research-v1.json.gz").exists(), "Frozen basket report unavailable")
    def test_real_fixed_design_and_every_members_funding_are_preserved(self):
        path = PROTOCOL.parents[1] / "runtime/verification/satellite-basket-research-v1.json.gz"
        self.assertEqual(sha256(path.read_bytes()).hexdigest(), "66d3807916069bfa76ef2de57d7b935cb5a8290df5100f31b496648e3baa0aa5")
        with gzip.open(path, "rt") as stream:
            report = json.load(stream)
        self.assertEqual((report["formal_account_runs"], report["compatible_old_accounts"],
                          report["new_variant_account_runs"], report["fresh_reused_main"]), (228, 72, 156, 4))
        self.assertEqual(set(report["cohorts"]), {"long4", "older4", "older9", "all13"})
        full, genuine_multi = 0, 0
        for cohort in report["cohorts"].values():
            self.assertEqual(len(cohort["scenarios"]), 12)
            self.assertEqual(len(cohort["fresh_entry"]), 8)
            for scenario in cohort["scenarios"].values():
                self.assertEqual(len(scenario["episodes"]), 9)
                for trial in scenario["policies"].values():
                    checked_metrics(trial)
                    if "account" not in trial:
                        continue
                    full += 1
                    for row in trial["daily_checks"]:
                        if row["event"] == "entry":
                            genuine_multi += len(row["memory_after"]["members"]) > 1
                        if row["event"] == "funded":
                            memory = row["memory_after"]
                            for code in memory["members"]:
                                self.assertGreaterEqual(row["current_fund_weights"][code],
                                    memory["entry_bases"][code] + memory["entry_excesses"][code] * .5)
            for entry in cohort["fresh_entry"].values():
                for trial in entry["policies"].values():
                    checked_metrics(trial)
        self.assertEqual(full, 16)
        self.assertGreater(genuine_multi, 0)


class ExecutedLotWeightTests(unittest.TestCase):
    def fixture(self):
        fractional = "0.1234567890123456789012345678901234567899"
        data = {"days": ["day0", "day1", "day2"], "prices": {"bank": [1.] * 3}}
        trades = [{"action": "BUY", "code": "bank", "lot_id": 1, "execution_date": "day0", "units": "1"},
                  {"action": "BUY", "code": "bank", "lot_id": 2, "execution_date": "day0", "units": fractional},
                  {"action": "SELL", "code": "bank", "lot_id": 1, "execution_date": "day1", "units": "1"},
                  {"action": "SELL", "code": "bank", "lot_id": 2, "execution_date": "day2", "units": fractional}]
        account = {"trades": trades, "equity_curve": [{"date": day, "equity": "10"} for day in data["days"]]}
        return account, data

    def test_closed_lots_leave_exact_zero_not_positive_cancellation_dust(self):
        account, data = self.fixture()
        with localcontext() as context:
            context.prec = 40
            legacy = Decimal(0)
            for trade in account["trades"]:
                legacy += Decimal(trade["units"]) * (1 if trade["action"] == "BUY" else -1)
            self.assertGreater(legacy, 0)
        rebuilt = rebuild_executed_weights(account, data)
        self.assertEqual(rebuilt["day2"]["bank"], 0.)

    def test_genuine_tiny_lot_is_not_deleted_by_an_epsilon(self):
        account, data = self.fixture()
        account["trades"].insert(2, {"action": "BUY", "code": "bank", "lot_id": 3, "execution_date": "day0", "units": "1"})
        account["trades"].append({"action": "SELL", "code": "bank", "lot_id": 3, "execution_date": "day2",
                                  "units": "0.9999999999999999999999999999999999999999"})
        self.assertGreater(rebuild_executed_weights(account, data)["day2"]["bank"], 0)

    def test_invalid_lot_actions_units_identity_and_oversell_are_rejected(self):
        account, data = self.fixture()
        for fields in ({"units": "NaN"}, {"units": "-1"}, {"units": "2"}, {"lot_id": 999},
                       {"action": "unknown"}, {"code": "other"}):
            changed = deepcopy(account)
            changed["trades"][2].update(fields)
            with self.assertRaises(ValueError):
                rebuild_executed_weights(changed, data)
        account["trades"][1]["lot_id"] = 1
        with self.assertRaises(ValueError):
            rebuild_executed_weights(account, data)

    @unittest.skipUnless(Path("runtime/verification/basket-aggregate-units-error-v1.json.gz").exists(), "Frozen audit-error account unavailable")
    def test_real_failed_account_is_reaudited_without_modifying_its_trades(self):
        from backend.core.trading_calendar import TradingCalendar
        from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds
        from scripts.research_defensive_allocations import BONDS
        from scripts.validate_fund_allocation import validate_allocation
        with gzip.open("runtime/verification/basket-aggregate-units-error-v1.json.gz", "rt") as stream:
            saved = json.load(stream)
        self.assertEqual(saved["per_lot_units"]["001595"], "0")
        self.assertGreater(Decimal(saved["aggregate_units"]["001595"]), 0)
        calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
        funds, _ = load_funds(INPUT, calendar)
        bonds, _ = load_funds(BONDS, calendar)
        funds.update(bonds)
        data = prepare_economic(prepare_local(funds, list(saved["checks"][0]["current_fund_weights"]), calendar))
        policy = SatellitePolicy(**saved["policy"])
        self.assertEqual(rebuild_executed_weights(saved["account"], data)[saved["date"]]["001595"], 0.)
        self.assertEqual(validate_allocation(saved["account"], data, calendar)["status"], "passed")
        self.assertEqual(audit_satellite(saved["account"], saved["checks"], data, policy, phase=saved["phase"])["status"], "passed")


if __name__ == "__main__":
    unittest.main()
