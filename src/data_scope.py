# -*- coding: utf-8 -*-
"""员工数据归属与手动同步队列。

该模块只处理本地边界和同步队列，不直接决定 GUI 展示。历史数据的归属
字段为空，普通员工不会看到；管理员可以查看全部历史与员工数据。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Mapping


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def user_id(user: Mapping[str, Any] | None) -> int | None:
    if not isinstance(user, Mapping):
        return None
    try:
        value = int(user.get("id") or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def is_admin(user: Mapping[str, Any] | None) -> bool:
    return isinstance(user, Mapping) and str(user.get("role") or "") == "admin"


_SYNC_SECRET_KEYS = frozenset({
    "password", "password_hash", "api_key", "api_token", "token",
    "cookie", "cookies", "bb_window_id", "output_dir",
})


def sanitize_sync_payload(value: Any, key: str = "") -> Any:
    """移除不应离开本机的凭据、浏览器窗口和本地路径信息。"""
    if key.lower() in _SYNC_SECRET_KEYS:
        return None
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize_sync_payload(item_value, str(item_key))
            for item_key, item_value in value.items()
            if str(item_key).lower() not in _SYNC_SECRET_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_sync_payload(item, key) for item in value]
    return value


class SyncStore:
    """对同步 outbox 做幂等写入和状态管理。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def enqueue(self, owner_user_id: int, entity_type: str, entity_id: int | None,
                payload: Mapping[str, Any] | None = None,
                operation: str = "upsert") -> int:
        owner_user_id = int(owner_user_id)
        if owner_user_id <= 0:
            raise ValueError("同步数据必须绑定员工账号")
        event_id = secrets.token_hex(16)
        stamp = now_iso()
        cur = self.conn.execute(
            "INSERT INTO sync_outbox "
            "(owner_user_id, entity_type, entity_id, operation, payload, "
            "client_event_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (owner_user_id, str(entity_type), entity_id, str(operation or "upsert"),
             json.dumps(sanitize_sync_payload(dict(payload or {})),
                        ensure_ascii=False, default=str),
             event_id, stamp, stamp),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def pending(self, owner_user_id: int, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM sync_outbox WHERE owner_user_id = ? "
            "AND status IN ('pending', 'failed') ORDER BY id LIMIT ?",
            (int(owner_user_id), max(1, min(500, int(limit or 100)))),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.get("payload") or "{}")
            except (TypeError, ValueError):
                item["payload"] = {}
            result.append(item)
        return result

    def mark_sent(self, ids: list[int]) -> None:
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self.conn.execute(
            f"UPDATE sync_outbox SET status='sent', last_error=NULL, "
            f"updated_at=? WHERE id IN ({marks})",
            [now_iso(), *[int(item) for item in ids]],
        )
        self.conn.commit()

    def mark_failed(self, ids: list[int], error: str) -> None:
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self.conn.execute(
            f"UPDATE sync_outbox SET status='failed', attempts=attempts+1, "
            f"last_error=?, updated_at=? WHERE id IN ({marks})",
            [str(error)[:1000], now_iso(), *[int(item) for item in ids]],
        )
        self.conn.commit()

    def state(self, owner_user_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM sync_state WHERE user_id = ?", (int(owner_user_id),)
        ).fetchone()
        if row:
            return dict(row)
        return {
            "user_id": int(owner_user_id), "enabled": 0, "server_url": "",
            "api_token": "", "device_name": "", "last_cursor": "", "last_sync_at": None,
            "status": "idle", "last_error": "",
        }

    def save_state(self, owner_user_id: int, *, enabled: bool | None = None,
                   server_url: str | None = None, device_name: str | None = None,
                   api_token: str | None = None,
                   status: str | None = None, last_error: str | None = None,
                   last_cursor: str | None = None, last_sync_at: str | None = None) -> dict[str, Any]:
        current = self.state(owner_user_id)
        values = {
            "enabled": int(current.get("enabled") if enabled is None else bool(enabled)),
            "server_url": str(current.get("server_url") or "" if server_url is None else server_url).strip(),
            "api_token": str(current.get("api_token") or "" if api_token is None else api_token).strip(),
            "device_name": str(current.get("device_name") or "" if device_name is None else device_name).strip(),
            "last_cursor": str(current.get("last_cursor") or "" if last_cursor is None else last_cursor),
            "last_sync_at": current.get("last_sync_at") if last_sync_at is None else last_sync_at,
            "status": str(current.get("status") or "idle" if status is None else status),
            "last_error": str(current.get("last_error") or "" if last_error is None else last_error)[:1000],
        }
        self.conn.execute(
            "INSERT INTO sync_state "
            "(user_id, enabled, server_url, device_name, last_cursor, last_sync_at, "
            "api_token, status, last_error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled, "
            "server_url=excluded.server_url, device_name=excluded.device_name, "
            "last_cursor=excluded.last_cursor, last_sync_at=excluded.last_sync_at, "
            "api_token=excluded.api_token, "
            "status=excluded.status, last_error=excluded.last_error, "
            "updated_at=excluded.updated_at",
            (int(owner_user_id), values["enabled"], values["server_url"],
             values["device_name"], values["last_cursor"], values["last_sync_at"],
             values["api_token"], values["status"], values["last_error"], now_iso()),
        )
        self.conn.commit()
        return self.state(owner_user_id)

    def pending_count(self, owner_user_id: int) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM sync_outbox WHERE owner_user_id = ? "
            "AND status IN ('pending', 'failed')", (int(owner_user_id),)
        ).fetchone()[0])


class SyncClient:
    """标准 JSON HTTP 同步客户端；只在用户点同步且配置完整时联网。"""

    @staticmethod
    def push(config: Mapping[str, Any], user: Mapping[str, Any],
             changes: list[Mapping[str, Any]]) -> dict[str, Any]:
        server_url = str(config.get("server_url") or "").strip().rstrip("/")
        if not server_url:
            raise ValueError("尚未配置同步服务器地址")
        if not changes:
            return {"accepted": 0, "cursor": "", "message": "没有待同步数据"}
        payload = {
            "employee": {
                "id": int(user.get("id") or 0),
                "username": str(user.get("username") or ""),
                "employee_name": str(user.get("employee_name") or ""),
            },
            "device_name": str(config.get("device_name") or ""),
            # 二次清洗同步事件，防止调用方误把令牌、Cookie 或本地窗口信息
            # 放在事件顶层（入队时 payload 已清洗，但 HTTP 调用也必须自守）。
            "changes": [sanitize_sync_payload(dict(item)) for item in changes],
        }
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        token = str(config.get("api_token") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            server_url + "/api/collector/sync", data=raw, headers=headers, method="POST"
        )
        timeout = max(3, min(120, int(config.get("timeout") or 20)))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
                result = json.loads(body or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise RuntimeError(f"同步服务器返回 HTTP {exc.code}: {detail[:500]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"无法连接同步服务器：{type(exc).__name__}") from exc
        except ValueError as exc:
            raise RuntimeError("同步服务器返回的内容不是有效 JSON") from exc
        if not isinstance(result, Mapping):
            raise RuntimeError("同步服务器返回格式无效")
        if result.get("ok") is False:
            raise RuntimeError(str(result.get("message") or "服务器拒绝同步"))
        return dict(result)


__all__ = [
    "SyncStore", "SyncClient", "is_admin", "user_id", "now_iso",
    "sanitize_sync_payload",
]
