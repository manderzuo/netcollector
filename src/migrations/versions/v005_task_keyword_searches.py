# -*- coding: utf-8 -*-
"""迁移 v005：任务级预制关键词搜索快照。"""

from ..runner import register


@register("005_task_keyword_searches")
def upgrade_005(conn):
    """记录任务创建时的逐词搜索计划，并给作品保留来源关键词。"""
    columns = {row[1] for row in conn.execute("PRAGMA table_info('videos')").fetchall()}
    if "search_query" not in columns:
        conn.execute("ALTER TABLE videos ADD COLUMN search_query TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_search_queries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id INTEGER NOT NULL,
          query_order INTEGER NOT NULL,
          query TEXT NOT NULL,
          exclude_terms TEXT NOT NULL DEFAULT '[]',
          target_count INTEGER NOT NULL DEFAULT 100,
          status TEXT NOT NULL DEFAULT 'pending',
          discovered_count INTEGER NOT NULL DEFAULT 0,
          new_count INTEGER NOT NULL DEFAULT 0,
          duplicate_count INTEGER NOT NULL DEFAULT 0,
          last_run_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
          UNIQUE(task_id, query_order),
          UNIQUE(task_id, query)
        );
        CREATE INDEX IF NOT EXISTS idx_task_search_queries_task_status
          ON task_search_queries(task_id, status, query_order);
        CREATE INDEX IF NOT EXISTS idx_videos_task_search_query
          ON videos(task_id, search_query);
        """
    )
