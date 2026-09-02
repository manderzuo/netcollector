# -*- coding: utf-8 -*-
"""本地/服务端共用的账户认证数据层。

第一阶段只负责账户、审批和角色，不参与采集业务数据。存储采用 SQLite，
密码使用 PBKDF2-SHA256 加随机盐保存，数据库中不出现明文密码。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
from datetime import datetime
from typing import Any


class AuthError(RuntimeError):
    """可安全展示给界面的认证业务错误。"""

    def __init__(self, code: str, message: str):
        self.code = str(code)
        self.message = str(message)
        super().__init__(self.message)


class AuthStore:
    """账户表访问器；每次操作使用独立连接，适合后台多线程调用。"""

    HASH_ALGORITHM = "sha256"
    HASH_ITERATIONS = 120_000
    MAX_USERNAME_LENGTH = 64
    MAX_PASSWORD_LENGTH = 256
    MAX_EMPLOYEE_NAME_LENGTH = 80

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        self._schema_lock = threading.Lock()
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def ensure_schema(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._schema_lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS app_users (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                      password_hash TEXT NOT NULL,
                      employee_name TEXT NOT NULL,
                      role TEXT NOT NULL DEFAULT 'employee',
                      status TEXT NOT NULL DEFAULT 'pending',
                      created_at TEXT NOT NULL,
                      approved_at TEXT,
                      last_login_at TEXT,
                      updated_at TEXT NOT NULL
                    )
                    """
                )
                # 只在管理员不存在时创建默认管理员；已有管理员密码绝不覆盖。
                row = conn.execute(
                    "SELECT id FROM app_users WHERE username = ?", ("admin",)
                ).fetchone()
                if row is None:
                    now = self._now()
                    conn.execute(
                        """
                        INSERT INTO app_users
                          (username, password_hash, employee_name, role, status,
                           created_at, approved_at, updated_at)
                        VALUES (?, ?, ?, 'admin', 'approved', ?, ?, ?)
                        """,
                        (
                            "admin", self.hash_password("abc123"), "系统管理员",
                            now, now, now,
                        ),
                    )
                else:
                    # admin 是保留账号，防止误注册后失去审批能力；不动其密码。
                    conn.execute(
                        """
                        UPDATE app_users
                           SET role = 'admin', status = 'approved',
                               employee_name = CASE WHEN employee_name = ''
                                                    THEN '系统管理员'
                                                    ELSE employee_name END,
                               updated_at = ?
                         WHERE username = ?
                        """,
                        (self._now(), "admin"),
                    )
                conn.commit()
            finally:
                conn.close()

    @classmethod
    def hash_password(cls, password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            cls.HASH_ALGORITHM,
            password.encode("utf-8"),
            salt,
            cls.HASH_ITERATIONS,
        )
        return "pbkdf2_{algorithm}${iterations}${salt}${digest}".format(
            algorithm=cls.HASH_ALGORITHM,
            iterations=cls.HASH_ITERATIONS,
            salt=base64.urlsafe_b64encode(salt).decode("ascii"),
            digest=base64.urlsafe_b64encode(digest).decode("ascii"),
        )

    @classmethod
    def verify_password(cls, password: str, encoded: str) -> bool:
        try:
            algorithm, iterations_text, salt_text, digest_text = str(encoded).split("$", 3)
            if not algorithm.startswith("pbkdf2_"):
                return False
            digest_algorithm = algorithm[len("pbkdf2_"):]
            iterations = int(iterations_text)
            salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
            expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
            actual = hashlib.pbkdf2_hmac(
                digest_algorithm, password.encode("utf-8"), salt, iterations
            )
            return hmac.compare_digest(actual, expected)
        except (TypeError, ValueError, UnicodeError):
            return False

    @classmethod
    def _text(cls, value: Any, field: str, maximum: int, *, required: bool = True) -> str:
        text = str(value or "").strip()
        if required and not text:
            raise AuthError("invalid_input", f"{field}不能为空")
        if len(text) > maximum:
            raise AuthError("invalid_input", f"{field}不能超过{maximum}个字符")
        if any(ord(char) < 32 for char in text):
            raise AuthError("invalid_input", f"{field}包含无效控制字符")
        return text

    @classmethod
    def _password(cls, value: Any) -> str:
        password = str(value or "")
        if len(password) < 6:
            raise AuthError("invalid_input", "密码至少需要6位")
        if len(password) > cls.MAX_PASSWORD_LENGTH:
            raise AuthError("invalid_input", "密码不能超过256位")
        return password

    @staticmethod
    def _public_user(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "username": str(row["username"]),
            "employee_name": str(row["employee_name"] or ""),
            "role": str(row["role"] or "employee"),
            "status": str(row["status"] or "pending"),
            "created_at": str(row["created_at"] or ""),
            "approved_at": str(row["approved_at"] or ""),
            "last_login_at": str(row["last_login_at"] or ""),
        }

    def register(self, username: Any, password: Any, employee_name: Any) -> dict[str, Any]:
        username = self._text(username, "账号", self.MAX_USERNAME_LENGTH)
        password = self._password(password)
        employee_name = self._text(
            employee_name, "员工姓名", self.MAX_EMPLOYEE_NAME_LENGTH
        )
        now = self._now()
        conn = self._connect()
        try:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO app_users
                      (username, password_hash, employee_name, role, status,
                       created_at, updated_at)
                    VALUES (?, ?, ?, 'employee', 'pending', ?, ?)
                    """,
                    (username, self.hash_password(password), employee_name, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise AuthError("username_exists", "该账号已存在") from exc
            conn.commit()
            row = conn.execute(
                "SELECT * FROM app_users WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self._public_user(row)
        finally:
            conn.close()

    def authenticate(self, username: Any, password: Any) -> dict[str, Any]:
        username = self._text(username, "账号", self.MAX_USERNAME_LENGTH)
        password = str(password or "")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM app_users WHERE username = ?", (username,)
            ).fetchone()
            if row is None or not self.verify_password(password, row["password_hash"]):
                raise AuthError("invalid_credentials", "账号或密码错误")
            status = str(row["status"] or "")
            if status == "pending":
                raise AuthError("account_pending", "账号正在等待管理员审批")
            if status == "rejected":
                raise AuthError("account_rejected", "账号注册申请未通过")
            if status == "disabled":
                raise AuthError("account_disabled", "账号已被管理员禁用")
            now = self._now()
            conn.execute(
                "UPDATE app_users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (now, now, int(row["id"])),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM app_users WHERE id = ?", (int(row["id"]),)
            ).fetchone()
            return self._public_user(row)
        finally:
            conn.close()

    def get(self, user_id: int) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM app_users WHERE id = ?", (int(user_id),)).fetchone()
            return self._public_user(row) if row else None
        finally:
            conn.close()

    def list_users(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM app_users ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id"
            ).fetchall()
            return [self._public_user(row) for row in rows]
        finally:
            conn.close()

    def _set_status(self, user_id: int, status: str) -> dict[str, Any]:
        if status not in {"approved", "rejected", "disabled"}:
            raise AuthError("invalid_input", "无效的账号状态")
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM app_users WHERE id = ?", (int(user_id),)).fetchone()
            if row is None:
                raise AuthError("not_found", "账号不存在")
            if str(row["username"]).casefold() == "admin":
                raise AuthError("forbidden", "不能修改管理员账号状态")
            now = self._now()
            approved_at = now if status == "approved" else row["approved_at"]
            conn.execute(
                "UPDATE app_users SET status = ?, approved_at = ?, updated_at = ? WHERE id = ?",
                (status, approved_at, now, int(user_id)),
            )
            conn.commit()
            return self._public_user(
                conn.execute("SELECT * FROM app_users WHERE id = ?", (int(user_id),)).fetchone()
            )
        finally:
            conn.close()

    def approve(self, user_id: int) -> dict[str, Any]:
        return self._set_status(user_id, "approved")

    def reject(self, user_id: int) -> dict[str, Any]:
        return self._set_status(user_id, "rejected")

    def disable(self, user_id: int) -> dict[str, Any]:
        return self._set_status(user_id, "disabled")

    def reset_password(self, user_id: int, password: Any) -> dict[str, Any]:
        password = self._password(password)
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM app_users WHERE id = ?", (int(user_id),)).fetchone()
            if row is None:
                raise AuthError("not_found", "账号不存在")
            now = self._now()
            conn.execute(
                "UPDATE app_users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (self.hash_password(password), now, int(user_id)),
            )
            conn.commit()
            return self._public_user(
                conn.execute("SELECT * FROM app_users WHERE id = ?", (int(user_id),)).fetchone()
            )
        finally:
            conn.close()


__all__ = ["AuthError", "AuthStore"]
