# -*- coding: utf-8 -*-
"""账户注册、审批、登录和密码存储回归测试。"""

from __future__ import annotations

import os
import tempfile
import unittest

import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from auth_store import AuthError, AuthStore  # noqa: E402


class TestAuthStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AuthStore(os.path.join(self.temp.name, "data", "app.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_default_admin_is_approved_and_password_is_hashed(self):
        admin = self.store.get(1)
        self.assertIsNotNone(admin)
        self.assertEqual(admin["username"], "admin")
        self.assertEqual(admin["role"], "admin")
        self.assertEqual(admin["status"], "approved")
        conn = self.store._connect()
        try:
            row = conn.execute(
                "SELECT password_hash FROM app_users WHERE username = 'admin'"
            ).fetchone()
        finally:
            conn.close()
        self.assertNotIn("abc123", row[0])

    def test_register_requires_six_char_password_and_employee_name(self):
        with self.assertRaises(AuthError) as short_password:
            self.store.register("worker", "12345", "员工")
        self.assertEqual(short_password.exception.code, "invalid_input")
        with self.assertRaises(AuthError) as missing_name:
            self.store.register("worker", "123456", "")
        self.assertEqual(missing_name.exception.code, "invalid_input")

    def test_pending_approval_login_and_disable(self):
        created = self.store.register("worker", "123456", "员工甲")
        self.assertEqual(created["status"], "pending")
        with self.assertRaises(AuthError) as pending:
            self.store.authenticate("worker", "123456")
        self.assertEqual(pending.exception.code, "account_pending")

        approved = self.store.approve(created["id"])
        self.assertEqual(approved["status"], "approved")
        logged_in = self.store.authenticate("worker", "123456")
        self.assertEqual(logged_in["employee_name"], "员工甲")
        disabled = self.store.disable(created["id"])
        self.assertEqual(disabled["status"], "disabled")
        with self.assertRaises(AuthError) as disabled_error:
            self.store.authenticate("worker", "123456")
        self.assertEqual(disabled_error.exception.code, "account_disabled")

    def test_duplicate_username_and_admin_status_protection(self):
        first = self.store.register("worker", "123456", "员工甲")
        with self.assertRaises(AuthError) as duplicate:
            self.store.register("WORKER", "654321", "员工乙")
        self.assertEqual(duplicate.exception.code, "username_exists")
        with self.assertRaises(AuthError) as protected:
            self.store.disable(1)
        self.assertEqual(protected.exception.code, "forbidden")
        self.assertEqual(self.store.get(first["id"])["username"], "worker")


if __name__ == "__main__":
    unittest.main()
