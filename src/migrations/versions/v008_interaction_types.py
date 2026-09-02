# -*- coding: utf-8 -*-
"""迁移 v008：区分评论回复与私信互动。

历史草稿全部属于评论回复；新增字段只扩展结构，不改变已有数据和状态。
"""

from ..runner import register


@register("008_interaction_types")
def upgrade_008(conn):
    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info('interaction_drafts')"
        ).fetchall()
    }
    if "interaction_type" not in columns:
        conn.execute(
            "ALTER TABLE interaction_drafts ADD COLUMN interaction_type "
            "TEXT NOT NULL DEFAULT 'comment_reply'"
        )
    conn.execute(
        "UPDATE interaction_drafts SET interaction_type = 'comment_reply' "
        "WHERE interaction_type IS NULL OR TRIM(interaction_type) = ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_interaction_drafts_type_status "
        "ON interaction_drafts(interaction_type, status, updated_at)"
    )
