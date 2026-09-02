# -*- coding: utf-8 -*-
"""独立的 2.0 QML 界面预览入口。

默认生产入口是 ``src/ui2/default_app.py``，它会同时启动本地后台服务；本文件
保留为无后台预览入口，方便只验收视觉和交互。未安装 PySide6 时给出明确提示。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

try:
    from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
except ImportError as exc:  # pragma: no cover - 取决于 v2 额外依赖
    raise SystemExit(
        "2.0 界面需要 PySide6，请先安装 requirements-v2.txt；1.2 旧界面不受影响。"
    ) from exc

try:
    from .bridge import Ui2Bridge  # type: ignore
    from ..backend_protocol import read_endpoint  # type: ignore
except ImportError:  # pragma: no cover - python src/ui2/qml_app.py
    HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from ui2.bridge import Ui2Bridge  # type: ignore
    from backend_protocol import read_endpoint  # type: ignore

try:
    from ..time_utils import beijing_now  # type: ignore
except ImportError:  # pragma: no cover - python src/ui2/qml_app.py
    from time_utils import beijing_now  # type: ignore


class QmlBridge(QObject):
    viewChanged = Signal()
    keywordGroupsChanged = Signal()
    authChanged = Signal()
    backendEvent = Signal(object)
    backendLogEvent = Signal(object)
    authEvent = Signal(object)
    syncChanged = Signal()
    adminChanged = Signal()
    logsChanged = Signal()
    beijingNowTextChanged = Signal()
    # 让 QML 在耗时命令期间立即锁定危险按钮，并在失败时恢复。
    commandFinished = Signal(str, bool, str)

    def __init__(self, bridge: Ui2Bridge):
        super().__init__()
        self._bridge = bridge
        self._keyword_groups: list[dict] = []
        self._view: dict = bridge.state.to_view_model()
        self._auth: dict = {
            "authenticated": False,
            "user": None,
            "message": "",
            "users": [],
        }
        self._sync: dict = {
            "enabled": False, "server_url": "", "device_name": "",
            "api_token_configured": False, "last_sync_at": None,
            "status": "idle", "last_error": "", "pending": 0,
        }
        self._admin: dict = {
            "summary": {}, "employees": [], "devices": [], "backups": [],
            "audit": {"items": [], "total": 0, "counts": {}},
        }
        # 定时发布的可选项和按钮状态需要随着北京时间推进而更新；只发送
        # 一个轻量信号，不触发后台快照或整页刷新。
        self._beijing_clock_timer = QTimer(self)
        # 选择器按分钟粒度工作，30 秒刷新一次足够及时，也不会给常驻的
        # QML 页面引入每秒一次的模型重算和重绘。
        self._beijing_clock_timer.setInterval(30000)
        self._beijing_clock_timer.timeout.connect(self.beijingNowTextChanged)
        self._beijing_clock_timer.start()
        bridge.add_listener(self._on_view)
        bridge.add_log_listener(self._on_log)
        self.backendEvent.connect(self._apply_view)
        self.backendLogEvent.connect(self._apply_log)
        self.authEvent.connect(self._apply_auth_event)

    def _on_view(self, view: dict) -> None:
        # BackendClient 的 reader 线程只发信号；实际属性更新回到 Qt 线程。
        self.backendEvent.emit(view)

    def _on_log(self, payload: dict) -> None:
        self.backendLogEvent.emit(payload)

    @Slot(object)
    def _apply_view(self, view: dict) -> None:
        self._view = dict(view or {})
        self.viewChanged.emit()
        self.logsChanged.emit()

    @Slot(object)
    def _apply_log(self, _payload: dict) -> None:
        # 日志只更新日志列表，不触发整棵页面重新布局，避免后台采集时界面抖动。
        self.logsChanged.emit()

    @Slot(object)
    def _apply_auth_event(self, event: dict) -> None:
        event = dict(event or {})
        kind = str(event.get("kind") or "")
        if kind == "login":
            user = dict(event.get("user") or {})
            self._bridge.set_auth_scope(user)
            self._keyword_groups = []
            self.keywordGroupsChanged.emit()
            self._auth.update({
                "authenticated": True,
                "user": user,
                "message": "",
            })
            self.authChanged.emit()
            self._bridge.refresh_async()
            self._bridge.refresh_leads_async()
            self._bridge.refresh_interactions_async()
            self.refreshKeywordGroups()
            self.refreshSyncStatus()
            if str(user.get("role") or "") == "admin":
                self.refreshAdminDashboard()
            return
        if kind == "logout":
            self._bridge.set_auth_scope(None)
            self._keyword_groups = []
            self.keywordGroupsChanged.emit()
            self._auth.update({"authenticated": False, "user": None, "message": ""})
            self._auth["users"] = []
            self._sync = {"enabled": False, "server_url": "", "device_name": "",
                          "api_token_configured": False, "last_sync_at": None,
                          "status": "idle", "last_error": "", "pending": 0}
            self._admin = {"summary": {}, "employees": [], "devices": [], "backups": [],
                           "audit": {"items": [], "total": 0, "counts": {}}}
            self.authChanged.emit()
            self.syncChanged.emit()
            self.adminChanged.emit()
            return
        if kind == "register":
            self._auth["message"] = "注册申请已提交，请等待管理员审批"
            self.authChanged.emit()
            return
        if kind == "users":
            self._auth["users"] = list(event.get("users") or [])
            self.authChanged.emit()
            return
        if kind == "error":
            self._auth["message"] = str(event.get("message") or "操作失败")
            self.authChanged.emit()

    @staticmethod
    def _error_message(exc: Exception) -> str:
        return str(getattr(exc, "message", "") or str(exc))

    @Property(str, notify=viewChanged)
    def currentPage(self) -> str:
        return str(self._view.get("page") or "overview")

    @Property(str, notify=beijingNowTextChanged)
    def beijingNowText(self) -> str:
        """当前北京时间，供定时发布选择器显示和过滤过去时间。"""

        return beijing_now().strftime("%Y-%m-%d %H:%M:%S")

    @Property(str, notify=viewChanged)
    def pageLabel(self) -> str:
        return str(self._view.get("page_label") or "采集总览")

    @Property(int, notify=viewChanged)
    def pageIndex(self) -> int:
        return int(self._view.get("page_index") or 0)

    @Property(str, notify=viewChanged)
    def connectionText(self) -> str:
        status = self._view.get("connection")
        if status == "connected":
            return "后台服务已连接"
        if status == "error":
            return "后台服务连接异常"
        return "等待后台服务"

    @Property(str, notify=viewChanged)
    def lastError(self) -> str:
        return str(self._view.get("last_error") or "")

    @Property(str, notify=viewChanged)
    def snapshotJson(self) -> str:
        return json.dumps(self._view.get("overview") or {}, ensure_ascii=False)

    @Property("QVariant", notify=viewChanged)
    def taskRows(self):
        return list(self._view.get("tasks") or [])

    @Property("QVariant", notify=viewChanged)
    def accountRows(self):
        return list(self._view.get("accounts") or [])

    @Property("QVariant", notify=keywordGroupsChanged)
    def keywordGroups(self):
        return list(self._keyword_groups)

    @Property("QVariant", notify=viewChanged)
    def leadRows(self):
        return list(self._view.get("leads") or [])

    @Property(int, notify=viewChanged)
    def leadTotal(self) -> int:
        return int((self._view.get("lead_meta") or {}).get("total") or 0)

    @Property(int, notify=viewChanged)
    def leadPage(self) -> int:
        return int((self._view.get("lead_meta") or {}).get("page") or 1)

    @Property(int, notify=viewChanged)
    def leadPages(self) -> int:
        return int((self._view.get("lead_meta") or {}).get("pages") or 0)

    @Property("QVariant", notify=viewChanged)
    def leadTaskOptions(self):
        return list((self._view.get("lead_meta") or {}).get("tasks") or [])

    @Property("QVariant", notify=viewChanged)
    def leadProvinceOptions(self):
        return list((self._view.get("lead_meta") or {}).get("provinces") or [])

    @Property("QVariant", notify=viewChanged)
    def leadStats(self):
        return dict((self._view.get("lead_meta") or {}).get("stats") or {})

    @Property("QVariant", notify=viewChanged)
    def interactionRows(self):
        return list(self._view.get("interactions") or [])

    @Property(int, notify=viewChanged)
    def interactionTotal(self) -> int:
        return int((self._view.get("interaction_meta") or {}).get("total") or 0)

    @Property(int, notify=viewChanged)
    def interactionPage(self) -> int:
        return int((self._view.get("interaction_meta") or {}).get("page") or 1)

    @Property(int, notify=viewChanged)
    def interactionPages(self) -> int:
        return int((self._view.get("interaction_meta") or {}).get("pages") or 0)

    @Property(str, notify=viewChanged)
    def interactionStatus(self) -> str:
        """当前列表数据对应的后台状态，用于阻止旧请求结果串到新页签。"""
        return str((self._view.get("interaction_meta") or {}).get("status") or "draft")

    @Property(str, notify=viewChanged)
    def interactionType(self) -> str:
        """当前互动列表对应的互动类型，避免评论/私信请求串页。"""
        return str(
            (self._view.get("interaction_meta") or {}).get(
                "interaction_type", "comment_reply"
            )
        )

    @Property("QVariant", notify=viewChanged)
    def interactionAccounts(self):
        return list((self._view.get("interaction_meta") or {}).get("accounts") or [])

    @Property("QVariant", notify=viewChanged)
    def interactionTemplates(self):
        return list((self._view.get("interaction_meta") or {}).get("templates") or [])

    @Property("QVariant", notify=viewChanged)
    def interactionCustomVariables(self):
        return list((self._view.get("interaction_meta") or {}).get("custom_variables") or [])

    @Property("QVariant", notify=viewChanged)
    def publishDraftRows(self):
        return list((self._view.get("publishing") or {}).get("items") or [])

    @Property(int, notify=viewChanged)
    def publishTotal(self) -> int:
        return int((self._view.get("publishing") or {}).get("total") or 0)

    @Property(int, notify=viewChanged)
    def publishPage(self) -> int:
        return int((self._view.get("publishing") or {}).get("page") or 1)

    @Property(int, notify=viewChanged)
    def publishPages(self) -> int:
        return int((self._view.get("publishing") or {}).get("pages") or 0)

    @Property("QVariant", notify=viewChanged)
    def publishStatusOptions(self):
        return list((self._view.get("publishing") or {}).get("status_options") or [])

    @Property("QVariant", notify=viewChanged)
    def publishPlatformOptions(self):
        return list((self._view.get("publishing") or {}).get("platform_options") or [])

    @Property("QVariant", notify=viewChanged)
    def publishAccountContentRows(self):
        return list((self._view.get("publishing") or {}).get("account_contents") or [])

    @Property(int, notify=viewChanged)
    def publishAccountContentTotal(self) -> int:
        return int((self._view.get("publishing") or {}).get("account_content_total") or 0)

    @Property(str, notify=viewChanged)
    def publishAccountContentSyncSource(self) -> str:
        return str((self._view.get("publishing") or {}).get("account_content_sync_source") or "")

    @Property(str, notify=viewChanged)
    def publishAccountContentSyncStatus(self) -> str:
        return str((self._view.get("publishing") or {}).get("account_content_sync_status") or "")

    @Property(str, notify=viewChanged)
    def publishAccountContentSyncError(self) -> str:
        return str((self._view.get("publishing") or {}).get("account_content_sync_error") or "")

    @Property(str, notify=viewChanged)
    def publishAccountContentProfileUrl(self) -> str:
        return str((self._view.get("publishing") or {}).get("account_content_profile_url") or "")

    @Property("QVariant", notify=viewChanged)
    def publishAccountContentComments(self):
        return list((self._view.get("publishing") or {}).get("account_content_comments") or [])

    @Property("QVariant", notify=viewChanged)
    def publishGeneratedContents(self):
        return list((self._view.get("publishing") or {}).get("generated_contents") or [])

    @Property(int, notify=viewChanged)
    def publishGeneratedTotal(self) -> int:
        return int((self._view.get("publishing") or {}).get("generated_total") or 0)

    @Property("QVariant", notify=viewChanged)
    def publishMessageRows(self):
        return list((self._view.get("publishing") or {}).get("messages") or [])

    @Property("QVariant", notify=viewChanged)
    def publishMessageGroups(self):
        return list((self._view.get("publishing") or {}).get("message_groups") or [])

    @Property(int, notify=viewChanged)
    def publishMessageTotal(self) -> int:
        return int((self._view.get("publishing") or {}).get("message_total") or 0)

    @Property(int, notify=viewChanged)
    def publishMessageUnread(self) -> int:
        return int((self._view.get("publishing") or {}).get("message_unread") or 0)

    @Property("QVariant", notify=viewChanged)
    def publishMessageTypeOptions(self):
        return list((self._view.get("publishing") or {}).get("message_type_options") or [])

    @Property(str, notify=viewChanged)
    def publishMessageSyncStatus(self) -> str:
        return str((self._view.get("publishing") or {}).get("message_sync_status") or "")

    @Property("QVariant", notify=viewChanged)
    def publishMessageSyncSummary(self):
        return dict((self._view.get("publishing") or {}).get("message_sync_summary") or {})

    @Property("QVariant", notify=viewChanged)
    def diagnosticHealthRows(self):
        return list((self._view.get("diagnostics") or {}).get("health") or [])

    @Property("QVariant", notify=viewChanged)
    def diagnosticAccountRows(self):
        return list((self._view.get("diagnostics") or {}).get("accounts") or [])

    @Property("QVariant", notify=logsChanged)
    def diagnosticLogs(self):
        return list((self._view.get("diagnostics") or {}).get("logs") or [])

    @Property("QVariant", notify=viewChanged)
    def diagnosticLogStats(self):
        return dict((self._view.get("diagnostics") or {}).get("log_stats") or {})

    @Property("QVariant", notify=viewChanged)
    def diagnosticBitBrowser(self):
        return dict((self._view.get("diagnostics") or {}).get("bitbrowser") or {})

    @Property("QVariant", notify=viewChanged)
    def diagnosticLlmApi(self):
        return dict((self._view.get("diagnostics") or {}).get("llm_api") or {})

    @Property("QVariant", notify=viewChanged)
    def diagnosticBitBrowserChecks(self):
        return list(((self._view.get("diagnostics") or {}).get("bitbrowser_inspection") or {}).get("checks") or [])

    @Property("QVariant", notify=viewChanged)
    def diagnosticBitBrowserWindows(self):
        return list(((self._view.get("diagnostics") or {}).get("bitbrowser_inspection") or {}).get("windows") or [])

    @Property("QVariant", notify=viewChanged)
    def diagnosticLiveHealthRows(self):
        return list(((self._view.get("diagnostics") or {}).get("live_health") or {}).get("rows") or [])

    @Property(str, notify=viewChanged)
    def diagnosticExportPath(self) -> str:
        return str((self._view.get("diagnostics") or {}).get("export_path") or "")

    @Property(str, notify=viewChanged)
    def diagnosticCheckedAt(self) -> str:
        return str((self._view.get("diagnostics") or {}).get("checked_at") or "")

    @Property("QVariant", notify=syncChanged)
    def syncStatus(self):
        return dict(self._sync)

    @Property("QVariant", notify=adminChanged)
    def adminDashboard(self):
        return dict(self._admin)

    @Property(bool, notify=authChanged)
    def authenticated(self) -> bool:
        return bool(self._auth.get("authenticated"))

    @Property(bool, notify=authChanged)
    def authIsAdmin(self) -> bool:
        return str((self._auth.get("user") or {}).get("role") or "") == "admin"

    @Property(str, notify=authChanged)
    def authUsername(self) -> str:
        return str((self._auth.get("user") or {}).get("username") or "")

    @Property(str, notify=authChanged)
    def authEmployeeName(self) -> str:
        return str((self._auth.get("user") or {}).get("employee_name") or "")

    @Property(str, notify=authChanged)
    def authMessage(self) -> str:
        return str(self._auth.get("message") or "")

    @Property("QVariant", notify=authChanged)
    def authUsers(self):
        return list(self._auth.get("users") or [])

    @Slot(str, str)
    def loginUser(self, username: str, password: str) -> None:
        self._auth["message"] = "正在登录…"
        self.authChanged.emit()
        self._bridge.command_async(
            "auth_login", {"username": str(username or ""), "password": str(password or "")},
            on_success=lambda result: self.authEvent.emit({
                "kind": "login", "user": (result or {}).get("user")
            }),
            on_error=lambda exc: self.authEvent.emit({
                "kind": "error", "message": self._error_message(exc)
            }),
        )

    @Slot(str, str, str)
    def registerUser(self, username: str, password: str, employee_name: str) -> None:
        self._auth["message"] = "正在提交注册申请…"
        self.authChanged.emit()
        self._bridge.command_async(
            "auth_register",
            {"username": str(username or ""), "password": str(password or ""),
             "employee_name": str(employee_name or "")},
            on_success=lambda _result: self.authEvent.emit({"kind": "register"}),
            on_error=lambda exc: self.authEvent.emit({
                "kind": "error", "message": self._error_message(exc)
            }),
        )

    @Slot()
    def logoutUser(self) -> None:
        self._bridge.command_async(
            "auth_logout", {},
            on_success=lambda _result: self.authEvent.emit({"kind": "logout"}),
            on_error=lambda exc: self.authEvent.emit({
                "kind": "error", "message": self._error_message(exc)
            }),
        )

    @Slot()
    def refreshAuthUsers(self) -> None:
        self._bridge.command_async(
            "auth_list_users", {},
            on_success=lambda result: self.authEvent.emit({
                "kind": "users", "users": (result or {}).get("items")
            }),
            on_error=lambda exc: self.authEvent.emit({
                "kind": "error", "message": self._error_message(exc)
            }),
        )

    def _auth_admin_action(self, command: str, user_id: int) -> None:
        self._bridge.command_async(
            command, {"user_id": int(user_id)},
            on_success=lambda _result: self.refreshAuthUsers(),
            on_error=lambda exc: self.authEvent.emit({
                "kind": "error", "message": self._error_message(exc)
            }),
        )

    @Slot(int)
    def approveAuthUser(self, user_id: int) -> None:
        self._auth_admin_action("auth_approve_user", user_id)

    @Slot(int)
    def rejectAuthUser(self, user_id: int) -> None:
        self._auth_admin_action("auth_reject_user", user_id)

    @Slot(int)
    def disableAuthUser(self, user_id: int) -> None:
        self._auth_admin_action("auth_disable_user", user_id)

    @Slot()
    def refreshSyncStatus(self) -> None:
        self._bridge.command_async(
            "sync_status", {},
            on_success=lambda result: self._apply_sync_status(result),
            on_error=lambda exc: self._apply_sync_status({
                "status": "error", "last_error": self._error_message(exc)
            }),
        )

    def _apply_sync_status(self, result) -> None:
        self._sync = dict(result or {})
        self.syncChanged.emit()

    @Slot(bool, str, str, str)
    def saveSyncSettings(self, enabled: bool, server_url: str,
                         device_name: str, api_token: str) -> None:
        self._bridge.command_async(
            "save_sync_settings",
            {"enabled": bool(enabled), "server_url": str(server_url or ""),
             "device_name": str(device_name or ""), "api_token": str(api_token or "")},
            on_success=lambda result: self._apply_sync_status(result),
            on_error=lambda exc: self._apply_sync_status({
                **self._sync, "status": "error", "last_error": self._error_message(exc)
            }),
        )

    @Slot()
    def syncNow(self) -> None:
        self._bridge.command_async(
            "sync_now", {},
            on_success=lambda result: self._apply_sync_status(result),
            on_error=lambda exc: self._apply_sync_status({
                **self._sync, "status": "error", "last_error": self._error_message(exc)
            }),
        )

    @Slot()
    def refreshAdminDashboard(self) -> None:
        self._bridge.command_async(
            "admin_dashboard", {},
            on_success=lambda result: self._apply_admin_dashboard(result),
            on_error=lambda exc: self._apply_admin_error(exc),
        )

    def _apply_admin_dashboard(self, result) -> None:
        self._admin = dict(result or {})
        self.adminChanged.emit()

    def _apply_admin_error(self, exc: Exception) -> None:
        self._admin = {
            **self._admin,
            "error": self._error_message(exc),
        }
        self.adminChanged.emit()

    @Slot()
    def createAdminBackup(self) -> None:
        self._bridge.command_async(
            "admin_create_backup", {},
            on_success=lambda result: self._apply_admin_action(result),
            on_error=lambda exc: self._apply_admin_error(exc),
        )

    @Slot()
    def refreshAdminBackups(self) -> None:
        self._bridge.command_async(
            "admin_list_backups", {},
            on_success=lambda result: self._merge_admin_rows("backups", result),
            on_error=lambda exc: self._apply_admin_error(exc),
        )

    @Slot(int)
    def validateAdminBackup(self, backup_id: int) -> None:
        self._bridge.command_async(
            "admin_validate_backup", {"backup_id": int(backup_id)},
            on_success=lambda result: self._apply_admin_action(result),
            on_error=lambda exc: self._apply_admin_error(exc),
        )

    @Slot(int)
    def restoreAdminBackup(self, backup_id: int) -> None:
        self._bridge.command_async(
            "admin_restore_backup", {"backup_id": int(backup_id)},
            on_success=lambda result: self._apply_admin_action(result),
            on_error=lambda exc: self._apply_admin_error(exc),
        )

    @Slot(int, str)
    def setAdminDeviceStatus(self, device_row_id: int, status: str) -> None:
        self._bridge.command_async(
            "admin_set_device_status",
            {"device_row_id": int(device_row_id), "status": str(status or "")},
            on_success=lambda _result: self.refreshAdminDashboard(),
            on_error=lambda exc: self._apply_admin_error(exc),
        )

    def _apply_admin_action(self, result) -> None:
        self._admin = {**self._admin, "last_action": dict(result or {}), "error": ""}
        self.adminChanged.emit()
        self.refreshAdminDashboard()

    def _merge_admin_rows(self, field: str, result) -> None:
        self._admin = {**self._admin, field: list((result or {}).get("items") or []), "error": ""}
        self.adminChanged.emit()

    @Slot(str)
    def selectPage(self, page: str) -> None:
        self._bridge.select_page(page)

    @Slot(str, str, int, str, str, str, int, int, int, bool, str, str, int, str)
    def createTask(self, platform: str, keyword: str, target_count: int,
                   account_names: str = "", execution_mode: str = "single",
                   search_sort: str = "default", keyword_group_id: int = 0,
                   batch_size: int = 10, cooldown_seconds: int = 60,
                   only_with_comments: bool = False,
                   collect_types: str = "video_info,author_info,engagement,comments,comment_user,region,intent",
                   output_dir: str = "", monitor_interval_seconds: int = 3600,
                   collect_mode: str = "standard") -> None:
        names = [item.strip() for item in str(account_names or "").split(",") if item.strip()]
        types = [item.strip() for item in str(collect_types or "").split(",") if item.strip()]
        self._run_command_async(
            "create_task",
            {"platform": str(platform or "douyin"), "keyword": str(keyword or "").strip(),
              "target_count": max(1, int(target_count or 1)), "task_accounts": names,
              "execution_mode": str(execution_mode or "single"),
              "search_sort": str(search_sort or "default"),
              "keyword_group_id": int(keyword_group_id or 0) or None,
              "batch_size": max(1, int(batch_size or 10)),
              "cooldown_seconds": max(0, int(cooldown_seconds or 0)),
              "only_with_comments": bool(only_with_comments),
              "collect_types": types,
              "output_dir": str(output_dir or "").strip() or None,
              "monitor_interval_seconds": max(60, int(monitor_interval_seconds or 3600)),
              "collect_mode": str(collect_mode or "standard")},
        )

    @Slot(str, str, str)
    def addAccount(self, name: str, platform: str, window_id: str = "") -> None:
        self._run_command_async(
            "add_account",
            {"name": str(name or "").strip(), "platform": str(platform or "douyin"),
             "bb_window_id": str(window_id or "").strip() or None},
        )

    @Slot(int)
    def removeAccount(self, account_id: int) -> None:
        self._run_command_async("remove_account", {"account_id": int(account_id)})

    @Slot(str, str)
    def createBrowserWindow(self, platform: str, name: str = "") -> None:
        """创建并打开 BitBrowser 窗口，完成后由 QML 刷新可绑定窗口列表。"""
        self._run_command_async(
            "create_browser_window",
            {"platform": str(platform or "douyin"), "name": str(name or "").strip()},
        )

    @Slot(str)
    def deleteBrowserWindow(self, window_id: str) -> None:
        """删除账号页选中的未绑定 BitBrowser 窗口。"""
        self._run_command_async(
            "delete_browser_window", {"window_id": str(window_id or "").strip()}
        )

    @Slot(int)
    def openAccountBrowser(self, account_id: int) -> None:
        self._run_command_async("open_account_browser", {"account_id": int(account_id)})

    @Slot(int)
    def bindAccount(self, account_id: int) -> None:
        self._run_command_async("bind_account", {"account_id": int(account_id)})

    @Slot()
    def refresh(self) -> None:
        self._bridge.refresh_async()

    @Slot(int, str, str, str, str, str, str, str)
    def refreshLeads(
        self, page: int = 1, task_id: str = "", platform: str = "",
        province: str = "", intent: str = "", sort_by: str = "comment_time",
        sort_order: str = "desc", keyword: str = "",
    ) -> None:
        args = {"page": max(1, int(page or 1)), "page_size": 50}
        if str(task_id or "").strip() and str(task_id) != "0":
            args["task_id"] = int(task_id)
        if platform:
            args["platform"] = str(platform)
        if province:
            args["province"] = str(province)
        if intent:
            args["intent_level"] = str(intent)
        if str(keyword or "").strip():
            args["keyword"] = str(keyword).strip()
        args["sort_by"] = str(sort_by or "comment_time")
        args["sort_order"] = str(sort_order or "desc")
        self._bridge.refresh_leads_async(args)

    @Slot()
    def refreshKeywordGroups(self) -> None:
        self._bridge.command_async(
            "list_keyword_groups", {}, on_success=self._apply_keyword_groups,
            on_error=self._on_command_error,
        )

    @Slot(str, str, str, str, str, str)
    def createKeywordGroup(
        self, name: str, platform: str, core_terms: str,
        synonym_terms: str, region_terms: str, exclude_terms: str,
    ) -> None:
        self._bridge.command_async(
            "create_keyword_group",
            {
                "name": str(name or "").strip(),
                "platform": str(platform or "").strip(),
                "core_terms": str(core_terms or ""),
                "synonym_terms": str(synonym_terms or ""),
                "region_terms": str(region_terms or ""),
                "exclude_terms": str(exclude_terms or ""),
            },
            on_success=lambda _result: self.refreshKeywordGroups(),
            on_error=self._on_command_error,
        )

    @Slot(int, str, str, str, str, str, str)
    def updateKeywordGroup(
        self, group_id: int, name: str, platform: str, core_terms: str,
        synonym_terms: str, region_terms: str, exclude_terms: str,
    ) -> None:
        self._bridge.command_async(
            "update_keyword_group",
            {
                "group_id": int(group_id or 0),
                "name": str(name or "").strip(),
                "platform": str(platform or "").strip(),
                "core_terms": str(core_terms or ""),
                "synonym_terms": str(synonym_terms or ""),
                "region_terms": str(region_terms or ""),
                "exclude_terms": str(exclude_terms or ""),
            },
            on_success=lambda _result: self.refreshKeywordGroups(),
            on_error=self._on_command_error,
        )

    @staticmethod
    def _coerce_id_list(values) -> list[int]:
        """把 QML 数组、QJSValue 和普通 Python 序列统一为 ID 列表。"""
        value = values
        # PySide6 在不同版本中可能把 QML 数组传成 QJSValue；直接 list()
        # 这种对象会抛 TypeError，导致误报“线索选择值无效”。
        to_variant = getattr(value, "toVariant", None)
        if callable(to_variant):
            value = to_variant()
        if value is None:
            return []
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            value = [value]
        try:
            raw_values = list(value)
        except TypeError as exc:
            raise ValueError("线索选择值无效") from exc
        result = []
        for raw in raw_values:
            try:
                lead_id = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("线索选择值无效") from exc
            if lead_id > 0 and lead_id not in result:
                result.append(lead_id)
        return result

    def _apply_keyword_groups(self, result) -> None:
        try:
            self._keyword_groups = list((result or {}).get("items") or [])
            self.keywordGroupsChanged.emit()
            self._on_view(self._bridge.state.to_view_model())
        except Exception as exc:
            self._on_command_error(exc)

    @Slot("QVariant")
    def addLeadsToInteraction(self, lead_ids) -> None:
        self._add_leads_to_interaction_command(lead_ids, "add_leads_to_interaction")

    @Slot("QVariant")
    def addLeadsToPrivateMessage(self, lead_ids) -> None:
        self._add_leads_to_interaction_command(lead_ids, "add_leads_to_private_message")

    def _add_leads_to_interaction_command(self, lead_ids, command: str) -> None:
        try:
            ids = self._coerce_id_list(lead_ids)
        except ValueError as exc:
            self._on_command_error(exc)
            return
        if not ids:
            return
        def on_success(_result):
            # 加入互动中心后，线索池和互动中心必须同时刷新；否则用户会看到
            # 线索已消失，但互动中心仍停留在旧快照中的错觉。
            self._bridge.refresh_leads_async()
            self._bridge.refresh_interactions_async()

        self._bridge.command_async(
            command,
            {"lead_ids": ids},
            # 保留当前任务/平台/地区/意向/排序筛选，加入互动中心不应把
            # 线索列表悄悄重置成第一页的全量结果。
            on_success=on_success,
            on_error=self._on_command_error,
        )

    @Slot("QVariant")
    def exportLeads(self, lead_ids) -> None:
        try:
            ids = self._coerce_id_list(lead_ids)
        except ValueError as exc:
            self._on_command_error(exc)
            return
        if not ids:
            self._on_command_error(ValueError("请先选择要导出的线索"))
            return
        self._run_command_async("export_leads", {"lead_ids": ids})

    @Slot("QVariant", bool, str, str, str, str, str)
    def exportLeadsByTask(self, lead_ids, all_filtered: bool = False,
                          task_id: str = "", platform: str = "",
                          province: str = "", intent_level: str = "",
                          keyword: str = "") -> None:
        """按任务拆分导出线索；全选时由后台按当前筛选取全量记录。"""
        try:
            ids = [] if all_filtered else self._coerce_id_list(lead_ids)
        except ValueError as exc:
            self._on_command_error(exc)
            return
        args = {
            "lead_ids": ids,
            "all_filtered": bool(all_filtered),
            "task_id": str(task_id or "0"),
            "platform": str(platform or ""),
            "province": str(province or ""),
            "intent_level": str(intent_level or ""),
            "keyword": str(keyword or ""),
        }
        self._run_command_async("export_leads_by_task", args)

    @Slot("QVariant", str)
    def tagLeads(self, lead_ids, tag: str) -> None:
        try:
            ids = self._coerce_id_list(lead_ids)
        except ValueError as exc:
            self._on_command_error(exc)
            return
        if not ids:
            self._on_command_error(ValueError("请先选择要打标签的线索"))
            return
        self._bridge.command_async(
            "tag_leads", {"lead_ids": ids, "tag": str(tag or "").strip()},
            on_success=lambda _result: self._bridge.refresh_leads_async(),
            on_error=self._on_command_error,
        )

    @Slot(int, str, int, str, str)
    def interactionAction(
        self, draft_id: int, action: str, account_id: int = 0,
        content: str = "", template_id: str = ""
    ) -> None:
        args = {"draft_id": int(draft_id), "action": str(action)}
        if int(account_id or 0) > 0:
            args["account_id"] = int(account_id)
        if content:
            args["content"] = str(content)
        if template_id:
            args["template_id"] = str(template_id)
        action_value = str(action or "").strip()

        def on_success(_result):
            # 列表刷新由 QML 根据当前页签负责。这里不能无条件使用
            # _last_interaction_args 刷新：用户在操作完成前切换页签，或执行
            # “待发送 -> 待生成”后，旧状态请求会把新页签覆盖成空列表。
            self.commandFinished.emit(
                "interaction_action", True,
                f"{action_value}:{int(draft_id)}",
            )

        self._bridge.command_async(
            "interaction_action",
            args,
            on_success=on_success,
            on_error=lambda exc: self._interaction_action_error(exc, draft_id),
        )

    @Slot(str, str, str)
    def exportInteractionsByTask(self, task_id: str = "", platform: str = "",
                                  status: str = "") -> None:
        self._run_command_async(
            "export_interactions_by_task",
            {"task_id": str(task_id or "0"), "platform": str(platform or ""),
             "status": str(status or "")},
        )

    def _interaction_action_error(self, exc: Exception, draft_id: int) -> None:
        self._on_command_error(exc)
        self.commandFinished.emit(
            "interaction_action", False,
            f"error:{int(draft_id)}:{str(exc or '互动操作失败')}"
        )

    @Slot(str, str, "QVariant")
    def saveTemplate(self, template_id: str, content: str, custom_variables) -> None:
        variables = {}
        for item in list(custom_variables or []):
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    variables[name] = str(item.get("value") or "")
        self._bridge.command_async(
            "save_template",
            {"template_id": str(template_id or ""), "content": str(content or ""),
             "custom_variables": variables},
            on_success=lambda _result: self._bridge.refresh_interactions_async(),
            on_error=self._on_command_error,
        )

    @Slot(int, str, str, str, str)
    def refreshInteractions(
        self, page: int = 1, status: str = "draft", platform: str = "",
        account_id: str = "", interaction_type: str = "comment_reply"
    ) -> None:
        args = {
            "page": max(1, int(page or 1)),
            "page_size": 50,
            "status": str(status or "draft"),
            "interaction_type": str(interaction_type or "comment_reply"),
        }
        if platform:
            args["platform"] = str(platform)
        if str(account_id or "").strip() and str(account_id) != "0":
            args["account_id"] = int(account_id)
        self._bridge.refresh_interactions_async(args)

    @Slot(int, str, str, str)
    def refreshPublishDrafts(self, page: int = 1, status: str = "all",
                             platform: str = "", keyword: str = "") -> None:
        args = {
            "page": max(1, int(page or 1)),
            "page_size": 50,
            "status": str(status or "all"),
            "platform": str(platform or ""),
            "keyword": str(keyword or ""),
        }

        def on_success(result):
            try:
                self._bridge.state.apply_publishing(result)
                self._on_view(self._bridge.state.to_view_model())
            except Exception as exc:
                self._on_command_error(exc)

        self._bridge.command_async(
            "list_publish_drafts", args,
            on_success=on_success, on_error=self._on_command_error,
        )

    @Slot(str, str, str, str)
    def createPublishDraft(self, title: str, body: str,
                           content_type: str = "text",
                           platforms: str = "douyin,xhs,bilibili,weibo") -> None:
        selected = [item.strip() for item in str(platforms or "").split(",") if item.strip()]
        self._bridge.command_async(
            "create_publish_draft",
            {"title": str(title or ""), "body": str(body or ""),
             "content_type": str(content_type or "text"), "platforms": selected},
            on_success=lambda _result: self.refreshPublishDrafts(),
            on_error=self._on_command_error,
        )

    @Slot(int, str, str, str, str, str)
    def updatePublishVariant(self, draft_id: int, platform: str, title: str,
                             body: str, topics: str = "",
                             content_type: str = "text") -> None:
        topic_values = [item.strip() for item in str(topics or "").replace("，", ",").split(",") if item.strip()]
        self._bridge.command_async(
            "update_publish_variant",
            {"draft_id": int(draft_id), "platform": str(platform or ""),
             "title": str(title or ""), "body": str(body or ""),
             "topics": topic_values, "content_type": str(content_type or "text")},
            on_success=lambda _result: self.refreshPublishDrafts(),
            on_error=self._on_command_error,
        )

    @Slot(int, str)
    def publishDraftAction(self, draft_id: int, action: str) -> None:
        self._bridge.command_async(
            "publish_draft_action",
            {"draft_id": int(draft_id), "action": str(action or "")},
            on_success=lambda _result: self.refreshPublishDrafts(),
            on_error=self._on_command_error,
        )

    @Slot(int)
    def deletePublishDraft(self, draft_id: int) -> None:
        self._bridge.command_async(
            "delete_publish_draft", {"draft_id": int(draft_id)},
            on_success=lambda _result: self.refreshPublishDrafts(),
            on_error=self._on_command_error,
        )

    @Slot(int, "QVariant")
    def addPublishAssets(self, draft_id: int, paths) -> None:
        """把文件选择器返回的 QUrl 列表转换为安全的本地素材列表。"""
        value = paths
        to_variant = getattr(value, "toVariant", None)
        if callable(to_variant):
            value = to_variant()
        try:
            raw_paths = list(value or [])
        except TypeError as exc:
            self._on_command_error(ValueError("素材选择结果无效"))
            return
        assets = []
        for raw in raw_paths:
            if isinstance(raw, QUrl):
                path = raw.toLocalFile()
            else:
                path = str(raw or "")
                if path.startswith("file:"):
                    path = QUrl(path).toLocalFile()
            path = str(path or "").strip()
            if not path:
                continue
            suffix = os.path.splitext(path)[1].lower()
            asset_type = "video" if suffix in {
                ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv",
                ".wmv", ".m4v", ".mpeg", ".mpg"
            } else "image"
            assets.append({"path": path, "asset_type": asset_type})
        if not assets:
            return
        self._bridge.command_async(
            "add_publish_assets", {"draft_id": int(draft_id), "assets": assets},
            on_success=lambda _result: self.refreshPublishDrafts(),
            on_error=self._on_command_error,
        )

    @Slot(int, int)
    def deletePublishAsset(self, draft_id: int, asset_id: int) -> None:
        self._bridge.command_async(
            "delete_publish_asset",
            {"draft_id": int(draft_id), "asset_id": int(asset_id)},
            on_success=lambda _result: self.refreshPublishDrafts(),
            on_error=self._on_command_error,
        )

    @Slot(int, str, int, bool, str, str, str)
    def previewPublishDraft(self, draft_id: int, platform: str,
                            account_id: int, fill_only: bool,
                            editor_title: str, editor_body: str,
                            editor_topics: str) -> None:
        """先保存当前平台版本，再在目标平台创作页填入；不会提交。"""
        self._bridge.command_async(
            "preview_publish_draft",
            {"draft_id": int(draft_id), "platform": str(platform or ""),
             "account_id": int(account_id), "fill_only": bool(fill_only),
             "editor_title": str(editor_title or ""),
             "editor_body": str(editor_body or ""),
             "editor_topics": str(editor_topics or "")},
            on_success=lambda _result: self._on_view(self._bridge.state.to_view_model()),
            on_error=self._on_command_error,
        )

    @Slot(int, str, int, str, str, str)
    def realPublishDraft(self, draft_id: int, platform: str, account_id: int,
                         editor_title: str, editor_body: str,
                         editor_topics: str) -> None:
        """先保存当前平台版本，再执行真实发布；页面已完成二次确认。"""
        self._bridge.command_async(
            "real_publish_draft",
            {"draft_id": int(draft_id), "platform": str(platform or ""),
             "account_id": int(account_id), "confirm_real_publish": True,
             "editor_title": str(editor_title or ""),
             "editor_body": str(editor_body or ""),
             "editor_topics": str(editor_topics or "")},
            timeout=180.0,
            on_success=lambda _result: self.refreshPublishDrafts(),
            on_error=self._on_command_error,
        )

    @Slot(int, str, str)
    def refreshAccountContents(self, account_id: int, platform: str = "", content_type: str = "") -> None:
        if int(account_id or 0) <= 0:
            self._on_command_error(ValueError("请先选择账号"))
            return
        self._bridge.command_async(
            "list_account_contents",
            {"account_id": int(account_id), "platform": str(platform or ""),
             "content_type": str(content_type or ""), "page": 1, "page_size": 30},
            on_success=lambda result, expected_account_id=int(account_id),
                expected_platform=str(platform or ""): self._apply_account_contents(
                    result, expected_account_id, expected_platform
                ),
            on_error=self._on_command_error,
        )

    @Slot(int, str)
    def syncAccountContents(self, account_id: int, platform: str = "") -> None:
        if int(account_id or 0) <= 0:
            self._on_command_error(ValueError("请先选择账号"))
            return
        self._bridge.command_async(
            "sync_account_contents",
            {"account_id": int(account_id), "platform": str(platform or ""), "limit": 100},
            on_success=lambda result, expected_account_id=int(account_id),
                expected_platform=str(platform or ""): self._apply_account_contents(
                    result, expected_account_id, expected_platform
                ),
            on_error=self._on_command_error,
        )

    @Slot(int)
    def refreshContentComments(self, account_content_id: int) -> None:
        self._bridge.command_async(
            "list_content_comments", {"account_content_id": int(account_content_id),
                                       "page": 1, "page_size": 50},
            on_success=self._apply_content_comments, on_error=self._on_command_error,
        )

    @Slot()
    def refreshGeneratedContents(self) -> None:
        self._bridge.command_async(
            "list_generated_contents", {"page": 1, "page_size": 20},
            on_success=self._apply_generated_contents, on_error=self._on_command_error,
        )

    @Slot(str, str, str)
    def generateContent(self, keyword: str, platform: str = "", source_ref: str = "") -> None:
        self._bridge.command_async(
            "generate_content", {"keyword": str(keyword or ""),
                                  "platform": str(platform or ""),
                                  "source_ref": str(source_ref or "")},
            on_success=lambda _result: self.refreshGeneratedContents(),
            on_error=self._on_command_error,
        )

    @Slot(int)
    def importGeneratedContent(self, generated_id: int) -> None:
        self._bridge.command_async(
            "import_generated_content", {"generated_id": int(generated_id)},
            on_success=lambda _result: self.refreshPublishDrafts(),
            on_error=self._on_command_error,
        )

    @Slot(int, str, int, str, str, str, str)
    def schedulePublish(self, draft_id: int, platform: str, account_id: int,
                        scheduled_at: str, editor_title: str,
                        editor_body: str, editor_topics: str) -> None:
        self._bridge.command_async(
            "schedule_publish", {"draft_id": int(draft_id), "platform": str(platform or ""),
                                  "account_id": int(account_id),
                                  "scheduled_at": str(scheduled_at or ""),
                                  "editor_title": str(editor_title or ""),
                                  "editor_body": str(editor_body or ""),
                                  "editor_topics": str(editor_topics or "")},
            on_success=lambda _result: self.refreshPublishDrafts(),
            on_error=self._on_command_error,
        )

    @Slot(int, str, str, bool, str)
    def refreshPublishedMessages(self, page: int = 1, platform: str = "",
                                 account_id: str = "", unread_only: bool = False,
                                 message_type: str = "") -> None:
        args = {"page": max(1, int(page or 1)), "page_size": 5000,
                "platform": str(platform or ""), "unread_only": bool(unread_only),
                "message_type": str(message_type or "")}
        if str(account_id or "").strip() and str(account_id) != "0":
            args["account_id"] = int(account_id)
        self._bridge.command_async(
            "list_published_messages", args,
            on_success=self._apply_published_messages, on_error=self._on_command_error,
        )

    @Slot(str, str, str, bool)
    def syncPublishedMessages(self, platform: str = "", account_id: str = "",
                              message_type: str = "", unread_only: bool = False) -> None:
        args = {"platform": str(platform or ""), "limit": 500}
        if str(account_id or "").strip() and str(account_id) != "0":
            args["account_id"] = int(account_id)

        def on_success(result):
            try:
                self._bridge.state.apply_message_sync(result)
                self._on_view(self._bridge.state.to_view_model())
                self.refreshPublishedMessages(1, platform, account_id, unread_only, message_type)
            except Exception as exc:
                self._on_command_error(exc)

        self._bridge.command_async(
            "sync_published_messages", args,
            on_success=on_success, on_error=self._on_command_error,
        )

    @Slot(int, bool)
    def markPublishedMessage(self, message_id: int, read: bool = True) -> None:
        self._bridge.command_async(
            "mark_published_message", {"message_id": int(message_id), "read": bool(read)},
            on_success=lambda _result: self.refreshPublishedMessages(),
            on_error=self._on_command_error,
        )

    @Slot()
    def testLlmApi(self) -> None:
        self._bridge.command_async(
            "test_llm_api", {}, on_success=lambda _result: self.refreshDiagnostics(),
            on_error=self._on_command_error,
        )

    @Slot("QVariant", int, bool, bool)
    def sendInteractions(self, draft_ids, account_id: int = 0, real_send: bool = False,
                         confirmed: bool = False) -> None:
        try:
            # QML 单条发送和一键发送都会传入 JavaScript 数组；不同 PySide6
            # 版本可能收到 list、QJSValue 或 QVariant。统一复用兼容转换，
            # 避免直接 list(QJSValue) 被误判为“互动选择值无效”。
            ids = self._coerce_id_list(draft_ids)
        except (TypeError, ValueError):
            self._on_command_error(ValueError("互动选择值无效"))
            self.commandFinished.emit("send_interactions", False, "互动选择值无效")
            return
        if not ids:
            self.commandFinished.emit("send_interactions", False, "没有选择待发送内容")
            return
        args = {"draft_ids": ids, "real_send": bool(real_send),
                "confirm_real_send": bool(confirmed)}
        if int(account_id or 0) > 0:
            args["account_id"] = int(account_id)

        def on_success(result):
            results = list((result or {}).get("results") or []) if isinstance(result, dict) else []
            failed = [item for item in results if not bool(item.get("ok"))]
            self._bridge.refresh_interactions_async()
            if failed:
                summary = "；".join(
                    f"草稿#{item.get('draft_id')}: {item.get('message') or '发送失败'}"
                    for item in failed[:3]
                )
                if len(failed) > 3:
                    summary += f"；另有 {len(failed) - 3} 条失败"
                self._on_command_error(ValueError(summary))
                self.commandFinished.emit("send_interactions", False, summary)
            else:
                self.commandFinished.emit("send_interactions", True, "发送处理完成")

        def on_error(exc):
            self._on_command_error(exc)
            self.commandFinished.emit("send_interactions", False, str(exc))

        # 回复操作需要打开目标作品、等待评论加载、滚动定位评论、动态点击
        # 回复控件并等待页面确认；单条操作可能超过普通后台命令的 10 秒。
        # 按条数给发送命令独立的等待窗口，避免前端先报超时而后台继续发送，
        # 导致用户误以为失败后重复点击。普通命令仍使用默认短超时。
        send_timeout = max(180.0, 90.0 * len(ids))
        self._bridge.command_async(
            "send_interactions",
            args,
            timeout=send_timeout,
            on_success=on_success,
            on_error=on_error,
        )

    @Slot()
    def refreshDiagnostics(self) -> None:
        self._bridge.command_async(
            "diagnostics_snapshot",
            {},
            on_success=lambda result: self._apply_diagnostics(result),
            on_error=self._on_command_error,
        )

    @Slot()
    def runPlatformHealth(self) -> None:
        self._bridge.command_async(
            "run_platform_health",
            {},
            on_success=lambda _result: self.refreshDiagnostics(),
            on_error=self._on_command_error,
        )

    @Slot(str, int, str, str, str, str, bool)
    def saveSettings(
        self, bitbrowser_url: str, bitbrowser_timeout: int,
        provider: str, api_url: str, api_key: str, model: str, enabled: bool,
    ) -> None:
        self._bridge.command_async(
            "save_settings",
            {
                "bitbrowser": {"base_url": str(bitbrowser_url or ""), "timeout": int(bitbrowser_timeout or 20)},
                "llm_api": {
                    "enabled": bool(enabled), "provider": str(provider or ""),
                    "base_url": str(api_url or ""), "api_key": str(api_key or ""),
                    "model": str(model or ""), "timeout": 60,
                },
            },
            on_success=lambda _result: self.refreshDiagnostics(),
            on_error=self._on_command_error,
        )

    @Slot()
    def inspectBitBrowser(self) -> None:
        self._bridge.command_async(
            "inspect_bitbrowser",
            {},
            on_success=lambda result: self._apply_bitbrowser_inspection(result),
            on_error=self._on_command_error,
        )

    @Slot()
    def runLiveDiagnostics(self) -> None:
        self._bridge.command_async(
            "run_live_diagnostics",
            {},
            on_success=lambda result: self._apply_live_health(result),
            on_error=self._on_command_error,
        )

    @Slot()
    def exportOperationLog(self) -> None:
        self._bridge.command_async(
            "export_operation_log",
            {},
            on_success=lambda result: self._apply_log_export(result),
            on_error=self._on_command_error,
        )

    def _apply_diagnostics(self, result) -> None:
        try:
            self._bridge.state.apply_diagnostics(result)
            self._on_view(self._bridge.state.to_view_model())
        except Exception as exc:
            self._on_command_error(exc)

    def _apply_bitbrowser_inspection(self, result) -> None:
        try:
            self._bridge.state.apply_bitbrowser_inspection(result)
            self._on_view(self._bridge.state.to_view_model())
        except Exception as exc:
            self._on_command_error(exc)

    def _apply_live_health(self, result) -> None:
        try:
            self._bridge.state.apply_live_health(result)
            self._on_view(self._bridge.state.to_view_model())
        except Exception as exc:
            self._on_command_error(exc)

    def _apply_log_export(self, result) -> None:
        try:
            self._bridge.state.apply_log_export(result)
            self._on_view(self._bridge.state.to_view_model())
        except Exception as exc:
            self._on_command_error(exc)

    def _apply_account_contents(self, result, expected_account_id: int = 0,
                                expected_platform: str = "") -> None:
        try:
            if isinstance(result, dict):
                result_account_id = int(result.get("account_id") or 0)
                result_platform = str(result.get("platform") or "")
                if expected_account_id and result_account_id and result_account_id != expected_account_id:
                    return
                if expected_platform and result_platform and result_platform != expected_platform:
                    return
            self._bridge.state.apply_account_contents(result)
            self._on_view(self._bridge.state.to_view_model())
        except Exception as exc:
            self._on_command_error(exc)

    def _apply_content_comments(self, result) -> None:
        try:
            self._bridge.state.apply_content_comments(result)
            self._on_view(self._bridge.state.to_view_model())
        except Exception as exc:
            self._on_command_error(exc)

    def _apply_generated_contents(self, result) -> None:
        try:
            self._bridge.state.apply_generated_contents(result)
            self._on_view(self._bridge.state.to_view_model())
        except Exception as exc:
            self._on_command_error(exc)

    def _apply_published_messages(self, result) -> None:
        try:
            self._bridge.state.apply_published_messages(result)
            self._on_view(self._bridge.state.to_view_model())
        except Exception as exc:
            self._on_command_error(exc)

    @Slot(int)
    def startTask(self, task_id: int) -> None:
        self._run_command_async("start", {"task_id": int(task_id)})

    @Slot(int)
    def pauseTask(self, task_id: int) -> None:
        self._run_command_async("pause", {"task_id": int(task_id)})

    @Slot(int)
    def resumeTask(self, task_id: int) -> None:
        # 1.2 的“继续”不是单独解除暂停，而是恢复人工状态后重新进入
        # Scheduler.start()；否则不完整/已停止任务点击后不会创建阶段线程。
        self._run_command_async("resume_task", {"task_id": int(task_id)})

    @Slot(int)
    def stopTask(self, task_id: int) -> None:
        self._run_command_async("stop_task", {"task_id": int(task_id)})

    @Slot(int)
    def exportTask(self, task_id: int) -> None:
        self._run_command_async("export_task", {"task_id": int(task_id)})

    @Slot(int)
    def openTaskFolder(self, task_id: int) -> None:
        self._run_command_async("open_task_folder", {"task_id": int(task_id)})

    @Slot(int)
    def deleteTask(self, task_id: int) -> None:
        self._run_command_async("delete_task", {"task_id": int(task_id)})

    @Slot(int, int, bool, str)
    def configureMonitoring(self, task_id: int, interval_seconds: int = 3600,
                            enabled: bool = True, search_sort: str = "") -> None:
        self._run_command_async(
            "configure_monitoring",
            {"task_id": int(task_id),
             "interval_seconds": max(60, int(interval_seconds or 3600)),
             "enabled": bool(enabled), "search_sort": str(search_sort or "") or None},
        )

    @Slot(str)
    def openSourceUrl(self, url: str) -> None:
        target = str(url or "").strip()
        if not target or not (target.startswith("http://") or target.startswith("https://")):
            self._on_command_error(ValueError("原作地址无效"))
            return
        # 线索中心的“点击查看”明确走 Chrome；找不到固定路径时再交给系统默认浏览器。
        candidates = [
            os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ]
        chrome = next((path for path in candidates if path and os.path.exists(path)), "")
        try:
            if chrome:
                subprocess.Popen([chrome, target], close_fds=True)
            elif hasattr(os, "startfile"):
                os.startfile(target)
            else:  # pragma: no cover
                import webbrowser
                webbrowser.open(target)
        except Exception as exc:
            self._on_command_error(exc)

    def _run_command_async(self, command: str, args: dict) -> None:
        """后台命令不占用 Qt 事件循环，完成后拉取一份最新快照。"""
        def on_success(result):
            self._bridge.refresh_async()
            self.commandFinished.emit(command, True, "")

        def on_error(exc):
            self._on_command_error(exc)
            self.commandFinished.emit(command, False, str(exc))

        self._bridge.command_async(
            command,
            args,
            on_success=on_success,
            on_error=on_error,
        )

    def _on_command_error(self, exc: Exception) -> None:
        try:
            self._bridge.state.mark_command_error(exc)
            self._on_view(self._bridge.state.to_view_model())
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多平台采集工作台 2.0 QML 界面")
    parser.add_argument("--endpoint", default="", help="后台服务地址 JSON 文件")
    args = parser.parse_args(argv)

    client_bridge = Ui2Bridge()
    if args.endpoint:
        try:
            client_bridge = Ui2Bridge.from_endpoint(read_endpoint(args.endpoint))
        except Exception as exc:
            client_bridge.state.mark_error(exc)
    # 使用跨平台 Basic 样式，允许统一自绘按钮/下拉框背景，避免 Windows 原生
    # 样式在高频重排时触发额外主题查询，也保证设计稿与实际 QML 一致。
    QQuickStyle.setStyle("Basic")
    app = QGuiApplication(sys.argv)
    engine = QQmlApplicationEngine()
    qml_bridge = QmlBridge(client_bridge)
    engine.rootContext().setContextProperty("backend", qml_bridge)
    qml_path = os.path.join(os.path.dirname(__file__), "qml", "main.qml")
    engine.load(QUrl.fromLocalFile(qml_path))
    if not engine.rootObjects():
        return 2
    client_bridge.connect_async()
    exit_code = app.exec()
    client_bridge.close()
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
