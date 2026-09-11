# -*- coding: utf-8 -*-
"""独立的 2.2.2 QML 界面预览入口。

默认生产入口是 ``src/ui2/default_app.py``，它会同时启动本地后台服务；本文件
保留为无后台预览入口，方便只验收视觉和交互。未安装 PySide6 时给出明确提示。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from typing import Any, Mapping
from urllib.parse import quote, urlsplit, urlunsplit

try:
    from PySide6.QtCore import QCoreApplication, QObject, Property, QTimer, QUrl, Signal, Slot
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
except ImportError as exc:  # pragma: no cover - 取决于 v2 额外依赖
    raise SystemExit(
        "2.2.2 界面需要 PySide6，请先安装 requirements-v2.txt；旧版入口不受影响。"
    ) from exc

try:
    from ..app_version import APP_VERSION  # type: ignore
    from .bridge import Ui2Bridge  # type: ignore
    from ..backend_protocol import read_endpoint  # type: ignore
except ImportError:  # pragma: no cover - python src/ui2/qml_app.py
    HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from app_version import APP_VERSION  # type: ignore
    from ui2.bridge import Ui2Bridge  # type: ignore
    from backend_protocol import read_endpoint  # type: ignore

try:
    from ..time_utils import beijing_now  # type: ignore
except ImportError:  # pragma: no cover - python src/ui2/qml_app.py
    from time_utils import beijing_now  # type: ignore

try:
    from ..config_loader import AppConfig  # type: ignore
except ImportError:  # pragma: no cover - python src/ui2/qml_app.py
    from config_loader import AppConfig  # type: ignore

try:
    from ..platform_runtime import open_url  # type: ignore
except ImportError:  # pragma: no cover - python src/ui2/qml_app.py
    from platform_runtime import open_url  # type: ignore

try:
    from ..update_checker import (  # type: ignore
        DEFAULT_MANIFEST_URL,
        UpdateCheckError,
        fetch_manifest,
        is_update_available,
        read_local_build_id,
    )
except ImportError:  # pragma: no cover - python src/ui2/qml_app.py
    from update_checker import (  # type: ignore
        DEFAULT_MANIFEST_URL,
        UpdateCheckError,
        fetch_manifest,
        is_update_available,
        read_local_build_id,
    )


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
    updateChanged = Signal()
    updateEvent = Signal(object)
    # 让 QML 在耗时命令期间立即锁定危险按钮，并在失败时恢复。
    commandFinished = Signal(str, bool, str)

    def __init__(self, bridge: Ui2Bridge):
        super().__init__()
        self._bridge = bridge
        self._keyword_groups: list[dict] = []
        self._view: dict = bridge.state.to_view_model()
        # 状态快照由后台 reader 线程接收，Qt 信号会在主线程排队处理。
        # 长任务期间如果后台更新速度超过 QML 重绘速度，逐条排队会让
        # 事件队列越来越长，表现为窗口卡住但后台仍在运行。只保留最新
        # 一份快照，并让 Qt 队列中最多存在一个待处理的刷新信号。
        self._view_signal_lock = threading.Lock()
        self._view_signal_pending = False
        self._pending_view: dict | None = None
        self._last_published_message_args: dict[str, Any] = {
            "page": 1, "platform": "", "account_id": "0",
            "unread_only": False, "message_type": "",
        }
        self._auth: dict = {
            "authenticated": False,
            "user": None,
            "message": "",
            "users": [],
        }
        auth_config = AppConfig().auth()
        self._auth_server_url = str(auth_config.get("server_url") or "").strip()
        try:
            self._auth_server_timeout = max(3, min(30, int(auth_config.get("timeout") or 8)))
        except (TypeError, ValueError):
            self._auth_server_timeout = 8
        self._sync: dict = {
            "enabled": False, "server_url": "", "device_name": "",
            "api_token_configured": False, "last_sync_at": None,
            "status": "idle", "last_error": "", "pending": 0,
        }
        self._admin: dict = {
            "summary": {}, "employees": [], "devices": [], "backups": [],
            "audit": {"items": [], "total": 0, "counts": {}},
        }
        self._update_status: dict = {
            "checking": False,
            "updating": False,
            "available": False,
            "current_version": APP_VERSION,
            "current_build_id": read_local_build_id(self._project_root()),
            "latest_version": "",
            "latest_build_id": "",
            "notes": "",
            "message": "尚未检查更新",
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
        self.updateEvent.connect(self._apply_update_status)

    @staticmethod
    def _project_root() -> str:
        # PyInstaller 运行时的 __file__ 可能位于临时解包目录；可写数据、
        # 更新器和免安装包资源都必须相对实际 EXE 所在目录解析。
        if getattr(sys, "frozen", False):
            return os.path.dirname(os.path.abspath(sys.executable))
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def _auth_session_path(self) -> str:
        """记住登录状态的文件位置；内容只含会话令牌，不含密码。"""
        return os.path.join(self._project_root(), "data", "auth_session.json")

    def _clear_auth_session(self) -> None:
        path = self._auth_session_path()
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def _save_auth_session(self, result: Mapping[str, Any] | None) -> None:
        payload = dict(result or {})
        local_token = str(payload.get("local_session_token") or "").strip()
        remote_token = str(payload.get("remote_session_token") or "").strip()
        if not local_token and not remote_token:
            return
        path = self._auth_session_path()
        directory = os.path.dirname(path)
        temporary = path + ".tmp"
        try:
            os.makedirs(directory, exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump({
                    "local_session_token": local_token,
                    "remote_session_token": remote_token,
                    "auth_server_url": str(payload.get("auth_server_url") or self._auth_server_url).strip(),
                }, stream, ensure_ascii=False)
            os.replace(temporary, path)
        except (OSError, TypeError, ValueError):
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass

    def _load_auth_session(self) -> dict[str, str]:
        try:
            with open(self._auth_session_path(), encoding="utf-8") as stream:
                value = json.load(stream)
            if not isinstance(value, Mapping):
                return {}
            return {
                "local_session_token": str(value.get("local_session_token") or "").strip(),
                "remote_session_token": str(value.get("remote_session_token") or "").strip(),
                "auth_server_url": str(value.get("auth_server_url") or self._auth_server_url).strip(),
            }
        except (OSError, UnicodeError, ValueError, TypeError):
            return {}

    def _on_view(self, view: dict) -> None:
        # BackendClient 的 reader 线程只发信号；实际属性更新回到 Qt 线程。
        # 快照到达过快时覆盖旧值，避免 Qt 事件队列堆积。
        with self._view_signal_lock:
            self._pending_view = dict(view or {})
            if self._view_signal_pending:
                return
            self._view_signal_pending = True
        self.backendEvent.emit(None)

    def _on_log(self, payload: dict) -> None:
        self.backendLogEvent.emit(payload)

    @Slot(object)
    def _apply_view(self, view: dict) -> None:
        with self._view_signal_lock:
            pending = self._pending_view
            self._pending_view = None
            self._view_signal_pending = False
        # 保留直接调用 _apply_view(dict) 的兼容性（旧版自动化/预览入口），
        # 正常后台事件则使用锁内拿到的最新快照。
        self._view = dict(pending if pending is not None else (view or {}))
        self.viewChanged.emit()

    @Slot(object)
    def _apply_log(self, _payload: dict) -> None:
        # 日志只更新日志列表，不触发整棵页面重新布局，避免后台采集时界面抖动。
        self._bridge.mark_log_signal_delivered()
        self.logsChanged.emit()

    @Slot(object)
    def _apply_update_status(self, payload: dict) -> None:
        self._update_status.update(dict(payload or {}))
        self.updateChanged.emit()

    @Slot(object)
    def _apply_auth_event(self, event: dict) -> None:
        event = dict(event or {})
        kind = str(event.get("kind") or "")
        if kind == "login":
            user = dict(event.get("user") or {})
            session = event.get("session")
            if bool(event.get("remember")) and isinstance(session, Mapping):
                self._save_auth_session(session)
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
            self.refreshDiagnostics()
            self.refreshPublishDrafts()
            if str(user.get("role") or "") == "admin":
                self.refreshAdminDashboard()
            return
        if kind == "logout":
            self._clear_auth_session()
            self._bridge.set_auth_scope(None)
            self._keyword_groups = []
            self.keywordGroupsChanged.emit()
            self._auth.update({"authenticated": False, "user": None, "message": ""})
            self.authChanged.emit()
            return
        if kind == "restore_error":
            self._clear_auth_session()
            self._bridge.set_auth_scope(None)
            self._auth.update({
                "authenticated": False, "user": None,
                "message": str(event.get("message") or "登录状态已失效，请重新登录"),
            })
            self.authChanged.emit()
            return
        if kind == "error":
            self._auth["message"] = str(event.get("message") or "操作失败")
            self.authChanged.emit()
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
        # 统一走状态模型，补齐旧库缺失的平台并提供中文平台/状态标签。
        return list(self._bridge.state.diagnostic_health_rows())

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
    def diagnosticTiebaApi(self):
        return dict((self._view.get("diagnostics") or {}).get("tieba_api") or {})

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

    @Property("QVariant", notify=updateChanged)
    def updateStatus(self):
        """当前更新检查状态；只包含版本和发布说明，不包含本地数据。"""

        return dict(self._update_status)

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

    @Property(str, notify=authChanged)
    def authServerUrl(self) -> str:
        return self._auth_server_url

    @Slot(str, str)
    @Slot(str, str, bool)
    def loginUser(self, username: str, password: str, remember: bool = False) -> None:
        remember = bool(remember)
        if not remember:
            # 用户取消勾选时立即撤销本地自动登录入口，避免程序在登录请求
            # 期间意外退出后仍自动恢复旧账号。
            self._clear_auth_session()
        self._auth["message"] = "正在登录…"
        self.authChanged.emit()
        self._bridge.command_async(
            "auth_login", {
                "username": str(username or ""), "password": str(password or ""),
                "auth_server_url": self._auth_server_url,
                "auth_server_timeout": self._auth_server_timeout,
            },
            on_success=lambda result: self.authEvent.emit({
                "kind": "login", "user": (result or {}).get("user"),
                "session": result or {}, "remember": remember,
            }),
            on_error=lambda exc: self.authEvent.emit({
                "kind": "error", "message": self._error_message(exc)
            }),
        )

    @Slot()
    def restoreAuthSession(self) -> None:
        session = self._load_auth_session()
        if not session.get("local_session_token") and not session.get("remote_session_token"):
            return
        args = dict(session)
        args["device_id"] = ""
        self._bridge.command_async(
            "auth_restore", args,
            on_success=lambda result: self.authEvent.emit({
                "kind": "login", "user": (result or {}).get("user"),
                "session": result or {}, "remember": True,
            }),
            on_error=lambda exc: self.authEvent.emit({
                "kind": "restore_error", "message": self._error_message(exc),
            }),
        )

    @Slot(str, str, str)
    def registerUser(self, username: str, password: str, employee_name: str) -> None:
        self._auth["message"] = "正在提交注册申请…"
        self.authChanged.emit()
        self._bridge.command_async(
            "auth_register",
            {"username": str(username or ""), "password": str(password or ""),
             "employee_name": str(employee_name or ""),
             "auth_server_url": self._auth_server_url,
             "auth_server_timeout": self._auth_server_timeout},
            on_success=lambda _result: self.authEvent.emit({"kind": "register"}),
            on_error=lambda exc: self.authEvent.emit({
                "kind": "error", "message": self._error_message(exc)
            }),
        )

    @Slot()
    def logoutUser(self) -> None:
        self._clear_auth_session()
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
        """保存账号后自动读取一次平台昵称；失败不回滚账号记录。"""
        args = {
            "name": str(name or "").strip(),
            "platform": str(platform or "douyin"),
            "bb_window_id": str(window_id or "").strip() or None,
        }

        def on_added(result):
            self._bridge.refresh_async()
            account_id = int((result or {}).get("account_id") or 0)
            self.commandFinished.emit(
                "add_account", True,
                "账号已保存，正在读取平台昵称…" if account_id > 0 else "账号已保存",
            )
            if account_id <= 0:
                return
            # 贴吧通过官方 API 令牌采集，不依赖 BitBrowser 窗口，也不需要走浏览器昵称绑定。
            if str(args.get("platform") or "") == "tieba":
                self._bridge.refresh_async()
                self.commandFinished.emit("add_account", True, "贴吧账号已保存；请在设置中配置 TB_TOKEN")
                return

            def on_bound(_bound):
                self._bridge.refresh_async()
                self.commandFinished.emit("bind_account", True, "已读取平台昵称")

            def on_bind_error(exc):
                # 账号已经保存，读取昵称失败只提示原因，不把“保存账号”误报为失败。
                self._on_command_error(exc)
                self.commandFinished.emit(
                    "bind_account", False,
                    f"账号已保存，但昵称读取失败：{self._error_message(exc)}",
                )

            self._bridge.command_async(
                "bind_account", {"account_id": account_id}, timeout=60.0,
                on_success=on_bound, on_error=on_bind_error,
            )

        def on_add_error(exc):
            self._on_command_error(exc)
            self.commandFinished.emit("add_account", False, self._error_message(exc))

        self._bridge.command_async(
            "add_account", args, on_success=on_added, on_error=on_add_error,
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
        # 读取账号主页通常需要导航和等待 SPA 渲染，不能使用普通命令的 10 秒超时。
        self._run_command_async(
            "bind_account", {"account_id": int(account_id)}, timeout=60.0
        )

    @Slot()
    def refreshAccountNicknames(self) -> None:
        """只刷新空闲账号的昵称，运行中的账号由后台主动跳过。"""
        def on_success(result):
            self._bridge.refresh_async()
            payload = result if isinstance(result, dict) else {}
            updated = len(payload.get("updated") or [])
            skipped = len(payload.get("skipped") or [])
            failed = len(payload.get("failed") or [])
            self.commandFinished.emit(
                "refresh_account_nicknames", True,
                f"昵称刷新完成：成功 {updated} 个，跳过 {skipped} 个，失败 {failed} 个",
            )

        def on_error(exc):
            self._on_command_error(exc)
            self.commandFinished.emit(
                "refresh_account_nicknames", False, self._error_message(exc)
            )

        self._bridge.command_async(
            "refresh_account_nicknames", {}, timeout=240.0,
            on_success=on_success, on_error=on_error,
        )

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

    @Slot(int, str)
    def switchInteractionType(self, lead_id: int, interaction_type: str) -> None:
        """为同一线索创建另一种互动草稿，成功后由 QML 切换页签。"""
        try:
            normalized_type = str(interaction_type or "").strip().lower()
            if normalized_type not in {"comment_reply", "private_message"}:
                raise ValueError("不支持的互动类型")
            normalized_lead_id = int(lead_id)
            if normalized_lead_id <= 0:
                raise ValueError("线索编号无效")
        except (TypeError, ValueError) as exc:
            self._on_command_error(exc)
            self.commandFinished.emit(
                "interaction_type_switch", False,
                f"error:{int(lead_id or 0)}:{self._error_message(exc)}",
            )
            return

        def on_success(_result):
            self.commandFinished.emit(
                "interaction_type_switch", True,
                f"{normalized_type}:{normalized_lead_id}",
            )

        def on_error(exc):
            self._on_command_error(exc)
            self.commandFinished.emit(
                "interaction_type_switch", False,
                f"error:{normalized_lead_id}:{self._error_message(exc)}",
            )

        self._bridge.command_async(
            "add_leads_to_interaction",
            {"lead_ids": [normalized_lead_id], "interaction_type": normalized_type},
            on_success=on_success,
            on_error=on_error,
        )

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
        value = custom_variables
        to_variant = getattr(value, "toVariant", None)
        if callable(to_variant):
            value = to_variant()
        try:
            raw_items = list(value or [])
        except TypeError:
            self._on_command_error(ValueError("模板变量列表无效"))
            self.commandFinished.emit("save_template", False, "模板变量列表无效")
            return
        variables = {}
        for item in raw_items:
            item_to_variant = getattr(item, "toVariant", None)
            if callable(item_to_variant):
                item = item_to_variant()
            if isinstance(item, Mapping):
                name = str(item.get("name") or "").strip()
                if name:
                    variables[name] = str(item.get("value") or "")

        def on_success(_result):
            self._bridge.refresh_interactions_async()
            self.commandFinished.emit(
                "save_template", True,
                f"模板“{str(template_id or '').strip()}”已保存，可继续编辑或关闭",
            )

        def on_error(exc):
            self._on_command_error(exc)
            self.commandFinished.emit("save_template", False, self._error_message(exc))

        self._bridge.command_async(
            "save_template",
            {"template_id": str(template_id or ""), "content": str(content or ""),
             "custom_variables": variables},
            on_success=on_success,
            on_error=on_error,
        )

    @Slot(int)
    def openPublishedMessageReply(self, message_id: int) -> None:
        """打开消息对应的 BitBrowser 页面，仅导航，不自动回复或发送。"""
        message_id = int(message_id or 0)
        if message_id <= 0:
            self.commandFinished.emit("open_published_message_browser", False, "消息编号无效")
            return

        def on_success(result):
            self.commandFinished.emit(
                "open_published_message_browser", True,
                str((result or {}).get("message") or "已打开对应浏览器回复页面"),
            )

        def on_error(exc):
            self._on_command_error(exc)
            self.commandFinished.emit(
                "open_published_message_browser", False, self._error_message(exc),
            )

        self._bridge.command_async(
            "open_published_message_browser", {"message_id": message_id},
            timeout=45.0, on_success=on_success, on_error=on_error,
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
        def on_success(result):
            self.refreshGeneratedContents()
            source_type = str((result or {}).get("source_type") or "")
            if source_type == "llm":
                message = "智能 API 已调用并生成内容"
            else:
                message = "智能 API 未返回有效内容，已使用本地模板"
            self.commandFinished.emit("generate_content", True, message)

        def on_error(exc):
            self._on_command_error(exc)
            self.commandFinished.emit("generate_content", False, self._error_message(exc))

        self._bridge.command_async(
            "generate_content", {"keyword": str(keyword or ""),
                                  "platform": str(platform or ""),
                                  "source_ref": str(source_ref or "")},
            on_success=on_success,
            on_error=on_error,
        )

    @Slot(int)
    def importGeneratedContent(self, generated_id: int) -> None:
        self._bridge.command_async(
            "import_generated_content", {"generated_id": int(generated_id)},
            on_success=lambda _result: self.refreshPublishDrafts(),
            on_error=self._on_command_error,
        )

    @Slot(int)
    def deleteGeneratedContent(self, generated_id: int) -> None:
        def on_success(_result):
            self.refreshGeneratedContents()
            self.commandFinished.emit("delete_generated_content", True, "生成内容已删除")

        def on_error(exc):
            self._on_command_error(exc)
            self.commandFinished.emit(
                "delete_generated_content", False, self._error_message(exc)
            )

        self._bridge.command_async(
            "delete_generated_content", {"generated_id": int(generated_id)},
            on_success=on_success, on_error=on_error,
        )

    @Slot(int, str, int, str, str, str, str, bool)
    def schedulePublish(self, draft_id: int, platform: str, account_id: int,
                        scheduled_at: str, editor_title: str,
                        editor_body: str, editor_topics: str,
                        real_send_authorized: bool = False) -> None:
        def on_success(result):
            self.refreshPublishDrafts()
            job_id = int((result or {}).get("job_id") or 0)
            when = str((result or {}).get("scheduled_at") or scheduled_at or "")
            match = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", when)
            display_when = (
                f"{match.group(1)}年{match.group(2)}月{match.group(3)}日 "
                f"{match.group(4)}:{match.group(5)}"
                if match else when.replace("T", " ")
            )
            if bool((result or {}).get("real_send_authorized")):
                message = (
                    f"定时发布已保存：任务#{job_id} · 北京时间 {display_when}；"
                    "到点自动打开账号浏览器执行发布"
                )
            else:
                message = (
                    f"定时发布已保存：任务#{job_id} · 北京时间 {display_when}；"
                    "未开启真实发布，到点不会点击最终发布按钮"
                )
            self.commandFinished.emit("schedule_publish", True, message)

        def on_error(exc):
            self._on_command_error(exc)
            self.commandFinished.emit(
                "schedule_publish", False, self._error_message(exc)
            )

        self._bridge.command_async(
            "schedule_publish", {"draft_id": int(draft_id), "platform": str(platform or ""),
                                  "account_id": int(account_id),
                                  "scheduled_at": str(scheduled_at or ""),
                                  "editor_title": str(editor_title or ""),
                                  "editor_body": str(editor_body or ""),
                                  "editor_topics": str(editor_topics or ""),
                                  "real_send_authorized": bool(real_send_authorized)},
            on_success=on_success,
            on_error=on_error,
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
        self._last_published_message_args = {
            "page": int(args["page"]),
            "platform": str(args["platform"]),
            "account_id": str(account_id or "0"),
            "unread_only": bool(unread_only),
            "message_type": str(message_type or ""),
        }
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
        filters = dict(getattr(self, "_last_published_message_args", {}) or {})
        self._bridge.command_async(
            "mark_published_message", {"message_id": int(message_id), "read": bool(read)},
            on_success=lambda _result: self.refreshPublishedMessages(
                int(filters.get("page") or 1),
                str(filters.get("platform") or ""),
                str(filters.get("account_id") or "0"),
                bool(filters.get("unread_only", False)),
                str(filters.get("message_type") or ""),
            ),
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
            # 客户端超时不代表后台发送已停止；后台可能仍在等待浏览器
            # 加载或页面确认。立即重读待发送列表，让 sending/failed/sent
            # 的真实状态覆盖旧行，避免用户再次点击造成重复发送。
            self._bridge.refresh_interactions_async()
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

    @Slot(str, bool)
    def saveTiebaSettings(self, token: str, enabled: bool) -> None:
        self._bridge.command_async(
            "save_tieba_settings",
            {"token": str(token or ""), "enabled": bool(enabled)},
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

    @Slot()
    def checkForUpdates(self) -> None:
        """在设置页手动检查更新，不阻塞 Qt 界面。"""

        if self._update_status.get("checking") or self._update_status.get("updating"):
            return
        root = self._project_root()
        current_build = read_local_build_id(root)
        self._update_status.update({
            "checking": True,
            "available": False,
            "current_version": APP_VERSION,
            "current_build_id": current_build,
            "latest_version": "",
            "latest_build_id": "",
            "notes": "",
            "message": "正在检查线上版本…",
        })
        self.updateChanged.emit()

        def worker() -> None:
            try:
                manifest_url = DEFAULT_MANIFEST_URL
                config_path = os.path.join(root, "config", "update.json")
                try:
                    with open(config_path, "r", encoding="utf-8-sig") as stream:
                        config = json.load(stream)
                    if isinstance(config, dict) and str(config.get("manifest_url") or "").strip():
                        manifest_url = str(config["manifest_url"]).strip()
                except (OSError, ValueError, TypeError):
                    # 配置损坏时使用官方默认地址，保证“检查更新”仍可用。
                    pass
                manifest = fetch_manifest(manifest_url, timeout=15)
                available, reason = is_update_available(
                    APP_VERSION, current_build, manifest
                )
                if available:
                    message = f"{reason}：{manifest['version']}"
                else:
                    message = reason
                self.updateEvent.emit({
                    "checking": False,
                    "available": bool(available),
                    "current_version": APP_VERSION,
                    "current_build_id": current_build,
                    "latest_version": manifest["version"],
                    "latest_build_id": manifest.get("build_id", ""),
                    "download_url": manifest["download_url"],
                    "sha256": manifest["sha256"],
                    "notes": manifest.get("notes", ""),
                    "message": message,
                })
            except (UpdateCheckError, OSError, ValueError, TypeError) as exc:
                self.updateEvent.emit({
                    "checking": False,
                    "available": False,
                    "message": f"检查更新失败：{self._error_message(exc)}",
                })

        threading.Thread(target=worker, name="ui2-update-check", daemon=True).start()

    @Slot()
    def installUpdate(self) -> None:
        """启动独立更新器，等待当前 GUI 退出后替换程序并重启。"""

        if not self._update_status.get("available"):
            self._update_status["message"] = "当前没有可安装的更新"
            self.updateChanged.emit()
            return
        root = self._project_root()
        updater_candidates = (
            os.path.join(root, "update.ps1"),
            os.path.join(root, "更新程序.ps1"),
            os.path.join(root, "更新程序", "更新程序.ps1"),
            os.path.join(root, "updater", "update.ps1"),
        )
        script = next((item for item in updater_candidates if os.path.isfile(item)), "")
        if not script:
            self._update_status["message"] = (
                "当前安装包缺少更新程序，请将便携更新器复制到软件目录后重试"
            )
            self.updateChanged.emit()
            return
        try:
            powershell = (
                os.environ.get("SystemRoot", r"C:\\Windows")
                + r"\System32\WindowsPowerShell\v1.0\powershell.exe"
            )
            if not os.path.exists(powershell):
                powershell = "powershell.exe"
            update_marker = os.path.join(self._project_root(), ".update_pending")
            with open(update_marker, "w", encoding="ascii") as stream:
                stream.write(f"pid={os.getpid()}\n")
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    script,
                    "-AppRoot",
                    root,
                    "-WaitForPid",
                    str(os.getpid()),
                    "-Force",
                    "-Restart",
                ],
                cwd=self._project_root(),
                creationflags=creation_flags,
                close_fds=True,
            )
            self._update_status.update({
                "updating": True,
                "available": False,
                "message": "更新程序已启动，正在关闭并重启软件…",
            })
            self.updateChanged.emit()
            QTimer.singleShot(500, self._quit_application_for_update)
        except (OSError, ValueError) as exc:
            try:
                marker = os.path.join(self._project_root(), ".update_pending")
                if os.path.exists(marker):
                    os.remove(marker)
            except OSError:
                pass
            self._update_status["message"] = f"启动更新失败：{self._error_message(exc)}"
            self.updateChanged.emit()

    @staticmethod
    def _quit_application_for_update() -> None:
        app = QCoreApplication.instance()
        if app is not None:
            app.quit()

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

    @staticmethod
    def _source_text_fragment(url: str, text: str) -> str:
        """用 Chrome 原生 text-fragment 让原作页尽量直接滚到评论正文。"""
        target = str(url or "").strip()
        anchor = " ".join(str(text or "").split()).strip()
        if not anchor:
            return target
        # 评论正文很长时取前 80 个字符，避免超长 URL 触发浏览器限制；
        # 平台的图片/表情评论没有正文时，调用方传入昵称作为兜底锚点。
        anchor = anchor[:80]
        parts = urlsplit(target)
        fragment = parts.fragment
        directive = ":~:text=" + quote(anchor, safe="")
        fragment = (fragment + "&" if fragment else "") + directive
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, fragment))

    @Slot(str)
    @Slot(str, str)
    def openSourceUrl(self, url: str, comment_text: str = "") -> None:
        target = str(url or "").strip()
        if not target or not (target.startswith("http://") or target.startswith("https://")):
            self._on_command_error(ValueError("原作地址无效"))
            return
        target = self._source_text_fragment(target, comment_text)
        try:
            open_url(target, prefer_chrome=True)
        except Exception as exc:
            self._on_command_error(exc)

    def _run_command_async(self, command: str, args: dict,
                           timeout: float | None = None) -> None:
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
            timeout=timeout,
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
    parser = argparse.ArgumentParser(description=f"多平台采集工作台 {APP_VERSION} QML 界面")
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
    engine.rootObjects()[0].setProperty("appVersion", APP_VERSION)
    client_bridge.connect_async(lambda _view: qml_bridge.restoreAuthSession())
    exit_code = app.exec()
    client_bridge.close()
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
