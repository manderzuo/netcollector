# -*- coding: utf-8 -*-
"""员工数据同步接收端（标准库 staging 实现）。

桌面端的 ``SyncClient`` 只在用户点击同步时 POST 到这里。接收端按员工
账号保存变更，使用 ``client_event_id`` 做幂等去重；它不接触 BitBrowser、
登录 Cookie 或本地数据库文件。生产部署时可将本模块挂到现有 HTTPS/Nginx
服务后面，或由现有后端按同一 JSON 契约实现。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

try:
    from .auth_store import AuthError, AuthStore  # type: ignore
except ImportError:  # pragma: no cover - standalone server deployment
    from auth_store import AuthError, AuthStore  # type: ignore


MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_CHANGES = 500


class SyncServerStore:
    """保存已经接收的员工同步事件；事件表只增不改。"""

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._ensure_schema()
        # 注册、登录和审批与数据同步共用同一个服务端 SQLite 文件，但使用
        # 独立的认证会话表；密码仍由 AuthStore 负责 PBKDF2 哈希保存。
        self.auth_store = AuthStore(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS employee_sync_changes (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      owner_user_id INTEGER NOT NULL,
                      employee_username TEXT NOT NULL,
                      employee_name TEXT NOT NULL DEFAULT '',
                      device_name TEXT NOT NULL DEFAULT '',
                      entity_type TEXT NOT NULL,
                      entity_id INTEGER,
                      operation TEXT NOT NULL,
                      payload TEXT NOT NULL DEFAULT '{}',
                      client_event_id TEXT NOT NULL UNIQUE,
                      received_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                    );
                    CREATE INDEX IF NOT EXISTS idx_employee_sync_owner
                      ON employee_sync_changes(owner_user_id, id);
                    CREATE TABLE IF NOT EXISTS collector_auth_sessions (
                      token_hash TEXT PRIMARY KEY,
                      user_id INTEGER NOT NULL,
                      created_at TEXT NOT NULL,
                      last_seen_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_collector_auth_session_user
                      ON collector_auth_sessions(user_id);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()

    def create_auth_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        stamp = ""  # 由 SQLite 生成同一时区的可读时间，避免服务端时区差异。
        with self._lock:
            conn = self._connect()
            try:
                stamp = conn.execute("SELECT datetime('now','localtime')").fetchone()[0]
                conn.execute(
                    "INSERT INTO collector_auth_sessions "
                    "(token_hash, user_id, created_at, last_seen_at) VALUES (?, ?, ?, ?)",
                    (self._token_hash(token), int(user_id), stamp, stamp),
                )
                conn.commit()
            finally:
                conn.close()
        return token

    def auth_user_for_token(self, token: str) -> dict[str, Any] | None:
        token = str(token or "").strip()
        if not token:
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT user_id FROM collector_auth_sessions WHERE token_hash = ?",
                    (self._token_hash(token),),
                ).fetchone()
                if row is None:
                    return None
                user = self.auth_store.get(int(row["user_id"]))
                if not user or user.get("status") != "approved":
                    return None
                stamp = conn.execute("SELECT datetime('now','localtime')").fetchone()[0]
                conn.execute(
                    "UPDATE collector_auth_sessions SET last_seen_at = ? WHERE token_hash = ?",
                    (stamp, self._token_hash(token)),
                )
                conn.commit()
                return user
            finally:
                conn.close()

    def revoke_auth_session(self, token: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM collector_auth_sessions WHERE token_hash = ?",
                    (self._token_hash(str(token or "").strip()),),
                )
                conn.commit()
            finally:
                conn.close()

    def accept(self, employee: Mapping[str, Any], device_name: str,
               changes: list[Mapping[str, Any]]) -> dict[str, Any]:
        owner_id = int(employee.get("id") or 0)
        username = str(employee.get("username") or "").strip()
        employee_name = str(employee.get("employee_name") or "").strip()
        if owner_id <= 0 or not username:
            raise ValueError("员工身份信息不完整")
        accepted: list[str] = []
        with self._lock:
            conn = self._connect()
            try:
                for change in changes:
                    if not isinstance(change, Mapping):
                        raise ValueError("同步变更格式无效")
                    event_id = str(change.get("client_event_id") or "").strip()
                    if not event_id or len(event_id) > 128:
                        raise ValueError("同步事件缺少有效幂等编号")
                    if int(change.get("owner_user_id") or 0) != owner_id:
                        raise ValueError("同步事件员工归属不一致")
                    payload = change.get("payload")
                    if not isinstance(payload, Mapping):
                        payload = {}
                    conn.execute(
                        "INSERT OR IGNORE INTO employee_sync_changes "
                        "(owner_user_id, employee_username, employee_name, device_name, "
                        "entity_type, entity_id, operation, payload, client_event_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            owner_id, username, employee_name, str(device_name or "").strip(),
                            str(change.get("entity_type") or "unknown")[:80],
                            change.get("entity_id"),
                            str(change.get("operation") or "upsert")[:30],
                            json.dumps(dict(payload), ensure_ascii=False, default=str),
                            event_id,
                        ),
                    )
                    # 已存在的同一事件也算已接收，保证客户端重试可以收敛。
                    accepted.append(event_id)
                conn.commit()
                row = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS cursor FROM employee_sync_changes"
                ).fetchone()
                return {
                    "ok": True,
                    "accepted": len(accepted),
                    "accepted_client_event_ids": accepted,
                    "cursor": str(int(row["cursor"] if row else 0)),
                }
            finally:
                conn.close()

    def list_changes(self, owner_user_id: int) -> list[dict[str, Any]]:
        """测试/管理员读取接口；不会被 HTTP 暴露给普通员工。"""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM employee_sync_changes WHERE owner_user_id = ? "
                    "ORDER BY id", (int(owner_user_id),)
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
            finally:
                conn.close()


class _SyncRequestHandler(BaseHTTPRequestHandler):
    server: "EmployeeSyncHTTPServer"

    def log_message(self, _format: str, *_args: Any) -> None:
        # 不把员工关键词、评论或令牌写到标准输出。
        return

    def _json(self, status: int, payload: Mapping[str, Any]) -> None:
        raw = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization") or ""
        expected = "Bearer " + self.server.api_token
        return bool(self.server.api_token) and hmac.compare_digest(supplied, expected)

    def _auth_token(self) -> str:
        supplied = str(self.headers.get("Authorization") or "").strip()
        if supplied.lower().startswith("bearer "):
            return supplied[7:].strip()
        return ""

    def _read_json(self) -> Mapping[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("请求大小无效")
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(body, Mapping):
            raise ValueError("请求必须是 JSON 对象")
        return body

    def _handle_auth(self, action: str) -> None:
        try:
            body = self._read_json()
            store = self.server.store
            if action == "register":
                user = store.auth_store.register(
                    body.get("username"), body.get("password"), body.get("employee_name")
                )
                self._json(HTTPStatus.OK, {"ok": True, "user": user})
                return
            if action == "login":
                user = store.auth_store.authenticate(body.get("username"), body.get("password"))
                session_token = store.create_auth_session(int(user["id"]))
                self._json(HTTPStatus.OK, {
                    "ok": True, "authenticated": True, "user": user,
                    "session_token": session_token,
                })
                return

            token = self._auth_token()
            user = store.auth_user_for_token(token)
            if not user:
                self._json(HTTPStatus.UNAUTHORIZED, {
                    "ok": False, "code": "auth_required", "message": "统一账号服务登录已失效，请重新登录",
                })
                return
            if action == "me":
                self._json(HTTPStatus.OK, {"ok": True, "authenticated": True, "user": user})
                return
            if action == "logout":
                store.revoke_auth_session(token)
                self._json(HTTPStatus.OK, {"ok": True, "authenticated": False})
                return
            if user.get("role") != "admin":
                self._json(HTTPStatus.FORBIDDEN, {
                    "ok": False, "code": "forbidden", "message": "只有管理员可以执行此操作",
                })
                return
            if action == "list_users":
                self._json(HTTPStatus.OK, {"ok": True, "items": store.auth_store.list_users()})
                return
            if action in {"approve_user", "reject_user", "disable_user"}:
                user_id = int(body.get("user_id") or 0)
                operation = {
                    "approve_user": store.auth_store.approve,
                    "reject_user": store.auth_store.reject,
                    "disable_user": store.auth_store.disable,
                }[action]
                self._json(HTTPStatus.OK, {"ok": True, "user": operation(user_id)})
                return
            if action == "reset_password":
                user_id = int(body.get("user_id") or 0)
                self._json(HTTPStatus.OK, {
                    "ok": True,
                    "user": store.auth_store.reset_password(user_id, body.get("password")),
                })
                return
            self._json(HTTPStatus.NOT_FOUND, {
                "ok": False, "code": "not_found", "message": "认证接口不存在",
            })
        except AuthError as exc:
            status = HTTPStatus.UNAUTHORIZED if exc.code in {
                "invalid_credentials", "account_pending", "account_rejected", "account_disabled",
            } else HTTPStatus.BAD_REQUEST
            self._json(status, {"ok": False, "code": exc.code, "message": exc.message})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {
                "ok": False, "code": "invalid_request", "message": str(exc),
            })
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False, "code": "server_error", "message": "统一账号服务内部错误",
            })

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/collector/health":
            self._json(HTTPStatus.OK, {"ok": True, "service": "employee-sync"})
            return
        if self.path == "/api/collector/auth/health":
            self._json(HTTPStatus.OK, {"ok": True, "service": "collector-auth"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "接口不存在"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.startswith("/api/collector/auth/"):
            self._handle_auth(path.rsplit("/", 1)[-1])
            return
        if path != "/api/collector/sync":
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "接口不存在"})
            return
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "message": "同步令牌无效"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {
                "ok": False, "message": "同步请求大小无效",
            })
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, Mapping):
                raise ValueError("同步请求必须是 JSON 对象")
            changes = body.get("changes")
            employee = body.get("employee")
            if not isinstance(employee, Mapping) or not isinstance(changes, list):
                raise ValueError("同步请求缺少员工或变更列表")
            if len(changes) > MAX_CHANGES:
                raise ValueError(f"单次最多同步 {MAX_CHANGES} 条")
            result = self.server.store.accept(
                employee, str(body.get("device_name") or ""), changes
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": str(exc)})
            return
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False, "message": "同步服务器内部错误",
            })
            return
        self._json(HTTPStatus.OK, result)


class EmployeeSyncHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, api_token: str, db_path: str):
        self.api_token = str(api_token or "").strip()
        if not self.api_token:
            raise ValueError("同步服务必须配置服务端令牌")
        self.store = SyncServerStore(db_path)
        super().__init__(address, _SyncRequestHandler)


def create_sync_server(host: str, port: int, api_token: str, db_path: str):
    """创建未启动的接收端，供部署脚本和端到端测试使用。"""
    return EmployeeSyncHTTPServer((host, int(port)), api_token, db_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="员工数据同步接收端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", required=True, help="服务端 SQLite 文件路径")
    parser.add_argument("--token", default=os.environ.get("COLLECTOR_SYNC_TOKEN", ""))
    args = parser.parse_args(argv)
    token = args.token or secrets.token_urlsafe(24)
    server = create_sync_server(args.host, args.port, token, args.db)
    print(f"员工同步服务已启动：http://{args.host}:{server.server_address[1]}")
    print("服务端令牌仅在启动时显示一次，请写入员工客户端的同步设置")
    print(token)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())


__all__ = [
    "EmployeeSyncHTTPServer", "SyncServerStore", "create_sync_server", "main",
]
