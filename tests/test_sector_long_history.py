from copy import deepcopy
from datetime import date
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backend.core.trading_calendar import TradingCalendar
from scripts.collect_sector_long_history import extend
from scripts.research_sector_long_horizons import attach_pool_coverage, checked_annual, reconstruct_board


class SectorLongHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = TradingCalendar(json.loads(Path("config/research-exchange-calendar-2009-2026-v2.json").read_text()))
        cls.as_of = date(2026, 10, 14)
        cls.annual = {year: [{"date": day.isoformat(), "close": "100"} for day in cls.calendar.sessions(
            date(year, 1, 1), min(date(year, 12, 31), cls.as_of))] for year in range(2017, 2027)}
        cls.board = {"code": "881101", "prices": {row["date"]: 100. for year in (2025, 2026) for row in cls.annual[year]}}

    def test_complete_history_preserves_every_original_price(self):
        prices, meta = extend(self.board, self.annual, calendar=self.calendar, as_of=self.as_of)
        self.assertEqual(meta["prepended_years"], list(range(2017, 2025)))
        self.assertEqual(meta["overlap_checked"], len(self.annual[2025]))
        self.assertTrue(all(prices[day] == value for day, value in self.board["prices"].items()))

    def test_any_changed_or_missing_overlap_retains_entire_original(self):
        for mutation in ("changed", "missing", "absent"):
            annual = deepcopy(self.annual)
            if mutation == "changed":
                annual[2025][0]["close"] = "101"
            elif mutation == "missing":
                annual[2025].pop()
            else:
                del annual[2025]
            prices, meta = extend(self.board, annual, calendar=self.calendar, as_of=self.as_of)
            self.assertEqual(prices, self.board["prices"])
            self.assertEqual(meta["status"], "retained_original")

    def test_year_gap_stops_extension_without_joining_older_islands(self):
        annual = deepcopy(self.annual)
        annual[2023] = []
        prices, meta = extend(self.board, annual, calendar=self.calendar, as_of=self.as_of)
        self.assertEqual(meta["prepended_years"], [2024])
        self.assertEqual(min(prices), self.annual[2024][0]["date"])
        self.assertNotIn(self.annual[2022][0]["date"], prices)

    def test_missing_interior_or_first_annual_session_is_not_filled(self):
        for index in (0, 10):
            annual = deepcopy(self.annual)
            del annual[2024][index]
            prices, meta = extend(self.board, annual, calendar=self.calendar, as_of=self.as_of)
            self.assertEqual(prices, self.board["prices"])
            self.assertEqual(meta["status"], "retained_original")

    def test_independent_publication_reconstruction_preserves_overlap_and_gap(self):
        for missing_year in (None, 2021, 2024, 2025):
            annual = deepcopy(self.annual)
            if missing_year:
                annual[missing_year] = []
            expected, _ = extend(self.board, annual, calendar=self.calendar, as_of=self.as_of)
            self.assertEqual(reconstruct_board(self.board, annual), expected)

    def archive(self, directory):
        data = ";".join(row["date"].replace("-", "") + ",100,100,100,100,10,1000,,,,0" for row in self.annual[2018])
        raw = ('quotebridge_v4_line_bk_881101_01_2018(' + json.dumps({"data": data}) + ')').encode()
        path = directory / "raw/881101/2018.js"
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
        return {"year": 2018, "url": "https://d.10jqka.com.cn/v4/line/bk_881101/01/2018.js",
                "status": "available", "http_status": 200, "raw_path": "raw/881101/2018.js",
                "sha256": sha256(raw).hexdigest(), "bytes": len(raw), "row_count": len(self.annual[2018]),
                "start": self.annual[2018][0]["date"], "end": self.annual[2018][-1]["date"]}

    def test_raw_archive_identity_bytes_and_coverage_are_revalidated(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            record = self.archive(directory)
            result = checked_annual(directory, record, "881101", calendar=self.calendar, as_of=self.as_of)
            self.assertEqual(len(result), len(self.annual[2018]))
            for field, value in (("sha256", "0" * 64), ("bytes", 1), ("row_count", 1),
                                 ("http_status", 403), ("url", "https://example.test/"),
                                 ("raw_path", "../outside.js"), ("raw_path", None)):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    checked_annual(directory, {**record, field: value}, "881101", calendar=self.calendar, as_of=self.as_of)

    def test_failed_request_does_not_become_an_available_year(self):
        record = {"year": 2018, "url": "https://d.10jqka.com.cn/v4/line/bk_881101/01/2018.js",
                  "status": "failed", "raw_path": None}
        self.assertEqual(checked_annual(Path("unused"), record, "881101", calendar=self.calendar, as_of=self.as_of), [])

    def test_early_global_axis_is_not_full_pool_price_or_signal_coverage(self):
        days = [day.isoformat() for day in self.calendar.sessions(date(2017, 1, 1), self.as_of)]
        inputs = {"days": days, "boards": [{"prices": {day: 100 for day in days}},
                   {"prices": {day: 100 for day in days if day >= "2019-01-02"}}]}
        episodes = [{"id": "old", "start": "2018-01-01", "end": "2018-12-31"},
                    {"id": "first", "start": "2019-01-01", "end": "2019-12-31"}]
        periods = {key: {"episodes": {row["id"]: {"calendar_coverage": "complete"} for row in episodes}}
                   for key in ("short", "medium", "long")}
        attach_pool_coverage(periods, inputs, self.calendar, episodes)
        for period in periods.values():
            self.assertEqual(period["episodes"]["old"]["calendar_axis_coverage"], "complete")
            self.assertEqual(period["episodes"]["old"]["calendar_coverage"], "insufficient_history")
            self.assertEqual(period["episodes"]["first"]["calendar_coverage"], "complete")
            self.assertEqual(period["episodes"]["first"]["full_pool_signal_coverage"], "partial")

    @unittest.skipUnless(Path("runtime/verification/sector-long-history-recovered-v1/collection.json").exists(),
                         "Frozen recovery archive unavailable")
    def test_recovery_changes_only_original_transport_failures_and_keeps_all_boards(self):
        original_path = Path("runtime/verification/sector-long-history-v1/collection.json")
        recovered_path = Path("runtime/verification/sector-long-history-recovered-v1/collection.json")
        self.assertEqual(sha256(original_path.read_bytes()).hexdigest(),
                         "d4e1e60bfeaef3e8f48e9868ca8aa9b80fcc938aa407d77db8cb21178849f592")
        self.assertEqual(sha256(recovered_path.read_bytes()).hexdigest(),
                         "3ae6e3753bf1036536d6e7698e52e99d62b8aa423b2db976312de71f1bd2ae65")
        old, new = json.loads(original_path.read_text()), json.loads(recovered_path.read_text())
        self.assertEqual([row["code"] for row in old["boards"]], [row["code"] for row in new["boards"]])
        attempted, recovered = 0, 0
        for a, b in zip(old["boards"], new["boards"]):
            for before, after in zip(a["requests"], b["requests"]):
                if before["status"] != "available" and before["http_status"] in (502, 504):
                    attempted += 1
                    recovered += after["status"] == "available"
                    previous = dict(after["previous_attempt"])
                    previous.pop("manifest")
                    self.assertEqual(previous, before)
                    self.assertEqual(after["url"], before["url"])
                else:
                    self.assertEqual(before, after)
        self.assertEqual((attempted, recovered, len(new["boards"])), (23, 10, 86))

    @unittest.skipUnless(Path("runtime/verification/sector-horizon-long-recovered-v1-reviewed.json.gz").exists(),
                         "Frozen long-sector report unavailable")
    def test_all_mature_primary_price_labels_and_full_pool_coverage_from_saved_input(self):
        path = Path("runtime/verification/sector-horizon-long-recovered-v1-reviewed.json.gz")
        self.assertEqual(sha256(path.read_bytes()).hexdigest(),
                         "a02a5c2d31dc602d4a4fd54880b54dc67baca341b930d23eddc89ab686349408")
        with gzip.open(path, "rt") as stream:
            report = json.load(stream)
        self.assertEqual(report["coverage"]["full_pool_common_start"], "2019-01-02")
        self.assertEqual(report["archive_validation"]["validated_annual_archives"], 749)
        self.assertEqual(report["archive_validation"]["original_prices_preserved"], 36894)
        self.assertEqual(report["old_report_compatibility"], "all_periods_cases_metrics_exact")
        inputs = report["input"]
        self.assertEqual(sha256(json.dumps(inputs, sort_keys=True, allow_nan=False).encode()).hexdigest(), report["input_sha256"])
        lookup = {day: i for i, day in enumerate(inputs["days"])}
        prices = {board["code"]: board["prices"] for board in inputs["boards"]}
        cache, checked = {}, 0

        def expected(codes, grid):
            key = (tuple(sorted(codes)), grid[0], grid[-1])
            if key not in cache:
                curve = [math.fsum(prices[code][day] / prices[code][grid[0]] for code in codes) / len(codes) for day in grid]
                peak, drawdown = 1., 0.
                for value in curve:
                    peak = max(peak, value)
                    drawdown = min(drawdown, value / peak - 1)
                cache[key] = ((curve[-1] - 1) * 100, drawdown * 100)
            return cache[key]

        for period in report["periods"].values():
            self.assertEqual(period["episodes"]["bear_2018"]["calendar_coverage"], "insufficient_history")
            self.assertEqual(period["episodes"]["bull_2019_2020"]["full_pool_signal_coverage"], "partial")
            self.assertEqual(period["episodes"]["bear_2021_2023"]["full_pool_signal_coverage"], "complete")
            for batch in period["cases"]:
                if not batch["primary"]:
                    continue
                for case in batch["methods"].values():
                    if case["status"] != "matured":
                        self.assertNotIn("selected_return_pct", case)
                        continue
                    start, end = lookup[case["entry_date"]], lookup[case["exit_date"]]
                    self.assertEqual(start, lookup[case["signal_date"]] + 1)
                    self.assertEqual(end - start, period["forward_sessions"])
                    grid = inputs["days"][start:end + 1]
                    selected, control = expected(case["selected_codes"], grid), expected(list(prices), grid)
                    for key, value in (("selected_return_pct", selected[0]), ("control_return_pct", control[0]),
                                       ("excess_return_pp", selected[0] - control[0]),
                                       ("selected_max_drawdown_pct", selected[1]), ("control_max_drawdown_pct", control[1])):
                        self.assertAlmostEqual(case[key], value, places=10)
                    checked += 1
        self.assertGreater(checked, 500)
