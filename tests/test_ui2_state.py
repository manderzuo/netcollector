# -*- coding: utf-8 -*-
"""2.0 UI 状态模型测试，不要求安装 Qt。"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from ui2.bridge import Ui2Bridge  # noqa: E402
from ui2.state import Ui2State  # noqa: E402


class _FakeBackendClient:
    def __init__(self):
        self.event_callback = None
        self.connected = False
        self.request_count = 0
        self.last_timeout = None

    def connect(self):
        time.sleep(0.05)
        self.connected = True

    def request(self, command, args=None, **kwargs):
        self.request_count += 1
        self.last_timeout = kwargs.get("timeout")
        time.sleep(0.05)
        return {"tasks": {}, "accounts": {}, "totals": {}}

    def close(self):
        self.connected = False


class TestUi2State(unittest.TestCase):
    def test_page_selection_and_snapshot_view_model(self):
        state = Ui2State()
        self.assertEqual(state.page_index, 0)
        state.select_page("leads")
        self.assertEqual(state.page_label, "线索中心")
        with self.assertRaises(ValueError):
            state.select_page("unknown")

        state.apply_snapshot({
            "tasks": {
                "7": {
                    "id": 7,
                    "keyword": "测试关键词",
                    "platform": "xhs",
                    "status": "phase_b_comments",
                    "target_count": 100,
                    "video_done": 25,
                    "comments": 18,
                }
            },
            "totals": {"tasks": 1, "videos_total": 40, "videos_done": 25, "comments": 18},
        })
        row = state.task_rows()[0]
        self.assertEqual(row["platform_label"], "小红书")
        self.assertEqual(row["status_label"], "评论采集中")
        self.assertEqual(row["progress"], 25)
        self.assertEqual(state.connection, "connected")
        self.assertEqual(state.overview()["comments"], 18)

    def test_account_rows_expose_platform_binding_and_status_in_chinese(self):
        state = Ui2State()
        state.apply_snapshot({
            "accounts": {
                "douyin:14": {
                    "id": 14,
                    "name": "12345678a",
                    "nickname": "抖音真实昵称",
                    "platform": "douyin",
                    "status": "waiting_human",
                    "bb_window_id": "window-14",
                    "cooldown_left_seconds": 2.8,
                    "processed_count": 12,
                    "batch_count": 3,
                },
                "xhs:8": {
                    "id": 8,
                    "name": "小红书账号",
                    "platform": "xhs",
                    "status": "idle",
                },
            }
        })
        rows = state.account_rows()
        self.assertEqual({row["platform_label"] for row in rows}, {"抖音", "小红书"})
        douyin = next(row for row in rows if row["id"] == 14)
        xhs = next(row for row in rows if row["id"] == 8)
        self.assertEqual(douyin["status_label"], "待人工验证")
        self.assertEqual(douyin["binding_label"], "已绑定窗口")
        self.assertEqual(douyin["cooldown_left_seconds"], 2)
        self.assertEqual(douyin["name"], "抖音真实昵称")
        self.assertEqual(douyin["account_key"], "12345678a")
        self.assertEqual(xhs["binding_label"], "未绑定窗口")

    def test_numeric_account_key_does_not_render_as_nickname(self):
        state = Ui2State()
        state.apply_snapshot({
            "accounts": {"douyin:16": {
                "id": 16, "name": "1234567899", "platform": "douyin",
                "status": "idle", "nickname": "", "nickname_resolved": False,
            }}
        })
        row = state.account_rows()[0]
        self.assertEqual(row["nickname"], "未读取昵称")
        self.assertEqual(row["name"], "未读取昵称")
        self.assertEqual(row["account_key"], "1234567899")
        self.assertFalse(row["nickname_resolved"])

    def test_alphanumeric_account_key_does_not_render_as_nickname(self):
        state = Ui2State()
        state.apply_snapshot({
            "accounts": {"douyin:14": {
                "id": 14, "name": "12345678a", "nickname": "12345678a",
                "nickname_resolved": False, "platform": "douyin",
                "status": "idle", "bb_window_id": "8f25390d00ab4325a53af73840a6e41b",
            }}
        })
        row = state.account_rows()[0]
        self.assertEqual(row["nickname"], "未读取昵称")
        self.assertEqual(row["name"], "未读取昵称")
        self.assertFalse(row["nickname_resolved"])

    def test_explicit_platform_nickname_is_rendered_even_if_it_looks_like_an_id(self):
        state = Ui2State()
        state.apply_snapshot({
            "accounts": {"douyin:16": {
                "id": 16, "name": "1234567899",
                "nickname": "抖音号：dyu431gedsv7",
                "nickname_resolved": True, "platform": "douyin",
                "status": "idle", "bb_window_id": "65b60ce115d24f56a85cbcc5b48ac9ca",
            }}
        })
        row = state.account_rows()[0]
        self.assertEqual(row["nickname"], "抖音号：dyu431gedsv7")
        self.assertEqual(row["name"], "抖音号：dyu431gedsv7")
        self.assertTrue(row["nickname_resolved"])

    def test_task_rows_expose_account_times_error_and_valid_counts(self):
        state = Ui2State()
        state.apply_snapshot({
            "tasks": {"4": {
                "id": 4, "keyword": "洗鞋", "platform": "douyin",
                "status": "exception", "error_message": "浏览器连接超时",
                "task_accounts": '["12345678a"]',
                "latest_run": {"started_at": "2026-09-03 09:00:00",
                                "finished_at": "2026-09-03 09:01:00"},
                "target_count": 10, "video_done": 5,
                "valid_video_done": 3, "comments": 8, "valid_comments": 2,
                "lead_count": 1,
            }},
            "accounts": {"douyin:14": {"id": 14, "name": "12345678a",
                                         "nickname": "测试昵称", "platform": "douyin"}},
        })
        row = state.task_rows()[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["status_label"], "失败")
        self.assertEqual(row["error_reason"], "浏览器连接超时")
        self.assertEqual(row["start_at"], "2026-09-03 09:00:00")
        self.assertEqual(row["end_at"], "2026-09-03 09:01:00")
        self.assertEqual(row["account_label"], "12345678a")
        self.assertEqual(row["valid_video_done"], 3)
        self.assertEqual(row["progress"], 30)
        account = state.account_rows()[0]
        self.assertEqual(account["name"], "测试昵称")

    def test_lead_rows_expose_comment_and_filter_labels_in_chinese(self):
        state = Ui2State()
        state.apply_leads({
            "items": [{
                "id": 9,
                "platform": "xhs",
                "nickname": "线索用户",
                "comment": "评论原文不能被简述替换",
                "comment_time": "2026-08-27 15:00",
                "source_url": "https://example.test/note/9",
                "region_province": "河南",
                "intent_level": "medium",
                "status": "draft_ready",
            }],
            "total": 1,
            "page": 1,
            "page_size": 50,
            "tasks": [{"id": 3, "keyword": "测试词", "platform": "xhs"}],
            "provinces": ["河南"],
        })
        row = state.lead_rows()[0]
        self.assertEqual(row["platform_label"], "小红书")
        self.assertEqual(row["intent_label"], "中")
        self.assertEqual(row["status_label"], "待生成")
        self.assertEqual(row["source_url_label"], "点击查看")
        self.assertEqual(row["comment"], "评论原文不能被简述替换")
        self.assertEqual(state.lead_task_options()[1]["label"], "任务#3 · 小红书 · 测试词")
        self.assertEqual(state.lead_province_options(), ["全部地区", "河南"])

    def test_busy_account_sync_does_not_clear_visible_contents(self):
        state = Ui2State()
        state.apply_account_contents({
            "account_id": 14,
            "platform": "douyin",
            "items": [{"id": 1, "content_id": "own-1", "title": "当前作品"}],
            "total": 1,
            "page": 1,
            "pages": 1,
            "sync_status": "success",
        })
        state.apply_account_contents({
            "account_id": 14,
            "platform": "douyin",
            "items": [],
            "total": 0,
            "page": 1,
            "pages": 0,
            "sync_source": "busy",
            "sync_status": "busy",
            "sync_error": "同步正在进行",
        })
        view = state.to_view_model()["publishing"]
        self.assertEqual(view["account_contents"][0]["content_id"], "own-1")
        self.assertEqual(view["account_content_sync_status"], "busy")

    def test_interaction_rows_keep_original_and_full_reply_content(self):
        state = Ui2State()
        state.apply_interactions({
            "status": "failed",
            "items": [{
                "draft_id": 12,
                "platform": "weibo",
                "nickname": "互动用户",
                "original_comment": "这是用户评论的原文",
                "content": "这是完整的回复话术正文，不是简述",
                "status": "failed",
                "failure_reason": "未找到回复输入框",
            }],
            "total": 1,
            "page": 1,
            "page_size": 50,
            "pages": 1,
            "accounts": [{"id": 5, "name": "微博账号", "platform": "weibo"}],
            "templates": [{"id": "custom", "label": "自定义", "content": "完整模板内容"}],
        })
        row = state.interaction_rows()[0]
        self.assertEqual(row["platform_label"], "微博")
        self.assertEqual(row["status_label"], "失败")
        self.assertEqual(row["original_comment"], "这是用户评论的原文")
        self.assertEqual(row["content"], "这是完整的回复话术正文，不是简述")
        self.assertEqual(row["failure_reason"], "未找到回复输入框")
        self.assertEqual(state.interaction_account_options()[0]["label"], "微博 · 微博账号")

    def test_diagnostics_rows_translate_status_and_keep_safe_summary(self):
        state = Ui2State()
        state.apply_diagnostics({
            "checked_at": "2026-08-27T16:00:00+08:00",
            "bitbrowser": {"configured": True, "timeout": 20},
            "llm_api": {"enabled": True, "provider": "本地服务", "model": "模型", "configured": True},
            "accounts": [{"platform": "douyin", "platform_label": "抖音", "accounts": 2, "bound_accounts": 1, "waiting_human": 0}],
            "health": [{"platform": "xhs", "status": "ok", "detail": "适配器正常"}],
            "logs": [{"timestamp": "2026-08-27T16:00:00+08:00", "message": "检查完成"}],
        })
        self.assertEqual(state.diagnostic_health_rows()[0]["platform_label"], "小红书")
        self.assertEqual(state.diagnostic_health_rows()[0]["status_label"], "正常")
        self.assertEqual(state.diagnostic_account_rows()[0]["bound_accounts"], 1)
        state.apply_bitbrowser_inspection({
            "healthy": True,
            "checks": [{"name": "本地服务", "status": "正常"}],
            "windows": [{"name": "窗口A", "port": "9222"}],
        })
        state.apply_log_export({"path": "data/logs/exports/log.txt", "count": 3})
        view = state.to_view_model()
        self.assertEqual(view["diagnostics"]["bitbrowser_inspection"]["windows"][0]["port"], "9222")
        self.assertEqual(view["diagnostics"]["export_path"], "data/logs/exports/log.txt")

    def test_live_diagnostic_rows_are_normalized_for_the_ui(self):
        state = Ui2State()
        state.apply_live_health({
            "checked_at": "2026-08-27T16:00:00+08:00",
            "rows": [{
                "platform": "douyin", "status": "human_required",
                "detail": "疑似出现人工验证", "account_details": [
                    {"account": "账号A", "status": "login_required"},
                ],
            }],
            "send_executed": False,
        })
        rows = state.diagnostic_live_health_rows()
        self.assertEqual(rows[0]["platform_label"], "抖音")
        self.assertEqual(rows[0]["status_label"], "需人工")
        self.assertEqual(rows[0]["account_details"][0]["status_label"], "需登录")

    def test_reset_user_scoped_clears_business_views_but_keeps_logs(self):
        state = Ui2State()
        state.apply_snapshot({
            "tasks": {"7": {"id": 7, "keyword": "甲的任务"}},
            "accounts": {"douyin:14": {"id": 14, "name": "甲的账号"}},
            "totals": {"tasks": 1, "accounts": 1},
        })
        state.apply_leads({
            "items": [{"id": 9, "nickname": "甲用户"}], "total": 1,
        })
        state.apply_interactions({
            "items": [{"draft_id": 12, "nickname": "甲用户"}], "total": 1,
        })
        state.append_log_event({"timestamp": "2026-09-02T15:00:00+08:00",
                                "message": "保留审计日志", "level": "info"})

        state.reset_user_scoped()
        view = state.to_view_model()
        self.assertEqual(view["tasks"], [])
        self.assertEqual(view["accounts"], [])
        self.assertEqual(view["leads"], [])
        self.assertEqual(view["interactions"], [])
        self.assertEqual(view["publishing"]["items"], [])
        self.assertEqual(view["diagnostics"]["logs"][-1]["message"], "保留审计日志")

    def test_bridge_auth_scope_resets_state_and_increments_generation(self):
        bridge = Ui2Bridge()
        bridge.state.apply_leads({
            "items": [{"id": 1, "nickname": "上一账号"}], "total": 1,
        })
        bridge.state.apply_interactions({
            "items": [{"draft_id": 1, "nickname": "上一账号"}], "total": 1,
        })
        first = bridge.session_generation()

        bridge.set_auth_scope({"id": 2, "role": "employee"})

        self.assertEqual(bridge.session_generation(), first + 1)
        self.assertEqual(bridge.state.lead_rows(), [])
        self.assertEqual(bridge.state.interaction_rows(), [])
        self.assertEqual(bridge.state.task_rows(), [])
        self.assertEqual(bridge.state.account_rows(), [])

    def test_bridge_ignores_snapshot_from_previous_auth_scope(self):
        bridge = Ui2Bridge()
        bridge.set_auth_scope({"id": 2, "role": "employee"})

        bridge._on_backend_event({
            "event": "state_snapshot",
            "payload": {
                "_auth_scope": {"user_id": 1, "role": "admin"},
                "tasks": {"1": {"id": 1, "keyword": "管理员任务"}},
            },
        })
        self.assertEqual(bridge.state.task_rows(), [])

        bridge._on_backend_event({
            "event": "state_snapshot",
            "payload": {
                "_auth_scope": {"user_id": 2, "role": "employee"},
                "tasks": {"2": {"id": 2, "keyword": "员工任务"}},
            },
        })
        self.assertEqual([row["id"] for row in bridge.state.task_rows()], [2])

    def test_bridge_without_backend_is_safe_and_explicit(self):
        bridge = Ui2Bridge()
        view = bridge.connect()
        self.assertEqual(view["connection"], "error")
        with self.assertRaises(RuntimeError):
            bridge.command("status")

    def test_bridge_forwards_long_running_command_timeout(self):
        client = _FakeBackendClient()
        bridge = Ui2Bridge(client)
        bridge.command("send_interactions", {"draft_ids": [1]}, timeout=180.0)
        self.assertEqual(client.last_timeout, 180.0)

    def test_qml_shell_contains_all_first_migration_pages(self):
        path = os.path.join(ROOT, "src", "ui2", "qml", "main.qml")
        with open(path, encoding="utf-8") as stream:
            qml = stream.read()
        for label in ("采集总览", "任务中心", "账号管理", "线索中心", "互动中心", "诊断与设置"):
            self.assertIn(label, qml)
        self.assertIn("backend.selectPage", qml)
        self.assertIn("backend.refresh", qml)
        self.assertIn("backend.resumeTask", qml)
        self.assertIn('text: window.taskActionText(modelData)', qml)
        self.assertIn('property bool taskPositionSaved: false', qml)
        self.assertIn('function requestDelete(task)', qml)
        self.assertIn('text: tasksPage.deletingTaskId === Number(modelData.id) ? "删除中…" : "删除"', qml)
        self.assertIn('id: deleteConfirmDialog', qml)
        self.assertIn('function onCommandFinished(command, ok, message)', qml)
        self.assertIn("backend.stopTask", qml)
        self.assertIn("backend.accountRows", qml)
        self.assertIn("backend.diagnosticBitBrowserWindows", qml)
        self.assertIn("function accountListRows()", qml)
        self.assertIn("function openForWindow(row)", qml)
        self.assertIn("backend.createBrowserWindow", qml)
        self.assertIn("backend.deleteBrowserWindow", qml)
        self.assertIn("function requestDeleteWindow(row)", qml)
        self.assertIn("id: browserWindowDeleteDialog", qml)
        self.assertIn("property string lastAutoInspectedPage", qml)
        self.assertIn("id: authHero", qml)
        self.assertIn("统一管理采集、互动与发布", qml)
        self.assertIn("text: \"欢迎回来\"", qml)
        self.assertIn("text: \"记住登录状态\"", qml)
        self.assertIn("注册申请", qml)
        self.assertIn("服务连接正常", qml)
        self.assertIn("page !== window.lastAutoInspectedPage", qml)
        self.assertIn("backend.leadRows", qml)
        self.assertIn("backend.refreshLeads", qml)
        self.assertIn("id: leadKeywordFilterField", qml)
        self.assertIn("关键词筛选：昵称、评论、词组", qml)
        self.assertIn("text: \"关键词管理\"", qml)
        self.assertIn("keywordGroupDialog.openForManage()", qml)
        self.assertIn("backend.updateKeywordGroup", qml)
        self.assertIn("backend.addLeadsToInteraction", qml)
        self.assertIn("backend.addLeadsToPrivateMessage", qml)
        self.assertIn("backend.interactionRows", qml)
        self.assertIn("backend.refreshInteractions", qml)
        self.assertIn("backend.sendInteractions", qml)
        self.assertIn("property string activeType: \"comment_reply\"", qml)
        self.assertIn("发私信", qml)
        self.assertIn("private_message", qml)
        self.assertIn("完整回复话术", qml)
        self.assertIn("backend.refreshDiagnostics", qml)
        self.assertIn("backend.runPlatformHealth", qml)
        self.assertIn("运行五平台检查", qml)
        self.assertIn("五平台健康状态", qml)
        self.assertIn("backend.saveSettings", qml)
        self.assertIn("智能回复与评论分析 API", qml)
        self.assertIn("publishApiProviderField", qml)
        self.assertIn("publishApiUrlField", qml)
        self.assertIn("publishApiModelField", qml)
        self.assertIn("publishApiKeyField", qml)
        self.assertIn("backend.testLlmApi()", qml)
        self.assertIn("backend.inspectBitBrowser", qml)
        self.assertIn("backend.runLiveDiagnostics", qml)
        self.assertIn("backend.exportOperationLog", qml)
        self.assertIn('id: personalCenterButton', qml)
        self.assertIn('onClicked: personalCenterDialog.open()', qml)
        self.assertIn('id: personalCenterDialog', qml)
        self.assertIn('backend.logoutUser()', qml)
        self.assertIn('text: "立即上传"', qml)
        self.assertIn('text: "用户审批"', qml)
        self.assertIn('text: "管理员中心"', qml)
        self.assertIn("maximumLineCount: 2", qml)
        self.assertIn("property real textBlockHeight", qml)
        self.assertIn("Flow {", qml)
        self.assertIn("Layout.preferredWidth: 300", qml)
        self.assertIn("function selectedAccountIsValid()", qml)
        self.assertIn("function resetPublishAccountForPlatform()", qml)
        self.assertIn("publishPage.resetPublishAccountForPlatform()", qml)
        self.assertIn("publishPage.selectedAccountIsValid()", qml)
        self.assertIn("function realPublishAllowed()", qml)
        self.assertIn("function realPublishDisabledReason()", qml)
        self.assertIn("import QtMultimedia", qml)
        self.assertIn("previewAssetUrl()", qml)
        self.assertIn("previewVideoPlayer", qml)
        self.assertIn("publishPage.editorTitle, publishPage.editorBody", qml)
        self.assertIn("publishPage.editorTopics)", qml)
        self.assertIn("id: schedulePickerDialog", qml)
        self.assertIn("id: schedulePickerHourChooser", qml)
        self.assertIn("id: schedulePickerMinuteChooser", qml)
        self.assertIn("function scheduleDisplayText()", qml)
        self.assertIn("function openSchedulePicker()", qml)
        self.assertIn("Overlay.modal: Rectangle", qml)
        self.assertIn("function scheduleSelectionIsFuture()", qml)
        self.assertIn("backend.beijingNowText", qml)
        self.assertNotIn("id: publishScheduledAt", qml)

    def test_async_bridge_does_not_block_and_coalesces_refresh(self):
        client = _FakeBackendClient()
        bridge = Ui2Bridge(client)
        connected = threading.Event()
        started = time.monotonic()
        bridge.connect_async(lambda _view: connected.set())
        self.assertLess(time.monotonic() - started, 0.04)
        self.assertTrue(connected.wait(1.0))

        before = client.request_count
        bridge.refresh_async()
        bridge.refresh_async()
        time.sleep(0.2)
        self.assertEqual(client.request_count - before, 1)
        bridge.close()


if __name__ == "__main__":
    unittest.main()
