# -*- coding: utf-8 -*-
"""
幂等迁移执行器（对应技术方案 Agent A：数据库与迁移）。

设计目标：
- 每个迁移版本是 (version, downgrade_none, upgrade(conn)) 三元组；
- 用 ``schema_migrations`` 表记录已执行版本，避免重复执行；
- 迁移只允许新增表/字段/索引，迁移失败后旧库必须仍可打开（事务回滚）；
- 不写入任何默认个人数据、不依赖固定磁盘路径。

本执行器只负责**新增线索/互动相关表**（CREATE TABLE IF NOT EXISTS + CREATE INDEX）。
旧库的 tasks/videos/accounts/comments 升级仍由 ``src/db.py`` 既有逻辑负责；
这里通过版本记录与旧逻辑正交运行，互不干扰。
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Dict, List, Optional, Tuple

Migration = Tuple[str, Callable[[sqlite3.Connection], None]]

# 已注册版本列表。新增迁移只需在 versions 包内添加模块并在此注册。
MIGRATIONS: List[Migration] = []


def register(version: str):
    """装饰器：把迁移函数注册进全局版本表。每个函数只接收 conn。"""

    def deco(fn: Callable[[sqlite3.Connection], None]) -> Callable[[sqlite3.Connection], None]:
        MIGRATIONS.append((version, fn))
        return fn

    return deco


_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
)
"""


class MigrationRunner:
    """按版本顺序执行未应用过的迁移。

    - ``run(conn)`` 逐版本开启独立事务：失败则回滚该版本，旧数据不受影响。
    - 未应用版本按注册顺序依次应用；已应用版本跳过。
    """

    def __init__(self, migrations: Optional[List[Migration]] = None):
        self._migrations: List[Migration] = migrations if migrations is not None else list(MIGRATIONS)
        self._versions = [v for v, _ in self._migrations]
        if len(self._versions) != len(set(self._versions)):
            raise ValueError("迁移版本号重复")

    # ------------------------------------------------------------------
    def _ensure_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(_MIGRATION_TABLE)

    def _applied(self, conn: sqlite3.Connection) -> set:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        return {r[0] for r in rows}

    # ------------------------------------------------------------------
    def pending(self, conn: sqlite3.Connection) -> List[str]:
        """返回尚未应用的版本号（按注册顺序）。"""
        self._ensure_table(conn)
        applied = self._applied(conn)
        return [v for v in self._versions if v not in applied]

    def run(self, conn: sqlite3.Connection) -> List[str]:
        """应用所有未执行迁移，返回本次应用的版本号列表。"""
        self._ensure_table(conn)
        applied = self._applied(conn)
        applied_now: List[str] = []
        for version, fn in self._migrations:
            if version in applied:
                continue
            try:
                conn.execute("BEGIN")
                fn(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations (version) VALUES (?)",
                    (version,),
                )
                conn.commit()
                applied_now.append(version)
            except Exception:
                conn.rollback()
                raise
        return applied_now

    def is_current(self, conn: sqlite3.Connection) -> bool:
        """是否已应用到最新版本。"""
        return len(self.pending(conn)) == 0
