from copy import deepcopy
from datetime import date
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import unittest

from backend.analysis.sector_direction_screen import DIRECTION_FIELDS, GATES, SORTS, gate_members, screened_choices
from backend.core.trading_calendar import get_calendar
from scripts.benchmark_sector_trend import rank_at
from scripts.research_sector_direction_screens import OLD, QUALITY_FIELDS, future_quality


def entry(code, **overrides):
    return {"code": code, **dict.fromkeys(DIRECTION_FIELDS, 1.), "state": "steady_up",
            "daily_volatility_pct": 1., **overrides}


class SectorDirectionScreenTests(unittest.TestCase):
    def test_gate_separates_direction_recovery_weakening_and_volatile_up(self):
        entries = [entry("up"), entry("old_winner", recent_fitted_return_pct=-1, state="weakening"),
                   entry("recovery", return_pct=-1, fitted_return_pct=-1, state="recovery"),
                   entry("volatile", state="volatile_up"), entry("zero", fast_ma_bias_pct=0, state="range")]
        gates = gate_members(entries)
        self.assertEqual(gates["direction"], ["up", "volatile"])
        self.assertEqual(gates["state"], ["up"])

    def test_negative_model_outputs_do_not_silently_remove_positive_direction(self):
        entries = [entry("up"), entry("down", return_pct=-1, state="down")]
        predicted = {"up": {"return_rank": -.1, "safety_rank": -.1},
                     "down": {"return_rank": .1, "safety_rank": .1}}
        gates, chosen = screened_choices(entries, predicted)
        for gate in GATES:
            for sort in SORTS:
                self.assertEqual(chosen[f"{gate}_{sort}"], ["up"])
            self.assertEqual(chosen[f"{gate}_count_return"], ["up"])
        self.assertEqual(gates["direction"], ["up"])
        entries[0].update(return_pct=-1, state="down")
        _, chosen = screened_choices(entries, predicted)
        self.assertTrue(all(value == [] for value in chosen.values()))

    def test_maximin_uses_full_pool_percentiles_not_filtered_pool_and_preserves_scaling(self):
        entries = [entry(code, fast_ma_bias_pct=1 if code in "ab" else -1, state="range") for code in "abcdefg"]
        values = [(0, 10), (5, 5), (10, 0), (6, 1), (7, 2), (8, 3), (9, 4)]
        predicted = {code: {"return_rank": a, "safety_rank": b} for code, (a, b) in zip("abcdefg", values)}
        _, chosen = screened_choices(entries, predicted)
        self.assertEqual(chosen["direction_maximin"], ["b", "a"])
        scaled = {code: {"return_rank": value["return_rank"] * 1000,
                         "safety_rank": value["safety_rank"] * .001} for code, value in predicted.items()}
        self.assertEqual(chosen, screened_choices(entries, scaled)[1])
        equal = {code: dict.fromkeys(("return_rank", "safety_rank"), 0.) for code in "abcdefg"}
        self.assertEqual(screened_choices(entries, equal)[1]["direction_maximin"], ["a", "b"])

    def test_no_forced_fill_and_exact_same_gate_counts(self):
        entries = [entry(str(i), return_pct=i + 1, state="steady_up") for i in range(15)]
        predicted = {row["code"]: {"return_rank": -i, "safety_rank": i} for i, row in enumerate(entries)}
        gates, chosen = screened_choices(entries, predicted)
        self.assertEqual(len(gates["direction"]), 15)
        self.assertTrue(all(len(codes) == 10 for codes in chosen.values()))
        for row in entries[4:]:
            row.update(fast_ma_bias_pct=-1, state="down")
        _, chosen = screened_choices(entries, predicted)
        self.assertTrue(all(len(codes) == 4 for codes in chosen.values()))

    def test_missing_duplicate_and_nonfinite_peers_rejected(self):
        entries = [entry("a"), entry("b")]
        predicted = {code: {"return_rank": 0, "safety_rank": 0} for code in "ab"}
        with self.assertRaises(ValueError):
            gate_members([entries[0], entries[0]])
        with self.assertRaises(ValueError):
            screened_choices(entries, {"a": predicted["a"]})
        with self.assertRaises(ValueError):
            screened_choices([entry("a", return_pct=math.nan), entry("b")], predicted)
        with self.assertRaises(ValueError):
            screened_choices(entries, {**predicted, "a": {"return_rank": math.inf, "safety_rank": 0}})

    def test_future_quality_distinguishes_terminal_spike_from_persistent_path(self):
        paths = {"up": [100, 101, 102, 103, 104], "down": [100, 99, 98, 97, 96],
                 "flat": [100] * 5, "spike": [100, 99, 98, 97, 110], "lost": [100, 105, 104, 103, 99]}
        days = list(map(str, range(5)))
        boards = [{"code": code, "prices": dict(zip(days, values))} for code, values in paths.items()]
        result = future_quality(boards, days, 0, 4)
        self.assertEqual(result["up"], {"occupancy": 1., "efficiency": 1., "persistent": True})
        self.assertAlmostEqual(result["down"]["efficiency"], -1)
        self.assertEqual(result["flat"], {"occupancy": .5, "efficiency": 0., "persistent": False})
        self.assertEqual(result["spike"]["occupancy"], .25)
        self.assertFalse(result["spike"]["persistent"])
        self.assertFalse(result["lost"]["persistent"])
        scaled = [{**board, "prices": {day: value * 7 for day, value in board["prices"].items()}} for board in boards]
        for code, value in future_quality(scaled, days, 0, 4).items():
            self.assertEqual(value, result[code])
        del scaled[0]["prices"]["4"]
        with self.assertRaises(ValueError):
            future_quality(scaled, days, 0, 4)

    def test_future_prices_cannot_change_past_gate_or_model_sort(self):
        days = [day.isoformat() for day in get_calendar().sessions(date(2025, 1, 2), date(2026, 10, 14))]
        boards = [{"code": code, "name": code, "collection_error": None,
                   "prices": {day: 100 * math.exp(.001 * (j + 1) * i) for i, day in enumerate(days)}}
                  for j, code in enumerate("abc")]
        predicted = {code: {"return_rank": i, "safety_rank": -i} for i, code in enumerate("abc")}
        at = 150
        changed = deepcopy(boards)
        for board in changed:
            for day in days[at + 1:]:
                board["prices"][day] *= 10000
        for window in (20, 60, 120):
            self.assertEqual(screened_choices(rank_at(boards, days, at, window), predicted),
                             screened_choices(rank_at(changed, days, at, window), predicted))

    @unittest.skipUnless(Path("runtime/verification/sector-direction-screen-research-v1.json.gz").exists(),
                         "Direction-screen report not yet generated")
    def test_frozen_selections_controls_primary_returns_and_future_path_quality(self):
        with gzip.open("runtime/verification/sector-direction-screen-research-v1.json.gz", "rt") as stream:
            report = json.load(stream)
        with gzip.open("runtime/verification/sector-rank-risk-research-v1.json.gz", "rt") as stream:
            reference = json.load(stream)
        self.assertFalse(report["investment_eligible"])
        for path, source in report["source_snapshots"].items():
            self.assertEqual(sha256(source.encode()).hexdigest(), report["source_hashes"][path])
        boards, days = report["input"]["boards"], report["input"]["days"]
        indexes = {day: i for i, day in enumerate(days)}
        checked = 0
        for period, value in report["periods"].items():
            window = value["forward_sessions"]
            self.assertEqual(len(value["episodes"]), 9)
            self.assertEqual(len(value["cases"]), len(reference["periods"][period]["cases"]))
            for case, original in zip(value["cases"], reference["periods"][period]["cases"]):
                for method in OLD:
                    stripped = {key: val for key, val in case["methods"][method].items() if key not in QUALITY_FIELDS}
                    self.assertEqual(stripped, original["methods"][method])
                if case["diagnostic"] is not None:
                    i = indexes[case["as_of"]]
                    gates, chosen = screened_choices(rank_at(boards, days, i, window), original["predictions"])
                    self.assertEqual(gates, case["diagnostic"]["gate_codes"])
                    for method, codes in chosen.items():
                        self.assertEqual(codes, case["methods"][method]["selected_codes"])
                if not case["primary"]:
                    continue
                for method, batch in case["methods"].items():
                    if batch["status"] != "matured":
                        self.assertNotIn("selected_return_pct", batch)
                        continue
                    start, end = indexes[batch["entry_date"]], indexes[batch["exit_date"]]
                    self.assertEqual(end - start, window)
                    selected = [board for board in boards if board["code"] in batch["selected_codes"]]
                    paths = [[board["prices"][day] / board["prices"][days[start]] for day in days[start:end + 1]] for board in selected]
                    curve = [math.fsum(path[i] for path in paths) / len(paths) for i in range(window + 1)]
                    peak, worst = 1., 0.
                    for point in curve:
                        peak = max(peak, point)
                        worst = min(worst, point / peak - 1)
                    self.assertAlmostEqual((curve[-1] - 1) * 100, batch["selected_return_pct"], places=9)
                    self.assertAlmostEqual(worst * 100, batch["selected_max_drawdown_pct"], places=9)
                    occupancies = [(sum(p > 1 for p in path[1:]) + .5 * sum(p == 1 for p in path[1:])) / window for path in paths]
                    persistent = [path[-1] > 1 and occupancy > .5 for path, occupancy in zip(paths, occupancies)]
                    self.assertAlmostEqual(sum(occupancies) / len(paths) * 100, batch["selected_above_entry_occupancy_pct"], places=10)
                    self.assertAlmostEqual(sum(persistent) / len(paths) * 100, batch["selected_persistent_up_fraction_pct"], places=10)
                    checked += 1
        self.assertGreater(checked, 1000)


if __name__ == "__main__":
    unittest.main()
