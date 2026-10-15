from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
import math
from pathlib import Path
import unittest

from backend.core.trading_calendar import get_calendar
from scripts.research_sector_horizons import METHODS, PERIODS, evaluate, paired, run, select


class SectorHorizonResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = get_calendar()
        cls.days = [d.isoformat() for d in cls.calendar.sessions(date(2025, 1, 2), date(2026, 9, 18))][:270]
        cls.boards = [{"code": str(j), "name": str(j), "collection_error": None,
                       "prices": {day: 100 * math.exp(.0006 * (j + 1) * i) for i, day in enumerate(cls.days)}}
                      for j in range(12)]

    def test_each_period_binds_to_its_own_forward_horizon_and_next_close(self):
        for window in PERIODS.values():
            cases = evaluate(self.boards, self.days, self.calendar, window, window)
            for case in cases.values():
                self.assertEqual(case["signal_date"], self.days[window])
                self.assertEqual(case["entry_date"], self.days[window + 1])
                self.assertEqual(case["exit_date"], self.days[2 * window + 1])
                self.assertEqual(case["status"], "matured")

    def test_future_mutations_do_not_change_selections(self):
        expected = evaluate(self.boards, self.days, self.calendar, 20, 20)
        changed = deepcopy(self.boards)
        for board in changed:
            for day in self.days[21:]:
                board["prices"][day] = 1
        actual = evaluate(changed, self.days, self.calendar, 20, 20)
        for method in METHODS:
            self.assertEqual(actual[method]["selected_codes"], expected[method]["selected_codes"])
            self.assertNotEqual(actual[method]["selected_return_pct"], expected[method]["selected_return_pct"])

    def test_missing_signal_data_blocks_full_pool_comparison(self):
        changed = deepcopy(self.boards)
        del changed[0]["prices"][self.days[10]]
        cases = evaluate(changed, self.days, self.calendar, 20, 20)
        self.assertTrue(all(case["status"] == "signal_incomplete" for case in cases.values()))

    def test_missing_unselected_future_blocks_outcome_without_filtering_losers(self):
        original = evaluate(self.boards, self.days, self.calendar, 20, 20)
        unselected = next(board["code"] for board in self.boards if board["code"] not in original["return_top10"]["selected_codes"])
        changed = deepcopy(self.boards)
        board = next(board for board in changed if board["code"] == unselected)
        del board["prices"][self.days[30]]
        cases = evaluate(changed, self.days, self.calendar, 20, 20)
        for method, case in cases.items():
            self.assertEqual(case["selected_codes"], original[method]["selected_codes"])
            self.assertEqual(case["status"], "outcome_incomplete")

    def test_current_gate_and_count_matched_controls_do_not_fill_empty_slots(self):
        entries = [{"code": "a", "score": 90, "return_pct": 1, "state": "steady_up"},
                   {"code": "b", "score": 70, "return_pct": 3, "state": "strengthening"},
                   {"code": "c", "score": 99, "return_pct": 9, "state": "weakening"}]
        chosen = select(entries)
        self.assertEqual([row["code"] for row in chosen["trend_gated"]], ["a", "b"])
        self.assertEqual([row["code"] for row in chosen["state_gated_return"]], ["b", "a"])
        self.assertEqual([row["code"] for row in chosen["matched_count_return"]], ["c", "b"])
        chosen = select([{**row, "state": "down"} for row in entries])
        self.assertEqual(chosen["trend_gated"], [])
        self.assertEqual(chosen["matched_count_return"], [])

    def test_primary_outcome_intervals_do_not_overlap_and_empty_is_not_zero_return(self):
        falling = deepcopy(self.boards)
        for board in falling:
            board["prices"] = {day: 100 * math.exp(-.001 * i) for i, day in enumerate(self.days)}
        report = run(falling, self.days, self.calendar, [{"id": "old_bear", "start": "2018-01-01", "end": "2018-12-31"}], [0, 20])
        for period in report.values():
            primary = [row["methods"]["return_top10"] for row in period["cases"] if row["primary"]]
            for left, right in zip(primary, primary[1:]):
                if left["exit_date"] and right["entry_date"]:
                    self.assertLess(left["exit_date"], right["entry_date"])
            summary = period["primary"]["methods"]["trend_gated"]
            self.assertEqual(summary["matured_batches"], 0)
            self.assertIsNone(summary["mean_selected_return_pct"])
            self.assertEqual(period["latest"]["candidates"], [])
            self.assertEqual(period["episodes"]["old_bear"]["calendar_coverage"], "insufficient_history")

    def test_identical_basket_in_different_rank_order_is_not_counted_as_new_choice(self):
        case = {"methods": {"a": {"status": "matured", "selected_return_pct": 1, "selected_codes": ["a", "b"]},
                            "b": {"status": "matured", "selected_return_pct": 1, "selected_codes": ["b", "a"]}}}
        self.assertEqual(paired([case], "a", "b")["same_selection_dates"], 1)

    def test_nontrading_holiday_does_not_turn_full_year_coverage_into_partial(self):
        report = run(self.boards, self.days, self.calendar,
                     [{"id": "2025", "start": "2025-01-01", "end": "2025-12-31"}], [0])
        self.assertEqual(self.days[0], "2025-01-02")
        for period in report.values():
            self.assertEqual(period["episodes"]["2025"]["calendar_coverage"], "complete")

    @unittest.skipUnless(Path("runtime/verification/sector-horizon-research-v1-reviewed.json").exists(),
                         "Frozen matched-horizon report unavailable")
    def test_frozen_report_price_baskets_and_drawdowns_from_raw_saved_prices(self):
        path = Path("runtime/verification/sector-horizon-research-v1-reviewed.json")
        self.assertEqual(sha256(path.read_bytes()).hexdigest(),
                         "846d702c07884b334f4b82836f19ff01c8d621a0a68f0b88bc6be9fd9ad8d40f")
        report = json.loads(path.read_text())
        self.assertEqual(sha256(json.dumps(report["input"], sort_keys=True, allow_nan=False).encode()).hexdigest(),
                         report["parsed_input_sha256"])
        prices = {board["code"]: board["prices"] for board in report["input"]["boards"]}
        days = report["input"]["days"]
        indexes = {day: i for i, day in enumerate(days)}

        def expected(codes, grid):
            curve = [math.fsum(prices[code][day] / prices[code][grid[0]] for code in codes) / len(codes)
                     for day in grid]
            peak, drawdown = 1., 0.
            for value in curve:
                peak = max(peak, value)
                drawdown = min(drawdown, value / peak - 1)
            return (curve[-1] - 1) * 100, drawdown * 100

        checked = 0
        for period, window in PERIODS.items():
            value = report["periods"][period]
            self.assertEqual((value["lookback_sessions"], value["forward_sessions"]), (window, window))
            self.assertEqual(len(value["episodes"]), 9)
            for batch in value["cases"]:
                for case in batch["methods"].values():
                    if case["status"] != "matured":
                        self.assertNotIn("selected_return_pct", case)
                        continue
                    start, end = indexes[case["entry_date"]], indexes[case["exit_date"]]
                    self.assertEqual(start, indexes[case["signal_date"]] + 1)
                    self.assertEqual(end - start, window)
                    selected = expected(case["selected_codes"], days[start:end + 1])
                    control = expected(list(prices), days[start:end + 1])
                    for key, result in (("selected_return_pct", selected[0]),
                                        ("selected_max_drawdown_pct", selected[1]),
                                        ("control_return_pct", control[0]),
                                        ("control_max_drawdown_pct", control[1]),
                                        ("excess_return_pp", selected[0] - control[0])):
                        self.assertAlmostEqual(case[key], result, places=10)
                    checked += 1
        self.assertGreater(checked, 500)
