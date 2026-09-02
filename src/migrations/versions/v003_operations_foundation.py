# -*- coding: utf-8 -*-
"""迁移 v003：任务运行、增量监控、关键词组、安全和诊断基础。"""

from ..runner import register


def _add_column_if_missing(conn, table: str, name: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


@register("003_operations_foundation")
def upgrade_003(conn):
    """只新增表、字段和索引；可在旧库上重复执行。"""
    _add_column_if_missing(conn, "tasks", "execution_mode", "TEXT NOT NULL DEFAULT 'once'")
    _add_column_if_missing(conn, "tasks", "keyword_group_id", "INTEGER")
    _add_column_if_missing(conn, "tasks", "next_run_at", "TEXT")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS collection_runs (
          run_id TEXT PRIMARY KEY,
          task_id INTEGER NOT NULL,
          platform TEXT NOT NULL,
          account_id INTEGER,
          execution_mode TEXT NOT NULL DEFAULT 'manual',
          status TEXT NOT NULL DEFAULT 'running',
          started_at TEXT NOT NULL,
          finished_at TEXT,
          stop_reason TEXT,
          discovered_count INTEGER NOT NULL DEFAULT 0,
          new_count INTEGER NOT NULL DEFAULT 0,
          duplicate_count INTEGER NOT NULL DEFAULT 0,
          failed_count INTEGER NOT NULL DEFAULT 0,
          metadata TEXT NOT NULL DEFAULT '{}',
          FOREIGN KEY(task_id) REFERENCES tasks(id),
          FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS collection_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL,
          stage TEXT NOT NULL,
          event_type TEXT NOT NULL,
          message TEXT,
          payload TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          FOREIGN KEY(run_id) REFERENCES collection_runs(run_id)
        );

        CREATE TABLE IF NOT EXISTS monitoring_rules (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id INTEGER NOT NULL UNIQUE,
          enabled INTEGER NOT NULL DEFAULT 1,
          interval_seconds INTEGER NOT NULL DEFAULT 3600,
          work_start TEXT,
          work_end TEXT,
          next_run_at TEXT,
          last_run_at TEXT,
          last_run_id TEXT,
          no_more_observed INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES tasks(id),
          FOREIGN KEY(last_run_id) REFERENCES collection_runs(run_id)
        );

        CREATE TABLE IF NOT EXISTS keyword_groups (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          platform TEXT,
          version INTEGER NOT NULL DEFAULT 1,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS keyword_terms (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          group_id INTEGER NOT NULL,
          term_type TEXT NOT NULL,
          term TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(group_id) REFERENCES keyword_groups(id) ON DELETE CASCADE,
          UNIQUE(group_id, term_type, term)
        );

        CREATE TABLE IF NOT EXISTS account_limits (
          account_id INTEGER PRIMARY KEY,
          hourly_collect_limit INTEGER NOT NULL DEFAULT 0,
          daily_reply_limit INTEGER NOT NULL DEFAULT 0,
          min_delay_seconds INTEGER NOT NULL DEFAULT 0,
          max_delay_seconds INTEGER NOT NULL DEFAULT 0,
          work_start TEXT,
          work_end TEXT,
          consecutive_failure_limit INTEGER NOT NULL DEFAULT 3,
          enabled INTEGER NOT NULL DEFAULT 1,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS health_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          platform TEXT NOT NULL,
          account_id INTEGER,
          check_name TEXT NOT NULL,
          status TEXT NOT NULL,
          detail TEXT,
          run_id TEXT,
          metadata TEXT NOT NULL DEFAULT '{}',
          observed_at TEXT NOT NULL,
          FOREIGN KEY(account_id) REFERENCES accounts(id),
          FOREIGN KEY(run_id) REFERENCES collection_runs(run_id)
        );

        CREATE TABLE IF NOT EXISTS backup_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          path TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'manual',
          size_bytes INTEGER NOT NULL DEFAULT 0,
          sha256 TEXT NOT NULL,
          verified INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_collection_runs_task_started
          ON collection_runs(task_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_collection_events_run_created
          ON collection_events(run_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_monitoring_rules_next_run
          ON monitoring_rules(enabled, next_run_at);
        CREATE INDEX IF NOT EXISTS idx_keyword_terms_group_type
          ON keyword_terms(group_id, term_type);
        CREATE INDEX IF NOT EXISTS idx_health_events_platform_observed
          ON health_events(platform, observed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_backup_records_created
          ON backup_records(created_at DESC);
        """
    )
