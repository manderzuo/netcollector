# -*- coding: utf-8 -*-
"""database.py — SQLite 数据层（新架构）。

迁移自旧 db.py 的表结构与访问函数，采用 Repository 模式：
- Database 类封装连接管理与事务
- TaskRepository / VideoRepository / AccountRepository / CommentRepository 分区
- 依赖单向：本模块不依赖任何业务模块

约定：
- 时间为本地 ISO 8601 字符串，字典序即时间先后序
- 写操作立即 commit
- UNIQUE 冲突一律 INSERT OR IGNORE 幂等处理
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema（迁移自旧 db.py，字段只增不删）
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  keyword TEXT NOT NULL,
  platform TEXT NOT NULL DEFAULT 'douyin',
  status TEXT NOT NULL DEFAULT 'pending',
  batch_size INTEGER NOT NULL DEFAULT 10,
  cooldown_seconds INTEGER NOT NULL DEFAULT 75,
  collect_mode TEXT NOT NULL DEFAULT 'standard',
  search_sort TEXT NOT NULL DEFAULT 'default',
  target_count INTEGER NOT NULL DEFAULT 100,
  only_with_comments INTEGER NOT NULL DEFAULT 0,
  collect_types TEXT NOT NULL DEFAULT '["video_info","author_info","engagement","comments","comment_user","region","intent"]',
  task_accounts TEXT NOT NULL DEFAULT '[]',
  output_dir TEXT,
  error_message TEXT,
  search_exhausted INTEGER NOT NULL DEFAULT 0,
  search_phase_complete INTEGER NOT NULL DEFAULT 0,
  search_stop_reason TEXT,
  search_stopped_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS videos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  platform TEXT NOT NULL DEFAULT 'douyin',
  vid TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT,
  author TEXT,
  extra TEXT,
  search_query TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  assigned_account TEXT,
  collected_at TEXT,
  FOREIGN KEY (task_id) REFERENCES tasks(id),
  UNIQUE (task_id, platform, vid)
);
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  bb_window_id TEXT,
  platform TEXT NOT NULL DEFAULT 'douyin',
  status TEXT NOT NULL DEFAULT 'idle',
  processed_count INTEGER NOT NULL DEFAULT 0,
  batch_count INTEGER NOT NULL DEFAULT 0,
  cd_until TEXT,
  wait_reason TEXT,
  wait_since TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id INTEGER NOT NULL,
  platform TEXT NOT NULL DEFAULT 'douyin',
  user_id TEXT,
  nickname TEXT,
  content TEXT,
  comment_time TEXT,
  extra TEXT,
  intent_score INTEGER DEFAULT 0,
  intent_label TEXT,
  reply_suggestion TEXT,
  UNIQUE (video_id, user_id, content),
  FOREIGN KEY (video_id) REFERENCES videos(id)
);
CREATE TABLE IF NOT EXISTS human_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER,
  action TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

# 迁移补列（幂等 ALTER TABLE）
_MIGRATION_COLUMNS = {
    "tasks": {
        "search_exhausted": "INTEGER NOT NULL DEFAULT 0",
        "search_phase_complete": "INTEGER NOT NULL DEFAULT 0",
        "search_stop_reason": "TEXT",
        "search_stopped_at": "TEXT",
        "updated_at": "TEXT",
    },
    "accounts": {
        "bb_window_id": "TEXT",
        "platform": "TEXT NOT NULL DEFAULT 'douyin'",
        "status": "TEXT NOT NULL DEFAULT 'idle'",
        "processed_count": "INTEGER NOT NULL DEFAULT 0",
        "batch_count": "INTEGER NOT NULL DEFAULT 0",
        "cd_until": "TEXT",
        "wait_reason": "TEXT",
        "wait_since": "TEXT",
    },
    "comments": {
        "intent_score": "INTEGER DEFAULT 0",
        "intent_label": "TEXT",
        "reply_suggestion": "TEXT",
    },
}


class Database:
    """SQLite 数据库封装。"""

    def __init__(self, db_path: str, check_same_thread: bool = True):
        self.db_path = db_path
        self.check_same_thread = check_same_thread

    def connect(self) -> sqlite3.Connection:
        """建立连接并初始化（幂等）。"""
        if self.db_path != ":memory:":
            parent = os.path.dirname(self.db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=self.check_same_thread)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema(conn)
        return conn

    def _init_schema(self, conn: sqlite3.Connection):
        conn.executescript(_SCHEMA_SQL)
        self._apply_migrations(conn)
        conn.commit()

    def _apply_migrations(self, conn: sqlite3.Connection):
        """幂等补列。"""
        for table, columns in _MIGRATION_COLUMNS.items():
            existing = {row["name"] for row in conn.execute(
                f"PRAGMA table_info({table})")}
            for col, ddl in columns.items():
                if col not in existing:
                    try:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                    except sqlite3.OperationalError:
                        pass


# ---------------------------------------------------------------------------
# Repository 层
# ---------------------------------------------------------------------------
class TaskRepository:
    """任务表访问。"""

    def __init__(self, db: Database):
        self._db = db

    def _conn(self) -> sqlite3.Connection:
        return self._db.connect()

    def create(self, keyword: str, platform: str = "douyin",
               batch_size: int = 10, cooldown_seconds: int = 75,
               collect_mode: str = "standard", target_count: int = 100,
               collect_types: Optional[list] = None,
               task_accounts: Optional[list] = None,
               only_with_comments: int = 0, output_dir: str = "",
               search_sort: str = "default") -> int:
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO tasks
               (keyword, platform, batch_size, cooldown_seconds, collect_mode,
                target_count, collect_types, task_accounts, only_with_comments,
                output_dir, search_sort)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (keyword, platform, batch_size, cooldown_seconds, collect_mode,
             target_count, json.dumps(collect_types or [], ensure_ascii=False),
             json.dumps(task_accounts or [], ensure_ascii=False),
             only_with_comments, output_dir, search_sort),
        )
        conn.commit()
        return cur.lastrowid

    def update_status(self, task_id: int, status: str, error_message: str = ""):
        conn = self._conn()
        conn.execute(
            "UPDATE tasks SET status=?, error_message=?, updated_at=? WHERE id=?",
            (status, error_message, _now_iso(), task_id),
        )
        conn.commit()

    def get(self, task_id: int) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_all(self) -> List[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def mark_search_complete(self, task_id: int, exhausted: bool = True,
                             reason: str = ""):
        conn = self._conn()
        conn.execute(
            "UPDATE tasks SET search_exhausted=?, search_stop_reason=?, "
            "search_stopped_at=?, updated_at=? WHERE id=?",
            (1 if exhausted else 0, reason, _now_iso(), _now_iso(), task_id),
        )
        conn.commit()


class VideoRepository:
    """作品表访问。"""

    def __init__(self, db: Database):
        self._db = db

    def _conn(self) -> sqlite3.Connection:
        return self._db.connect()

    def insert(self, task_id: int, vid: str, url: str, title: str = "",
               author: str = "", extra: Optional[dict] = None,
               platform: str = "douyin", search_query: str = "") -> int:
        conn = self._conn()
        cur = conn.execute(
            """INSERT OR IGNORE INTO videos
               (task_id, platform, vid, url, title, author, extra, search_query)
               VALUES (?,?,?,?,?,?,?,?)""",
            (task_id, platform, vid, url, title, author,
             json.dumps(extra or {}, ensure_ascii=False), search_query),
        )
        conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        # 已存在则返回既有 id
        row = conn.execute(
            "SELECT id FROM videos WHERE task_id=? AND vid=? AND platform=?",
            (task_id, vid, platform),
        ).fetchone()
        return row["id"] if row else 0

    def mark_assigned(self, task_id: int, account: str, vids: List[str]):
        conn = self._conn()
        for vid in vids:
            conn.execute(
                "UPDATE videos SET status='assigned', assigned_account=? "
                "WHERE task_id=? AND vid=?",
                (account, task_id, vid),
            )
        conn.commit()

    def get_by_status(self, task_id: int, status: str) -> List[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM videos WHERE task_id=? AND status=?",
            (task_id, status),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_done(self, video_id: int, collected_at: str = ""):
        conn = self._conn()
        conn.execute(
            "UPDATE videos SET status='done', collected_at=? WHERE id=?",
            (collected_at or _now_iso(), video_id),
        )
        conn.commit()


class AccountRepository:
    """账号表访问。"""

    def __init__(self, db: Database):
        self._db = db

    def _conn(self) -> sqlite3.Connection:
        return self._db.connect()

    def upsert(self, name: str, bb_window_id: str = "",
               platform: str = "douyin") -> int:
        conn = self._conn()
        row = conn.execute(
            "SELECT id FROM accounts WHERE bb_window_id=?",
            (bb_window_id,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM accounts WHERE name=? AND platform=?",
                (name, platform),
            ).fetchone()
        if row:
            conn.execute(
                "UPDATE accounts SET name=?, platform=? WHERE id=?",
                (name, platform, row["id"]),
            )
            conn.commit()
            return row["id"]
        cur = conn.execute(
            "INSERT INTO accounts (name, bb_window_id, platform) VALUES (?,?,?)",
            (name, bb_window_id, platform),
        )
        conn.commit()
        return cur.lastrowid

    def update_status(self, account_id: int, status: str,
                      wait_reason: str = "", wait_since: str = ""):
        conn = self._conn()
        conn.execute(
            "UPDATE accounts SET status=?, wait_reason=?, wait_since=? WHERE id=?",
            (status, wait_reason, wait_since or _now_iso(), account_id),
        )
        conn.commit()

    def begin_cooldown(self, account_id: int, seconds: int):
        conn = self._conn()
        cd_until = _now_iso(offset_seconds=seconds)
        conn.execute(
            "UPDATE accounts SET status='cooldown', cd_until=? WHERE id=?",
            (cd_until, account_id),
        )
        conn.commit()

    def reset_batch(self, account_id: int):
        conn = self._conn()
        conn.execute(
            "UPDATE accounts SET batch_count=0 WHERE id=?", (account_id,))
        conn.commit()

    def list_by_platform(self, platform: str) -> List[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM accounts WHERE platform=?", (platform,)).fetchall()
        return [dict(r) for r in rows]


class CommentRepository:
    """评论表访问。"""

    def __init__(self, db: Database):
        self._db = db

    def _conn(self) -> sqlite3.Connection:
        return self._db.connect()

    def insert(self, video_id: int, user_id: str, nickname: str, content: str,
               comment_time: str = "", extra: Optional[dict] = None,
               platform: str = "douyin", intent_score: int = 0,
               intent_label: str = "", reply_suggestion: str = ""):
        conn = self._conn()
        conn.execute(
            """INSERT OR IGNORE INTO comments
               (video_id, platform, user_id, nickname, content, comment_time,
                extra, intent_score, intent_label, reply_suggestion)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (video_id, platform, user_id, nickname, content, comment_time,
             json.dumps(extra or {}, ensure_ascii=False),
             intent_score, intent_label, reply_suggestion),
        )
        conn.commit()

    def list_by_video(self, video_id: int) -> List[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM comments WHERE video_id=?", (video_id,)).fetchall()
        return [dict(r) for r in rows]


def _now_iso(offset_seconds: int = 0) -> str:
    """返回本地 ISO 时间（可带偏移）。"""
    dt = datetime.now()
    if offset_seconds:
        from datetime import timedelta
        dt += timedelta(seconds=offset_seconds)
    return dt.isoformat(timespec="seconds")
