# -*- coding: utf-8 -*-
"""2.0 后台服务兼容层测试。

测试使用临时数据库和 FakeCollector，不启动真实浏览器，不读取项目个人数据。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
import json
from datetime import datetime, timedelta
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import db  # noqa: E402
from backend_client import BackendClient, BackendError  # noqa: E402
from backend_protocol import (  # noqa: E402
    BackendEndpoint,
    ProtocolError,
    decode_message,
    encode_message,
    make_command,
    read_endpoint,
    validate_command,
    write_endpoint,
)
from backend_service import BackendService  # noqa: E402
from interactions.repository import InteractionRepository  # noqa: E402
from interactions.models import ReplyActionResult  # noqa: E402
from interactions.private_message import (  # noqa: E402
    PrivateMessageTarget,
    _private_message_script,
    _private_page_score,
    _private_page_state_script,
    _same_page_url,
)
from interactions.service import InteractionService  # noqa: E402
from leads.repository import LeadRepository  # noqa: E402
from scheduler import FakeCollector, Scheduler  # noqa: E402
from time_utils import beijing_now  # noqa: E402


class TestBackendProtocol(unittest.TestCase):
    def test_private_message_script_keeps_javascript_trace_object_literal(self):
        script = _private_message_script(
            "测试用户", "user-1", "私信测试正文", confirm=False
        )
        self.assertIn("Object.assign({step}, extra || {})", script)
        self.assertNotIn("Object.assign({step}, extra || {{}})", script)
        self.assertIn("platform === 'douyin'", script)
        self.assertIn("? /^(私信|发私信)$/", script)
        self.assertIn("private_message_input_probe", script)
        self.assertIn("private_account_not_supported", script)
        self.assertIn("private_account_detected", script)
        self.assertIn("const privateCandidates = () =>", script)
        self.assertNotIn(
            "const all = Array.from(document.querySelectorAll('button,[role=\"button\"],a,span,div'))",
            script,
        )
        self.assertIn("private_message_button_probe", script)
        self.assertIn("private_message_input_state_probe", script)
        self.assertIn("private_message_button_clicked', {page:", script)

    def test_private_message_page_selection_avoids_bitbrowser_and_creator_pages(self):
        target = PrivateMessageTarget(
            lead_id=1,
            draft_id=2,
            platform="douyin",
            account_id=14,
            account_name="测试账号",
            account_status="idle",
            bb_window_id="window-1",
            platform_user_id="user-1",
            nickname="测试用户",
            profile_url="https://www.douyin.com/user/user-1",
        )
        self.assertLess(
            _private_page_score(
                {"type": "page", "url": "https://console.bitbrowser.net/?id=window-1"},
                target,
            ),
            -99999,
        )
        self.assertLess(
            _private_page_score(
                {"type": "page", "url": "https://creator.douyin.com/creator-micro/content/manage"},
                target,
            ),
            -99999,
        )
        self.assertGreater(
            _private_page_score(
                {"type": "page", "url": target.profile_url}, target
            ),
            300,
        )
        self.assertGreater(
            _private_page_score(
                {"type": "page", "url": "https://www.douyin.com/user/other"},
                target,
            ),
            150,
        )

    def test_private_message_page_state_script_contains_readiness_signals(self):
        script = _private_page_state_script()
        self.assertIn("readyState: document.readyState", script)
        self.assertIn("privateButtonCount", script)
        self.assertIn("privateNotice", script)

    def test_private_message_target_page_compares_path_not_only_host(self):
        self.assertFalse(_same_page_url(
            "https://www.douyin.com/user/self",
            "https://www.douyin.com/user/target-id?from=message",
        ))
        self.assertTrue(_same_page_url(
            "https://www.douyin.com/user/target-id?from=self",
            "https://www.douyin.com/user/target-id?from=message",
        ))

    def test_command_round_trip_and_unknown_command_rejected(self):
        message = make_command("status", {"unused": 1}, token="local-token")
        parsed = validate_command(decode_message(encode_message(message)))
        self.assertEqual(parsed["command"], "status")
        self.assertEqual(parsed["token"], "local-token")
        with self.assertRaises(ProtocolError):
            make_command("execute_arbitrary_method")

    def test_endpoint_is_written_atomically_and_read_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "runtime", "backend_endpoint.json")
            endpoint = BackendEndpoint("127.0.0.1", 45899, "token", pid=7)
            self.assertEqual(write_endpoint(endpoint, path), os.path.abspath(path))
            loaded = read_endpoint(path)
            self.assertEqual(loaded, endpoint)
            self.assertFalse(os.path.exists(path + ".tmp"))


class TestBackendService(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "backend.db")
        self.scheduler = Scheduler(
            self.db_path, bb=None, collector=FakeCollector(video_count=2)
        )
        self.account_id = self.scheduler.add_account(
            "测试账号", bb_window_id="demo-test", platform="douyin"
        )
        self.events = []
        self.events_lock = threading.Lock()
        self.service = BackendService(
            self.scheduler,
            endpoint_path=os.path.join(self.temp.name, "backend_endpoint.json"),
            snapshot_interval=0.05,
        )
        endpoint = self.service.start()
        self.client = BackendClient(endpoint, timeout=3, event_callback=self._on_event)
        self.client.connect()
        # 业务命令现在要求已认证；测试客户端使用内置管理员登录，保持其余
        # 采集/线索/互动回归测试覆盖原有业务契约。
        self.client.request("auth_login", {"username": "admin", "password": "abc123"})

    def tearDown(self):
        try:
            self.client.close()
        finally:
            self.service.stop(shutdown_scheduler=True)
            self.scheduler.shutdown(close_connections=True)
            self.temp.cleanup()

    def _on_event(self, message):
        with self.events_lock:
            self.events.append(message)

    def _events(self):
        with self.events_lock:
            return list(self.events)

    def test_status_command_and_state_event(self):
        report = self.client.request("status")
        self.assertIn("tasks", report)
        self.assertIn("accounts", report)
        deadline = time.time() + 2
        while time.time() < deadline:
            if any(item.get("event") == "state_snapshot" for item in self._events()):
                break
            time.sleep(0.02)
        self.assertTrue(any(item.get("event") == "state_snapshot" for item in self._events()))

    def test_refresh_account_nicknames_updates_idle_and_skips_working(self):
        working_id = self.scheduler.add_account(
            "工作中账号", bb_window_id="demo-working", platform="douyin"
        )
        # 模拟另一个仍存活的调度器持有租约；刷新昵称时才应跳过该账号。
        # 没有租约的 working 属于旧版本/异常退出残留，会被自动校正为 idle。
        self.scheduler.conn.execute(
            "UPDATE accounts SET status = 'working', runtime_owner = ?, "
            "runtime_heartbeat = ? WHERE id = ?",
            (f"{os.getpid()}:other-scheduler", datetime.now().isoformat(), working_id),
        )
        self.scheduler.conn.commit()

        def fake_bind(args):
            account_id = int(args["account_id"])
            db.update_account_nickname(
                self.scheduler.conn, account_id, f"平台昵称-{account_id}"
            )
            return {
                "account_id": account_id,
                "nickname": f"平台昵称-{account_id}",
            }

        with patch.object(self.service, "_bind_account", side_effect=fake_bind) as bind:
            result = self.client.request("refresh_account_nicknames")

        self.assertEqual(result["updated"], [
            {"account_id": self.account_id, "nickname": f"平台昵称-{self.account_id}"}
        ])
        self.assertEqual(result["skipped"], [
            {"account_id": working_id, "reason": "账号正在工作，已跳过"}
        ])
        self.assertEqual(result["failed"], [])
        bind.assert_called_once_with({"account_id": self.account_id})
        row = self.scheduler.conn.execute(
            "SELECT nickname FROM accounts WHERE id = ?", (self.account_id,)
        ).fetchone()
        self.assertEqual(row["nickname"], f"平台昵称-{self.account_id}")

    def test_status_report_keeps_explicit_platform_nickname(self):
        db.update_account_nickname(self.scheduler.conn, self.account_id, "12345678a")
        report = self.scheduler.status_report()
        row = report["accounts"][f"douyin:{self.account_id}"]
        self.assertEqual(row["nickname"], "12345678a")
        self.assertTrue(row["nickname_resolved"])

    def test_bind_account_persists_explicit_numeric_platform_nickname(self):
        ws_path = self.service._account_ws_path("douyin")
        with open(ws_path, "w", encoding="utf-8") as stream:
            stream.write("demo-test\nws://127.0.0.1:9222/devtools/browser/test\n")
        with patch.object(self.service, "_open_account_browser"), patch(
            "account_reader.read_account",
            return_value={"logged_in": True, "nick": "1234567899", "uid": "u-14"},
        ) as reader:
            result = self.service._bind_account({"account_id": self.account_id})
        reader.assert_called_once_with(
            "douyin",
            "ws://127.0.0.1:9222/devtools/browser/test",
            expected_uid="测试账号",
        )
        self.assertEqual(result["nickname"], "1234567899")
        row = self.scheduler.conn.execute(
            "SELECT nickname FROM accounts WHERE id = ?", (self.account_id,)
        ).fetchone()
        self.assertEqual(row["nickname"], "1234567899")

    def test_authentication_and_admin_approval_flow(self):
        unauthenticated = BackendClient(self.service.endpoint, timeout=3)
        unauthenticated.connect()
        try:
            self.assertTrue(unauthenticated.request("status")["auth_required"])
            with self.assertRaises(BackendError) as error:
                unauthenticated.request("list_leads", {"page": 1})
            self.assertEqual(error.exception.code, "auth_required")
        finally:
            unauthenticated.close()

        registered = self.client.request("auth_register", {
            "username": "new-employee",
            "password": "secret6",
            "employee_name": "新员工",
        })
        self.assertEqual(registered["status"], "pending")
        with self.assertRaises(BackendError) as pending_error:
            self.client.request("auth_login", {
                "username": "new-employee", "password": "secret6"
            })
        self.assertEqual(pending_error.exception.code, "account_pending")
        with self.assertRaises(BackendError) as logged_out_error:
            self.client.request("auth_list_users")
        self.assertEqual(logged_out_error.exception.code, "auth_required")
        self.client.request("auth_login", {"username": "admin", "password": "abc123"})

        users = self.client.request("auth_list_users")["items"]
        pending = next(item for item in users if item["username"] == "new-employee")
        approved = self.client.request("auth_approve_user", {"user_id": pending["id"]})
        self.assertEqual(approved["user"]["status"], "approved")

        employee = BackendClient(self.service.endpoint, timeout=3)
        employee.connect()
        try:
            logged_in = employee.request("auth_login", {
                "username": "new-employee", "password": "secret6"
            })
            self.assertEqual(logged_in["user"]["employee_name"], "新员工")
            with self.assertRaises(BackendError) as forbidden_error:
                employee.request("auth_list_users")
            self.assertEqual(forbidden_error.exception.code, "forbidden")
        finally:
            employee.close()

    def test_client_can_disconnect_and_reconnect(self):
        self.assertIn("tasks", self.client.request("status"))
        self.client.close()
        self.assertFalse(self.client.connected)
        self.client.connect()
        self.assertTrue(self.client.connected)
        login = self.client.request("auth_login", {"username": "admin", "password": "abc123"})
        self.assertIn("accounts", self.client.request("status"))
        self.assertTrue(login.get("local_session_token"))

        restored_client = BackendClient(self.service.endpoint, timeout=3)
        restored_client.connect()
        try:
            restored = restored_client.request("auth_restore", {
                "local_session_token": login["local_session_token"],
            })
            self.assertTrue(restored["authenticated"])
            self.assertEqual(restored["user"]["username"], "admin")
        finally:
            restored_client.close()

    def test_create_browser_window_restores_account_setup_flow(self):
        class FakeBitBrowser:
            def __init__(self):
                self.created = None
                self.opened = None

            def create_window(self, **kwargs):
                self.created = kwargs
                return {"id": "new-kuaishou-window"}

            def open_browser(self, browser_id, **kwargs):
                self.opened = (browser_id, kwargs)
                # 没有返回 CDP 地址时，服务仍应保留已创建的 profile，
                # 并把“需要手动打开/刷新”作为可恢复结果返回。
                return {}

        fake = FakeBitBrowser()
        self.scheduler.bb = fake
        result = self.client.request("create_browser_window", {
            "platform": "kuaishou",
            "name": "快手测试窗口",
        })

        self.assertEqual(result["window_id"], "new-kuaishou-window")
        self.assertEqual(result["platform"], "kuaishou")
        self.assertFalse(result["opened"])
        self.assertEqual(fake.created["name"], "快手测试窗口")
        self.assertEqual(fake.created["platform"], "https://www.kuaishou.com/new-reco")
        self.assertEqual(fake.created["url"], "https://www.kuaishou.com/new-reco")
        self.assertEqual(fake.opened[0], "new-kuaishou-window")

    def test_delete_browser_window_removes_only_unbound_profile(self):
        class FakeBitBrowser:
            def __init__(self):
                self.closed = []
                self.deleted = []

            def close_browser(self, browser_id):
                self.closed.append(browser_id)
                return {"closed": True}

            def delete_browser(self, browser_id):
                self.deleted.append(browser_id)
                return {"deleted": True}

        fake = FakeBitBrowser()
        self.scheduler.bb = fake
        cache_path = self.service._account_ws_path("douyin")
        with open(cache_path, "w", encoding="utf-8") as stream:
            stream.write("extra-window\nws://127.0.0.1:9222\n")

        with patch("backend_service.time.sleep") as sleep:
            result = self.client.request("delete_browser_window", {
                "window_id": "extra-window",
            })

        self.assertEqual(result["window_id"], "extra-window")
        self.assertTrue(result["deleted"])
        self.assertEqual(fake.closed, ["extra-window"])
        self.assertEqual(fake.deleted, ["extra-window"])
        sleep.assert_called_once_with(5)
        self.assertFalse(os.path.exists(cache_path))

    def test_delete_browser_window_rejects_bound_profile(self):
        class FakeBitBrowser:
            def close_browser(self, _browser_id):
                raise AssertionError("bound profile must not be touched")

            def delete_browser(self, _browser_id):
                raise AssertionError("bound profile must not be touched")

        self.scheduler.bb = FakeBitBrowser()
        with self.assertRaises(BackendError) as context:
            self.client.request("delete_browser_window", {
                "window_id": "demo-test",
            })
        self.assertIn("已绑定账号", str(context.exception))

    def test_commands_manage_task_without_exposing_scheduler(self):
        created = self.client.request("create_task", {
            "keyword": "后台服务测试",
            "platform": "douyin",
            "target_count": 2,
            "task_accounts": ["测试账号"],
        })
        task_id = created["task_id"]
        self.assertIsInstance(task_id, int)
        task = self.client.request("get_task", {"task_id": task_id})
        self.assertEqual(task["keyword"], "后台服务测试")

        self.assertEqual(
            self.client.request("pause", {"task_id": task_id}),
            {"task_id": task_id, "paused": True},
        )
        self.assertEqual(
            self.client.request("resume", {"task_id": task_id}),
            {"task_id": task_id, "paused": False},
        )
        self.assertEqual(
            self.client.request("start", {"task_id": task_id})["task_id"], task_id
        )

        deadline = time.time() + 4
        final = None
        while time.time() < deadline:
            final = self.client.request("status")
            # JSON 对象键在传输后统一为字符串；服务端不改变 Scheduler
            # 的报表结构，客户端按协议读取字符串任务 ID。
            state = final["tasks"][str(task_id)]["status"]
            if state == "done":
                break
            time.sleep(0.03)
        self.assertIsNotNone(final)
        self.assertEqual(final["tasks"][str(task_id)]["status"], "done")
        self.assertTrue(any(item.get("event") == "log" for item in self._events()))

    def test_pause_is_not_blocked_by_long_running_start_command(self):
        task_id = self.scheduler.create_task(
            "并行控制测试", platform="douyin", target_count=1,
            task_accounts=["测试账号"],
        )
        entered = threading.Event()
        release = threading.Event()

        def blocking_start(*_args, **_kwargs):
            entered.set()
            release.wait(3)
            return task_id

        with patch.object(self.scheduler, "start", side_effect=blocking_start):
            start_result = []
            start_thread = threading.Thread(
                target=lambda: start_result.append(
                    self.client.request("start", {"task_id": task_id})
                ),
                daemon=True,
            )
            start_thread.start()
            self.assertTrue(entered.wait(1))

            started_at = time.perf_counter()
            paused = self.client.request("pause", {"task_id": task_id}, timeout=1)
            pause_elapsed = time.perf_counter() - started_at
            self.assertEqual(paused, {"task_id": task_id, "paused": True})
            self.assertLess(pause_elapsed, 1.0)

            release.set()
            start_thread.join(2)
            self.assertEqual(start_result, [{"task_id": task_id}])

    def test_duplicate_delete_is_rejected_while_first_delete_is_running(self):
        task_id = self.scheduler.create_task(
            "重复删除保护", platform="douyin", target_count=1,
            task_accounts=["测试账号"],
        )
        entered = threading.Event()
        release = threading.Event()

        def blocking_delete(_task_id):
            entered.set()
            release.wait(3)
            return True

        with patch.object(self.scheduler, "delete_task", side_effect=blocking_delete):
            first_result = []
            first_thread = threading.Thread(
                target=lambda: first_result.append(
                    self.client.request("delete_task", {"task_id": task_id})
                ),
                daemon=True,
            )
            first_thread.start()
            self.assertTrue(entered.wait(1))

            started_at = time.perf_counter()
            duplicate = self.client.request(
                "delete_task", {"task_id": task_id}, timeout=1
            )
            duplicate_elapsed = time.perf_counter() - started_at
            self.assertEqual(
                duplicate,
                {"task_id": task_id, "deleted": False, "already_in_progress": True},
            )
            self.assertLess(duplicate_elapsed, 1.0)

            release.set()
            first_thread.join(2)
            self.assertEqual(first_result, [{"task_id": task_id, "deleted": True}])

    def test_human_resume_does_not_reopen_completed_search_phase(self):
        task_id = 16
        task = {"id": task_id, "search_exhausted": 1, "task_accounts": "[]"}
        start_calls = []
        with patch.object(self.scheduler, "get_task", return_value=task), \
                patch.object(self.scheduler, "status_report", return_value={
                    "tasks": {str(task_id): {
                        "id": task_id,
                        "status": "paused",
                        "latest_run": {"stop_reason": "需要人工验证：验证码"},
                    }},
                    "accounts": {},
                }), \
                patch.object(self.scheduler, "waiting_accounts_for_task", return_value=set()), \
                patch.object(self.scheduler, "resume"), \
                patch.object(self.scheduler, "start", side_effect=lambda tid, **kwargs: start_calls.append((tid, kwargs))):
            result = self.service._resume_task(task_id)

        self.assertTrue(result["resumed"])
        self.assertFalse(result["force_search"])
        self.assertEqual(start_calls, [(task_id, {"force_search": False})])

    def test_bad_token_is_rejected(self):
        bad = BackendClient(
            self.service.endpoint.__class__(
                self.service.endpoint.host,
                self.service.endpoint.port,
                "wrong-token",
                pid=self.service.endpoint.pid,
                started_at=self.service.endpoint.started_at,
            ),
            timeout=2,
        )
        try:
            bad.connect()
            with self.assertRaises(BackendError) as ctx:
                bad.request("status")
            self.assertEqual(ctx.exception.code, "unauthorized")
        finally:
            bad.close()

    def _seed_lead(self):
        task_id = self.scheduler.create_task(
            "线索测试", platform="douyin", target_count=10
        )
        conn = db.init_db(self.db_path, check_same_thread=False)
        try:
            repo = LeadRepository(conn)
            lead_id, _created = repo.upsert_lead({
                "platform": "douyin",
                "platform_user_id": "user-backend-1",
                "nickname": "后台线索用户",
                "dedupe_key": "douyin:user-backend-1",
                "region_province": "河南",
                "region_source": "manual",
                "region_confidence": 100,
                "intent_level": "low",
                "intent_score": 1,
                "summary_text": "不会写入此字段",
                "status": "new",
            })
            repo.add_evidence({
                "lead_id": lead_id,
                "task_id": task_id,
                "evidence_type": "comment",
                "evidence_text": "后台线索的评论原文",
                "occurred_at": "2026-08-27T15:00:00",
            })
            return task_id, lead_id
        finally:
            conn.close()

    def test_list_leads_returns_filtered_rows_and_chinese_ready_fields(self):
        task_id, lead_id = self._seed_lead()
        result = self.client.request("list_leads", {
            "task_id": task_id,
            "platform": "douyin",
            "page": 1,
            "page_size": 20,
        })
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], lead_id)
        self.assertEqual(result["items"][0]["nickname"], "后台线索用户")
        self.assertEqual(result["items"][0]["comment"], "后台线索的评论原文")
        self.assertEqual(result["items"][0]["source_url"], "")
        self.assertEqual(result["tasks"][0]["id"], task_id)

    def test_lead_keyword_filter_is_applied_alongside_intent_filter(self):
        task_id, _lead_id = self._seed_lead()
        matched = self.client.request("list_leads", {
            "task_id": task_id,
            "intent_level": "low",
            "keyword": "评论原文",
            "page": 1,
            "page_size": 20,
        })
        self.assertEqual(matched["total"], 1)

        # 关键词和意向是 AND 关系；任意一个条件不满足都不应返回记录。
        not_matched = self.client.request("list_leads", {
            "task_id": task_id,
            "intent_level": "high",
            "keyword": "评论原文",
            "page": 1,
            "page_size": 20,
        })
        self.assertEqual(not_matched["total"], 0)

    def test_keyword_group_can_be_edited_from_backend_entry(self):
        created = self.client.request("create_keyword_group", {
            "name": "后台可编辑词组",
            "platform": "douyin",
            "core_terms": "旧词",
        })
        group_id = created["id"]
        updated = self.client.request("update_keyword_group", {
            "group_id": group_id,
            "name": "后台已编辑词组",
            "platform": "xhs",
            "core_terms": "新词、第二个词",
            "synonym_terms": "替代词",
        })
        self.assertEqual(updated["name"], "后台已编辑词组")
        self.assertEqual(updated["platform"], "xhs")
        self.assertEqual(updated["terms"]["core"], ["新词", "第二个词"])
        groups = self.client.request("list_keyword_groups")
        found = next(item for item in groups["items"] if item["id"] == group_id)
        self.assertEqual(found["terms"]["synonym"], ["替代词"])

    def test_manual_selected_lead_can_enter_interaction_without_auto_qualification(self):
        _task_id, lead_id = self._seed_lead()
        result = self.client.request("add_leads_to_interaction", {
            "lead_ids": [lead_id],
        })
        self.assertEqual(len(result["added"]), 1)
        self.assertEqual(result["added"][0]["lead_id"], lead_id)
        self.assertEqual(result["failed"], [])
        conn = db.init_db(self.db_path, check_same_thread=False)
        try:
            drafts = InteractionRepository(conn).list_drafts(
                lead_id=lead_id, page=1, page_size=20
            )
            self.assertEqual(drafts["total"], 1)
        finally:
            conn.close()

        repeated = self.client.request("add_leads_to_interaction", {
            "lead_ids": [lead_id],
        })
        self.assertEqual(repeated["existing"], [{"lead_id": lead_id}])

    def test_interaction_list_keeps_full_reply_and_original_comment(self):
        _task_id, lead_id = self._seed_lead()
        added = self.client.request("add_leads_to_interaction", {"lead_ids": [lead_id]})
        draft_id = added["added"][0]["draft_id"]
        result = self.client.request("list_interactions", {"status": "draft"})
        self.assertEqual(result["total"], 1)
        row = result["items"][0]
        self.assertEqual(row["draft_id"], draft_id)
        self.assertEqual(row["original_comment"], "后台线索的评论原文")
        self.assertIn("后台线索用户", row["content"])
        self.assertTrue(any(item["content"] for item in result["templates"]))

        self.client.request("interaction_action", {
            "draft_id": draft_id,
            "action": "enter_send",
            "content": "人工审核后的完整回复正文",
        })
        self.client.request("interaction_action", {
            "draft_id": draft_id,
            "action": "assign_account",
            "account_id": self.account_id,
        })
        queued = self.client.request("list_interactions", {"status": "queued"})
        self.assertEqual(queued["items"][0]["content"], "人工审核后的完整回复正文")
        self.assertEqual(queued["items"][0]["reply_account_id"], self.account_id)

        self.client.request("interaction_action", {
            "draft_id": draft_id,
            "action": "return_queued",
        })
        returned = self.client.request("list_interactions", {"status": "draft"})
        self.assertEqual(returned["total"], 1)
        self.assertEqual(returned["items"][0]["draft_id"], draft_id)
        self.assertEqual(returned["items"][0]["content"], "人工审核后的完整回复正文")
        self.assertEqual(
            self.client.request("list_interactions", {"status": "queued"})["total"],
            0,
        )

    def test_private_message_drafts_are_separate_and_simulation_only_fills(self):
        _task_id, lead_id = self._seed_lead()
        conn = db.init_db(self.db_path, check_same_thread=False)
        try:
            conn.execute(
                "UPDATE leads SET profile_url = ? WHERE id = ?",
                ("https://www.douyin.com/user/private-target", lead_id),
            )
            conn.commit()
        finally:
            conn.close()

        added = self.client.request("add_leads_to_private_message", {
            "lead_ids": [lead_id],
        })
        self.assertEqual(len(added["added"]), 1)
        draft_id = added["added"][0]["draft_id"]
        listed = self.client.request("list_interactions", {
            "status": "draft", "interaction_type": "private_message",
        })
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["interaction_type"], "private_message")
        self.assertEqual(listed["items"][0]["interaction_type_label"], "发私信")
        self.assertEqual(
            listed["items"][0]["platform_user_id"], "user-backend-1"
        )
        self.assertEqual(
            listed["items"][0]["profile_url"],
            "https://www.douyin.com/user/private-target",
        )
        self.assertEqual(
            self.client.request("list_interactions", {
                "status": "draft", "interaction_type": "comment_reply",
            })["total"],
            0,
        )

        self.client.request("interaction_action", {
            "draft_id": draft_id, "action": "enter_send",
            "content": "这是私信测试正文",
        })
        self.client.request("interaction_action", {
            "draft_id": draft_id, "action": "assign_account",
            "account_id": self.account_id,
        })
        conn = db.init_db(self.db_path, check_same_thread=False)
        try:
            class FakePrivateMessageAdapter:
                def __init__(self):
                    self.calls = []

                def send_message(self, target, content, *, confirm=False):
                    self.calls.append((target, content, confirm))
                    return ReplyActionResult(
                        ok=True, stage="filled_waiting_confirmation",
                        message="私信内容已填入，未点击发送", verified=True,
                        target=target,
                    )

            fake = FakePrivateMessageAdapter()
            service = InteractionService(
                InteractionRepository(conn), LeadRepository(conn),
                private_message_adapter=fake, config={},
            )
            result = service.simulate_private_message(draft_id, self.account_id)
            self.assertTrue(result.ok)
            self.assertEqual(fake.calls[0][1], "这是私信测试正文")
            self.assertFalse(fake.calls[0][2])
            self.assertEqual(
                InteractionRepository(conn).get_draft(draft_id)["status"],
                "queued",
            )
        finally:
            conn.close()

    def test_publish_uses_latest_platform_editor_snapshot_before_browser_fill(self):
        xhs_account_id = self.scheduler.add_account(
            "小红书测试账号", bb_window_id="xhs-window", platform="xhs"
        )
        draft_id = self.client.request("create_publish_draft", {
            "title": "统一旧标题", "body": "统一旧正文", "platforms": ["xhs"],
        })["draft_id"]
        self.scheduler.bb = object()
        captured = {}

        class FakePublishingAdapter:
            def __init__(self, _browser):
                pass

            def preview_fill(self, **kwargs):
                captured.update(kwargs)
                return {
                    "ok": True, "verified": True, "mismatches": [],
                    "expected": {"title": kwargs["title"], "body": kwargs["body"], "topics": kwargs["topics"]},
                    "actual": {"title": kwargs["title"], "body": kwargs["body"], "topics": kwargs["topics"]},
                }

        with patch("publishing.browser.PublishingBrowserAdapter", FakePublishingAdapter):
            self.service._preview_publish_draft({
                "draft_id": draft_id, "account_id": xhs_account_id, "platform": "xhs",
                "editor_title": "小红书已编辑标题",
                "editor_body": "小红书已编辑正文",
                "editor_topics": "春日内容, 小红书测试",
            })

        self.assertEqual(captured["title"], "小红书已编辑标题")
        self.assertEqual(captured["body"], "小红书已编辑正文")
        self.assertEqual(captured["topics"], ["春日内容", "小红书测试"])
        variant = self.client.request("list_publish_drafts")["items"][0]["variants"][0]
        self.assertEqual(variant["title"], "小红书已编辑标题")
        self.assertEqual(variant["body"], "小红书已编辑正文")
        self.assertEqual(variant["topics"], ["春日内容", "小红书测试"])

    def test_schedule_publish_command_uses_beijing_time_and_rejects_past(self):
        xhs_account_id = self.scheduler.add_account(
            "小红书定时账号", bb_window_id="xhs-schedule-window", platform="xhs"
        )
        draft_id = self.client.request("create_publish_draft", {
            "title": "北京时间定时测试", "body": "只做队列模拟", "platforms": ["xhs"],
        })["draft_id"]
        future = (beijing_now() + timedelta(days=1)).replace(second=0)
        result = self.client.request("schedule_publish", {
            "draft_id": draft_id, "platform": "xhs", "account_id": xhs_account_id,
            "scheduled_at": future.strftime("%Y-%m-%d %H:%M"),
            "editor_title": "北京时间定时测试", "editor_body": "只做队列模拟",
            "editor_topics": "",
        })
        self.assertEqual(result["scheduled_at"], future.isoformat(timespec="seconds"))
        with self.assertRaises(BackendError):
            self.client.request("schedule_publish", {
                "draft_id": draft_id, "platform": "xhs", "account_id": xhs_account_id,
                "scheduled_at": "2020-01-01 00:00",
            })

    def test_scheduled_publish_runner_executes_due_authorized_job(self):
        account_id = self.scheduler.add_account(
            "定时执行账号", bb_window_id="scheduled-window", platform="xhs"
        )
        draft_id = self.client.request("create_publish_draft", {
            "title": "定时执行标题", "body": "定时执行正文", "platforms": ["xhs"],
        })["draft_id"]
        future = (beijing_now() + timedelta(hours=1)).replace(second=0)
        created = self.client.request("schedule_publish", {
            "draft_id": draft_id, "platform": "xhs", "account_id": account_id,
            "scheduled_at": future.strftime("%Y-%m-%d %H:%M"),
            "editor_title": "定时执行标题", "editor_body": "定时执行正文",
            "editor_topics": "", "real_send_authorized": True,
        })
        job_id = int(created["job_id"])
        self.assertTrue(created["real_send_authorized"])

        # 通过数据库把计划时间推进到过去，模拟“到点”，不等待真实时钟。
        due = (beijing_now() - timedelta(minutes=1)).isoformat(timespec="seconds")
        with self.service._lead_lock:
            conn = self.service._lead_connection()
            try:
                with conn:
                    conn.execute(
                        "UPDATE publish_jobs SET scheduled_at = ? WHERE id = ?",
                        (due, job_id),
                    )
            finally:
                conn.close()

        calls = []

        class FakePublishingBrowserAdapter:
            def __init__(self, _browser, on_log=None):
                self.on_log = on_log

            def real_publish(self, **kwargs):
                calls.append(kwargs)
                return {
                    "send_clicked": True,
                    "submission": {"clicked": True, "label": "发布"},
                    "message": "已发布",
                }

        self.scheduler.bb = object()
        with patch("publishing.browser.PublishingBrowserAdapter", FakePublishingBrowserAdapter):
            self.assertEqual(self.service.run_scheduled_publish_once(), 1)

        rows = self.client.request("list_publish_drafts")["items"]
        row = next(item for item in rows if int(item["id"]) == draft_id)
        self.assertEqual(row["latest_publish_job"]["status"], "published")
        self.assertEqual(row["latest_publish_job"]["current_step"], "completed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["title"], "定时执行标题")

    def test_send_interactions_marks_per_item_exception_as_failed(self):
        class FailingInteractionService:
            def __init__(self):
                self.failed = []

            def simulate_browser_reply(self, draft_id, account_id):
                raise RuntimeError("模拟定位失败")

            def mark_reply_failed(self, draft_id, message, account_id):
                self.failed.append((draft_id, message, account_id))

        fake = FailingInteractionService()
        original = self.service._interaction_service
        self.service._interaction_service = lambda _conn: fake
        try:
            result = self.service._send_interactions({
                "draft_ids": [21], "account_id": self.account_id,
                "real_send": False,
            })
        finally:
            self.service._interaction_service = original
        self.assertFalse(result["results"][0]["ok"])
        self.assertEqual(result["results"][0]["message"], "模拟定位失败")
        self.assertEqual(fake.failed, [(21, "模拟定位失败", self.account_id)])

    def test_template_save_apply_and_edit_are_persisted_through_backend(self):
        _task_id, lead_id = self._seed_lead()
        added = self.client.request("add_leads_to_interaction", {"lead_ids": [lead_id]})
        draft_id = added["added"][0]["draft_id"]
        with patch("config_loader.default_config_dir", return_value=self.temp.name):
            saved = self.client.request("save_template", {
                "template_id": "郑州首轮",
                "content": "您好{{昵称}}，欢迎咨询{{门店名称}}。",
                "custom_variables": [{"name": "门店名称", "value": "郑州体验店"}],
            })
            self.assertTrue(saved["saved"])
            self.assertIn({"name": "门店名称", "value": "郑州体验店"}, saved["custom_variables"])
            applied = self.client.request("interaction_action", {
                "draft_id": draft_id,
                "action": "apply_template",
                "template_id": "郑州首轮",
            })
            self.assertTrue(applied["ok"])
            self.client.request("interaction_action", {
                "draft_id": draft_id,
                "action": "update_content",
                "content": "人工修改后的完整正文",
            })
            result = self.client.request("list_interactions", {"status": "draft"})
        self.assertEqual(result["items"][0]["content"], "人工修改后的完整正文")
        template = next(item for item in result["templates"] if item["id"] == "郑州首轮")
        self.assertEqual(template["content"], "您好{{昵称}}，欢迎咨询{{门店名称}}。")
        self.assertEqual(template["label"], "郑州首轮")
        with patch("config_loader.default_config_dir", return_value=self.temp.name):
            cleared = self.client.request("save_template", {
                "template_id": "郑州首轮",
                # 删除自定义变量前，模板正文也应不再引用它；否则保存后
                # 模板会变成不可渲染状态，后端应正确拒绝这类配置。
                "content": "您好{{昵称}}，欢迎咨询。",
                "custom_variables": [],
            })
        self.assertEqual(cleared["custom_variables"], [])

    def test_diagnostics_snapshot_is_read_only_and_redacts_secrets(self):
        result = self.client.request("diagnostics_snapshot")
        self.assertEqual(
            [item["platform"] for item in result["accounts"]],
            ["douyin", "xhs", "bilibili", "weibo", "kuaishou"],
        )
        self.assertNotIn("api_key", result["llm_api"])
        self.assertNotIn("api_key", result["llm_api"])
        self.assertIn("logs", result)
        self.assertIn("log_stats", result)
        self.assertIn("normal", result["log_stats"])

    def test_leads_and_interactions_can_be_exported_by_task(self):
        task_id, lead_id = self._seed_lead()
        lead_export = self.client.request("export_leads_by_task", {
            "lead_ids": [lead_id],
        })
        self.assertEqual(lead_export["count"], 1)
        self.assertEqual(lead_export["task_count"], 1)
        self.assertTrue(os.path.exists(lead_export["paths"][0]))
        with open(lead_export["paths"][0], encoding="utf-8-sig", newline="") as stream:
            text = stream.read()
        self.assertIn("任务编号", text)
        self.assertIn(str(task_id), text)
        self.assertIn("后台线索的评论原文", text)

        added = self.client.request("add_leads_to_interaction", {"lead_ids": [lead_id]})
        draft_id = added["added"][0]["draft_id"]
        self.client.request("interaction_action", {
            "draft_id": draft_id, "action": "enter_send", "content": "完整回复正文",
        })
        conn = db.init_db(self.db_path, check_same_thread=False)
        try:
            conn.execute(
                "INSERT INTO interaction_events "
                "(lead_id, draft_id, event_type, result, detail, created_at) "
                "VALUES (?, ?, 'customer_reply', 'ok', '客户已回复', '2026-08-31T10:00:00')",
                (lead_id, draft_id),
            )
            conn.commit()
        finally:
            conn.close()
        interaction_export = self.client.request("export_interactions_by_task", {})
        self.assertEqual(interaction_export["count"], 1)
        self.assertTrue(os.path.exists(interaction_export["paths"][0]))
        with open(interaction_export["paths"][0], encoding="utf-8-sig", newline="") as stream:
            text = stream.read()
        self.assertIn("客户是否回复", text)
        self.assertIn("完整回复正文", text)
        self.assertIn("是", text)

    def test_platform_health_command_returns_five_platform_rows(self):
        result = self.client.request("run_platform_health")
        self.assertEqual(
            {item["platform"] for item in result["rows"]},
            {"douyin", "xhs", "bilibili", "weibo", "kuaishou"},
        )

    def test_publish_workspace_commands_keep_generation_and_drafts_separate(self):
        # 用例验证旧的本地模板兜底路径，不能读取开发机上的真实 LLM 配置。
        with patch("config_loader.default_config_dir", return_value=self.temp.name):
            generated = self.client.request("generate_content", {
                "keyword": "测试选题", "platform": "douyin",
            })
        self.assertGreater(generated["id"], 0)
        generated_rows = self.client.request("list_generated_contents", {})
        self.assertEqual(generated_rows["total"], 1)
        imported = self.client.request("import_generated_content", {
            "generated_id": generated["id"],
        })
        self.assertGreater(imported["draft_id"], 0)
        drafts = self.client.request("list_publish_drafts", {})
        self.assertEqual(drafts["total"], 1)

    def test_generate_content_uses_configured_llm_and_saves_structured_result(self):
        llm_result = {
            "healthy": True,
            "title": "智能生成标题",
            "body": "这是可以直接进入发布草稿的完整正文。",
            "topics": ["#测试选题", "#实用分享"],
            "outline": "开头钩子 · 主体信息 · 行动引导",
            "score": {"内容价值": 92, "风险等级": "低"},
        }
        with patch("config_loader.default_config_dir", return_value=self.temp.name), \
                patch("llm_api.LLMApiClient.generate_content", return_value=llm_result):
            config_path = os.path.join(self.temp.name, "app_config.json")
            with open(config_path, "w", encoding="utf-8") as stream:
                json.dump({"llm_api": {
                    "enabled": True, "base_url": "https://api.example.test/v1",
                    "model": "model-a", "api_key": "secret",
                }}, stream, ensure_ascii=False)
            generated = self.client.request("generate_content", {
                "keyword": "测试选题", "platform": "douyin",
            })
        self.assertEqual(generated["source_type"], "llm")
        self.assertEqual(generated["body"], llm_result["body"])
        self.assertEqual(generated["topics"], llm_result["topics"])
        self.assertEqual(generated["outline"], llm_result["outline"])
        self.assertEqual(generated["score"]["内容价值"], 92)

    def test_publish_workspace_account_contents_and_messages_are_safe_empty_reads(self):
        contents = self.client.request("list_account_contents", {
            "account_id": self.account_id, "platform": "douyin",
        })
        self.assertEqual(contents["total"], 0)
        synced = self.client.request("sync_account_contents", {
            "account_id": self.account_id, "platform": "douyin",
        })
        self.assertEqual(synced["synced"], 0)
        self.assertEqual(synced["sync_status"], "local_only")
        self.assertTrue(synced["read_only"])
        messages = self.client.request("list_published_messages", {
            "platform": "douyin", "unread_only": True,
        })
        self.assertEqual(messages["total"], 0)

    def test_live_profile_sync_does_not_seed_collected_or_recommendation_content(self):
        # 真实浏览器同步失败/返回空时，不能再回退到“作者=账号昵称”的采集缓存；
        # 否则抖音入口落到推荐页时，账号信息页会显示错误作品。
        self.scheduler.bb = object()
        task_id = self.scheduler.create_task("主页安全回退测试", platform="douyin")
        db.insert_video(
            self.scheduler.conn,
            task_id,
            "collected-video",
            "https://www.douyin.com/video/collected-video",
            title="采集缓存，不是主页作品",
            author="测试账号",
            platform="douyin",
        )
        fake_result = {
            "ok": True,
            "items": [],
            "profile_url": "https://www.douyin.com/recommend",
            "reader_version": "test-reader",
            "reason_code": "profile_route_not_reached",
            "diagnostics": {
                "page_url": "https://www.douyin.com/recommend",
                "scope_selector": '[data-e2e="user-post-list"]',
                "scope_found": False,
                "read_rounds": 20,
                "raw_unique": 0,
                "returned": 0,
            },
            "reason": "抖音账号主页导航未生效，已拒绝读取推荐页内容",
        }
        with patch(
            "publishing.account_content_reader.read_account_contents",
            return_value=fake_result,
        ):
            synced = self.client.request("sync_account_contents", {
                "account_id": self.account_id,
                "platform": "douyin",
            })
        self.assertEqual(synced["sync_status"], "failed")
        self.assertEqual(synced["sync_source"], "profile_sync")
        self.assertEqual(synced["total"], 0)
        self.assertIn("推荐页", synced["sync_error"])
        self.assertEqual(synced["reader_version"], "test-reader")
        self.assertEqual(synced["sync_reason_code"], "profile_route_not_reached")

    def test_sync_published_messages_reads_account_scope_and_upserts_full_batch(self):
        # 只替换测试用的 scheduler 浏览器对象，并 mock 只读适配器；不触碰真实
        # BitBrowser，也不发送任何消息。
        self.scheduler.bb = object()
        payload = {
            "ok": True,
            "items": [
                {"message_id": "m-reply", "message_type": "reply",
                 "nickname": "访客", "content": "回复内容", "can_reply": True,
                 "source_url": "https://www.douyin.com/video/1",
                 "extra": {"quote_content": "原评论", "event_time": "刚刚"}},
                {"message_id": "m-like", "message_type": "like",
                 "nickname": "点赞者", "content": "", "can_reply": False},
            ],
            "categories": ["interaction", "private"], "errors": [],
        }
        with patch("publishing.message_reader.read_account_messages", return_value=payload):
            result = self.client.request("sync_published_messages", {
                "account_id": self.account_id, "platform": "douyin", "limit": 500,
            })
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["inserted"], 2)
        messages = self.client.request("list_published_messages", {
            "account_id": self.account_id, "platform": "douyin",
        })
        self.assertEqual(messages["total"], 2)
        self.assertEqual({row["message_type"] for row in messages["items"]}, {"reply", "like"})
        reply = next(row for row in messages["items"] if row["message_type"] == "reply")
        self.assertEqual(reply["quote_content"], "原评论")

    def test_message_center_reply_opens_bound_browser_without_sending(self):
        class FakeBitBrowser:
            def __init__(self):
                self.opened = []

            def open_browser(self, window_id, **kwargs):
                self.opened.append((window_id, kwargs))
                return {"ws": "ws://127.0.0.1:9222/devtools/browser/message-test"}

        class FakeCdpSession:
            navigated = []

            def __init__(self, _ws_url, timeout=30.0):
                self.timeout = timeout

            async def connect(self):
                return self

            async def attach_page(self):
                return "message-page"

            async def navigate(self, url, _session_id, wait_load=True, timeout=25.0):
                self.navigated.append((url, wait_load, timeout))

            async def close(self):
                return None

        fake_browser = FakeBitBrowser()
        self.scheduler.bb = fake_browser
        payload = {
            "ok": True,
            "items": [{
                "message_id": "m-reply-open",
                "message_type": "reply",
                "nickname": "访客",
                "content": "我想了解一下",
                "can_reply": True,
                "source_url": "https://www.douyin.com/video/reply-open",
            }],
            "categories": ["interaction"], "errors": [],
        }
        with patch("publishing.message_reader.read_account_messages", return_value=payload):
            self.client.request("sync_published_messages", {
                "account_id": self.account_id, "platform": "douyin",
            })
        messages = self.client.request("list_published_messages", {
            "account_id": self.account_id, "platform": "douyin",
        })
        message_id = int(messages["items"][0]["id"])
        # 仅测试服务编排，不连接真实 CDP。测试运行时可能未安装
        # websockets，因此先提供一个占位模块再替换 CdpSession。
        with patch.dict(sys.modules, {"websockets": object()}):
            with patch("cdp.CdpSession", FakeCdpSession):
                result = self.client.request("open_published_message_browser", {
                    "message_id": message_id,
                })
        self.assertTrue(result["opened"])
        self.assertTrue(result["manual_only"])
        self.assertEqual(fake_browser.opened[0][0], "demo-test")
        self.assertEqual(
            fake_browser.opened[0][1]["new_page_url"],
            "https://www.douyin.com/video/reply-open",
        )
        self.assertEqual(
            FakeCdpSession.navigated[-1][0],
            "https://www.douyin.com/video/reply-open",
        )

    def test_real_publish_requires_explicit_second_confirmation(self):
        with self.assertRaises(ValueError):
            self.service._real_publish_draft({
                "draft_id": 1, "account_id": self.account_id,
                "platform": "douyin", "confirm_real_publish": False,
            })


if __name__ == "__main__":
    unittest.main()
