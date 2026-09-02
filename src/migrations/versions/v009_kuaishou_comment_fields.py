# -*- coding: utf-8 -*-
"""迁移 v009：修复快手 DOM 兜底评论的字段串列。

部分旧版本把快手评论卡片的“昵称/时间”写进了昵称或正文列。迁移只处理
明确标记为 ``kuaishou_comment_dom`` 的历史行，不触碰快手接口评论和其他
平台数据。只有没有正文、且没有被互动操作引用的 DOM 元数据行才会清理；
这样不会误删已经进入互动流程的记录。
"""

from __future__ import annotations

import json

from ..runner import register

try:  # db.py 以顶层模块方式加载 src；测试也沿用该导入方式。
    from kuaishou_comment_fields import normalize_kuaishou_dom_fields
except ImportError:  # pragma: no cover - 包方式运行时的兼容路径
    from ...kuaishou_comment_fields import normalize_kuaishou_dom_fields


@register("009_kuaishou_comment_fields")
def upgrade_009(conn):
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info('comments')").fetchall()
    }
    if "extra" not in columns:
        return

    rows = conn.execute(
        "SELECT id, nickname, content, comment_time, extra FROM comments "
        "WHERE LOWER(platform) = 'kuaishou'"
    ).fetchall()
    for row in rows:
        try:
            extra = json.loads(row["extra"] or "{}")
        except (TypeError, ValueError):
            extra = {}
        if not isinstance(extra, dict) or extra.get("source") != "kuaishou_comment_dom":
            continue
        nickname, content, comment_time = normalize_kuaishou_dom_fields(
            row["nickname"], row["content"], row["comment_time"]
        )
        if not content:
            lead_rows = conn.execute(
                "SELECT id FROM leads WHERE source_comment_id = ?",
                (row["id"],),
            ).fetchall()
            can_remove = True
            for lead in lead_rows:
                lead_id = lead["id"]
                related = conn.execute(
                    "SELECT 1 FROM interaction_drafts WHERE lead_id = ? LIMIT 1",
                    (lead_id,),
                ).fetchone()
                related = related or conn.execute(
                    "SELECT 1 FROM interaction_events WHERE lead_id = ? LIMIT 1",
                    (lead_id,),
                ).fetchone()
                related = related or conn.execute(
                    "SELECT 1 FROM lead_assignments WHERE lead_id = ? LIMIT 1",
                    (lead_id,),
                ).fetchone()
                if related:
                    can_remove = False
                    break
            if can_remove:
                for lead in lead_rows:
                    lead_id = lead["id"]
                    conn.execute(
                        "DELETE FROM lead_audit_log WHERE lead_id = ?", (lead_id,)
                    )
                    conn.execute(
                        "DELETE FROM lead_evidence WHERE lead_id = ?", (lead_id,)
                    )
                    conn.execute(
                        "DELETE FROM leads WHERE id = ?", (lead_id,)
                    )
                conn.execute(
                    "UPDATE interaction_drafts SET source_comment_id = NULL "
                    "WHERE source_comment_id = ?", (row["id"],)
                )
                conn.execute("DELETE FROM comments WHERE id = ?", (row["id"],))
                continue
            # 已经被互动引用的异常行保留，但不再让 UI 展示昵称/时间混合串。
            content = "[非文字评论]"
        if (
            nickname != (row["nickname"] or "")
            or content != (row["content"] or "")
            or comment_time != (row["comment_time"] or "")
        ):
            conn.execute(
                "UPDATE comments SET nickname = ?, content = ?, comment_time = ? "
                "WHERE id = ?",
                (nickname or "匿名用户", content, comment_time, row["id"]),
            )
        conn.execute(
            "UPDATE lead_evidence SET evidence_text = ?, occurred_at = ? "
            "WHERE comment_id = ?",
            (content, comment_time, row["id"]),
        )
        conn.execute(
            "UPDATE leads SET nickname = ?, last_interaction_at = ? "
            "WHERE source_comment_id = ?",
            (nickname or "匿名用户", comment_time, row["id"]),
        )
