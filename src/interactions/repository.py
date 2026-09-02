# -*- coding: utf-8 -*-
"""互动仓库（技术方案 Agent A：数据访问层）。

覆盖 interaction_drafts / interaction_events / suppression_list 的存取。
所有查询参数化，禁止拼接用户输入。不承担业务判定（判定在 policy / service）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from leads.models import BatchResult, DraftStatus, is_valid_draft_transition
from .models import DraftDetail


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _to_json(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class InteractionRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # ------------------------------------------------------------------
    # interaction_events
    # ------------------------------------------------------------------
    def log_event(self, lead_id: int, event_type: str, draft_id=None,
                  account_id=None, idempotency_key=None,
                  result=None, detail=None) -> int:
        """写一条互动事件。idempotency_key 唯一（5.5）。

        detail 支持 str / dict / list，dict/list 自动 JSON 序列化。
        """
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO interaction_events
                 (lead_id, draft_id, account_id, idempotency_key, event_type,
                  result, detail, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (lead_id, draft_id, account_id, idempotency_key, event_type,
             result, _to_json(detail) if isinstance(detail, (dict, list)) else detail,
             _now_iso()),
        )
        self._conn.commit()
        if cur.rowcount:
            return cur.lastrowid
        # 同一个幂等键重复上报时返回原事件，不再向上抛 UNIQUE 异常。
        if idempotency_key is not None:
            row = self._conn.execute(
                "SELECT id FROM interaction_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row:
                return row["id"]
        raise RuntimeError("互动事件写入失败")

    def events_for(self, lead_id: int) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM interaction_events WHERE lead_id = ? ORDER BY created_at, id",
            (lead_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # interaction_drafts
    # ------------------------------------------------------------------
    def create_draft(self, lead_id: int, channel: str, content: str,
                     generation_mode: str = "manual", template_id=None,
                     policy_snapshot: Optional[dict] = None,
                     status: str = DraftStatus.DRAFT, target: Optional[dict] = None,
                     reply_account_id: Optional[int] = None,
                     interaction_type: str = "comment_reply") -> int:
        interaction_type = str(interaction_type or "comment_reply").strip().lower()
        if interaction_type not in {"comment_reply", "private_message"}:
            raise ValueError(f"不支持的互动类型: {interaction_type}")
        now = _now_iso()
        target_data = target.as_dict() if hasattr(target, "as_dict") else (target or {})
        source_comment_id = target_data.get("local_comment_id")
        source_video_id = target_data.get("video_id")
        if reply_account_id is None:
            reply_account_id = target_data.get("account_id")
        cur = self._conn.execute(
            """INSERT INTO interaction_drafts
                 (lead_id, channel, interaction_type, template_id, content, generation_mode,
                  policy_snapshot, status, created_at, updated_at,
                  source_comment_id, source_video_id, reply_account_id,
                  platform_comment_id, source_video_url, source_nickname,
                  source_content)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (lead_id, channel, interaction_type, template_id, content, generation_mode,
             _to_json(policy_snapshot or {}), status, now, now,
             source_comment_id, source_video_id, reply_account_id,
             target_data.get("platform_comment_id"), target_data.get("video_url"),
             target_data.get("nickname"), target_data.get("content")),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_draft(self, draft_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM interaction_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_draft_status(self, draft_id: int, status: str,
                            reviewed_by=None, reviewed_at=None) -> None:
        draft = self.get_draft(draft_id)
        if draft is None:
            raise ValueError(f"草稿不存在: {draft_id}")
        current = draft.get("status") or DraftStatus.DRAFT
        if not is_valid_draft_transition(current, status):
            raise ValueError(f"非法草稿状态变更: {current} -> {status}")
        fields = ["status = ?", "updated_at = ?"]
        params: list = [status, _now_iso()]
        if reviewed_by is not None:
            fields.append("reviewed_by = ?")
            params.append(reviewed_by)
        if reviewed_at is not None:
            fields.append("reviewed_at = ?")
            params.append(reviewed_at)
        params.append(draft_id)
        self._conn.execute(
            f"UPDATE interaction_drafts SET {', '.join(fields)} WHERE id = ?", params
        )
        self._conn.commit()

    def cancel_open_drafts(self, lead_id: int) -> int:
        """将该线索尚未发送的草稿全部取消，返回取消数量。"""
        rows = self._conn.execute(
            "SELECT id FROM interaction_drafts "
            "WHERE lead_id = ? AND status IN (?,?,?,?)",
            (lead_id, DraftStatus.DRAFT, DraftStatus.PENDING_REVIEW,
             DraftStatus.APPROVED, DraftStatus.QUEUED),
        ).fetchall()
        count = 0
        for row in rows:
            self.update_draft_status(row["id"], DraftStatus.CANCELLED)
            count += 1
        return count

    def update_draft_content(self, draft_id: int, content: str) -> None:
        self._conn.execute(
            "UPDATE interaction_drafts SET content = ?, updated_at = ? WHERE id = ?",
            (content, _now_iso(), draft_id),
        )
        self._conn.commit()

    def assign_reply_account(self, draft_id: int, account_id: int) -> None:
        self._conn.execute(
            "UPDATE interaction_drafts SET reply_account_id = ?, updated_at = ? WHERE id = ?",
            (account_id, _now_iso(), draft_id),
        )
        self._conn.commit()

    def reset_draft_for_generation(self, draft_id: int) -> None:
        """清除失败发送上下文，准备重新编辑和生成。"""
        self._conn.execute(
            """UPDATE interaction_drafts
               SET reply_account_id = NULL, reviewed_by = NULL,
                   reviewed_at = NULL, updated_at = ?
             WHERE id = ?""",
            (_now_iso(), draft_id),
        )
        self._conn.commit()

    def list_reply_accounts(self, include_unavailable: bool = False,
                            owner_user_id: Optional[int] = None) -> List[dict]:
        """返回互动中心账号；筛选历史回复时可包含已停用账号。"""
        sql = "SELECT id, name, bb_window_id, platform, status FROM accounts"
        conditions = []
        params: list[Any] = []
        if not include_unavailable:
            conditions.append("status NOT IN ('dead', 'frozen', 'waiting_human')")
        if owner_user_id is not None:
            conditions.append("owner_user_id = ?")
            params.append(int(owner_user_id))
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY platform, name, id"
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_drafts(self, status: Optional[str] = None, lead_id: Optional[int] = None,
                    lead_status: Optional[str] = None,
                    page: int = 1, page_size: int = 50, *,
                    statuses: Optional[List[str]] = None,
                    platform: Optional[str] = None,
                    account_id: Optional[int] = None,
                    interaction_type: Optional[str] = None,
                    data_owner_user_id: Optional[int] = None) -> Dict[str, Any]:
        """按状态/线索分页列出草稿，返回 {items, total, page, page_size}。"""
        where = []
        params: list = []
        if status:
            where.append("d.status = ?")
            params.append(status)
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            where.append(f"d.status IN ({placeholders})")
            params.extend(statuses)
        if lead_id is not None:
            where.append("d.lead_id = ?")
            params.append(lead_id)
        if lead_status:
            where.append("l.status = ?")
            params.append(lead_status)
        if platform:
            where.append("l.platform = ?")
            params.append(platform)
        if data_owner_user_id is not None:
            where.append("l.data_owner_user_id = ?")
            params.append(int(data_owner_user_id))
        if account_id is not None:
            where.append("d.reply_account_id = ?")
            params.append(account_id)
        if interaction_type:
            interaction_type = str(interaction_type).strip().lower()
            if interaction_type not in {"comment_reply", "private_message"}:
                raise ValueError(f"不支持的互动类型: {interaction_type}")
            where.append("COALESCE(NULLIF(d.interaction_type, ''), 'comment_reply') = ?")
            params.append(interaction_type)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        page = max(1, page)
        page_size = min(max(1, page_size), 500)

        total = self._conn.execute(
            f"SELECT COUNT(*) c FROM interaction_drafts d "
            f"LEFT JOIN leads l ON l.id = d.lead_id{where_sql}", params
        ).fetchone()["c"]

        rows = self._conn.execute(
            f"""SELECT d.*, l.nickname AS lead_nickname, l.platform AS lead_platform,
                       l.platform_user_id, l.profile_url,
                       l.region_province, l.region_source, l.region_confidence,
                       l.last_interaction_at, l.intent_level, l.pool,
                       l.status AS lead_status,
                       a.name AS reply_account_name,
                       a.platform AS reply_account_platform,
                       COALESCE(
                           NULLIF(d.source_content, ''),
                           c.content,
                           (SELECT e.evidence_text
                              FROM lead_evidence e
                             WHERE e.lead_id = d.lead_id
                               AND TRIM(COALESCE(e.evidence_text, '')) <> ''
                             ORDER BY COALESCE(e.occurred_at, e.collected_at) DESC,
                                      e.id DESC LIMIT 1),
                           ''
                       ) AS original_comment,
                       (SELECT ie.detail FROM interaction_events ie
                         WHERE ie.draft_id = d.id AND ie.event_type = 'send_failed'
                         ORDER BY ie.created_at DESC, ie.id DESC LIMIT 1) AS failure_reason
                       ,(SELECT COALESCE(e.task_id, v.task_id)
                           FROM lead_evidence e
                           LEFT JOIN comments cc ON cc.id = e.comment_id
                           LEFT JOIN videos v ON v.id = COALESCE(e.video_id, cc.video_id)
                          WHERE e.lead_id = d.lead_id
                          ORDER BY COALESCE(e.occurred_at, e.collected_at) DESC, e.id DESC
                          LIMIT 1) AS task_id
                       ,CASE WHEN EXISTS (
                           SELECT 1 FROM interaction_events ce
                            WHERE ce.draft_id = d.id
                              AND ce.event_type IN ('customer_reply', 'customer_replied')
                       ) OR EXISTS (
                           SELECT 1 FROM published_messages pm
                            WHERE pm.reply_draft_id = d.id
                              AND pm.message_type IN ('reply', 'comment')
                       ) THEN 1 ELSE 0 END AS customer_replied
                FROM interaction_drafts d
                LEFT JOIN leads l ON l.id = d.lead_id
                LEFT JOIN accounts a ON a.id = d.reply_account_id
                LEFT JOIN comments c ON c.id = d.source_comment_id
                {where_sql}
                ORDER BY d.updated_at DESC, d.id DESC
                LIMIT ? OFFSET ?""",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            from leads.models import LeadStatus

            items.append(
                DraftDetail(
                    id=d["id"], lead_id=d["lead_id"], channel=d["channel"],
                    interaction_type=d.get("interaction_type") or "comment_reply",
                    template_id=d.get("template_id"), content=d.get("content") or "",
                    generation_mode=d.get("generation_mode") or "manual",
                    policy_snapshot=self._from_json(d.get("policy_snapshot")),
                    status=d.get("status") or DraftStatus.DRAFT,
                    reviewed_by=d.get("reviewed_by"), reviewed_at=d.get("reviewed_at"),
                    created_at=d.get("created_at"), updated_at=d.get("updated_at"),
                    reply_account_id=d.get("reply_account_id"),
                    reply_account_name=d.get("reply_account_name"),
                    reply_account_platform=d.get("reply_account_platform"),
                    source_content=d.get("original_comment") or d.get("source_content"),
                    failure_reason=d.get("failure_reason"),
                    task_id=d.get("task_id"),
                    customer_replied=bool(d.get("customer_replied")),
                    lead={
                        "nickname": d.get("lead_nickname"),
                        "platform": d.get("lead_platform"),
                        "platform_user_id": d.get("platform_user_id"),
                        "profile_url": d.get("profile_url"),
                        "region_province": d.get("region_province"),
                        "region_source": d.get("region_source"),
                        "region_confidence": d.get("region_confidence"),
                        "last_interaction_at": d.get("last_interaction_at"),
                        "intent_level": d.get("intent_level"),
                        "pool": d.get("pool"),
                        "status": d.get("lead_status"),
                    },
                )
            )
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    @staticmethod
    def _from_json(value):
        if not value:
            return {}
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # suppression_list
    # ------------------------------------------------------------------
    def add_suppression(self, platform: str, dedupe_key: str, reason: str,
                        platform_user_id=None) -> Optional[int]:
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO suppression_list
                 (platform, platform_user_id, dedupe_key, reason, created_at)
               VALUES (?,?,?,?,?)""",
            (platform, platform_user_id, dedupe_key, reason, _now_iso()),
        )
        self._conn.commit()
        return cur.lastrowid if cur.rowcount else None

    def is_suppressed(self, dedupe_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM suppression_list WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return row is not None

    def list_suppressions(self) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM suppression_list ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def remove_suppression(self, dedupe_key: str) -> None:
        self._conn.execute(
            "DELETE FROM suppression_list WHERE dedupe_key = ?", (dedupe_key,)
        )
        self._conn.commit()
