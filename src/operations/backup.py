# -*- coding: utf-8 -*-
"""SQLite 在线备份和完整性校验。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class BackupResult:
    path: str
    size_bytes: int
    sha256: str
    verified: bool


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_sqlite(path: str) -> tuple[bool, str]:
    if not os.path.isfile(path):
        return False, "备份文件不存在"
    try:
        conn = sqlite3.connect(path)
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.close()
    except sqlite3.DatabaseError as exc:
        return False, f"SQLite校验失败：{exc}"
    if quick != "ok":
        return False, f"SQLite quick_check：{quick}"
    if fk:
        return False, f"外键校验失败：{len(fk)} 项"
    return True, "ok"


def backup_sqlite(source_path: str, destination_dir: str, *, kind: str = "manual",
                  record_conn=None) -> BackupResult:
    if not os.path.isfile(source_path):
        raise FileNotFoundError(source_path)
    os.makedirs(destination_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"platform_gui_{stamp}_{kind}.db"
    target = os.path.abspath(os.path.join(destination_dir, filename))
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    verified, _reason = validate_sqlite(target)
    size = os.path.getsize(target)
    digest = _hash_file(target)
    if record_conn is not None:
        record_conn.execute(
            "INSERT INTO backup_records (path, kind, size_bytes, sha256, verified, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (target, kind, size, digest, int(verified), datetime.now().isoformat(timespec="seconds")),
        )
        record_conn.commit()
    return BackupResult(target, size, digest, verified)
