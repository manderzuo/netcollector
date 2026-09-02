# -*- coding: utf-8 -*-
"""发布工作区的数据服务。

这里保存的是账号作品缓存、内容生成候选和已发布作品的消息缓存。它不执行
浏览器点击，浏览器预览仍由现有 PublishingBrowserAdapter 负责。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Mapping

from .service import PLATFORM_LABELS, PLATFORMS
from .message_reader import MESSAGE_TYPE_LABELS


# 账号信息页读取的是“账号自己的作品”，它的支持范围可以先于发布中心
# 扩展。快手已经有账号主页只读适配器，但还没有接入发布编辑器，因此不能
# 直接把它加入 publishing.service.PLATFORMS，否则会让发布中心生成出暂
# 不可发布的快手版本。这里单独维护账号作品缓存的平台注册表。
ACCOUNT_CONTENT_PLATFORMS = (*PLATFORMS, "kuaishou")
ACCOUNT_CONTENT_PLATFORM_LABELS = {
    **PLATFORM_LABELS,
    "kuaishou": "快手",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return fallback
    return parsed


class PublishingWorkspaceService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @staticmethod
    def _platform(value: Any) -> str:
        platform = str(value or "").strip().lower()
        if platform and platform not in PLATFORMS:
            raise ValueError("发布平台无效")
        return platform

    @staticmethod
    def _account_content_platform(value: Any) -> str:
        platform = str(value or "").strip().lower()
        if platform and platform not in ACCOUNT_CONTENT_PLATFORMS:
            raise ValueError("账号作品平台无效")
        return platform

    def _account(self, account_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT id, name, platform, bb_window_id FROM accounts WHERE id = ?",
            (int(account_id),),
        ).fetchone()
        if row is None:
            raise ValueError("账号不存在")
        return row

    def seed_account_contents(self, account_id: int, platform: str,
                              limit: int = 100) -> int:
        """同步能够确认属于该账号的作品到账号内容缓存。

        ``assigned_account`` 只表示“被哪个账号分配去采集”，不能代表作品
        作者。这里必须只接受作品作者与账号名一致的记录，避免把采集任务
        的结果错误展示成账号自己的作品。后续平台主页只读适配器也写入同
        一张缓存表，界面不需要改变。
        """
        account = self._account(account_id)
        platform = self._account_content_platform(platform) or str(account["platform"] or "")
        if platform not in ACCOUNT_CONTENT_PLATFORMS:
            raise ValueError("账号平台无效")
        name = str(account["name"] or "")
        rows = self.conn.execute(
            "SELECT v.vid, v.url, v.title, v.author, v.extra, v.collected_at, "
            "(SELECT COUNT(*) FROM comments c WHERE c.video_id = v.id) AS comment_count "
            "FROM videos v WHERE v.platform = ? AND v.author = ? "
            "ORDER BY v.collected_at DESC, v.id DESC LIMIT ?",
            (platform, name, max(1, min(500, int(limit or 100)))),
        ).fetchall()
        now = _now()
        count = 0
        with self.conn:
            # 账号内容缓存必须可重建。清掉此前可能由旧版本按
            # ``assigned_account`` 错误写入的记录，避免错误作品继续残留在页面。
            self.conn.execute(
                "DELETE FROM account_contents WHERE account_id = ? AND platform = ?",
                (int(account_id), platform),
            )
            for row in rows:
                extra = _json(row["extra"], {})
                if not isinstance(extra, Mapping):
                    extra = {}
                content_type = str(extra.get("content_type") or "video").lower()
                if content_type not in {"video", "image", "note", "article"}:
                    content_type = "video"
                extra["__account_content_origin"] = "author_verified_collected"
                self.conn.execute(
                    "INSERT INTO account_contents "
                    "(account_id, platform, content_id, content_type, url, title, "
                    "description, published_at, comment_count, extra, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(account_id, platform, content_id) DO UPDATE SET "
                    "url=excluded.url, title=excluded.title, description=excluded.description, "
                    "published_at=excluded.published_at, comment_count=excluded.comment_count, "
                    "extra=excluded.extra, fetched_at=excluded.fetched_at",
                    (account_id, platform, str(row["vid"] or ""), content_type,
                     str(row["url"] or ""), str(row["title"] or ""),
                     str(extra.get("description") or row["title"] or ""),
                     str(row["collected_at"] or "") or None,
                     int(row["comment_count"] or 0),
                     json.dumps(dict(extra), ensure_ascii=False), now),
                )
                count += 1
        return count

    def replace_profile_contents(self, account_id: int, platform: str,
                                 items: list[Mapping[str, Any]],
                                 *, profile_url: str = "") -> int:
        """用一次主页只读同步结果重建账号作品缓存。

        只有读取成功且至少拿到一条作品时才由后端调用此方法。这样网络、
        登录态或页面加载失败时不会把上一次可用的账号作品缓存误删掉。
        """
        account = self._account(account_id)
        platform = self._account_content_platform(platform) or str(account["platform"] or "")
        if platform not in ACCOUNT_CONTENT_PLATFORMS:
            raise ValueError("账号平台无效")
        now = _now()
        count = 0
        with self.conn:
            self.conn.execute(
                "DELETE FROM account_contents WHERE account_id = ? AND platform = ?",
                (int(account_id), platform),
            )
            for raw in items or []:
                if not isinstance(raw, Mapping):
                    continue
                content_id = str(raw.get("content_id") or "").strip()
                url = str(raw.get("url") or "").strip()
                if not content_id or not url:
                    continue
                content_type = str(raw.get("content_type") or "video").strip().lower()
                if content_type not in {"video", "image", "note", "article"}:
                    content_type = "video"
                extra = raw.get("extra")
                extra = dict(extra) if isinstance(extra, Mapping) else {}
                extra["__account_content_origin"] = "profile_sync"
                if profile_url:
                    extra["profile_url"] = str(profile_url)
                self.conn.execute(
                    "INSERT INTO account_contents "
                    "(account_id, platform, content_id, content_type, url, title, "
                    "description, cover_url, published_at, like_count, comment_count, "
                    "share_count, favorite_count, extra, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(account_id, platform, content_id) DO UPDATE SET "
                    "content_type=excluded.content_type, url=excluded.url, "
                    "title=excluded.title, description=excluded.description, "
                    "cover_url=excluded.cover_url, published_at=excluded.published_at, "
                    "like_count=excluded.like_count, comment_count=excluded.comment_count, "
                    "share_count=excluded.share_count, favorite_count=excluded.favorite_count, "
                    "extra=excluded.extra, fetched_at=excluded.fetched_at",
                    (
                        int(account_id), platform, content_id, content_type, url,
                        str(raw.get("title") or "未命名内容"),
                        str(raw.get("description") or ""),
                        str(raw.get("cover_url") or ""),
                        str(raw.get("published_at") or "") or None,
                        max(0, int(raw.get("like_count") or 0)),
                        max(0, int(raw.get("comment_count") or 0)),
                        max(0, int(raw.get("share_count") or 0)),
                        max(0, int(raw.get("favorite_count") or 0)),
                        json.dumps(extra, ensure_ascii=False), now,
                    ),
                )
                count += 1
        return count

    def list_account_contents(self, account_id: int, platform: str = "",
                              content_type: str = "", page: int = 1,
                              page_size: int = 30) -> dict[str, Any]:
        account = self._account(account_id)
        platform = self._account_content_platform(platform) or str(account["platform"] or "")
        content_type = str(content_type or "").strip().lower()
        if content_type and content_type not in {"video", "image", "note", "article"}:
            raise ValueError("内容类型无效")
        page = max(1, int(page or 1))
        page_size = min(100, max(1, int(page_size or 30)))
        # 账号内容表本身也要支持未来的平台主页只读适配器写入。当前缓存
        # 只接受作品作者与账号名匹配的记录；未来主页适配器写入时使用
        # ``__account_content_origin=profile_sync``，不会被这里误过滤。
        where = [
            "ac.account_id = ?",
            "ac.platform = ?",
            "(EXISTS (SELECT 1 FROM videos v WHERE v.platform = ac.platform "
            "AND v.vid = ac.content_id AND v.author = "
            "(SELECT name FROM accounts WHERE id = ac.account_id)) "
            "OR instr(ac.extra, '\"__account_content_origin\":\"profile_sync\"') > 0 "
            "OR instr(ac.extra, '\"__account_content_origin\": \"profile_sync\"') > 0)",
        ]
        params: list[Any] = [int(account_id), platform]
        if content_type:
            where.append("content_type = ?")
            params.append(content_type)
        clause = " AND ".join(where)
        total = int(self.conn.execute(
            f"SELECT COUNT(*) FROM account_contents ac WHERE {clause}", params
        ).fetchone()[0])
        rows = self.conn.execute(
            "SELECT ac.id, ac.account_id, ac.platform, ac.content_id, ac.content_type, "
            "ac.url, ac.title, "
            "ac.description, ac.cover_url, ac.published_at, ac.like_count, ac.comment_count, "
            "ac.share_count, ac.favorite_count, ac.extra, ac.fetched_at "
            f"FROM account_contents ac "
            f"WHERE {clause} ORDER BY ac.published_at DESC, ac.id DESC "
            "LIMIT ? OFFSET ?", [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["platform_label"] = ACCOUNT_CONTENT_PLATFORM_LABELS.get(platform, platform)
            item["content_type_label"] = {
                "video": "视频", "image": "图文", "note": "笔记", "article": "文章",
            }.get(item["content_type"], "内容")
            item["extra"] = _json(item.get("extra"), {})
            items.append(item)
        return {
            "items": items, "total": total, "page": page, "page_size": page_size,
            "pages": (total + page_size - 1) // page_size if total else 0,
            "account_id": int(account_id), "platform": platform,
            "platform_label": ACCOUNT_CONTENT_PLATFORM_LABELS.get(platform, platform),
        }

    def list_content_comments(self, account_content_id: int,
                              page: int = 1, page_size: int = 50) -> dict[str, Any]:
        page = max(1, int(page or 1))
        page_size = min(100, max(1, int(page_size or 50)))
        total = int(self.conn.execute(
            "SELECT COUNT(*) FROM account_content_comments WHERE account_content_id = ?",
            (int(account_content_id),),
        ).fetchone()[0])
        if total == 0:
            # 采集库已有的评论直接作为只读回退来源，让账号信息页在首次同步
            # 后也能看到原文；新接入的账号内容适配器仍可写入专用缓存表。
            rows = self.conn.execute(
                "SELECT c.id, c.platform, c.user_id, c.nickname, c.content, "
                "c.comment_time, 0 AS like_count, 0 AS parent_id, 0 AS is_reply, "
                "c.extra FROM account_contents ac JOIN videos v "
                "ON v.platform = ac.platform AND v.vid = ac.content_id "
                "JOIN comments c ON c.video_id = v.id "
                "WHERE ac.id = ? ORDER BY c.comment_time DESC, c.id DESC LIMIT ? OFFSET ?",
                (int(account_content_id), page_size, (page - 1) * page_size),
            ).fetchall()
            total = int(self.conn.execute(
                "SELECT COUNT(*) FROM account_contents ac JOIN videos v "
                "ON v.platform = ac.platform AND v.vid = ac.content_id "
                "JOIN comments c ON c.video_id = v.id WHERE ac.id = ?",
                (int(account_content_id),),
            ).fetchone()[0])
            return {"items": [dict(row) for row in rows], "total": total,
                    "page": page, "page_size": page_size,
                    "pages": (total + page_size - 1) // page_size if total else 0}
        rows = self.conn.execute(
            "SELECT id, platform, user_id, nickname, content, comment_time, like_count, "
            "parent_id, is_reply, extra FROM account_content_comments "
            "WHERE account_content_id = ? ORDER BY comment_time DESC, id DESC LIMIT ? OFFSET ?",
            (int(account_content_id), page_size, (page - 1) * page_size),
        ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total,
                "page": page, "page_size": page_size,
                "pages": (total + page_size - 1) // page_size if total else 0}

    def list_generated(self, page: int = 1, page_size: int = 20,
                       owner_user_id: int | None = None) -> dict[str, Any]:
        page = max(1, int(page or 1))
        page_size = min(100, max(1, int(page_size or 20)))
        where = ""
        params: list[Any] = []
        if owner_user_id is not None:
            where = " WHERE owner_user_id = ?"
            params.append(int(owner_user_id))
        total = int(self.conn.execute(
            "SELECT COUNT(*) FROM generated_contents" + where, params
        ).fetchone()[0])
        rows = self.conn.execute(
            "SELECT id, title, body, source_type, source_ref, platforms, topics, outline, "
            "score_json, status, created_at, updated_at FROM generated_contents "
            + where + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["platforms"] = _json(item.get("platforms"), list(PLATFORMS))
            item["topics"] = _json(item.get("topics"), [])
            item["score"] = _json(item.pop("score_json", "{}"), {})
            items.append(item)
        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "pages": (total + page_size - 1) // page_size if total else 0}

    def save_generated(self, *, title: str, body: str, platform: str = "",
                       source_ref: str = "", source_type: str = "llm",
                       topics: list[str] | None = None, outline: str = "",
                       score: Mapping[str, Any] | None = None,
                       owner_user_id: int | None = None) -> dict[str, Any]:
        """保存一条结构化生成结果，供智能 API 和本地模板共用。"""
        title = str(title or "").strip()
        body = str(body or "").strip()
        if not title or not body:
            raise ValueError("生成内容的标题和正文不能为空")
        platform = self._platform(platform)
        platforms = [platform] if platform else list(PLATFORMS)
        now = _now()
        normalized_topics = [str(item).strip() for item in (topics or []) if str(item).strip()]
        normalized_score = dict(score) if isinstance(score, Mapping) else {}
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO generated_contents "
                "(title, body, source_type, source_ref, platforms, topics, outline, "
                "score_json, owner_user_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, body, str(source_type or "llm"), str(source_ref or ""),
                 json.dumps(platforms, ensure_ascii=False),
                 json.dumps(normalized_topics, ensure_ascii=False), str(outline or ""),
                 json.dumps(normalized_score, ensure_ascii=False), owner_user_id, now, now),
            )
            generated_id = int(cursor.lastrowid)
        return {
            "id": generated_id, "title": title, "body": body,
            "platforms": platforms, "topics": normalized_topics,
            "outline": str(outline or ""), "score": normalized_score,
            "source_type": str(source_type or "llm"),
            "source_ref": str(source_ref or ""),
        }

    def generate_local(self, keyword: str, platform: str = "",
                       source_ref: str = "") -> dict[str, Any]:
        keyword = str(keyword or "").strip() or "平台内容"
        platform = self._platform(platform)
        label = PLATFORM_LABELS.get(platform, "多平台") if platform else "多平台"
        title = f"{keyword}｜实用内容分享"
        body = (
            f"围绕“{keyword}”整理一份清晰、实用的分享。\n"
            f"从真实场景出发，提炼重点信息，并给出可以直接执行的建议。\n"
            f"欢迎在评论区交流你的经验和需求。"
        )
        return self.save_generated(
            title=title, body=body, platform=platform, source_ref=source_ref,
            source_type="local_template", topics=[keyword],
            outline=f"{label} · 问题切入 · 实用建议 · 评论互动",
            score={"可读性": 85, "实用性": 82},
        )

    def get_generated(self, generated_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT id, title, body, source_type, source_ref, platforms, topics, outline, "
            "score_json, status, created_at, updated_at FROM generated_contents WHERE id = ?",
            (int(generated_id),),
        ).fetchone()
        if row is None:
            raise ValueError("生成内容不存在")
        item = dict(row)
        item["platforms"] = _json(item.get("platforms"), list(PLATFORMS))
        item["topics"] = _json(item.get("topics"), [])
        item["score"] = _json(item.pop("score_json", "{}"), {})
        return item

    def upsert_messages(self, account_id: int, platform: str,
                        items: list[Mapping[str, Any]] | None) -> dict[str, int]:
        """写入一次账号级全量消息同步结果。

        消息中心以平台消息 ID 为幂等键；重新同步只更新来源和内容，不覆盖
        用户已经标记的已读状态，也不清空已有回复草稿。这样分页读取和后台
        重复同步都不会制造重复消息。
        """
        account = self._account(account_id)
        platform = self._platform(platform) or str(account["platform"] or "")
        if platform not in PLATFORMS:
            raise ValueError("账号平台无效")
        now = _now()
        inserted = updated = skipped = 0
        with self.conn:
            for raw in items or []:
                if not isinstance(raw, Mapping):
                    skipped += 1
                    continue
                message_id = str(raw.get("message_id") or "").strip()
                if not message_id:
                    skipped += 1
                    continue
                kind = str(raw.get("message_type") or "other").strip().lower()
                if kind not in MESSAGE_TYPE_LABELS:
                    kind = "other"
                extra = raw.get("extra")
                extra = dict(extra) if isinstance(extra, Mapping) else {}
                created_at = str(raw.get("created_at") or "").strip() or now
                fetched_at = str(extra.get("fetched_at") or now).strip() or now
                existed = self.conn.execute(
                    "SELECT 1 FROM published_messages WHERE account_id = ? "
                    "AND platform = ? AND message_id = ?",
                    (int(account_id), platform, message_id),
                ).fetchone() is not None
                self.conn.execute(
                    "INSERT INTO published_messages "
                    "(account_id, platform, content_id, message_id, message_type, "
                    "user_id, nickname, content, created_at, is_read, reply_draft_id, "
                    "extra, source_url, source_title, message_url, can_reply, "
                    "reply_target_id, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, "
                    "NULL, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(account_id, platform, message_id) DO UPDATE SET "
                    "content_id=excluded.content_id, message_type=excluded.message_type, "
                    "user_id=excluded.user_id, nickname=excluded.nickname, content=excluded.content, "
                    "created_at=excluded.created_at, extra=excluded.extra, "
                    "source_url=excluded.source_url, source_title=excluded.source_title, "
                    "message_url=excluded.message_url, can_reply=excluded.can_reply, "
                    "reply_target_id=excluded.reply_target_id, fetched_at=excluded.fetched_at",
                    (
                        int(account_id), platform,
                        str(raw.get("content_id") or ""), message_id, kind,
                        str(raw.get("user_id") or ""), str(raw.get("nickname") or "匿名用户"),
                        str(raw.get("content") or ""), created_at,
                        json.dumps(extra, ensure_ascii=False),
                        str(raw.get("source_url") or ""), str(raw.get("source_title") or ""),
                        str(raw.get("message_url") or ""),
                        1 if bool(raw.get("can_reply")) else 0,
                        str(raw.get("reply_target_id") or ""), fetched_at,
                    ),
                )
                if existed:
                    updated += 1
                else:
                    inserted += 1
        return {"inserted": inserted, "updated": updated,
                "processed": inserted + updated, "skipped": skipped}

    def list_messages(self, platform: str = "", account_id: int = 0,
                      unread_only: bool = False, page: int = 1,
                      page_size: int = 50, message_type: str = "",
                      owner_user_id: int | None = None) -> dict[str, Any]:
        platform = self._platform(platform)
        message_type = str(message_type or "").strip().lower()
        if message_type and message_type not in MESSAGE_TYPE_LABELS:
            raise ValueError("消息类型无效")
        page = max(1, int(page or 1))
        # 消息中心以会话为单位展示；必须一次拿到当前筛选范围内的全部消息，
        # 否则同一用户的历史消息会被分页拆散到多个会话中。
        page_size = min(5000, max(1, int(page_size or 50)))
        where = ["1=1"]
        params: list[Any] = []
        if platform:
            where.append("m.platform = ?")
            params.append(platform)
        if int(account_id or 0) > 0:
            where.append("m.account_id = ?")
            params.append(int(account_id))
        if owner_user_id is not None:
            where.append("a.owner_user_id = ?")
            params.append(int(owner_user_id))
        if message_type:
            where.append("m.message_type = ?")
            params.append(message_type)
        if unread_only:
            where.append("m.is_read = 0")
        clause = " AND ".join(where)
        total = int(self.conn.execute(
            f"SELECT COUNT(*) FROM published_messages m LEFT JOIN accounts a ON a.id = m.account_id WHERE {clause}", params
        ).fetchone()[0])
        unread_where = [item for item in where if item != "m.is_read = 0"]
        # ``m.is_read = 0`` 是字面量条件，没有对应绑定参数；不能从
        # params 中删除最后一项，否则带平台/账号筛选时会发生绑定错位。
        unread_params = list(params)
        unread_clause = " AND ".join(unread_where)
        unread = int(self.conn.execute(
            f"SELECT COUNT(*) FROM published_messages m LEFT JOIN accounts a ON a.id = m.account_id WHERE {unread_clause} AND m.is_read = 0",
            unread_params,
        ).fetchone()[0])
        rows = self.conn.execute(
            "SELECT m.id, m.account_id, m.platform, m.content_id, m.message_id, "
            "m.message_type, m.user_id, m.nickname, m.content, m.created_at, "
            "m.is_read, m.reply_draft_id, m.extra, m.source_url, m.source_title, "
            "m.message_url, m.can_reply, m.reply_target_id, m.fetched_at, "
            "a.name AS account_name "
            f"FROM published_messages m LEFT JOIN accounts a ON a.id = m.account_id "
            f"WHERE {clause} ORDER BY m.created_at DESC, m.id DESC LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["platform_label"] = PLATFORM_LABELS.get(item.get("platform"), item.get("platform"))
            item["message_type_label"] = MESSAGE_TYPE_LABELS.get(item.get("message_type"), "其他")
            item["extra"] = _json(item.get("extra"), {})
            item["quote_content"] = str(item["extra"].get("quote_content") or "") if isinstance(item["extra"], Mapping) else ""
            item["action"] = str(item["extra"].get("action") or "") if isinstance(item["extra"], Mapping) else ""
            item["event_time"] = str(item["extra"].get("event_time") or item.get("created_at") or "") if isinstance(item["extra"], Mapping) else str(item.get("created_at") or "")
            item["is_read"] = bool(item.get("is_read"))
            item["can_reply"] = bool(item.get("can_reply"))
            items.append(item)
        return {"items": items, "total": total, "unread": unread, "page": page,
                "page_size": page_size,
                "pages": (total + page_size - 1) // page_size if total else 0,
                "platform": platform, "account_id": int(account_id or 0),
                "message_type": message_type, "unread_only": bool(unread_only),
                "message_type_options": [
                    {"value": value, "label": label}
                    for value, label in MESSAGE_TYPE_LABELS.items()
                ]}

    def mark_message_read(self, message_id: int, read: bool = True) -> None:
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE published_messages SET is_read = ? WHERE id = ?",
                (1 if read else 0, int(message_id)),
            )
        if cursor.rowcount <= 0:
            raise ValueError("消息不存在")


__all__ = ["PublishingWorkspaceService"]
