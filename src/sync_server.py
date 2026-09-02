# -*- coding: utf-8 -*-
"""员工数据同步接收端（标准库 staging 实现）。

桌面端的 ``SyncClient`` 只在用户点击同步时 POST 到这里。接收端按员工
账号保存变更，使用 ``client_event_id`` 做幂等去重；它不接触 BitBrowser、
登录 Cookie 或本地数据库文件。生产部署时可将本模块挂到现有 HTTPS/Nginx
服务后面，或由现有后端按同一 JSON 契约实现。
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import sqlite3
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping


MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_CHANGES = 500


class SyncServerStore:
    """保存已经接收的员工同步事件；事件表只增不改。"""

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._ensure_schema()

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
                    """
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

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/collector/health":
            self._json(HTTPStatus.OK, {"ok": True, "service": "employee-sync"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "接口不存在"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/collector/sync":
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
