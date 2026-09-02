# -*- coding: utf-8 -*-
"""审核回复的原评论定位数据。

回复不能只按昵称搜索。这里把线索、原评论、来源作品和采集账号拼成一个
不可变目标，浏览器适配器只接收这个目标，不直接猜测数据库关系。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Optional


class ReplyTargetError(ValueError):
    """原评论无法安全定位时抛出。"""


@dataclass(frozen=True)
class ReplyTarget:
    lead_id: int
    draft_id: Optional[int]
    platform: str
    account_id: Optional[int]
    account_name: Optional[str]
    account_status: Optional[str]
    bb_window_id: Optional[str]
    video_id: Optional[int]
    video_platform_id: Optional[str]
    video_url: Optional[str]
    local_comment_id: Optional[int]
    platform_comment_id: Optional[str]
    platform_user_id: Optional[str]
    nickname: Optional[str]
    content: Optional[str]
    comment_time: Optional[str]

    def as_dict(self) -> dict:
        return asdict(self)

    def validate_for_browser(self) -> None:
        missing = []
        if not self.platform:
            missing.append("平台")
        if not self.video_url:
            missing.append("来源作品地址")
        if not self.nickname and not self.platform_user_id:
            missing.append("用户标识")
        # 图片/表情评论可能没有可见正文，也可能因平台采集接口未返回 cid。
        # 这类评论仍可用“用户 + 评论时间 + 作品”定位；真正的唯一性由
        # 浏览器适配器在页面内二次确认，避免把同一用户的其它评论误当目标。
        if (not self.content and not self.platform_comment_id
                and not ((self.nickname or self.platform_user_id)
                         and self.comment_time)):
            missing.append("评论内容、平台评论 ID 或评论时间")
        if not self.bb_window_id:
            missing.append("绑定的浏览器窗口")
        if missing:
            raise ReplyTargetError("回复目标信息不完整: " + "、".join(missing))


def _parse_extra(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class ReplyTargetResolver:
    """从草稿/线索解析唯一的原评论定位目标。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def resolve_for_draft(self, draft_id: int,
                          account_id: Optional[int] = None) -> ReplyTarget:
        draft = self._conn.execute(
            "SELECT id, lead_id, source_comment_id, reply_account_id "
            "FROM interaction_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if draft is None:
            raise ReplyTargetError(f"草稿不存在: {draft_id}")
        selected_account = account_id if account_id is not None else draft["reply_account_id"]
        return self.resolve_for_lead(
            draft["lead_id"], draft_id=draft_id,
            source_comment_id=draft["source_comment_id"],
            account_id=selected_account,
        )

    def resolve_for_lead(self, lead_id: int, *, draft_id: Optional[int] = None,
                         source_comment_id: Optional[int] = None,
                         account_id: Optional[int] = None) -> ReplyTarget:
        lead = self._conn.execute(
            "SELECT id, platform, source_comment_id FROM leads WHERE id = ?",
            (lead_id,),
        ).fetchone()
        if lead is None:
            raise ReplyTargetError(f"线索不存在: {lead_id}")

        comment_id = source_comment_id or lead["source_comment_id"]
        if comment_id is None:
            row = self._conn.execute(
                """SELECT e.comment_id
                   FROM lead_evidence e
                   JOIN comments c ON c.id = e.comment_id
                   WHERE e.lead_id = ? AND e.comment_id IS NOT NULL
                   ORDER BY COALESCE(e.occurred_at, c.comment_time) DESC, e.id DESC
                   LIMIT 1""",
                (lead_id,),
            ).fetchone()
            comment_id = row["comment_id"] if row else None
        if comment_id is None:
            raise ReplyTargetError("线索没有可定位的原评论")

        row = self._conn.execute(
            """SELECT l.platform AS lead_platform,
                      c.id AS local_comment_id, c.platform AS comment_platform,
                      c.user_id AS platform_user_id, c.nickname, c.content,
                      c.comment_time, c.extra,
                      v.id AS video_id, v.platform AS video_platform,
                      v.vid AS video_platform_id, v.url AS video_url,
                      v.assigned_account
               FROM leads l
               LEFT JOIN comments c ON c.id = ?
               LEFT JOIN videos v ON v.id = c.video_id
               WHERE l.id = ?""",
            (comment_id, lead_id),
        ).fetchone()
        if row is None or row["local_comment_id"] is None:
            raise ReplyTargetError(f"原评论不存在: {comment_id}")

        platform = row["video_platform"] or row["comment_platform"] or row["lead_platform"]
        account = None
        if account_id is not None:
            account = self._conn.execute(
                "SELECT id, name, platform, status, bb_window_id FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            if account is None:
                raise ReplyTargetError(f"回复账号不存在: {account_id}")
            if account["platform"] != platform:
                raise ReplyTargetError("回复账号与原评论平台不一致")
        elif row["assigned_account"]:
            account = self._conn.execute(
                """SELECT id, name, platform, status, bb_window_id
                   FROM accounts WHERE platform = ? AND name = ?
                   ORDER BY id LIMIT 1""",
                (platform, row["assigned_account"]),
            ).fetchone()

        extra = _parse_extra(row["extra"])
        platform_comment_id = (
            extra.get("platform_comment_id") or extra.get("cid")
            or extra.get("comment_id")
        )
        return ReplyTarget(
            lead_id=lead_id,
            draft_id=draft_id,
            platform=platform or "unknown",
            account_id=account["id"] if account else None,
            account_name=account["name"] if account else row["assigned_account"],
            account_status=account["status"] if account else None,
            bb_window_id=account["bb_window_id"] if account else None,
            video_id=row["video_id"],
            video_platform_id=row["video_platform_id"],
            video_url=row["video_url"],
            local_comment_id=row["local_comment_id"],
            platform_comment_id=str(platform_comment_id) if platform_comment_id else None,
            platform_user_id=row["platform_user_id"],
            nickname=row["nickname"],
            content=row["content"],
            comment_time=row["comment_time"],
        )
