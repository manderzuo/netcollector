# -*- coding: utf-8 -*-
"""迁移 v007：消息中心全量同步字段。

旧版 ``published_messages`` 只够保存作品评论缓存。全量消息中心还需要
保留来源作品、消息入口、回复资格和读取批次，便于区分评论、点赞、关注、
私信及系统通知，也便于后续从可回复消息进入互动中心。
"""

from ..runner import register


@register("007_message_center_full")
def upgrade_007(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info('published_messages')").fetchall()}
    additions = {
        "source_url": "TEXT NOT NULL DEFAULT ''",
        "source_title": "TEXT NOT NULL DEFAULT ''",
        "message_url": "TEXT NOT NULL DEFAULT ''",
        "can_reply": "INTEGER NOT NULL DEFAULT 0",
        "reply_target_id": "TEXT NOT NULL DEFAULT ''",
        "fetched_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE published_messages ADD COLUMN {name} {definition}")
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_published_messages_type
          ON published_messages(account_id, platform, message_type, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_published_messages_fetched
          ON published_messages(fetched_at DESC);
        """
    )
