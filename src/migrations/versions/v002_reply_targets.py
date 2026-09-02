# -*- coding: utf-8 -*-
"""迁移 v002：把审核草稿绑定到具体原评论/作品/账号。"""

from ..runner import register


@register("002_reply_targets")
def upgrade_002(conn):
    """只新增字段和索引，兼容已存在的 v001 数据库。"""
    columns = {
        "source_comment_id": "INTEGER",
        "source_video_id": "INTEGER",
        "reply_account_id": "INTEGER",
        "platform_comment_id": "TEXT",
        "source_video_url": "TEXT",
        "source_nickname": "TEXT",
        "source_content": "TEXT",
    }
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info('interaction_drafts')").fetchall()
    }
    for name, sql_type in columns.items():
        if name not in existing:
            conn.execute(
                f"ALTER TABLE interaction_drafts ADD COLUMN {name} {sql_type}"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_interaction_drafts_source_comment "
        "ON interaction_drafts(source_comment_id)"
    )
