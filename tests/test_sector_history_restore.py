from __future__ import annotations

from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend.core.config import Settings
from backend.core.trading_calendar import get_calendar
from backend.storage.database import connection_scope, migrate
from backend.storage.research import digest
from backend.storage.sector_heat import save_sector_heat
from scripts.restore_sector_history import execute, verify_archive


AS_OF = date(2026, 9, 18)
FETCHED_AT = "2026-09-19T11:54:47+00:00"


def price_rows(days) -> list[dict[str, str]]:
    return [{"date": day.isoformat(), "open": "100", "high": "102", "low": "99", "close": "101",
             "volume": "1000", "amount": "100000"} for day in days]


def write_json(path: Path, value: dict) -> bytes:
    body = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    path.write_bytes(body)
    return body


class SectorHistoryRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(database_path=self.root / "app.sqlite3")
        self.archive = self.root / "fixture-annual-live"
        self.codes = [f"{881101 + index:06}" for index in range(90)]
        calendar = get_calendar()
        self.all_days = calendar.sessions(date(2025, 1, 1), AS_OF)
        self.current_days = self.all_days[-140:]
        self.current_rows = price_rows(self.current_days)
        members = [
            {"code": code, "name": f"合成行业{index:02}", "kind": "行业", "source_id": "sector_daily.ths",
             "heat_rank": index + 1, "heat_value": str(1000 - index), "heat_updated_at": AS_OF.isoformat(),
             "updated_at": FETCHED_AT, "collection_error": None, "rows": deepcopy(self.current_rows),
             "history_source_id": "sector_daily.ths", "history_source_code": "bk_" + code,
             "history_identity_match": "native_source_code", "constituents": [], "constituent_error": None}
            for index, code in enumerate(self.codes)
        ]
        save_sector_heat(
            self.settings, as_of=AS_OF.isoformat(), updated_at=FETCHED_AT,
            catalog_count=90, requested_count=90, members=members,
            universe_scope="verified_industry_all",
        )
        self._write_archive(members)

    def _write_archive(self, members):
        self.archive.mkdir()
        base = {
            "exported_at": FETCHED_AT,
            "as_of": AS_OF.isoformat(),
            "universe_snapshot": {"as_of": AS_OF.isoformat(), "universe_scope": "verified_industry_all"},
            "calendar": get_calendar().document,
            "boards": [
                {key: item[key] for key in ("code", "name", "source_id", "updated_at",
                                             "history_source_id", "history_source_code",
                                             "history_identity_match", "rows")}
                for item in members
            ],
        }
        base_bytes = write_json(self.archive / "base-input.json", base)
        output = deepcopy(base)
        manifest_items = []
        for board in output["boards"]:
            annual_rows, requests = [], []
            for year in (2025, 2026):
                rows = price_rows(get_calendar().sessions(
                    date(year, 1, 1), min(date(year, 12, 31), AS_OF),
                ))
                annual_rows.extend(rows)
                raw = (
                    f"quotebridge_v4_line_bk_{board['code']}_01_{year}("
                    + json.dumps({"data": ";".join(
                        f"{row['date'].replace('-', '')},{row['open']},{row['high']},{row['low']},"
                        f"{row['close']},{row['volume']},{row['amount']},0,0,0,0" for row in rows
                    )}, ensure_ascii=False, separators=(",", ":"))
                    + ");"
                ).encode("gb18030")
                relative = Path("raw") / board["code"] / f"{year}.js"
                (self.archive / relative).parent.mkdir(parents=True, exist_ok=True)
                (self.archive / relative).write_bytes(raw)
                url = f"https://d.10jqka.com.cn/v4/line/bk_{board['code']}/01/{year}.js"
                requests.append({
                    "year": year, "url": url, "status": "available", "http_status": 200,
                    "fetched_at": FETCHED_AT, "raw_path": relative.as_posix(),
                    "sha256": sha256(raw).hexdigest(), "bytes": len(raw),
                    "response_truncated": False, "error": None, "response_url": url,
                    "media_type": "application/javascript", "row_count": len(rows),
                    "start": rows[0]["date"], "end": rows[-1]["date"],
                })
            board["rows"] = annual_rows
            manifest_items.append({
                "code": board["code"], "name": board["name"], "status": "extended",
                "original_rows": len(self.current_rows), "requests": requests, "error": None,
                "published_rows": len(annual_rows), "overlap_closes_checked": len(self.current_rows),
                "start": annual_rows[0]["date"], "end": annual_rows[-1]["date"],
            })
        manifest = {
            "version": "ths-annual-validation-history-v1",
            "started_at": FETCHED_AT, "completed_at": "2026-09-19T11:56:17+00:00",
            "base_input_sha256": sha256(base_bytes).hexdigest(), "base_input_path": "base-input.json",
            "frozen_as_of": AS_OF.isoformat(), "requested_years": [2025, 2026],
            "request_policy": {"workers": 2, "minimum_start_interval_seconds": .5,
                               "timeout_seconds": 20, "attempts_per_url": 1, "cookies": False},
            "coverage": {"pool_count": 90, "extended_boards": 90, "retained_original_boards": 0,
                         "scheduled_requests": 180, "validated_annual_responses": 180,
                         "published_rows": sum(len(item["rows"]) for item in output["boards"]),
                         "full_pool_history_ready": True},
            "calendar_version": get_calendar().version, "calendar_hash": get_calendar().hash,
            "limitations": [], "boards": manifest_items,
        }
        write_json(self.archive / "collection.json", manifest)
        output["history_collection"] = manifest
        write_json(self.archive / "input.json", output)

    def database_counts(self):
        with connection_scope(self.settings) as connection:
            return {
                "rows": sum(len(json.loads(row[0])) for row in connection.execute(
                    "SELECT rows_json FROM sector_heat_member"
                )),
                "assets": connection.execute("SELECT COUNT(*) FROM raw_asset").fetchone()[0],
                "recoveries": connection.execute(
                    "SELECT COUNT(*) FROM collection_run WHERE target='sector_history_restore'"
                ).fetchone()[0],
            }

    def test_dry_run_verifies_every_asset_without_writing_database(self):
        before = self.database_counts()
        result = execute(self.settings, self.archive)
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual((result["board_count"], result["raw_asset_count"]), (90, 180))
        self.assertEqual((result["previous_rows"], result["restored_rows"]), (12600, 37530))
        self.assertEqual(result["overlap_rows_checked"], 12600)
        self.assertEqual(self.database_counts(), before)

    def test_apply_requires_backup_and_commits_history_assets_and_audit_together(self):
        with self.assertRaisesRegex(ValueError, "必须使用--backup"):
            execute(self.settings, self.archive, apply=True)
        backup = self.root / "backup" / "before.sqlite3"
        result = execute(self.settings, self.archive, apply=True, backup_path=backup)
        self.assertEqual(result["mode"], "applied")
        self.assertTrue(backup.is_file())
        self.assertEqual(self.database_counts(), {"rows": 37530, "assets": 180, "recoveries": 1})
        with connection_scope(self.settings) as connection:
            first_seen = connection.execute("SELECT MIN(first_seen_at),MAX(first_seen_at) FROM raw_asset").fetchone()
            attempts = connection.execute(
                "SELECT COUNT(*) FROM collection_attempt WHERE run_id=?", (result["recovery_id"],)
            ).fetchone()[0]
        self.assertEqual(tuple(first_seen), (FETCHED_AT, FETCHED_AT))
        self.assertEqual(attempts, 180)
        backup_db = sqlite3.connect(backup)
        try:
            old_rows = sum(len(json.loads(row[0])) for row in backup_db.execute(
                "SELECT rows_json FROM sector_heat_member"
            ))
            old_assets = backup_db.execute("SELECT COUNT(*) FROM raw_asset").fetchone()[0]
        finally:
            backup_db.close()
        self.assertEqual((old_rows, old_assets), (12600, 0))

    def test_tampered_raw_response_fails_before_backup_or_write(self):
        path = self.archive / "raw" / self.codes[0] / "2025.js"
        path.write_bytes(path.read_bytes() + b"x")
        backup = self.root / "should-not-exist.sqlite3"
        before = self.database_counts()
        with self.assertRaisesRegex(ValueError, "SHA256"):
            execute(self.settings, self.archive, apply=True, backup_path=backup)
        self.assertFalse(backup.exists())
        self.assertEqual(self.database_counts(), before)

    def test_full_field_overlap_conflict_is_rejected(self):
        base_path = self.archive / "base-input.json"
        base = json.loads(base_path.read_text())
        base["boards"][0]["rows"][0]["volume"] = "1001"
        base_bytes = write_json(base_path, base)
        manifest_path = self.archive / "collection.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["base_input_sha256"] = sha256(base_bytes).hexdigest()
        write_json(manifest_path, manifest)
        published_path = self.archive / "input.json"
        published = json.loads(published_path.read_text())
        published["history_collection"] = manifest
        write_json(published_path, published)
        with connection_scope(self.settings) as connection:
            row = connection.execute("SELECT rows_json FROM sector_heat_member WHERE code=?", (self.codes[0],)).fetchone()
            rows = json.loads(row[0]); rows[0]["volume"] = "1001"
            connection.execute("UPDATE sector_heat_member SET rows_json=? WHERE code=?",
                               (json.dumps(rows, separators=(",", ":")), self.codes[0]))
        with self.assertRaisesRegex(ValueError, "重叠值冲突"):
            verify_archive(self.settings, self.archive)
        self.assertEqual(self.database_counts()["rows"], 12600)

    def test_apply_time_asset_timestamp_conflict_rolls_back_every_change(self):
        manifest = json.loads((self.archive / "collection.json").read_text())
        request = manifest["boards"][0]["requests"][0]
        body = (self.archive / request["raw_path"]).read_bytes()
        asset_id = digest([request["url"], request["media_type"], request["sha256"]])
        migrate(self.settings)
        with connection_scope(self.settings) as connection:
            connection.execute("INSERT INTO raw_asset VALUES(?,?,?,?,?,?)", (
                asset_id, request["url"], request["media_type"], body,
                request["sha256"], "2026-09-20T00:00:00+00:00",
            ))
        backup = self.root / "rollback-backup.sqlite3"
        with self.assertRaisesRegex(ValueError, "不能伪造"):
            execute(self.settings, self.archive, apply=True, backup_path=backup)
        self.assertTrue(backup.exists())
        self.assertEqual(self.database_counts(), {"rows": 12600, "assets": 1, "recoveries": 0})


if __name__ == "__main__":
    unittest.main()
