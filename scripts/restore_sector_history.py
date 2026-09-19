#!/usr/bin/env python3
"""Verify and atomically restore archived THS annual sector history."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.config import Settings
from backend.core.trading_calendar import TradingCalendar, get_calendar
from backend.storage.database import connection_scope, migrate
from backend.storage.research import canonical, digest, utc_now
from backend.storage.research_backup import backup_database
from backend.storage.sector_heat import merge_consistent_sector_history
from scripts.collect_sector_validation_history import parse_annual

ARCHIVE_VERSION = "ths-annual-validation-history-v1"
RESTORE_VERSION = "sector-history-restore-v1"
SOURCE_ID = "sector_daily.ths"
YEARS = (2025, 2026)
BOARD_COUNT = 90


@dataclass(frozen=True)
class VerifiedAsset:
    code: str
    year: int
    url: str
    media_type: str
    body: bytes
    body_hash: str
    asset_id: str
    fetched_at: str
    http_status: int


@dataclass(frozen=True)
class VerifiedBoard:
    code: str
    name: str
    current_rows_hash: str
    current_row_count: int
    current_start: str
    current_end: str
    restored_rows: list[dict[str, Any]]
    overlap_row_count: int


@dataclass(frozen=True)
class RecoveryPlan:
    archive: Path
    archive_manifest_hash: str
    base_input_hash: str
    archive_started_at: str
    archive_completed_at: str
    frozen_as_of: str
    boards: tuple[VerifiedBoard, ...]
    assets: tuple[VerifiedAsset, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "version": RESTORE_VERSION,
            "archive_version": ARCHIVE_VERSION,
            "archive": str(self.archive),
            "archive_manifest_sha256": self.archive_manifest_hash,
            "base_input_sha256": self.base_input_hash,
            "archive_started_at": self.archive_started_at,
            "archive_completed_at": self.archive_completed_at,
            "frozen_as_of": self.frozen_as_of,
            "source_id": SOURCE_ID,
            "board_count": len(self.boards),
            "raw_asset_count": len(self.assets),
            "previous_rows": sum(item.current_row_count for item in self.boards),
            "restored_rows": sum(len(item.restored_rows) for item in self.boards),
            "overlap_rows_checked": sum(item.overlap_row_count for item in self.boards),
            "previous_start": min(item.current_start for item in self.boards),
            "previous_end": max(item.current_end for item in self.boards),
            "restored_start": min(item.restored_rows[0]["date"] for item in self.boards),
            "restored_end": max(item.restored_rows[-1]["date"] for item in self.boards),
            "verification": [
                "manifest_and_raw_sha256",
                "exact_ths_code_year_jsonp",
                "complete_reviewed_trading_calendar",
                "current_identity_and_full_field_overlap",
                "all_90_boards_before_write",
            ],
        }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON字段重复：{key}")
        result[key] = value
    return result


def _read_json(path: Path, *, maximum: int = 64 * 1024 * 1024) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"档案文件缺失或为符号链接：{path.name}")
    body = path.read_bytes()
    if not body or len(body) > maximum:
        raise ValueError(f"档案文件为空或过大：{path.name}")
    value = json.loads(body, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError(f"档案文件不是JSON对象：{path.name}")
    return value, body


def _aware_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}缺少带时区时间")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field}缺少时区")
    return value


def _rows_hash(rows: list[dict[str, Any]]) -> str:
    return sha256(canonical(rows).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> tuple[str, int]:
    checksum, size = sha256(), 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
            size += len(chunk)
    return checksum.hexdigest(), size


def _read_current(settings: Settings) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    database = settings.database_path.resolve()
    if not database.is_file():
        raise ValueError("恢复目标数据库不存在")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        snapshot_row = connection.execute("SELECT * FROM sector_heat_snapshot WHERE id=1").fetchone()
        rows = connection.execute("SELECT * FROM sector_heat_member ORDER BY code").fetchall()
    finally:
        connection.close()
    if snapshot_row is None:
        raise ValueError("目标数据库没有板块快照")
    snapshot = dict(snapshot_row)
    return snapshot, {row["code"]: dict(row) for row in rows}


def verify_archive(settings: Settings, archive: Path) -> RecoveryPlan:
    """Verify the complete archive and current snapshot without writing either."""
    if archive.is_symlink() or not archive.is_dir() or not archive.name.endswith("annual-live"):
        raise ValueError("只接受名称以 annual-live 结尾的独立年度原始档案目录")
    archive = archive.resolve()
    manifest, manifest_bytes = _read_json(archive / "collection.json")
    base, base_bytes = _read_json(archive / "base-input.json")
    published, _ = _read_json(archive / "input.json")
    manifest_hash = sha256(manifest_bytes).hexdigest()
    base_hash = sha256(base_bytes).hexdigest()
    if manifest.get("version") != ARCHIVE_VERSION or manifest.get("base_input_path") != "base-input.json":
        raise ValueError("档案版本或基础快照路径不受支持")
    if manifest.get("base_input_sha256") != base_hash:
        raise ValueError("基础快照SHA256与采集清单不符")
    if published.get("history_collection") != manifest:
        raise ValueError("发布输入中的采集清单与collection.json不一致")
    started_at = _aware_timestamp(manifest.get("started_at"), "started_at")
    completed_at = _aware_timestamp(manifest.get("completed_at"), "completed_at")
    started_time = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    completed_time = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    if started_time > completed_time:
        raise ValueError("年度档案完成时间早于开始时间")
    if manifest.get("requested_years") != list(YEARS):
        raise ValueError("年度档案年份范围不是2025与2026")
    coverage = manifest.get("coverage", {})
    expected_coverage = {
        "pool_count": BOARD_COUNT,
        "extended_boards": BOARD_COUNT,
        "retained_original_boards": 0,
        "scheduled_requests": BOARD_COUNT * len(YEARS),
        "validated_annual_responses": BOARD_COUNT * len(YEARS),
        "full_pool_history_ready": True,
    }
    if any(coverage.get(key) != value for key, value in expected_coverage.items()):
        raise ValueError("年度档案不是90个行业全部成功的完整原始档案")
    if manifest.get("frozen_as_of") != base.get("as_of") or published.get("as_of") != base.get("as_of"):
        raise ValueError("年度档案截止日不一致")
    frozen_as_of = date.fromisoformat(base["as_of"])
    calendar = TradingCalendar(base.get("calendar", {}))
    if calendar.hash != manifest.get("calendar_hash") or calendar.version != manifest.get("calendar_version"):
        raise ValueError("年度档案交易日历证明不一致")
    local_calendar = get_calendar()
    if calendar.hash != local_calendar.hash or calendar.version != local_calendar.version:
        raise ValueError("年度档案交易日历与当前已核验日历不一致")
    if not calendar.is_session(frozen_as_of):
        raise ValueError("年度档案截止日不是已核验交易日")

    base_boards = base.get("boards")
    published_boards = published.get("boards")
    manifest_boards = manifest.get("boards")
    if not all(isinstance(value, list) and len(value) == BOARD_COUNT
               for value in (base_boards, published_boards, manifest_boards)):
        raise ValueError("年度档案必须包含90个行业")
    if coverage.get("published_rows") != sum(len(item.get("rows", [])) for item in published_boards):
        raise ValueError("年度档案发布行数与清单不一致")
    by_base = {str(item.get("code")): item for item in base_boards}
    by_published = {str(item.get("code")): item for item in published_boards}
    by_manifest = {str(item.get("code")): item for item in manifest_boards}
    if any(len(value) != BOARD_COUNT for value in (by_base, by_published, by_manifest)):
        raise ValueError("年度档案行业代码重复")

    snapshot, current = _read_current(settings)
    if (snapshot.get("as_of") != base["as_of"] or snapshot.get("universe_scope") != "verified_industry_all"
            or len(current) != BOARD_COUNT or set(current) != set(by_base)
            or set(by_base) != set(by_published) or set(by_base) != set(by_manifest)):
        raise ValueError("当前数据库与年度档案的冻结90行业身份或截止日不一致")

    verified_assets: list[VerifiedAsset] = []
    verified_boards: list[VerifiedBoard] = []
    expected_raw_paths: set[Path] = set()
    seen_urls: set[str] = set()
    for code in sorted(current):
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError(f"行业代码无效：{code}")
        base_board, output_board, item = by_base[code], by_published[code], by_manifest[code]
        database_board = current[code]
        name = str(base_board.get("name", ""))
        expected_source_code = "bk_" + code
        if (not name or output_board.get("name") != name or item.get("name") != name
                or database_board.get("name") != name or database_board.get("source_id") != SOURCE_ID
                or database_board.get("history_source_id") != SOURCE_ID
                or database_board.get("history_source_code") != expected_source_code
                or database_board.get("history_identity_match") != "native_source_code"
                or base_board.get("source_id") != SOURCE_ID
                or base_board.get("history_source_id") != SOURCE_ID
                or base_board.get("history_source_code") != expected_source_code
                or output_board.get("source_id") != SOURCE_ID
                or output_board.get("history_source_id") != SOURCE_ID
                or output_board.get("history_source_code") != expected_source_code):
            raise ValueError(f"行业{code}的来源或身份不一致")
        current_rows = json.loads(database_board["rows_json"])
        base_rows = base_board.get("rows")
        if not isinstance(base_rows, list) or _rows_hash(base_rows) != _rows_hash(current_rows):
            raise ValueError(f"行业{code}当前日线不等于年度档案冻结基线")
        requests = item.get("requests")
        if (item.get("status") != "extended" or not isinstance(requests, list)
                or len(requests) != len(YEARS) or {request.get("year") for request in requests} != set(YEARS)):
            raise ValueError(f"行业{code}没有两个完整年度响应")
        annual_rows: list[dict[str, Any]] = []
        for request in sorted(requests, key=lambda value: value["year"]):
            year = request["year"]
            expected_url = f"https://d.10jqka.com.cn/v4/line/bk_{code}/01/{year}.js"
            expected_relative = Path("raw") / code / f"{year}.js"
            raw_path = archive / expected_relative
            expected_raw_paths.add(raw_path.resolve())
            fetched_at = _aware_timestamp(request.get("fetched_at"), f"{code}/{year} fetched_at")
            fetched_time = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            if not started_time <= fetched_time <= completed_time:
                raise ValueError(f"行业{code} {year}采集时间超出档案运行区间")
            if (request.get("url") != expected_url or request.get("response_url") != expected_url
                    or request.get("raw_path") != expected_relative.as_posix()
                    or request.get("status") != "available" or request.get("http_status") != 200
                    or request.get("response_truncated") is not False or request.get("error") is not None
                    or request.get("media_type") != "application/javascript"):
                raise ValueError(f"行业{code} {year}来源请求证明不完整")
            if expected_url in seen_urls:
                raise ValueError(f"年度来源URL重复：{expected_url}")
            seen_urls.add(expected_url)
            raw_parent = archive / "raw" / code
            if (raw_path.is_symlink() or raw_parent.is_symlink() or (archive / "raw").is_symlink()
                    or not raw_path.is_file() or raw_path.resolve().parent != raw_parent.resolve()
                    or not raw_path.resolve().is_relative_to(archive)):
                raise ValueError(f"行业{code} {year}原始响应路径无效")
            body = raw_path.read_bytes()
            body_hash = sha256(body).hexdigest()
            if (not body or len(body) > 10 * 1024 * 1024 or request.get("bytes") != len(body)
                    or request.get("sha256") != body_hash):
                raise ValueError(f"行业{code} {year}原始响应SHA256或长度不符")
            rows = parse_annual(body, code=code, year=year, as_of=frozen_as_of, calendar=calendar)
            if (request.get("row_count") != len(rows) or request.get("start") != rows[0]["date"]
                    or request.get("end") != rows[-1]["date"]):
                raise ValueError(f"行业{code} {year}响应行数或日期范围与清单不符")
            annual_rows.extend(rows)
            verified_assets.append(VerifiedAsset(
                code=code, year=year, url=expected_url, media_type=request["media_type"], body=body,
                body_hash=body_hash, asset_id=digest([expected_url, request["media_type"], body_hash]),
                fetched_at=fetched_at, http_status=200,
            ))
        restored_rows, merge = merge_consistent_sector_history(current_rows, annual_rows)
        quality = calendar.validate_grid(
            [row["date"] for row in restored_rows], end=frozen_as_of, minimum=len(restored_rows),
        )
        if quality["status"] != "ready":
            raise ValueError(f"行业{code}恢复后交易日不完整：{quality['errors']}")
        if restored_rows != output_board.get("rows"):
            raise ValueError(f"行业{code}重算历史与档案发布输入不一致")
        if (item.get("original_rows") != len(current_rows)
                or item.get("published_rows") != len(restored_rows)
                or item.get("overlap_closes_checked") != merge["overlap_row_count"]
                or item.get("start") != restored_rows[0]["date"]
                or item.get("end") != restored_rows[-1]["date"]):
            raise ValueError(f"行业{code}恢复统计与采集清单不一致")
        verified_boards.append(VerifiedBoard(
            code=code, name=name, current_rows_hash=_rows_hash(current_rows),
            current_row_count=len(current_rows), current_start=current_rows[0]["date"],
            current_end=current_rows[-1]["date"], restored_rows=restored_rows,
            overlap_row_count=merge["overlap_row_count"],
        ))
    raw_entries = list((archive / "raw").rglob("*"))
    if any(path.is_symlink() for path in raw_entries):
        raise ValueError("年度原始档案raw目录包含符号链接")
    actual_raw_paths = {path.resolve() for path in raw_entries if path.is_file()}
    if actual_raw_paths != expected_raw_paths or len(verified_assets) != BOARD_COUNT * len(YEARS):
        raise ValueError("年度原始档案包含缺失或未登记的响应文件")
    return RecoveryPlan(
        archive=archive, archive_manifest_hash=manifest_hash, base_input_hash=base_hash,
        archive_started_at=started_at, archive_completed_at=completed_at,
        frozen_as_of=base["as_of"], boards=tuple(verified_boards), assets=tuple(verified_assets),
    )


def _apply_plan(settings: Settings, plan: RecoveryPlan, *, backup: dict[str, Any]) -> dict[str, Any]:
    migrate(settings)
    applied_at = utc_now()
    recovery_id = uuid4().hex
    summary = plan.summary()
    result = {**summary, "mode": "applied", "recovery_id": recovery_id,
              "applied_at": applied_at, "backup": backup}
    with connection_scope(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if connection.execute(
                "SELECT 1 FROM collection_run WHERE target='sectors' AND state='running' LIMIT 1"
            ).fetchone():
                raise ValueError("板块采集正在运行，拒绝并发恢复")
            snapshot = connection.execute("SELECT as_of,universe_scope FROM sector_heat_snapshot WHERE id=1").fetchone()
            current_rows = connection.execute("SELECT * FROM sector_heat_member ORDER BY code").fetchall()
            current = {row["code"]: dict(row) for row in current_rows}
            if (snapshot is None or snapshot["as_of"] != plan.frozen_as_of
                    or snapshot["universe_scope"] != "verified_industry_all"
                    or set(current) != {item.code for item in plan.boards}):
                raise ValueError("备份后数据库快照发生变化，拒绝恢复")
            for board in plan.boards:
                row = current[board.code]
                rows = json.loads(row["rows_json"])
                if (row["name"] != board.name or row["source_id"] != SOURCE_ID
                        or row["history_source_id"] != SOURCE_ID
                        or row["history_source_code"] != "bk_" + board.code
                        or _rows_hash(rows) != board.current_rows_hash):
                    raise ValueError(f"备份后行业{board.code}身份或日线发生变化，拒绝恢复")
            for asset in plan.assets:
                existing = connection.execute("SELECT * FROM raw_asset WHERE asset_id=?", (asset.asset_id,)).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO raw_asset VALUES(?,?,?,?,?,?)",
                        (asset.asset_id, asset.url, asset.media_type, asset.body,
                         asset.body_hash, asset.fetched_at),
                    )
                else:
                    if (existing["source_url"] != asset.url or existing["media_type"] != asset.media_type
                            or bytes(existing["body"]) != asset.body or existing["body_hash"] != asset.body_hash):
                        raise ValueError(f"原始资产标识冲突：{asset.url}")
                    existing_first_seen = datetime.fromisoformat(
                        existing["first_seen_at"].replace("Z", "+00:00")
                    )
                    archived_first_seen = datetime.fromisoformat(asset.fetched_at.replace("Z", "+00:00"))
                    if existing_first_seen > archived_first_seen:
                        raise ValueError(f"原始资产已有首次见到时间晚于档案时间，不能伪造：{asset.url}")
            for board in plan.boards:
                count = connection.execute(
                    """UPDATE sector_heat_member SET rows_json=?
                       WHERE code=? AND source_id=? AND history_source_id=? AND history_source_code=?""",
                    (json.dumps(board.restored_rows, ensure_ascii=False, separators=(",", ":")),
                     board.code, SOURCE_ID, SOURCE_ID, "bk_" + board.code),
                ).rowcount
                if count != 1:
                    raise ValueError(f"行业{board.code}恢复更新数量异常")
            connection.execute(
                "INSERT INTO collection_run VALUES(?,?,?,?,'success',?,?)",
                (recovery_id, "sector_history_restore", applied_at, applied_at, os.getpid(), canonical(result)),
            )
            connection.executemany(
                """INSERT INTO collection_attempt
                   (run_id,occurred_at,attempt,url,http_status,error,raw_asset_id)
                   VALUES(?,?,1,?,?,NULL,?)""",
                [(recovery_id, asset.fetched_at, asset.url, asset.http_status, asset.asset_id)
                 for asset in plan.assets],
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    return result


def execute(settings: Settings, archive: Path, *, apply: bool = False,
            backup_path: Path | None = None) -> dict[str, Any]:
    plan = verify_archive(settings, archive)
    if not apply:
        if backup_path is not None:
            raise ValueError("dry-run不创建备份；仅在--apply时提供--backup")
        return {**plan.summary(), "mode": "dry-run", "eligible": True, "database_changed": False}
    if backup_path is None:
        raise ValueError("正式恢复必须使用--backup指定不存在的新备份文件")
    backup = backup_database(settings, backup_path)
    backup_hash, backup_size = _file_sha256(backup_path)
    backup = {**backup, "sha256": backup_hash, "bytes": backup_size}
    return _apply_plan(settings, plan, backup=backup)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只核验，不写数据库（默认）")
    mode.add_argument("--apply", action="store_true", help="备份后原子写入已核验历史与来源证明")
    parser.add_argument("--backup", type=Path, help="--apply所需的不存在的新SQLite备份路径")
    args = parser.parse_args(argv)
    try:
        result = execute(Settings(database_path=args.database), args.archive,
                         apply=args.apply, backup_path=args.backup)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps({"status": "verified" if result["mode"] == "dry-run" else "applied", **result},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
