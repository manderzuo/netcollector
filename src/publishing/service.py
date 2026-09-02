# -*- coding: utf-8 -*-
"""发布中心的数据服务。

这里只负责草稿、平台版本和发布状态，不操作浏览器。浏览器执行由后续
平台适配器负责，保证发布中心可以先稳定运行并独立测试。
"""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from datetime import datetime
from typing import Any, Mapping

try:
    from ..time_utils import normalize_scheduled_at  # type: ignore
except ImportError:  # pragma: no cover - 直接以 src 为模块根目录时
    from time_utils import normalize_scheduled_at  # type: ignore


PLATFORMS = ("douyin", "xhs", "bilibili", "weibo")
PLATFORM_LABELS = {
    "douyin": "抖音",
    "xhs": "小红书",
    "bilibili": "B站",
    "weibo": "微博",
}
PUBLISH_STATUS_LABELS = {
    "draft": "草稿",
    "review": "待审核",
    "approved": "已批准",
    "queued": "待发布",
    "running": "发布中",
    "published": "已发布",
    "human_required": "待人工处理",
    "retryable_failed": "可重试失败",
    "failed": "失败",
    "cancelled": "已取消",
}

VIDEO_ASSET_SUFFIXES = frozenset({
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".mpeg", ".mpg",
})
IMAGE_ASSET_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif", ".heic", ".heif",
})


def _infer_asset_type(path: str, declared_type: str = "") -> str:
    """优先使用文件后缀，兼容旧数据中遗漏或错误的素材类型。"""
    suffix = os.path.splitext(str(path or "").strip())[1].lower()
    if suffix in VIDEO_ASSET_SUFFIXES:
        return "video"
    if suffix in IMAGE_ASSET_SUFFIXES:
        return "image"
    declared = str(declared_type or "").strip().lower()
    return declared if declared in {"image", "video"} else "image"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return fallback
    return parsed


class PublishingService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def list_drafts(self, *, status: str = "all", platform: str = "",
                    keyword: str = "", page: int = 1,
                    page_size: int = 50,
                    owner_user_id: int | None = None) -> dict[str, Any]:
        status = str(status or "all").strip().lower()
        platform = str(platform or "").strip().lower()
        keyword = str(keyword or "").strip()
        if status not in {"all", *PUBLISH_STATUS_LABELS}:
            raise ValueError("发布状态无效")
        if platform and platform not in PLATFORMS:
            raise ValueError("发布平台无效")
        page = max(1, int(page or 1))
        page_size = min(100, max(1, int(page_size or 50)))
        where = []
        params: list[Any] = []
        if status != "all":
            where.append("d.status = ?")
            params.append(status)
        if platform:
            where.append("EXISTS (SELECT 1 FROM publish_variants vf "
                         "WHERE vf.draft_id = d.id AND vf.platform = ?)")
            params.append(platform)
        if keyword:
            where.append("(d.title LIKE ? OR d.source_content LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        if owner_user_id is not None:
            where.append("d.owner_user_id = ?")
            params.append(int(owner_user_id))
        clause = " WHERE " + " AND ".join(where) if where else ""
        total = int(self.conn.execute(
            "SELECT COUNT(*) FROM publish_drafts d" + clause, params
        ).fetchone()[0])
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            "SELECT d.id, d.title, d.source_content, d.content_type, d.status, "
            "d.version, d.created_by, d.created_at, d.updated_at "
            "FROM publish_drafts d" + clause +
            " ORDER BY d.updated_at DESC, d.id DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            variants = self._variants(int(row["id"]))
            if platform:
                variants = [v for v in variants if v["platform"] == platform]
            item["status_label"] = PUBLISH_STATUS_LABELS.get(
                item["status"], item["status"]
            )
            item["platforms"] = [v["platform"] for v in variants]
            item["platform_labels"] = [
                PLATFORM_LABELS.get(v["platform"], v["platform"])
                for v in variants
            ]
            item["variants"] = variants
            item["asset_count"] = int(self.conn.execute(
                "SELECT COUNT(*) FROM publish_assets WHERE draft_id = ?",
                (int(row["id"]),),
            ).fetchone()[0])
            asset_rows = self.conn.execute(
                "SELECT id, path, asset_type, sha256, width, height, duration_ms, "
                "validation, created_at FROM publish_assets WHERE draft_id = ? "
                "ORDER BY id",
                (int(row["id"]),),
            ).fetchall()
            item["assets"] = [dict(asset) for asset in asset_rows]
            items.append(item)
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size if total else 0,
            "status": status,
            "platform": platform,
            "status_options": [
                {"value": key, "label": label}
                for key, label in PUBLISH_STATUS_LABELS.items()
            ],
            "platform_options": [
                {"value": key, "label": label} for key, label in PLATFORM_LABELS.items()
            ],
        }

    def create_draft(self, *, title: str, body: str,
                     content_type: str = "text",
                     platforms: list[str] | None = None,
                     owner_user_id: int | None = None) -> int:
        title = str(title or "").strip()
        body = str(body or "").strip()
        if not title and not body:
            raise ValueError("标题和正文不能同时为空")
        selected = []
        for value in platforms or list(PLATFORMS):
            value = str(value or "").strip().lower()
            if value in PLATFORMS and value not in selected:
                selected.append(value)
        if not selected:
            raise ValueError("至少选择一个发布平台")
        now = _now()
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO publish_drafts "
                "(title, source_content, content_type, status, owner_user_id, created_at, updated_at) "
                "VALUES (?, ?, ?, 'draft', ?, ?, ?)",
                (title, body, str(content_type or "text"), owner_user_id, now, now),
            )
            draft_id = int(cursor.lastrowid)
            for platform in selected:
                self.conn.execute(
                    "INSERT INTO publish_variants "
                    "(draft_id, platform, content_type, title, body, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (draft_id, platform, str(content_type or "text"), title, body, now, now),
                )
        return draft_id

    def get_variant(self, draft_id: int, platform: str) -> dict[str, Any]:
        """读取一条草稿的平台版本，供预览填入和后续发布任务复用。"""
        draft_id = int(draft_id)
        platform = str(platform or "").strip().lower()
        if platform not in PLATFORMS:
            raise ValueError("发布平台无效")
        row = self.conn.execute(
            "SELECT d.id AS draft_id, d.title AS source_title, "
            "d.source_content, d.status AS draft_status, "
            "v.id AS variant_id, v.platform, v.content_type, v.title, v.body, "
            "v.topics, v.cover_path, v.settings, v.status, "
            "v.created_at, v.updated_at "
            "FROM publish_drafts d JOIN publish_variants v ON v.draft_id = d.id "
            "WHERE d.id = ? AND v.platform = ?",
            (draft_id, platform),
        ).fetchone()
        if row is None:
            raise ValueError("发布草稿或平台版本不存在")
        result = dict(row)
        result["platform_label"] = PLATFORM_LABELS.get(platform, platform)
        result["topics"] = _json(result.get("topics"), [])
        result["settings"] = _json(result.get("settings"), {})
        asset_rows = self.conn.execute(
            "SELECT id, path, asset_type, sha256, width, height, duration_ms, "
            "validation, created_at FROM publish_assets WHERE draft_id = ? "
            "ORDER BY id",
            (draft_id,),
        ).fetchall()
        result["assets"] = [dict(asset) for asset in asset_rows]
        result["status_label"] = PUBLISH_STATUS_LABELS.get(
            result.get("status"), result.get("status")
        )
        result["draft_status_label"] = PUBLISH_STATUS_LABELS.get(
            result.get("draft_status"), result.get("draft_status")
        )
        return result

    def add_assets(self, draft_id: int,
                   assets: list[Mapping[str, Any]] | None = None) -> list[int]:
        """保存用户选择的本地素材元数据，不覆盖原文件。"""
        draft_id = int(draft_id)
        if self.conn.execute(
            "SELECT 1 FROM publish_drafts WHERE id = ?", (draft_id,)
        ).fetchone() is None:
            raise ValueError("发布草稿不存在")
        created: list[int] = []
        for raw in assets or []:
            if not isinstance(raw, Mapping):
                continue
            path = os.path.abspath(str(raw.get("path") or "").strip())
            if not path:
                continue
            if not os.path.isfile(path):
                raise ValueError(f"素材文件不存在：{path}")
            asset_type = _infer_asset_type(path, str(raw.get("asset_type") or ""))
            if asset_type not in {"image", "video"}:
                raise ValueError("素材类型必须是图片或视频")
            digest = hashlib.sha256()
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            with self.conn:
                cursor = self.conn.execute(
                    "INSERT INTO publish_assets "
                    "(draft_id, path, asset_type, sha256, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (draft_id, path, asset_type, digest.hexdigest(), _now()),
                )
            created.append(int(cursor.lastrowid))
        if created:
            with self.conn:
                self.conn.execute(
                    "UPDATE publish_drafts SET version = version + 1, updated_at = ? "
                    "WHERE id = ?",
                    (_now(), draft_id),
                )
        return created

    def delete_asset(self, draft_id: int, asset_id: int) -> None:
        """删除草稿对素材的引用，不删除用户磁盘上的原始文件。"""
        draft_id = int(draft_id)
        asset_id = int(asset_id)
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM publish_assets WHERE id = ? AND draft_id = ?",
                (asset_id, draft_id),
            )
            if cursor.rowcount <= 0:
                raise ValueError("发布素材不存在")
            self.conn.execute(
                "UPDATE publish_drafts SET version = version + 1, updated_at = ? "
                "WHERE id = ?",
                (_now(), draft_id),
            )

    def update_variant(self, *, draft_id: int, platform: str, title: str,
                       body: str, topics: list[str] | None = None,
                       content_type: str = "text") -> None:
        draft_id = int(draft_id)
        platform = str(platform or "").strip().lower()
        if platform not in PLATFORMS:
            raise ValueError("发布平台无效")
        if self.conn.execute(
            "SELECT 1 FROM publish_drafts WHERE id = ?", (draft_id,)
        ).fetchone() is None:
            raise ValueError("发布草稿不存在")
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE publish_variants SET content_type = ?, title = ?, body = ?, "
                "topics = ?, updated_at = ? WHERE draft_id = ? AND platform = ?",
                (str(content_type or "text"), str(title or ""), str(body or ""),
                 json.dumps(list(topics or []), ensure_ascii=False), now,
                 draft_id, platform),
            )
            self.conn.execute(
                "UPDATE publish_drafts SET version = version + 1, updated_at = ? "
                "WHERE id = ?",
                (now, draft_id),
            )

    def change_status(self, draft_id: int, status: str) -> None:
        draft_id = int(draft_id)
        status = str(status or "").strip().lower()
        if status not in {"draft", "review", "approved", "queued", "published", "cancelled"}:
            raise ValueError("草稿状态变更无效")
        row = self.conn.execute(
            "SELECT status FROM publish_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if row is None:
            raise ValueError("发布草稿不存在")
        if row["status"] == "published" and status != "cancelled":
            raise ValueError("已发布内容不能退回草稿")
        with self.conn:
            self.conn.execute(
                "UPDATE publish_drafts SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), draft_id),
            )

    def schedule_variant(self, *, draft_id: int, platform: str,
                         account_id: int, scheduled_at: str = "") -> int:
        """创建待发布任务，并把计划时间固定解释为北京时间。

        这里仍只创建队列记录；真实浏览器发送必须经过显式授权和发布执行器。
        但无论从 GUI 还是后台直接调用，都不能写入已经过去的计划时间。
        """
        draft_id = int(draft_id)
        account_id = int(account_id)
        platform = str(platform or "").strip().lower()
        if platform not in PLATFORMS:
            raise ValueError("发布平台无效")
        row = self.conn.execute(
            "SELECT id FROM publish_variants WHERE draft_id = ? AND platform = ?",
            (draft_id, platform),
        ).fetchone()
        if row is None:
            raise ValueError("发布平台版本不存在")
        account = self.conn.execute(
            "SELECT platform FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if account is None:
            raise ValueError("发布账号不存在")
        if str(account["platform"] or "").strip().lower() != platform:
            raise ValueError("发布账号与平台不匹配")
        raw_scheduled_at = str(scheduled_at or "").strip()
        normalized_scheduled_at = (
            normalize_scheduled_at(raw_scheduled_at) if raw_scheduled_at else ""
        )
        now = _now()
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO publish_jobs "
                "(variant_id, account_id, scheduled_at, status, current_step, "
                "real_send_authorized, created_at, updated_at) VALUES (?, ?, ?, 'queued', ?, 0, ?, ?)",
                (int(row["id"]), account_id, normalized_scheduled_at or None,
                 "scheduled" if normalized_scheduled_at else "queued", now, now),
            )
            self.conn.execute(
                "UPDATE publish_drafts SET status = 'queued', updated_at = ? WHERE id = ?",
                (now, draft_id),
            )
            return int(cursor.lastrowid)

    def delete_draft(self, draft_id: int) -> None:
        draft_id = int(draft_id)
        row = self.conn.execute(
            "SELECT status FROM publish_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if row is None:
            raise ValueError("发布草稿不存在")
        if row["status"] in {"running", "published"}:
            raise ValueError("发布中或已发布内容不能删除")
        with self.conn:
            self.conn.execute("DELETE FROM publish_drafts WHERE id = ?", (draft_id,))

    def _variants(self, draft_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, draft_id, platform, content_type, title, body, topics, "
            "cover_path, settings, status, created_at, updated_at "
            "FROM publish_variants WHERE draft_id = ? ORDER BY id",
            (draft_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["platform_label"] = PLATFORM_LABELS.get(
                item["platform"], item["platform"]
            )
            item["topics"] = _json(item.get("topics"), [])
            item["settings"] = _json(item.get("settings"), {})
            item["status_label"] = PUBLISH_STATUS_LABELS.get(
                item.get("status"), item.get("status")
            )
            result.append(item)
        return result


__all__ = ["PublishingService", "PLATFORMS", "PLATFORM_LABELS", "PUBLISH_STATUS_LABELS"]
