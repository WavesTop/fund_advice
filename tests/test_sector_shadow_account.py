from copy import deepcopy
from datetime import date
import gzip
from hashlib import sha256
import json
from pathlib import Path
import unittest

from backend.analysis.fund_allocation import AllocationExecution, simulate_allocations
from backend.analysis.sector_shadow_account import METHODS, SectorShadowAllocator, source_targets
from backend.core.trading_calendar import get_calendar
from backend.core.trading_calendar import TradingCalendar
from scripts.research_sector_shadow_accounts import prepare, schedule_at
from scripts.validate_fund_allocation import validate_allocation
from scripts.validate_sector_shadow_account import audit_shadow_account


class SectorShadowAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = get_calendar()
        cls.days = [day.isoformat() for day in cls.calendar.sessions(date(2025, 1, 2), date(2025, 3, 31))]
        cls.codes = [f"a{i}" for i in range(10)] + [f"b{i}" for i in range(10)]
        cls.data = {"days": cls.days, "prices": {code: [100.] * len(cls.days) for code in cls.codes},
                    "distributions": {code: [0.] * len(cls.days) for code in cls.codes}}

    def source(self, index, selected):
        return {"source_date": self.days[index], "selections": {method: list(selected) for method in METHODS[:6]}}

    def account(self, schedule, *, repair=True, lag=1):
        allocate = SectorShadowAllocator(self.codes, schedule, "maximin_top10", repair=repair)
        account = simulate_allocations(self.data, self.calendar, allocate, start=0,
            execution=AllocationExecution(review_sessions=1, initial_review=True, execution_lag_sessions=lag,
                rebalance_band=.03, relative_rebalance_band=.25), include_portfolio_state=True)
        self.assertEqual(validate_allocation(account, self.data, self.calendar)["status"], "passed")
        self.assertEqual(audit_shadow_account(account, allocate.checks, self.data, schedule,
                                             "maximin_top10", repair=repair)["status"], "passed")
        return account, allocate.checks

    def test_partial_and_empty_lists_hold_cash_and_exposure_control_matches_budget(self):
        source = self.source(0, self.codes[:2])
        target = source_targets(source, self.codes, "upward_quality")
        self.assertAlmostEqual(sum(target.values()), .2)
        self.assertEqual(target[self.codes[0]], .1)
        control = source_targets(source, self.codes, "exposure_matched_all")
        self.assertAlmostEqual(sum(control.values()), .2)
        empty = self.source(0, [])
        self.assertEqual(sum(source_targets(empty, self.codes, "upward_quality").values()), 0)
        account, _ = self.account({0: empty})
        self.assertEqual(account["total_return_pct"], 0)
        self.assertEqual(account["trades"], [])

    def test_incumbent_list_is_not_fully_liquidated_and_rebought_on_review(self):
        schedule = {0: self.source(0, self.codes[:10]), 20: self.source(20, self.codes[:10])}
        account, _ = self.account(schedule)
        self.assertEqual(len(account["trades"]), 20)
        self.assertTrue(all(row["reason"] == "terminal_liquidation" for row in account["trades"] if row["action"] == "SELL"))

    def test_repair_waits_for_receipts_and_event_only_does_not_fill_automatically(self):
        schedule = {0: self.source(0, self.codes[:10]), 10: self.source(10, self.codes[10:])}
        repaired, _ = self.account(schedule)
        sale = next(row for row in repaired["trades"] if row["action"] == "SELL" and row["signal_date"] == self.days[10])
        buy = next(row for row in repaired["trades"] if row["action"] == "BUY" and row["code"].startswith("b"))
        self.assertGreater(buy["execution_date"], sale["settlement_date"])
        event, _ = self.account(schedule, repair=False)
        self.assertFalse(any(row["action"] == "BUY" and row["code"].startswith("b") for row in event["trades"]))

    def test_queued_review_uses_known_deferred_source_and_trace_tampering_is_rejected(self):
        schedule = {0: self.source(0, self.codes[:10]), 1: self.source(1, self.codes[10:])}
        account, checks = self.account(schedule, lag=2)
        self.assertTrue(any(row["action"] == "review" and row["source_date"] < row["signal_date"] for row in checks))
        for mutation in ("source", "target", "missing"):
            altered = deepcopy(checks)
            if mutation == "source":
                altered[0]["source_date"] = self.days[-1]
            elif mutation == "target":
                altered[0]["targets"][self.codes[0]] = .2
            else:
                altered.pop()
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                audit_shadow_account(account, altered, self.data, schedule, "maximin_top10", repair=True)

    def test_future_outcome_fields_cannot_change_prepared_sources(self):
        rows = [{"secondary": True, "learning": {"status": "fitted", "as_of": self.days[index],
                 "latest_label_end": self.days[index - 1], "latest_ready_date": self.days[index]},
                 "fit_validation": {"status": "passed"}, "methods": {method: {"selected_codes": self.codes[:10],
                 "status": "matured", "selected_return_pct": 999} for method in METHODS[:6]}} for index in range(5, 36, 5)]
        reference = {"input": {"days": self.days, "boards": [{"code": code, "prices": dict.fromkeys(self.days, 100.)}
                     for code in self.codes]}, "coverage": {"full_pool_common_start": self.days[0]},
                     "periods": {period: {"cases": deepcopy(rows)} for period in ("short", "medium", "long")}}
        expected = prepare(reference)
        for period in reference["periods"].values():
            for row in period["cases"]:
                for method in row["methods"].values():
                    method.update(status="not_matured", selected_return_pct=-999999)
        self.assertEqual(prepare(reference), expected)
        with self.assertRaises(ValueError):
            schedule_at(expected[1]["short"], len(self.days), 20, 0)

    @unittest.skipUnless(Path("runtime/verification/sector-shadow-account-v1.json.gz").exists(),
                         "Frozen sector shadow-account report unavailable")
    def test_full_matrix_defaults_and_independent_cash_lot_source_replay(self):
        path = Path("runtime/verification/sector-shadow-account-v1.json.gz")
        self.assertEqual(sha256(path.read_bytes()).hexdigest(),
                         "1d780346171f2916089d274b9db737936aadb09d255880a8b3d1218cb0fd2321")
        with gzip.open(path, "rt") as stream:
            report = json.load(stream)
        self.assertEqual(report["formal_account_runs"], 312)
        self.assertFalse(report["investment_eligible"])
        self.assertFalse(report["production_ranking_changed"])
        data, calendar = report["data"], TradingCalendar(report["calendar"])
        full, runs = 0, 0
        for period, value in report["periods"].items():
            sources = {int(key): source for key, source in report["source_schedules"][period].items()}
            start = data["days"].index(value["main_start"])
            schedule = schedule_at(sources, start, value["review_sessions"], 0)
            self.assertEqual(len(value["scenarios"]), 9)
            self.assertEqual(len(value["fresh_entry"]), 4)
            for scenario in [*value["scenarios"].values(), *value["fresh_entry"].values()]:
                self.assertEqual(set(scenario["policies"]), set(METHODS))
                runs += len(scenario["policies"])
                for method, trial in scenario["policies"].items():
                    self.assertEqual(trial["ledger_validation"]["status"], "passed")
                    self.assertEqual(trial["signal_validation"]["status"], "passed")
                    self.assertEqual(len(trial["episodes"]), 9)
                    if "account" not in trial:
                        continue
                    full += 1
                    self.assertEqual(validate_allocation(trial["account"], data, calendar)["status"], "passed")
                    self.assertEqual(audit_shadow_account(trial["account"], trial["checks"], data, schedule,
                                     method, repair=True)["status"], "passed")
                    for field in ("total_return_pct", "max_drawdown_pct", "total_fees", "final_equity", "trade_count"):
                        self.assertEqual(trial["metrics"][field], trial["account"][field])
                    self.assertEqual(trial["account"]["start_date"], value["main_start"])
        self.assertEqual((runs, full), (312, 24))
