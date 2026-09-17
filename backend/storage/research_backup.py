"""Consistent SQLite backup including bounded raw BLOBs and immutable manifests."""
from __future__ import annotations
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from backend.core.config import Settings
from backend.storage.database import connection_scope, migrate
from backend.storage.research import digest


def backup_database(settings: Settings, output: Path) -> dict:
    output = output.resolve()
    if output.exists() or output == settings.database_path.resolve():
        raise ValueError("备份目标必须是不存在的新文件，禁止覆盖源库或旧备份")
    migrate(settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.research-backup-', suffix='.sqlite3', dir=output.parent)
    os.close(fd)
    try:
        with connection_scope(settings) as source:
            target = sqlite3.connect(temporary)
            try:
                source.backup(target)
                target.execute('PRAGMA journal_mode=DELETE')
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or target.execute('PRAGMA foreign_key_check').fetchall():
                    raise ValueError('备份结构或外键校验失败')
                for body, expected in target.execute('SELECT body,body_hash FROM raw_asset'):
                    if sha256(body).hexdigest() != expected:
                        raise ValueError('备份原始资料哈希不符')
                for payload, expected in target.execute('SELECT manifest_json,manifest_hash FROM data_snapshot'):
                    if digest(json.loads(payload)) != expected:
                        raise ValueError('备份快照哈希不符')
                counts = {table: target.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                          for table in ('raw_asset','data_revision','data_snapshot','analysis_run','validation_trial','forward_observation','validation_report')}
            finally:
                target.close()
        with open(temporary, 'rb') as handle:
            os.fsync(handle.fileno())
        # Atomic no-clobber publication. The temporary file is on the same filesystem.
        os.link(temporary, output)
        return {'output':str(output),'counts':counts,'integrity':'ok','raw_and_manifest_hashes':'verified',
                'note':'备份可包含running任务；恢复后须确认原工作进程已停止再解除残留互斥。'}
    finally:
        Path(temporary).unlink(missing_ok=True)
