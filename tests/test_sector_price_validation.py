import contextlib
from copy import deepcopy
from datetime import date
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.analysis.sector_price_validation import (
    label_case, run_diagnostic, signals_at, summarize,
)
from backend.analysis.sector_status import analyze_index
from backend.analysis.sector_strength import attach_strength, build_advantage_summary
from backend.core.trading_calendar import get_calendar
from scripts.validate_sector_prices import main, read_input


ROOT = Path(__file__).resolve().parents[1]


def snapshot(session_count=42, *, flat=False):
    """Four artificial industries on reviewed sessions; no network or database."""
    calendar = get_calendar()
    sessions = calendar.sessions(date(2025, 1, 2), date(2026, 9, 18))[:session_count]
    boards = []
    for code, slope in [("a", 4), ("b", 2), ("c", 1), ("d", 0)]:
        boards.append({
            "code": code, "name": code, "source_id": "sector_daily.ths",
            "rows": [{"date": day.isoformat(), "close": str(100 + (0 if flat else slope * i))}
                     for i, day in enumerate(sessions)],
        })
    return {"as_of": sessions[-1].isoformat(), "calendar": deepcopy(calendar.document), "boards": boards}


def protocol():
    return json.loads((ROOT / "config/sector-price-validation-v1.json").read_text(encoding="utf-8"))


class SectorPriceValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = get_calendar()
        cls.baseline = snapshot(364)
        cls.report = run_diagnostic(cls.baseline, protocol())

    def test_future_prices_and_missing_future_rows_cannot_change_signal(self):
        original = snapshot(150)
        signal_day = original["boards"][0]["rows"][120]["date"]
        expected = signals_at(original["boards"], signal_day, self.calendar)
        altered = deepcopy(original)
        for board in altered["boards"]:
            for row in board["rows"]:
                if row["date"] > signal_day:
                    row["close"] = "1" if board["code"] == "a" else "9999999"
        altered["boards"][0]["rows"].pop()
        self.assertEqual(signals_at(altered["boards"], signal_day, self.calendar), expected)
        self.assertEqual({key: [row["code"] for row in value["items"]] for key, value in expected.items()},
                         {"short": ["a"], "medium": ["a"], "long": ["a"]})

    def test_selection_matches_current_production_analysis_and_ranking(self):
        original = snapshot(150)
        signal_day = original["boards"][0]["rows"][120]["date"]
        items = []
        for board in original["boards"]:
            items.append({"code": board["code"], "name": board["name"], "kind": "行业",
                          "source_id": "sector_daily.ths", "universe_type": "hot_board",
                          **analyze_index(board["rows"], as_of=date.fromisoformat(signal_day), calendar=self.calendar)})
        attach_strength(items, {"hot_board": signal_day, "tracked_index": None})
        expected = {row["id"]: row for row in build_advantage_summary(items)}
        self.assertEqual(signals_at(original["boards"], signal_day, self.calendar), expected)

    def test_labels_start_at_next_close_and_end_after_exact_horizon(self):
        dates = [row["date"] for row in self.baseline["boards"][0]["rows"]]
        for key, horizon in [("short", 20), ("medium", 60), ("long", 120)]:
            with self.subTest(period=key):
                case = self.report["periods"][key]["cases"][0]
                self.assertEqual(case["status"], "matured")
                self.assertEqual(case["selected_codes"], ["a"])
                self.assertEqual(case["signal_date"], dates[horizon])
                self.assertEqual(case["entry_date"], dates[horizon + 1])
                self.assertEqual(case["exit_date"], dates[2 * horizon + 1])
                self.assertEqual(self.calendar.shift(date.fromisoformat(case["entry_date"]), horizon).isoformat(),
                                 case["exit_date"])
                expected_return = ((100 + 4 * (2 * horizon + 1)) / (100 + 4 * (horizon + 1)) - 1) * 100
                self.assertAlmostEqual(case["selected_return_pct"], expected_return)
                expected_control = sum(((100 + slope * (2 * horizon + 1)) /
                                        (100 + slope * (horizon + 1)) - 1) * 100
                                       for slope in [4, 2, 1, 0]) / 4
                self.assertAlmostEqual(case["control_return_pct"], expected_control)

    def test_primary_holding_intervals_do_not_overlap_for_any_horizon(self):
        for key, horizon in [("short", 20), ("medium", 60), ("long", 120)]:
            with self.subTest(period=key):
                cases = self.report["periods"][key]["cases"]
                primary = [case for case in cases if case["primary_schedule"]]
                matured = [case for case in primary if case["status"] == "matured"]
                self.assertGreaterEqual(len(matured), 2)
                for left, right in zip(matured, matured[1:]):
                    self.assertLess(left["exit_date"], right["entry_date"])
                for left, right in zip(primary, primary[1:]):
                    self.assertEqual(self.calendar.shift(date.fromisoformat(left["signal_date"]), horizon + 1).isoformat(),
                                     right["signal_date"])
                self.assertEqual(self.report["periods"][key]["primary"]["scheduled_batches"], len(primary))
                self.assertEqual(self.report["periods"][key]["overlapping_secondary"]["scheduled_batches"], len(cases))

    def test_missing_selected_future_price_retains_signal_and_full_pool_denominator(self):
        original = snapshot()
        expected = run_diagnostic(original, protocol())
        altered = deepcopy(original)
        altered["boards"][0]["rows"].pop(30)
        result = run_diagnostic(altered, protocol())
        expected_case = expected["periods"]["short"]["cases"][0]
        actual = result["periods"]["short"]["cases"][0]
        for field in ["signal_date", "selected_codes", "selected_names", "pool_count", "eligible_count", "entry_date", "exit_date"]:
            self.assertEqual(actual[field], expected_case[field])
        self.assertEqual(actual["selected_codes"], ["a"])
        self.assertEqual(actual["pool_count"], 4)
        self.assertEqual(actual["status"], "outcome_incomplete")
        self.assertEqual(actual["missing_codes"], ["a"])
        self.assertNotIn("selected_return_pct", actual)
        self.assertEqual(result["periods"]["short"]["primary"]["scheduled_batches"],
                         expected["periods"]["short"]["primary"]["scheduled_batches"])
        self.assertEqual(result["periods"]["short"]["primary"]["status_counts"]["outcome_incomplete"], 1)

    def test_missing_control_member_outcome_also_blocks_comparison(self):
        original = snapshot()
        original["boards"][-1]["rows"].pop(30)
        dates = [row["date"] for row in original["boards"][0]["rows"]][21:42]
        case = label_case(original["boards"], ["a"], dates)
        self.assertEqual(case, {"status": "outcome_incomplete", "missing_codes": ["d"]})

    def test_no_selection_and_not_matured_batches_remain_in_report(self):
        flat = run_diagnostic(snapshot(flat=True), protocol())["periods"]["short"]
        self.assertEqual(len(flat["cases"]), 22)
        self.assertTrue(all(case["status"] == "no_selection" for case in flat["cases"]))
        self.assertTrue(all(case["selected_codes"] == [] and case["pool_count"] == 4 for case in flat["cases"]))
        self.assertEqual(flat["primary"]["status_counts"], {"no_selection": 2})
        rising = run_diagnostic(snapshot(), protocol())["periods"]["short"]
        self.assertEqual(len(rising["cases"]), 22)
        self.assertEqual(rising["overlapping_secondary"]["status_counts"], {"matured": 1, "not_matured": 21})
        self.assertEqual(rising["cases"][-1]["selected_codes"], ["a"])
        self.assertIsNone(rising["cases"][-1]["entry_date"])
        self.assertIsNone(rising["cases"][-1]["exit_date"])
        self.assertNotIn("selected_return_pct", rising["cases"][-1])

    def test_summary_counts_batches_not_members_and_converts_bps_to_percentage_points(self):
        cases = [
            {"status": "matured", "signal_date": "2025-02-05", "selected_codes": ["a", "b", "c"],
             "selected_return_pct": 2.0, "control_return_pct": 1.0, "excess_return_pp": 1.0,
             "selected_max_drawdown_pct": -3.0},
            {"status": "matured", "signal_date": "2025-03-06", "selected_codes": ["d"],
             "selected_return_pct": -1.0, "control_return_pct": 1.0, "excess_return_pp": -2.0,
             "selected_max_drawdown_pct": -4.0},
            {"status": "not_matured", "selected_codes": ["a", "b"]},
            {"status": "no_selection", "selected_codes": []},
            {"status": "outcome_incomplete", "selected_codes": ["c"]},
        ]
        result = summarize(cases, [0, 20, 50, 100])
        self.assertEqual(result["scheduled_batches"], 5)
        self.assertEqual(result["matured_batches"], 2)
        self.assertEqual(result["selected_observations"], 4)
        self.assertEqual(result["mean_selected_return_pct"], 0.5)
        self.assertEqual(result["mean_excess_return_pp"], -0.5)
        self.assertEqual(result["positive_batch_ratio_pct"], 50)
        self.assertEqual(result["outperforming_batch_ratio_pct"], 50)
        self.assertEqual(result["worst_within_batch_drawdown_pct"], -4)
        for row, expected in zip(result["cost_stress"], [0.5, 0.3, 0.0, -0.5]):
            self.assertAlmostEqual(row["mean_selected_after_haircut_pct"], expected)
            self.assertEqual(row["positive_batches_after_haircut"], 1)
        empty = summarize([], [20])
        self.assertIsNone(empty["mean_selected_return_pct"])
        self.assertIsNone(empty["positive_batch_ratio_pct"])
        self.assertIsNone(empty["cost_stress"][0]["mean_selected_after_haircut_pct"])

    def test_empty_duplicate_or_foreign_source_pool_is_rejected(self):
        for defect in ["empty", "duplicate", "foreign"]:
            with self.subTest(defect=defect):
                original = snapshot()
                if defect == "empty":
                    original["boards"] = []
                elif defect == "duplicate":
                    original["boards"].append(deepcopy(original["boards"][0]))
                else:
                    original["boards"][0]["source_id"] = "other.provider"
                with self.assertRaises(ValueError):
                    run_diagnostic(original, protocol())

    def test_modified_entry_or_horizon_rules_cannot_masquerade_as_fixed_protocol(self):
        for field, value in [("entry_policy", "same_session_close"),
                             ("horizons", {"short": 10, "medium": 60, "long": 120})]:
            with self.subTest(field=field):
                altered = protocol()
                altered[field] = value
                with self.assertRaises(ValueError):
                    run_diagnostic(snapshot(), altered)

    def test_cli_replays_frozen_input_without_opening_database(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            frozen = snapshot()
            source = parent / "snapshot.json"
            source.write_text(json.dumps(frozen), encoding="utf-8")
            output = parent / "result"
            with patch("sys.argv", ["validate_sector_prices", "--input", str(source), "--output", str(output)]), \
                    patch("scripts.validate_sector_prices.read_input", side_effect=AssertionError("must not access database")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            self.assertEqual(json.loads((output / "input.json").read_text()), frozen)
            saved_protocol = json.loads((output / "protocol.json").read_text())
            result = json.loads((output / "report.json").read_text())
            self.assertEqual(result, run_diagnostic(frozen, saved_protocol))
            self.assertIn("backend/core/trading_calendar.py", saved_protocol["implementation_files"])
            self.assertIn("backend/analysis/sector_strength.py", saved_protocol["implementation_files"])
            self.assertFalse(result["investment_operations_enabled"])
            self.assertEqual(result["investment_effectiveness"], "not_established")

    def test_snapshot_reader_is_read_only_and_rejects_partial_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "fixture.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE sector_heat_snapshot (id INTEGER, requested_count INTEGER, universe_scope TEXT, as_of TEXT)")
                connection.execute("CREATE TABLE sector_heat_member (code TEXT, name TEXT, source_id TEXT, updated_at TEXT, history_source_id TEXT, history_source_code TEXT, history_identity_match TEXT, rows_json TEXT)")
                connection.execute("INSERT INTO sector_heat_snapshot VALUES (1, 1, 'verified_industry_all', '2025-01-02')")
                connection.execute("INSERT INTO sector_heat_member VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                   ("881001", "fixture", "sector_daily.ths", "2025-01-03T00:00:00Z",
                                    "sector_daily.ths", "881001", "native_source_code",
                                    '[{"date":"2025-01-02","close":"100"}]'))
            before = database.read_bytes()
            frozen = read_input(database)
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(frozen["boards"][0]["code"], "881001")
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE sector_heat_snapshot SET requested_count=2")
            with self.assertRaisesRegex(ValueError, "完整行业目录"):
                read_input(database)


if __name__ == "__main__":
    unittest.main()
