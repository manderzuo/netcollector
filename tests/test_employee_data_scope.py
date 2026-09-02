# -*- coding: utf-8 -*-
"""第二阶段：员工数据隔离与手动同步回归测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import db  # noqa: E402
from backend_client import BackendClient, BackendError  # noqa: E402
from backend_service import BackendService  # noqa: E402
from data_scope import SyncClient, SyncStore  # noqa: E402
from scheduler import FakeCollector, Scheduler  # noqa: E402
from sync_server import create_sync_server  # noqa: E402


class TestEmployeeDataScope(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "platform.db")
        self.scheduler = Scheduler(
            self.db_path, bb=None, collector=FakeCollector(video_count=1)
        )
        self.service = BackendService(
            self.scheduler,
            endpoint_path=os.path.join(self.temp.name, "backend_endpoint.json"),
            snapshot_interval=0.05,
        )
        endpoint = self.service.start()
        self.admin = BackendClient(endpoint, timeout=3)
        self.admin.connect()
        self.admin.request("auth_login", {"username": "admin", "password": "abc123"})

        self.employee_a = self._create_employee("employee-a", "员工甲")
        self.employee_b = self._create_employee("employee-b", "员工乙")
        self.clients = []
        self.sync_server = None
        self.sync_thread = None

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.admin.close()
        self.service.stop(shutdown_scheduler=True)
        self.scheduler.shutdown(close_connections=True)
        if self.sync_server is not None:
            self.sync_server.shutdown()
            self.sync_server.server_close()
        if self.sync_thread is not None:
            self.sync_thread.join(timeout=2)
        self.temp.cleanup()

    def _create_employee(self, username: str, name: str) -> dict:
        created = self.admin.request("auth_register", {
            "username": username,
            "password": "secret6",
            "employee_name": name,
        })
        user_id = int(created["id"])
        approved = self.admin.request("auth_approve_user", {"user_id": user_id})
        return dict(approved["user"])

    def _login_employee(self, username: str) -> BackendClient:
        client = BackendClient(self.service.endpoint, timeout=3)
        client.connect()
        client.request("auth_login", {"username": username, "password": "secret6"})
        self.clients.append(client)
        return client

    def test_migration_creates_scope_and_sync_tables(self):
        conn = db.init_db(os.path.join(self.temp.name, "fresh.db"))
        try:
            self.assertIn("owner_user_id", {
                row[1] for row in conn.execute("PRAGMA table_info('tasks')")
            })
            self.assertIn("data_owner_user_id", {
                row[1] for row in conn.execute("PRAGMA table_info('leads')")
            })
            self.assertIn("api_token", {
                row[1] for row in conn.execute("PRAGMA table_info('sync_state')")
            })
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("sync_outbox", tables)
            self.assertIn("sync_state", tables)
            versions = {
                row[0] for row in conn.execute("SELECT version FROM schema_migrations")
            }
            self.assertIn("010_employee_data_scope", versions)
            self.assertIn("011_admin_control_plane", versions)
            self.assertIn("device_id", {
                row[1] for row in conn.execute("PRAGMA table_info('sync_state')")
            })
            self.assertIn("employee_devices", tables)
        finally:
            conn.close()

    def test_admin_dashboard_tracks_devices_and_can_disable_one(self):
        employee = self._login_employee("employee-a")
        dashboard = self.admin.request("admin_dashboard")
        devices = [
            item for item in dashboard["devices"]
            if int(item["user_id"]) == int(self.employee_a["id"])
        ]
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["status"], "active")
        self.assertGreaterEqual(dashboard["summary"]["active_devices"], 1)

        with self.assertRaises(BackendError) as forbidden:
            employee.request("admin_dashboard")
        self.assertEqual(forbidden.exception.code, "forbidden")

        row_id = int(devices[0]["id"])
        changed = self.admin.request("admin_set_device_status", {
            "device_row_id": row_id, "status": "disabled",
        })
        self.assertEqual(changed["status"], "disabled")
        employee.request("auth_logout")
        with self.assertRaises(BackendError) as disabled:
            employee.request("auth_login", {
                "username": "employee-a", "password": "secret6",
            })
        self.assertEqual(disabled.exception.code, "device_disabled")
        another_connection = BackendClient(self.service.endpoint, timeout=3)
        another_connection.connect()
        try:
            with self.assertRaises(BackendError) as disabled_after_reconnect:
                another_connection.request("auth_login", {
                    "username": "employee-a", "password": "secret6",
                })
            self.assertEqual(disabled_after_reconnect.exception.code, "device_disabled")
        finally:
            another_connection.close()

    def test_admin_backup_validate_and_restore_never_overwrites_live_db(self):
        with self.assertRaises(BackendError) as forbidden:
            self._login_employee("employee-a").request("admin_create_backup")
        self.assertEqual(forbidden.exception.code, "forbidden")

        created = self.admin.request("admin_create_backup")
        self.assertTrue(created["verified"])
        self.assertTrue(os.path.isfile(created["path"]))
        rows = self.admin.request("admin_list_backups")["items"]
        row = next(item for item in rows if item["path"] == created["path"])
        backup_id = int(row["id"])

        validated = self.admin.request("admin_validate_backup", {
            "backup_id": backup_id,
        })
        self.assertTrue(validated["verified"])
        restored = self.admin.request("admin_restore_backup", {
            "backup_id": backup_id,
        })
        self.assertTrue(restored["verified"])
        self.assertTrue(os.path.isfile(restored["path"]))
        self.assertNotEqual(os.path.abspath(restored["path"]), os.path.abspath(self.db_path))
        self.assertTrue(os.path.isfile(self.db_path))

        audit = self.admin.request("admin_audit_logs", {"limit": 200})
        actors = {
            str(item.get("actor_username") or "") for item in audit["items"]
        }
        self.assertIn("admin", actors)

    def test_sync_store_preserves_and_updates_api_token(self):
        self.assertEqual(
            BackendService._safe_operation_args({"api_token": "secret-token"})["api_token"],
            "***已脱敏***",
        )
        conn = db.init_db(os.path.join(self.temp.name, "sync-store.db"))
        try:
            store = SyncStore(conn)
            store.save_state(7, enabled=True, server_url="https://sync.example",
                             api_token="first-token", device_name="设备一")
            store.save_state(7, api_token="second-token")
            state = store.state(7)
            self.assertEqual(state["api_token"], "second-token")
            event_id = store.enqueue(7, "lead", 9, {
                "nickname": "测试", "bb_window_id": "local-window",
                "output_dir": r"C:\private\exports",
            })
            self.assertEqual(store.pending_count(7), 1)
            pending = store.pending(7)[0]
            self.assertEqual(pending["id"], event_id)
            self.assertNotIn("bb_window_id", pending["payload"])
            self.assertNotIn("output_dir", pending["payload"])
            store.mark_sent([event_id])
            self.assertEqual(store.pending_count(7), 0)
        finally:
            conn.close()

    def test_employee_can_only_see_and_operate_owned_rows(self):
        employee_a = self._login_employee("employee-a")
        employee_b = self._login_employee("employee-b")

        task_a = employee_a.request("create_task", {
            "keyword": "员工甲关键词", "platform": "douyin", "target_count": 1,
        })["task_id"]
        task_b = employee_b.request("create_task", {
            "keyword": "员工乙关键词", "platform": "douyin", "target_count": 1,
        })["task_id"]
        account_a = employee_a.request("add_account", {
            "name": "员工甲账号", "bb_window_id": "window-a", "platform": "douyin",
        })["account_id"]
        account_b = employee_b.request("add_account", {
            "name": "员工乙账号", "bb_window_id": "window-b", "platform": "douyin",
        })["account_id"]

        status_a = employee_a.request("status")
        status_b = employee_b.request("status")
        self.assertEqual(set(status_a["tasks"]), {str(task_a)})
        self.assertEqual(set(status_b["tasks"]), {str(task_b)})
        self.assertEqual(set(status_a["accounts"]), {f"douyin:{account_a}"})
        self.assertEqual(set(status_b["accounts"]), {f"douyin:{account_b}"})

        with self.assertRaises(BackendError) as task_error:
            employee_b.request("get_task", {"task_id": task_a})
        self.assertEqual(task_error.exception.code, "forbidden")
        with self.assertRaises(BackendError) as account_error:
            employee_b.request("open_account_browser", {"account_id": account_a})
        self.assertEqual(account_error.exception.code, "forbidden")

        group_a = employee_a.request("create_keyword_group", {
            "name": "甲的词组", "platform": "douyin", "core_terms": ["甲关键词"],
        })["id"]
        self.assertEqual(
            [item["id"] for item in employee_a.request("list_keyword_groups")["items"]],
            [group_a],
        )
        self.assertEqual(employee_b.request("list_keyword_groups")["items"], [])

        admin_status = self.admin.request("status")
        self.assertEqual(set(admin_status["tasks"]), {str(task_a), str(task_b)})
        self.assertEqual(len(admin_status["accounts"]), 2)

        self.assertEqual(
            employee_a.request("delete_task", {"task_id": task_a}),
            {"task_id": task_a, "deleted": True},
        )
        conn = db.init_db(self.db_path, check_same_thread=False)
        try:
            deletion = conn.execute(
                "SELECT operation, entity_type, entity_id FROM sync_outbox "
                "WHERE owner_user_id = ? AND operation = 'delete' AND entity_id = ?",
                (int(self.employee_a["id"]), int(task_a)),
            ).fetchone()
            self.assertIsNotNone(deletion)
            self.assertEqual(tuple(deletion), ("delete", "task", int(task_a)))
        finally:
            conn.close()

    def test_leads_are_isolated_and_manual_sync_is_owner_scoped(self):
        employee_a = self._login_employee("employee-a")
        employee_b = self._login_employee("employee-b")
        lead_ids = []
        conn = db.init_db(self.db_path, check_same_thread=False)
        try:
            for owner_id, nickname in ((self.employee_a["id"], "甲用户"),
                                       (self.employee_b["id"], "乙用户")):
                cur = conn.execute(
                    "INSERT INTO leads (platform, platform_user_id, nickname, dedupe_key, "
                    "first_seen_at, last_seen_at, data_owner_user_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("douyin", f"uid-{owner_id}", nickname, f"employee-{owner_id}",
                     "2026-09-02T10:00:00", "2026-09-02T10:00:00", int(owner_id),
                     "2026-09-02T10:00:00", "2026-09-02T10:00:00"),
                )
                lead_ids.append(int(cur.lastrowid))
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(
            [item["nickname"] for item in employee_a.request(
                "list_leads", {"page": 1, "page_size": 20}
            )["items"]],
            ["甲用户"],
        )
        self.assertEqual(
            [item["nickname"] for item in employee_b.request(
                "list_leads", {"page": 1, "page_size": 20}
            )["items"]],
            ["乙用户"],
        )
        with self.assertRaises(BackendError) as lead_error:
            employee_b.request("add_leads_to_interaction", {"lead_ids": [lead_ids[0]]})
        self.assertEqual(lead_error.exception.code, "forbidden")

        employee_a.request("save_sync_settings", {
            "enabled": True,
            "server_url": "https://sync.example",
            "device_name": "甲电脑",
            "api_token": "token-a",
        })
        # 留空密钥只表示“不修改已保存密钥”，接口返回只给布尔值，不泄露密钥。
        saved = employee_a.request("save_sync_settings", {
            "enabled": True,
            "server_url": "https://sync.example",
            "device_name": "甲电脑",
            "api_token": "",
        })
        self.assertTrue(saved["api_token_configured"])
        self.assertNotIn("api_token", saved)

        conn = db.init_db(self.db_path, check_same_thread=False)
        try:
            SyncStore(conn).enqueue(int(self.employee_a["id"]), "lead", lead_ids[0],
                                    {"nickname": "甲用户"})
            SyncStore(conn).enqueue(int(self.employee_b["id"]), "lead", lead_ids[1],
                                    {"nickname": "乙用户"})
        finally:
            conn.close()

        pushed = {}

        def fake_push(config, user, changes):
            pushed["config"] = dict(config)
            pushed["user"] = dict(user)
            pushed["changes"] = list(changes)
            return {"accepted": len(changes), "cursor": "cursor-a"}

        with patch("data_scope.SyncClient.push", side_effect=fake_push):
            result = employee_a.request("sync_now")
        self.assertTrue(result["ok"])
        self.assertEqual(result["synced"], 1)
        self.assertEqual(pushed["user"]["id"], int(self.employee_a["id"]))
        self.assertEqual(len(pushed["changes"]), 1)
        self.assertEqual(pushed["changes"][0]["owner_user_id"], int(self.employee_a["id"]))
        self.assertEqual(employee_a.request("sync_status")["pending"], 0)

    def test_sync_client_and_receiver_store_owner_scoped_events(self):
        self.sync_server = create_sync_server(
            "127.0.0.1", 0, "server-token", os.path.join(self.temp.name, "server.db")
        )
        self.sync_thread = threading.Thread(
            target=self.sync_server.serve_forever, daemon=True
        )
        self.sync_thread.start()
        base_url = f"http://127.0.0.1:{self.sync_server.server_address[1]}"
        change = {
            "owner_user_id": 11,
            "entity_type": "lead",
            "entity_id": 5,
            "operation": "upsert",
            "client_event_id": "event-1",
            "payload": {"nickname": "甲用户", "api_token": "不要上传"},
        }
        config = {"server_url": base_url, "api_token": "server-token", "timeout": 3}
        result = SyncClient.push(
            config,
            {"id": 11, "username": "employee-a", "employee_name": "员工甲"},
            [change],
        )
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(len(self.sync_server.store.list_changes(11)), 1)
        self.assertEqual(self.sync_server.store.list_changes(12), [])
        self.assertNotIn("api_token", self.sync_server.store.list_changes(11)[0]["payload"])

        # 同一客户端事件可安全重试，不会在服务端产生重复记录。
        retry = SyncClient.push(
            config,
            {"id": 11, "username": "employee-a", "employee_name": "员工甲"},
            [change],
        )
        self.assertEqual(retry["accepted"], 1)
        self.assertEqual(len(self.sync_server.store.list_changes(11)), 1)

        with self.assertRaises(RuntimeError):
            SyncClient.push(
                {"server_url": base_url, "api_token": "wrong-token", "timeout": 3},
                {"id": 11, "username": "employee-a"},
                [change],
            )


if __name__ == "__main__":
    unittest.main()
