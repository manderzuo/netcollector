# -*- coding: utf-8 -*-
"""迁移 v010：员工数据归属与手动同步队列。

只增加可为空的归属字段和同步基础表。历史数据保持原样，归属为空的
历史数据只对管理员可见，不会被普通员工自动认领或上传。
"""

from ..runner import register


def _add_column_if_missing(conn, table: str, name: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _create_owner_index(conn, name: str, table: str, columns: tuple[str, ...]) -> None:
    """为新归属字段建索引，并兼容极简旧库缺少 updated_at 的情况。"""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
    usable = tuple(column for column in columns if column in existing)
    if not usable:
        return
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {name} ON {table}({', '.join(usable)})"
    )


@register("010_employee_data_scope")
def upgrade_010(conn):
    for table, column in (
        ("tasks", "owner_user_id"),
        ("accounts", "owner_user_id"),
        ("keyword_groups", "owner_user_id"),
        # leads.owner_id 已经是“负责人”业务字段，不能复用。
        ("leads", "data_owner_user_id"),
        ("generated_contents", "owner_user_id"),
        ("publish_drafts", "owner_user_id"),
    ):
        _add_column_if_missing(conn, table, column, "INTEGER")

    # 旧版极简库可能没有 tasks.updated_at；索引字段按现有列动态取交集，
    # 归属字段仍然一定存在，迁移不会因一个可选排序字段失败。
    for index_name, table, columns in (
        ("idx_tasks_owner", "tasks", ("owner_user_id", "updated_at")),
        ("idx_accounts_owner", "accounts", ("owner_user_id", "platform", "name")),
        ("idx_keyword_groups_owner", "keyword_groups", ("owner_user_id", "updated_at")),
        ("idx_leads_data_owner", "leads", ("data_owner_user_id", "updated_at")),
        ("idx_generated_contents_owner", "generated_contents", ("owner_user_id", "updated_at")),
        ("idx_publish_drafts_owner", "publish_drafts", ("owner_user_id", "updated_at")),
    ):
        _create_owner_index(conn, index_name, table, columns)

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sync_outbox (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          owner_user_id INTEGER NOT NULL,
          entity_type TEXT NOT NULL,
          entity_id INTEGER,
          operation TEXT NOT NULL DEFAULT 'upsert',
          payload TEXT NOT NULL DEFAULT '{}',
          client_event_id TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sync_outbox_owner_status
          ON sync_outbox(owner_user_id, status, id);

        CREATE TABLE IF NOT EXISTS sync_state (
          user_id INTEGER PRIMARY KEY,
          enabled INTEGER NOT NULL DEFAULT 0,
          server_url TEXT NOT NULL DEFAULT '',
          device_name TEXT NOT NULL DEFAULT '',
          last_cursor TEXT NOT NULL DEFAULT '',
          last_sync_at TEXT,
          status TEXT NOT NULL DEFAULT 'idle',
          last_error TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL
        );
        """
    )
    _add_column_if_missing(conn, "sync_state", "api_token", "TEXT NOT NULL DEFAULT ''")
