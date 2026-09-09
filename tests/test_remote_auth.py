# -*- coding: utf-8 -*-
"""跨设备统一账号注册、审批和登录回归测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from remote_auth import RemoteAuthClient, RemoteAuthError  # noqa: E402
from sync_server import create_sync_server  # noqa: E402
from backend_client import BackendClient  # noqa: E402
from backend_service import BackendService  # noqa: E402
from scheduler import FakeCollector, Scheduler  # noqa: E402


class RemoteAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_sync_server(
            "127.0.0.1", 0, "sync-test-token",
            os.path.join(self.temp.name, "server.db"),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.client = RemoteAuthClient(f"http://{host}:{port}", timeout=3)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_employee_registration_is_visible_to_admin_and_approval_unlocks_login(self):
        registered = self.client.register("worker-remote", "secret6", "远程员工")
        self.assertEqual(registered["user"]["status"], "pending")

        with self.assertRaises(RemoteAuthError) as pending:
            self.client.login("worker-remote", "secret6")
        self.assertEqual(pending.exception.code, "account_pending")

        admin = self.client.login("admin", "abc123")
        admin_client = RemoteAuthClient(f"http://{self.server.server_address[0]}:{self.server.server_address[1]}", timeout=3)
        users = admin_client.list_users(admin["session_token"])
        pending_user = next(item for item in users["items"] if item["username"] == "worker-remote")
        approved = admin_client.set_status(
            "approve_user", pending_user["id"], admin["session_token"]
        )
        self.assertEqual(approved["user"]["status"], "approved")

        employee = self.client.login("worker-remote", "secret6")
        self.assertEqual(employee["user"]["employee_name"], "远程员工")
        self.assertTrue(self.client.me(employee["session_token"])["authenticated"])
        with self.assertRaises(RemoteAuthError) as forbidden:
            admin_client.list_users(employee["session_token"])
        self.assertEqual(forbidden.exception.code, "forbidden")
        self.client.logout(employee["session_token"])


class BackendRemoteAuthTests(unittest.TestCase):
    """验证实际 GUI 后台命令也会使用统一认证服务。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.auth_server = create_sync_server(
            "127.0.0.1", 0, "sync-test-token",
            os.path.join(self.temp.name, "auth.db"),
        )
        self.auth_thread = threading.Thread(
            target=self.auth_server.serve_forever, daemon=True
        )
        self.auth_thread.start()
        auth_host, auth_port = self.auth_server.server_address[:2]
        self.auth_url = f"http://{auth_host}:{auth_port}"
        self.scheduler = Scheduler(
            os.path.join(self.temp.name, "backend.db"),
            bb=None,
            collector=FakeCollector(video_count=0),
        )
        self.service = BackendService(
            self.scheduler,
            endpoint_path=os.path.join(self.temp.name, "endpoint.json"),
            snapshot_interval=0.05,
        )
        self.endpoint = self.service.start()
        self.client = BackendClient(self.endpoint, timeout=3)
        self.client.connect()

    def tearDown(self):
        try:
            self.client.close()
        finally:
            self.service.stop(shutdown_scheduler=True)
            self.scheduler.shutdown(close_connections=True)
            self.auth_server.shutdown()
            self.auth_server.server_close()
            self.auth_thread.join(timeout=2)
            self.temp.cleanup()

    def test_backend_command_routes_employee_approval_to_remote_service(self):
        registered = self.client.request("auth_register", {
            "username": "backend-remote-worker",
            "password": "secret6",
            "employee_name": "后台远程员工",
            "auth_server_url": self.auth_url,
            "auth_server_timeout": 3,
        })
        self.assertEqual(registered["status"], "pending")

        admin = self.client.request("auth_login", {
            "username": "admin",
            "password": "abc123",
            "auth_server_url": self.auth_url,
            "auth_server_timeout": 3,
        })
        self.assertEqual(admin["user"]["role"], "admin")
        users = self.client.request("auth_list_users")
        worker = next(item for item in users["items"]
                      if item["username"] == "backend-remote-worker")
        self.assertEqual(worker["status"], "pending")
        approved = self.client.request("auth_approve_user", {"user_id": worker["id"]})
        self.assertEqual(approved["user"]["status"], "approved")

        self.client.request("auth_logout")
        employee = self.client.request("auth_login", {
            "username": "backend-remote-worker",
            "password": "secret6",
            "auth_server_url": self.auth_url,
            "auth_server_timeout": 3,
        })
        self.assertEqual(employee["user"]["employee_name"], "后台远程员工")
        self.assertEqual(self.client.request("auth_me")["user"]["username"],
                         "backend-remote-worker")


if __name__ == "__main__":
    unittest.main()
