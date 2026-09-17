from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from backend.core.config import Settings
from backend.core.errors import AppError

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def connect(settings: Settings) -> sqlite3.Connection:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(settings.database_path, timeout=settings.busy_timeout_ms / 1000, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {settings.busy_timeout_ms}")
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise AppError("sqlite_configuration_error", f"无法启用 WAL（实际模式：{mode}）", 500)
        connection.execute("PRAGMA synchronous = FULL")
        return connection
    except Exception:
        connection.close()
        raise


@contextmanager
def connection_scope(settings: Settings) -> Iterator[sqlite3.Connection]:
    connection = connect(settings)
    try:
        yield connection
    finally:
        connection.close()


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def _execute_script_atomically(connection: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("迁移文件包含未结束的 SQL 语句")


def migrate(settings: Settings) -> int:
    with connection_scope(settings) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS schema_migration (version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))" )
        applied = {row[0]: row[1] for row in connection.execute("SELECT version, checksum FROM schema_migration")}
        for path in _migration_files():
            version = int(path.name[:3])
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            if version in applied:
                if applied[version] != checksum:
                    raise AppError("migration_checksum_mismatch", f"迁移 {path.name} 校验和不匹配", 500)
                continue
            try:
                connection.execute("BEGIN IMMEDIATE")
                # Another process may have migrated after the initial read.
                current = connection.execute("SELECT checksum FROM schema_migration WHERE version=?", (version,)).fetchone()
                if current is not None:
                    if current[0] != checksum:
                        raise ValueError("并发迁移的校验和不匹配")
                    connection.execute("COMMIT")
                    continue
                _execute_script_atomically(connection, path.read_text(encoding="utf-8"))
                connection.execute("INSERT INTO schema_migration(version, name, checksum) VALUES (?, ?, ?)", (version, path.name, checksum))
                connection.execute("COMMIT")
            except Exception as exc:
                connection.execute("ROLLBACK")
                raise AppError("migration_failed", f"迁移 {path.name} 失败", 500, {"reason": str(exc)}) from exc
        return max(applied.keys() | {int(p.name[:3]) for p in _migration_files()}, default=0)


def sqlite_runtime_info(connection: sqlite3.Connection) -> dict[str, object]:
    return {
        "sqlite_version": sqlite3.sqlite_version,
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1,
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "busy_timeout_ms": connection.execute("PRAGMA busy_timeout").fetchone()[0],
        "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
    }
