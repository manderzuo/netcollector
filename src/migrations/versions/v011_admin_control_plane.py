# -*- coding: utf-8 -*-
"""迁移 v011：管理员设备登记与可控运维基础。"""

from ..runner import register


def _add_column_if_missing(conn, table: str, name: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


@register("011_admin_control_plane")
def upgrade_011(conn):
    """登记设备状态，便于管理员发现异常终端并撤销其登记。"""
    _add_column_if_missing(conn, "sync_state", "device_id", "TEXT NOT NULL DEFAULT ''")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS employee_devices (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          device_id TEXT NOT NULL,
          device_name TEXT NOT NULL DEFAULT '',
          client_version TEXT NOT NULL DEFAULT '2.0',
          status TEXT NOT NULL DEFAULT 'active',
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          last_ip TEXT NOT NULL DEFAULT '',
          UNIQUE(user_id, device_id),
          FOREIGN KEY(user_id) REFERENCES app_users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_employee_devices_user_seen
          ON employee_devices(user_id, last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_employee_devices_status
          ON employee_devices(status, last_seen_at DESC);
        """
    )

