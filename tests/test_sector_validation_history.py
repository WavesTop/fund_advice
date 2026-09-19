"""Offline history-validation guards; all prices here are synthetic fixtures."""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from datetime import date
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend.core.trading_calendar import get_calendar
from scripts.collect_sector_validation_history import collect, merge_history, parse_annual


AS_OF = date(2026, 9, 18)
CODE = "881101"


def fixture_rows(year: int) -> list[dict[str, str]]:
    return [{"date": day.isoformat(), "open": "10", "high": "12", "low": "9",
             "close": "11", "volume": "100", "amount": "1100"}
            for day in get_calendar().sessions(date(year, 1, 1), min(date(year, 12, 31), AS_OF))]


def annual_payload(rows: list[dict], *, code: str = CODE, year: int = 2025,
                   period: str = "01") -> bytes:
    data = ";".join(
        ",".join([row["date"].replace("-", ""),
                  *[row[field] for field in ("open", "high", "low", "close", "volume", "amount")],
                  "", "", "", "0"])
        for row in rows
    )
    return (f"quotebridge_v4_line_bk_{code}_{period}_{year}("
            + json.dumps({"data": data}) + ")").encode()


class SectorValidationHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = get_calendar()
        cls.annual = {year: fixture_rows(year) for year in (2025, 2026)}

    def parse(self, raw: bytes, *, year: int = 2025, as_of: date = AS_OF):
        return parse_annual(raw, code=CODE, year=year, as_of=as_of, calendar=self.calendar)

    def test_complete_annual_sessions_and_original_closes_allow_merge(self):
        annual = []
        for year, expected in self.annual.items():
            actual = self.parse(annual_payload(expected, year=year), year=year)
            self.assertEqual(actual, expected)
            annual.extend(actual)
        board = {"code": CODE, "rows": deepcopy(self.annual[2026][-140:])}
        merged, checked = merge_history(board, annual, as_of=AS_OF, calendar=self.calendar)
        self.assertEqual(merged, annual)
        self.assertEqual(checked, 140)
        self.assertEqual(merged[0]["date"], "2025-01-02")
        self.assertEqual(merged[-1]["date"], AS_OF.isoformat())

    def test_callback_must_match_code_period_and_year(self):
        for changes in ({"code": "881102"}, {"period": "02"}, {"year": 2026}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "回调"):
                self.parse(annual_payload(self.annual[2025], **changes))

    def test_date_must_belong_to_requested_year(self):
        rows = deepcopy(self.annual[2025])
        rows[0]["date"] = "2026-01-05"
        with self.assertRaisesRegex(ValueError, "日期越界"):
            self.parse(annual_payload(rows))

    def test_missing_first_or_interior_session_is_rejected(self):
        for index in (0, 25):
            rows = deepcopy(self.annual[2025])
            del rows[index]
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "日历不完整"):
                self.parse(annual_payload(rows))

    def test_invalid_ohlc_is_rejected(self):
        for field, value in (("high", "10"), ("low", "12"), ("close", "0")):
            rows = deepcopy(self.annual[2025])
            rows[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.parse(annual_payload(rows))

    def test_duplicate_date_is_rejected(self):
        rows = deepcopy(self.annual[2025])
        rows.append(deepcopy(rows[0]))
        with self.assertRaisesRegex(ValueError, "日期重复"):
            self.parse(annual_payload(rows))

    def test_response_after_frozen_cutoff_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "日期越界"):
            self.parse(annual_payload(self.annual[2026], year=2026),
                       year=2026, as_of=date(2026, 9, 17))

    def test_changed_or_absent_frozen_close_prevents_merge(self):
        annual = deepcopy(self.annual[2025] + self.annual[2026])
        board = {"code": CODE, "rows": deepcopy(self.annual[2026][-140:])}
        board["rows"][0]["close"] = "10.99"
        with self.assertRaisesRegex(ValueError, "冻结收盘价不一致"):
            merge_history(board, annual, as_of=AS_OF, calendar=self.calendar)
        board["rows"][0]["close"] = "11"
        missing = [row for row in annual if row["date"] != board["rows"][0]["date"]]
        with self.assertRaisesRegex(ValueError, "缺少原始行情日期"):
            merge_history(board, missing, as_of=AS_OF, calendar=self.calendar)

    def test_either_failed_year_retains_whole_board_and_frozen_90_pool(self):
        boards = [{"code": str(881100 + i), "name": f"测试行业{i}",
                   "source_id": "sector_daily.ths", "rows": deepcopy(self.annual[2026][-3:])}
                  for i in range(90)]
        snapshot = {"as_of": AS_OF.isoformat(), "calendar": self.calendar.document,
                    "boards": boards, "other_frozen_metadata": {"keep": "unchanged"}}
        original = json.dumps(snapshot, ensure_ascii=False).encode()
        # Fail the first year of one industry and the second year of another.
        failures = {(boards[0]["code"], 2025), (boards[1]["code"], 2026)}

        def fake_year(*, code, year, **kwargs):
            failed = (code, year) in failures
            return ([] if failed else deepcopy(self.annual[year]),
                    {"year": year, "status": "failed" if failed else "available",
                     "error": "synthetic refusal" if failed else None})

        with TemporaryDirectory() as temp:
            input_path, output = Path(temp) / "frozen.json", Path(temp) / "collected"
            input_path.write_bytes(original)
            with patch("scripts.collect_sector_validation_history.collect_year", side_effect=fake_year) as fetch, \
                    patch("scripts.collect_sector_validation_history.urlopen", side_effect=AssertionError("network forbidden")), \
                    redirect_stdout(StringIO()):
                metadata = collect(input_path, output)
            result = json.loads((output / "input.json").read_text())
            self.assertEqual(fetch.call_count, 180)
            self.assertEqual([b["code"] for b in result["boards"]], [b["code"] for b in boards])
            self.assertEqual(result["boards"][:2], boards[:2])
            self.assertTrue(all(len(b["rows"]) == 417 for b in result["boards"][2:]))
            self.assertEqual(result["other_frozen_metadata"], snapshot["other_frozen_metadata"])
            self.assertEqual(metadata["coverage"]["pool_count"], 90)
            self.assertEqual(metadata["coverage"]["extended_boards"], 88)
            self.assertEqual(metadata["coverage"]["retained_original_boards"], 2)
            self.assertEqual(metadata["coverage"]["validated_annual_responses"], 178)
            self.assertFalse(metadata["coverage"]["full_pool_history_ready"])
            self.assertEqual(input_path.read_bytes(), original)
            self.assertEqual((output / "base-input.json").read_bytes(), original)
            self.assertEqual(metadata["base_input_sha256"], sha256(original).hexdigest())
            self.assertTrue(all(item["error"] for item in metadata["boards"][:2]))
            self.assertEqual(json.loads((output / "collection.json").read_text()), metadata)


if __name__ == "__main__":
    unittest.main()
