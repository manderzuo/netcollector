#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gui.py — 多账号采集平台桌面 GUI（Python tkinter，零第三方依赖）。

设计要点（按用户需求 6 点）：
  1. 账号 = BitBrowser 窗口：账号管理页列出 BitBrowser 已有的全部窗口(profile)，
     每个窗口带「打开浏览器」按钮 → 点开弹浏览器 → 用户手动登录 → 登录成功即激活。
     登录态保存在 BitBrowser profile 里，登录一次后下次打开免登录。
  2. 打开几个浏览器 = 激活几个账号：任务分配按已激活(已打开)的账号数来均分。
  3. 平台全中文：平台下拉显示「抖音/小红书」，账号实时状态等所有 GUI 文案全中文。
  4. 登录失效/验证码 → 弹窗 + 顶部红条 + 系统通知，提示用户人工介入（P7）。
  5. 界面流畅：轮询改用增量刷新 + 节流，避免卡顿。

运行:
  python src/gui.py            # 真实 BitBrowser 采集（默认）
  python src/gui.py --demo     # 假数据演示（不看浏览器）
"""

import os
import json
import re
import sys
import socket
import threading
import time
import tkinter as tk
import queue
from collections import deque
from datetime import datetime
from tkinter import ttk, messagebox, filedialog
try:
    import customtkinter as ctk
except ImportError:
    ctk = None

from ui.theme import COLORS, FONTS, RADIUS, SPACE, status_palette
from ui.widgets import LineIcon, MetricCard, ModernTable, NavButton, PlatformBadge, PlatformPicker, StatusBadge

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import db  # noqa: E402
from scheduler import FakeCollector, Scheduler  # noqa: E402
from debug_trace import (  # noqa: E402
    DebugTrace, register_trace_sink, unregister_trace_sink,
)
from operation_log import OperationLog  # noqa: E402
from search_sort import sort_key, sort_label, sort_labels, latest_sort_key  # noqa: E402

# 线索运营与互动中心（Agent A~G 交付，Agent H 集成）
from leads.repository import LeadRepository, LeadQuery  # noqa: E402
from leads.service import LeadService  # noqa: E402
from leads.dashboard import DashboardQueryService  # noqa: E402
from interactions.repository import InteractionRepository  # noqa: E402
from interactions.service import InteractionService  # noqa: E402
from interactions.browser_reply import BitBrowserReplyAdapter  # noqa: E402
from ui.pages.leads_page import LeadsPage, LeadPagePresenter  # noqa: E402
from ui.pages.interaction_page import InteractionPage, InteractionPagePresenter  # noqa: E402
from ui.pages.settings_page import SettingsPage  # noqa: E402
from ui.pages.keywords_page import KeywordsPage  # noqa: E402
from ui.dialogs import ask_centered_confirm  # noqa: E402
from config_loader import AppConfig  # noqa: E402

DEFAULT_DB = os.path.join(PROJECT_ROOT, "data", "platform_gui.db")
_INSTANCE_LOCK_HANDLE = None


def _acquire_instance_lock() -> bool:
    """保证同一发布目录只运行一个 GUI，避免两个 Scheduler 共用同一数据库。"""
    global _INSTANCE_LOCK_HANDLE
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        server.bind(("127.0.0.1", 45873))
        server.listen(1)
    except OSError:
        server.close()
        return False
    _INSTANCE_LOCK_HANDLE = server
    return True

# 平台中文名 <-> 内部名
PLATFORM_CN = {"douyin": "抖音", "xhs": "小红书", "weibo": "微博", "bilibili": "B站", "kuaishou": "快手"}
PLATFORM_EN = {v: k for k, v in PLATFORM_CN.items()}

# 状态中文映射（账号/任务）
ACCT_STATUS_CN = {
    "idle": "空闲", "working": "工作", "cooldown": "冷却", "busy": "工作中",
    "ready": "就绪", "disabled": "停用", "dead": "失效",
    "waiting_human": "需人工", "frozen": "已冻结",
    "need_login": "待登录", "active": "已激活", "closed": "未打开",
}
TASK_STATUS_CN = {
    "pending": "待开始", "phase_a_search": "采集中", "phase_b_comments": "采集中",
    "running": "采集中", "paused": "暂停", "incomplete": "暂停",
    "done": "完成", "failed": "失败", "aborted": "停止", "stopped": "停止",
    "no_account": "无可用账号", "waiting_account": "等待账号",
}
MODE_CN = {"fast": "快速", "standard": "标准", "deep": "深度"}
MODE_EN = {v: k for k, v in MODE_CN.items()}
COLLECT_TYPE_OPTIONS = [
    ("video_info", "视频基础信息"),
    ("author_info", "作者/账号信息"),
    ("engagement", "点赞、收藏、分享等互动数据"),
    ("comments", "评论内容"),
    ("comment_user", "评论用户信息"),
    ("region", "地区/省份"),
    ("intent", "意向识别"),
]
DEFAULT_COLLECT_TYPES = {key for key, _ in COLLECT_TYPE_OPTIONS}


class UnavailableCollector:
    """正式后端不可用时明确失败，绝不静默生成演示数据。"""

    def __init__(self, reason):
        self.reason = str(reason)

    def _raise(self):
        raise RuntimeError(f"真实采集器不可用：{self.reason}")

    def search(self, *args, **kwargs):
        self._raise()

    def fetch_comments(self, *args, **kwargs):
        self._raise()


class GuiApp:
    def __init__(self, root, db_path=DEFAULT_DB, demo=False):
        self.root = root
        self.db_path = db_path
        self.demo = demo
        self._perf_trace = DebugTrace("gui_performance")
        self._operation_log = OperationLog(
            os.path.join(PROJECT_ROOT, "data", "logs", "operation_events.jsonl")
        )
        # 后台线程只入队，Tk 主线程按批次刷新。完整记录仍立即写入
        # operation_events.jsonl，界面展示队列只负责避免 after(0) 风暴。
        self._log_display_queue = deque()
        self._log_display_queue_lock = threading.Lock()
        self._log_flush_after = None
        self._log_display_limit = 5000
        self._log_flush_batch_size = 200
        self._log_display_dropped = 0
        # Tk 只能由主线程访问。后台采集/诊断线程不再直接调用 root.after，
        # 否则主线程一旦繁忙，调用方会全部阻塞在 Tcl 锁上，最终形成线程风暴。
        self._ui_dispatch_queue = deque()
        self._ui_dispatch_queue_lock = threading.Lock()
        self._ui_dispatch_after = None
        self._ui_dispatch_limit = 4000
        self._ui_dispatch_batch_size = 200
        self._ui_dispatch_dropped = 0
        # 所有 DebugTrace（浏览器回复、CDP、BitBrowser、页面性能）统一汇总到
        # 设置中心的后台操作日志；原组件 JSONL 仍保留，便于按组件深挖。
        self._trace_sink = self._on_debug_trace
        register_trace_sink(self._trace_sink)
        self._operation_log_hourly_after = None
        self._operation_log_hourly_after = self.root.after(
            3600000, self._hourly_operation_log_snapshot
        )
        self._selected_collect_types = set(DEFAULT_COLLECT_TYPES)
        self._real_send_enabled = False
        self._keyword_group_options = []
        self._keyword_group_load_inflight = False
        self.var_keyword_group_id = tk.StringVar(value="")
        self._task_popup = None
        self._native_wndproc_ref = None
        self._native_prev_wndproc = None
        self._native_user32 = None
        self._interactive_move_size = False
        self._shell_frozen_for_move = False
        self.root.title("多账号采集平台")
        self._ui_prefs_path = os.path.join(PROJECT_ROOT, "data", "gui_preferences.json")
        self.app_config = AppConfig()
        saved_geometry = ""
        try:
            with open(self._ui_prefs_path, encoding="utf-8") as f:
                saved_geometry = json.load(f).get("geometry", "")
        except Exception:
            pass
        self.root.geometry(saved_geometry or "1400x860")
        self.root.minsize(1100, 720)
        # 主窗口始终允许通过系统边框调整尺寸。显式设置可避免 CTk/Windows
        # 恢复上一次窗口状态后遗留不可缩放标志。
        self.root.resizable(True, True)
        self._save_prefs_after = None

        # 心跳：供外部监控脚本判断主线程是否卡死。
        # _last_activity 只在主线程（_render_report 回调）里更新；
        # 心跳线程把它落盘成 heartbeat 文件，监控器据此判定 GUI 是否还在响应。
        self._last_activity = time.time()
        self._heartbeat_stop = threading.Event()
        self._heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat.start()

        # 点击/滚轮审计必须完整保留，但不能让每次鼠标释放都同步打开
        # operation_events.jsonl。否则日志文件变大后，TAB 切换会被磁盘写入
        # 卡住。队列只由主线程快速入队，文件追加交给单独线程。
        self._audit_queue = queue.Queue(maxsize=4000)
        self._audit_stop = threading.Event()
        self._audit_dropped = 0
        self._audit_writer = threading.Thread(
            target=self._audit_log_loop, name="gui-audit-log-writer", daemon=True
        )
        self._audit_writer.start()

        # ---- BitBrowser 客户端 + 采集器 ----
        self.bb = None
        self.collector = None
        self.sched = None
        self._init_backend()

        self._poll_stop = threading.Event()
        self._refresh_guard = threading.Lock()
        self._refresh_requested = threading.Event()
        self._refresh_inflight = False
        self._refresh_report_lock = threading.Lock()
        self._pending_report = None
        self._report_delivery_scheduled = False
        self._latest_report = {}
        # 刷新状态只允许一个长期存活的后台线程；轮询线程只发请求。
        self._refresh_worker = threading.Thread(
            target=self._refresh_worker_loop, name="gui-status-refresh", daemon=True
        )
        self._refresh_worker.start()
        self._poll = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll.start()

        self._build_ui()
        # 这唯一一个 Tk 队列泵由构造 GUI 的主线程创建；后台线程永远不碰 Tk。
        self._ui_dispatch_after = self.root.after(30, self._drain_ui_dispatch_queue)
        # 记录真实界面点击，包含控件、坐标、当前页面和控件文本/值。
        # 不拦截事件，只追加审计记录。
        self.root.bind_all("<ButtonRelease-1>", self._audit_ui_click, add="+")
        self.root.bind_all("<MouseWheel>", self._audit_ui_wheel, add="+")
        if self.sched is not None:
            self.sched.log_callback = self._log
            try:
                # 启动即恢复定时增量监控；单次任务不会被触发，旧版本任务也不受影响。
                self.sched.start_monitoring()
            except Exception as exc:
                self._log(f"监控后台启动失败，普通采集功能不受影响：{exc}")
        if getattr(self, "_backend_error", None):
            self._ui_call(lambda: messagebox.showerror(
                "真实采集器不可用",
                f"程序不会切换到演示数据。请修复环境后重启。\n\n{self._backend_error}"))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_all()
        self._window_refresh_after = None
        self.refresh_window_list()
        # BitBrowser 列表/状态查询是同步网络请求，不能高频阻塞 tkinter 主线程。
        self._window_refresh_after = self.root.after(5000, self.refresh_window_list)
        self.root.bind("<Configure>", self._on_window_resize, add="+")
        self._install_native_move_size_hook()

    def _install_native_move_size_hook(self):
        """在 Windows 原生移动/缩放会话期间冻结 Tk 内容布局。

        Tk 的 ``<Configure>`` 只能在窗口尺寸变化后收到通知，已经晚于
        CustomTkinter 开始重排控件。监听原生 WM_ENTERSIZEMOVE/EXITSIZEMOVE
        可以把内容区暂时固定在最后一帧，松开鼠标后再一次性恢复布局。
        """
        if os.name != "nt" or self._native_wndproc_ref is not None:
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = int(self.root.winfo_id())
            wndproc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                ctypes.c_size_t, ctypes.c_ssize_t,
            )

            def wndproc(window, message, wparam, lparam):
                if message == 0x0231:  # WM_ENTERSIZEMOVE
                    self._interactive_move_size = True
                    try:
                        self._ui_call(self._freeze_shell_for_move_size)
                    except Exception:
                        pass
                elif message == 0x0232:  # WM_EXITSIZEMOVE
                    self._interactive_move_size = False
                    try:
                        self._ui_call(self._unfreeze_shell_after_move_size)
                    except Exception:
                        pass
                previous = self._native_prev_wndproc
                if previous:
                    return user32.CallWindowProcW(
                        ctypes.c_void_p(previous), ctypes.c_void_p(window),
                        ctypes.c_uint(message), ctypes.c_size_t(wparam),
                        ctypes.c_ssize_t(lparam),
                    )
                return user32.DefWindowProcW(
                    ctypes.c_void_p(window), ctypes.c_uint(message),
                    ctypes.c_size_t(wparam), ctypes.c_ssize_t(lparam),
                )

            callback = wndproc_type(wndproc)
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
            ]
            previous = user32.SetWindowLongPtrW(
                ctypes.c_void_p(hwnd), ctypes.c_int(-4),
                ctypes.cast(callback, ctypes.c_void_p),
            )
            if not previous:
                return
            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                ctypes.c_size_t, ctypes.c_ssize_t,
            ]
            user32.DefWindowProcW.restype = ctypes.c_ssize_t
            user32.DefWindowProcW.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t,
                ctypes.c_ssize_t,
            ]
            self._native_user32 = user32
            self._native_prev_wndproc = int(previous)
            self._native_wndproc_ref = callback
            self._perf_trace.emit("native_move_size_hook", installed=True)
        except Exception as exc:  # noqa: BLE001
            self._native_wndproc_ref = None
            self._native_prev_wndproc = None
            self._native_user32 = None
            self._log(f"原生窗口拖动优化未启用：{type(exc).__name__}: {exc}")

    def _freeze_shell_for_move_size(self):
        if self._shell_frozen_for_move or not getattr(self, "shell", None):
            return
        try:
            width = max(1, self.shell.winfo_width())
            height = max(1, self.shell.winfo_height())
            self.shell.configure(width=width, height=height)
            self.shell.pack_forget()
            # CTkFrame 不允许 place(width=..., height=...)，先设置固定尺寸
            # 再使用原生 place，保留拖动开始前最后一帧。
            self.shell.place(x=0, y=0)
            self._shell_frozen_for_move = True
        except Exception:
            self._shell_frozen_for_move = False

    def _unfreeze_shell_after_move_size(self):
        if not self._shell_frozen_for_move:
            self._finish_window_configure()
            return
        try:
            self.shell.place_forget()
            self.shell.pack(fill="both", expand=True)
            self._shell_frozen_for_move = False
            self._resize_needs_layout = True
            self._last_window_configure_at = time.monotonic()
            self._finish_window_configure()
        except Exception:
            self._shell_frozen_for_move = False

    def _uninstall_native_move_size_hook(self):
        if self._native_user32 is None or self._native_prev_wndproc is None:
            return
        try:
            import ctypes
            self._native_user32.SetWindowLongPtrW(
                ctypes.c_void_p(int(self.root.winfo_id())), ctypes.c_int(-4),
                ctypes.c_void_p(self._native_prev_wndproc),
            )
        except Exception:
            pass
        finally:
            self._native_wndproc_ref = None
            self._native_prev_wndproc = None
            self._native_user32 = None

    def _on_window_resize(self, event=None):
        """记录高频窗口变化；所有实际工作都推迟到拖动结束后执行。"""
        if event is not None and event.widget is not self.root:
            return
        self._last_window_configure_at = time.monotonic()
        if event is None:
            size = (self.root.winfo_width(), self.root.winfo_height())
        else:
            # 直接使用事件自带尺寸，避免拖动每个像素都同步查询 Tcl。
            size = (int(event.width), int(event.height))
        if size == getattr(self, "_last_resize_size", None):
            size_changed = False
        else:
            self._last_resize_size = size
            size_changed = True
        # 移动和缩放时每秒可能产生数百个 Configure。只保留一个定时器，
        # 不在每个像素上反复 after_cancel/after，也不访问磁盘。
        self._resize_needs_layout = (
            getattr(self, "_resize_needs_layout", False) or size_changed
        )
        if getattr(self, "_interactive_move_size", False):
            # 原生移动/缩放会话由 WM_EXITSIZEMOVE 收尾，期间不排版、不落盘。
            return
        if getattr(self, "_window_configure_after", None) is None:
            self._window_configure_after = self.root.after(
                220, self._finish_window_configure
            )

    def _finish_window_configure(self):
        """窗口停止移动/缩放后，再统一调整布局并保存一次位置。"""
        if getattr(self, "_interactive_move_size", False):
            self._window_configure_after = None
            return
        started = time.perf_counter()
        elapsed = time.monotonic() - getattr(
            self, "_last_window_configure_at", time.monotonic()
        )
        if elapsed < 0.20:
            delay = max(30, int((0.22 - elapsed) * 1000))
            self._window_configure_after = self.root.after(
                delay, self._finish_window_configure
            )
            return
        self._window_configure_after = None
        if getattr(self, "_resize_needs_layout", False):
            self._resize_needs_layout = False
            self._apply_responsive_scale()
        self._save_preferences()
        self._perf_trace.emit(
            "window_configure_finished",
            width=self.root.winfo_width(),
            height=self.root.winfo_height(),
            finalize_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    def _apply_responsive_scale(self):
        self._resize_pending = False
        try:
            width = max(1100, self.root.winfo_width())
            scale = max(0.82, min(1.18, width / 1600.0))
            if abs(scale - getattr(self, "_ui_scale", 0)) < 0.03:
                return
            self._ui_scale = scale
            # 不在拖动窗口边框时修改 Tk 全局缩放。全局 scaling 会重新计算
            # 所有控件的请求尺寸，和 Windows 的实时边框尺寸形成反馈循环，
            # 表现为窗口拖不动、回弹或严重卡顿。这里只调整少量导航尺寸。
            nav_size = max(13, min(15, int(14 * scale)))
            for btn in self._nav_buttons.values():
                btn.configure(font=("Microsoft YaHei UI", nav_size))
            # 导航项每项 48px，再加上下间距；高度过小会把后面的导航项
            # 裁在侧栏容器之外，导致页面虽已注册却不可见。
            nav_count = len(getattr(self, "_nav_buttons", {})) or 5
            nav_min_height = nav_count * 48 + (nav_count * 6)
            available = max(360, self.sidebar.winfo_height() - self.brand.winfo_height() - 48)
            self.nav_area.configure(
                height=max(nav_min_height, min(300, int(available * 0.4)))
            )
        except Exception:
            pass

    def _save_preferences(self):
        self._save_prefs_after = None
        try:
            os.makedirs(os.path.dirname(self._ui_prefs_path), exist_ok=True)
            with open(self._ui_prefs_path, "w", encoding="utf-8") as f:
                json.dump({"geometry": self.root.geometry()}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 后端初始化
    # ------------------------------------------------------------------ #
    def _init_backend(self):
        if not os.path.exists(os.path.dirname(self.db_path)):
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        if self.demo:
            self.collector = FakeCollector(video_count=25)
            self.sched = Scheduler(self.db_path, bb=None, collector=self.collector)
            for i in range(1, 4):
                self.sched.add_account(f"演示账号{i}", bb_window_id=f"demo-{i}", platform="douyin")
        else:
            from bitbrowser import BitBrowserClient
            bb_config = self.app_config.bitbrowser()
            self.bb = BitBrowserClient(
                base_url=bb_config.get("base_url") or "http://127.0.0.1:54345",
                timeout=float(bb_config.get("timeout") or 20),
            )
            self.live_collector = None
            try:
                from live_collector import HybridCollector, LiveCollector
                self.live_collector = LiveCollector(
                    platforms=("douyin", "xhs", "kuaishou"), bb=self.bb
                )
                # 微博/B站正式走 BitBrowser 窗口。不要读取旧的 Chrome ws 文件，
                # 否则“打开/绑定”入口会误判成 Chrome 会话并跳过 BitBrowser。
                self.weibo_collector = None
                self.bilibili_collector = None
                self.collector = HybridCollector(self.live_collector, bb=self.bb)
            except Exception as e:
                self._backend_error = str(e)
                print(f"[gui] 真实采集器不可用：{e}")
                self.collector = UnavailableCollector(e)
            self.sched = Scheduler(self.db_path, bb=self.bb, collector=self.collector)
            # 正式迁移到 BitBrowser 后，移除旧的普通 Chrome 虚拟绑定，避免任务误用旧会话。
            self.sched.remove_account("微博 Chrome")
            self.sched.remove_account("B站 Chrome")
        self.sched.cooldown_handler = self._cooldown_skip if self.demo else None

    def _cooldown_skip(self, account, seconds):
        self._log(f"账号 {account} 冷却 {seconds}s")
        return True

    # ------------------------------------------------------------------ #
    # UI 构建
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        self.root.configure(bg=COLORS["window"])
        if ctk:
            ctk.set_appearance_mode("dark")
        try:
            self.root.iconbitmap(os.path.join(PROJECT_ROOT, "assets", "app_icon.ico"))
        except Exception:
            pass
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=COLORS["window"])
        style.configure("TLabel", background=COLORS["window"], foreground=COLORS["text_2"],
                        font=FONTS["body"])
        style.configure("TButton", background=COLORS["surface_3"], foreground=COLORS["text"],
                        padding=(12, 7), font=FONTS["body"])
        style.map("TButton", background=[("active", COLORS["surface_hover"])])
        style.configure("TEntry", fieldbackground=COLORS["surface"], foreground=COLORS["text"],
                        bordercolor=COLORS["border"], lightcolor=COLORS["border"],
                        darkcolor=COLORS["surface"], insertcolor=COLORS["text"],
                        padding=8, font=FONTS["body"])
        style.configure("TCombobox", fieldbackground=COLORS["surface"], background=COLORS["surface_3"],
                        foreground=COLORS["text"], bordercolor=COLORS["border"],
                        lightcolor=COLORS["border"], darkcolor=COLORS["surface"],
                        arrowcolor=COLORS["text_2"], padding=7, borderwidth=0,
                        relief="flat", focuscolor=COLORS["surface"], font=FONTS["body"])
        style.map("TCombobox", fieldbackground=[("readonly", COLORS["surface"]),
                                                 ("disabled", COLORS["surface_2"])],
                  foreground=[("readonly", COLORS["text"]), ("disabled", COLORS["subtle"])],
                  selectbackground=[("readonly", COLORS["primary_soft"])],
                  selectforeground=[("readonly", COLORS["text"])])
        style.configure("TSpinbox", fieldbackground=COLORS["surface"], foreground=COLORS["text"],
                        bordercolor=COLORS["border"], lightcolor=COLORS["border"],
                        darkcolor=COLORS["surface"], arrowcolor=COLORS["text_2"],
                        padding=7, font=FONTS["body"])
        style.configure("TCheckbutton", background=COLORS["window"], foreground=COLORS["text_2"])
        style.map("TCheckbutton", background=[("active", COLORS["surface_hover"])],
                  foreground=[("disabled", COLORS["subtle"])])
        style.configure("TRadiobutton", background=COLORS["surface_2"], foreground=COLORS["text"])
        style.map("TRadiobutton", background=[("active", COLORS["surface_hover"])])
        style.configure("TNotebook", background=COLORS["window"], borderwidth=0, relief="flat",
                        tabmargins=(0, 0, 0, 0), lightcolor=COLORS["window"], darkcolor=COLORS["window"])
        style.layout("Hidden.TNotebook", [("Notebook.client", {"sticky": "nswe"})])
        style.configure("TNotebook.Tab", background=COLORS["surface_2"], foreground=COLORS["muted"],
                        padding=(18, 9), borderwidth=0, relief="flat",
                        lightcolor=COLORS["surface_2"], darkcolor=COLORS["surface_2"],
                        focuscolor=COLORS["surface_2"], font=FONTS["helper"])
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Treeview", background=COLORS["surface_2"], fieldbackground=COLORS["surface_2"],
                        foreground=COLORS["text_2"], rowheight=42, borderwidth=0,
                        relief="flat", font=FONTS["table"],
                        lightcolor=COLORS["surface_2"], darkcolor=COLORS["surface_2"])
        style.configure("Treeview.Heading", background=COLORS["surface"], foreground=COLORS["muted"],
                        relief="flat", borderwidth=0, font=FONTS["table_bold"],
                        lightcolor=COLORS["surface"], darkcolor=COLORS["surface"])
        style.map("Treeview", background=[("selected", COLORS["primary_soft"])],
                  foreground=[("selected", COLORS["text"])])
        style.configure("Account.Treeview", background=COLORS["surface_2"],
                        fieldbackground=COLORS["surface_2"], foreground=COLORS["text_2"],
                        rowheight=45, borderwidth=0, relief="flat", font=FONTS["table"],
                        lightcolor=COLORS["surface_2"], darkcolor=COLORS["surface_2"])
        style.configure("Account.Treeview.Heading", background=COLORS["surface"],
                        foreground=COLORS["muted"], relief="flat", borderwidth=0,
                        font=FONTS["table_bold"], lightcolor=COLORS["surface"],
                        darkcolor=COLORS["surface"])
        style.map("Account.Treeview", background=[("selected", COLORS["primary_soft"])],
                  foreground=[("selected", COLORS["text"])])
        style.map("Treeview.Heading", background=[("active", COLORS["surface_hover"])],
                  foreground=[("active", COLORS["text"])])

        self.shell = ctk.CTkFrame(self.root, fg_color=COLORS["window"], corner_radius=0)
        self.shell.pack(fill="both", expand=True)
        self.shell.grid_rowconfigure(0, weight=1)
        self.shell.grid_columnconfigure(1, weight=1)
        self.workspace = self.shell
        self.sidebar = ctk.CTkFrame(self.shell, fg_color=COLORS["sidebar"], width=252,
                                    corner_radius=0, border_width=0)
        self.sidebar.grid(row=0, column=0, sticky="nsw")
        self.sidebar.grid_propagate(False)
        self.sidebar.pack_propagate(False)
        self.content = ctk.CTkFrame(self.shell, fg_color=COLORS["window"], corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.log_panel = ctk.CTkFrame(self.sidebar, fg_color=COLORS["log_bg"],
                                      corner_radius=RADIUS["card"], border_width=1,
                                      border_color=COLORS["border_soft"])
        # 侧栏宽度固定，无需在主窗口拖动期间让三个容器重复计算字体。

        self.brand = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand = self.brand
        brand.pack(fill="x", padx=18, pady=(20, 18))
        self._brand_img = None
        try:
            # 使用带 Alpha 通道的版本，避免品牌图在深色侧栏上出现方形底框。
            brand_path = os.path.join(PROJECT_ROOT, "assets", "app_icon_transparent.png")
            self._brand_img = tk.PhotoImage(file=brand_path)
            factor = max(1, self._brand_img.width() // 38)
            self._brand_img = self._brand_img.subsample(factor, factor)
            tk.Label(brand, image=self._brand_img, bg=COLORS["sidebar"], bd=0).pack(
                side="left", padx=(0, 9))
        except Exception:
            LineIcon(brand, "brand", color=COLORS["text_2"], size=30,
                     bg=COLORS["sidebar"]).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(brand, text="采集工作台", text_color=COLORS["text"],
                     font=("Microsoft YaHei UI", 16, "bold"), anchor="w").pack(side="left")

        # 关键词组通过“新建任务”内的入口管理，不再占用左侧主导航位置。
        nav_count = 6
        self.nav_area = ctk.CTkFrame(self.sidebar, fg_color="transparent",
                                     height=nav_count * 54)
        self.nav_area.pack(fill="x", padx=12, pady=(0, 12))
        self.nav_area.pack_propagate(False)
        self._nav_buttons = {}
        for key, label in (("overview", "总览"), ("tasks", "任务中心"),
                           ("accounts", "账号管理"),
                           ("leads", "线索中心"), ("interaction", "互动中心"),
                           ("settings", "设置")):
            btn = NavButton(self.nav_area, key, label,
                            lambda k=key: self._show_page(k))
            btn.pack(fill="x", padx=2, pady=3)
            self._nav_buttons[key] = btn

        ctk.CTkFrame(self.sidebar, fg_color=COLORS["border_soft"], height=1).pack(fill="x", padx=18, pady=(2, 10))
        self.log_panel.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        top = ctk.CTkFrame(self.content, fg_color=COLORS["window"], corner_radius=0, height=54)
        top.pack(fill="x")
        top.pack_propagate(False)
        self.monitor_label = ctk.CTkLabel(top, text="", text_color=COLORS["muted"],
                                          font=FONTS["helper"])
        self.monitor_label.pack(side="right", padx=(10, 28))
        self._update_monitor_clock()
        self.new_task_btn = None

        self.page_host = ctk.CTkFrame(self.content, fg_color=COLORS["window"], corner_radius=0)
        self.page_host.pack(fill="both", expand=True)
        for option, value in {
            "*TCombobox*Listbox.background": COLORS["surface"],
            "*TCombobox*Listbox.foreground": COLORS["text"],
            "*TCombobox*Listbox.selectBackground": COLORS["primary_soft"],
            "*TCombobox*Listbox.selectForeground": COLORS["text"],
            "*TCombobox*Listbox.borderWidth": 0,
            "*TCombobox*Listbox.highlightThickness": 0,
        }.items():
            self.root.option_add(option, value)
        self.banner = tk.Label(self.page_host, text="", bg=COLORS["warning_bg"],
                               fg=COLORS["warning"], anchor="w", justify="left",
                               padx=18, wraplength=980,
                               font=FONTS["body_bold"], height=2)
        self.banner.bind("<Button-1>", lambda _e: self._dismiss_human_banner(), add="+")
        self.banner.pack_forget()

        self.overview_page = ctk.CTkFrame(self.page_host, fg_color=COLORS["window"], corner_radius=0)
        self.tab_acct = ctk.CTkFrame(self.page_host, fg_color=COLORS["window"], corner_radius=0)
        self.tab_task = ctk.CTkFrame(self.page_host, fg_color=COLORS["window"], corner_radius=0)
        self.logs_page = self.log_panel
        # 所有主页面在启动阶段一次性构造并挂入布局。后续切换只 lift，
        # 不再在第一次点击页签时创建整棵控件树。
        self.leads_page = None
        self.interaction_page = None
        self.settings_page = None
        self.keywords_page = None

        self._build_overview()
        self._build_acct_tab()
        self._build_task_tab()
        self._build_logs_page()
        self._ensure_lead_pages("leads")
        self._ensure_lead_pages("interaction")
        self._ensure_settings_page()
        self._ensure_keywords_page()
        self._refresh_keyword_group_options()
        self._install_scroll_router()

        self._last_task_card_signature = None
        self._task_card_refs = {}
        self._last_account_signature = None
        self._last_overview_signature = None
        self._human_banner_key = ""
        self._human_banner_dismissed = False

        # 页面全部常驻 page_host；即使暂时位于后层，也先完成布局和首轮数据
        # 渲染，确保之后的左侧 TAB 切换只有层级变化。
        resident_views = (
            self.overview_page, self.tab_acct, self.tab_task,
            self.keywords_page, self.leads_page, self.interaction_page,
            self.settings_page,
        )
        self.root.update_idletasks()
        host_width = max(1, self.page_host.winfo_width())
        host_height = max(1, self.page_host.winfo_height())
        for view in resident_views:
            if view is not None and not view.winfo_manager():
                # 页面一次性挂到同一个层叠容器中，后续 TAB 切换只 lift，
                # 不再 place_forget/place 反复映射整棵控件树。
                # 所有页面都跟随容器尺寸，避免切换后出现旧尺寸残留。
                view.configure(width=host_width, height=host_height)
                view.place(relx=0, rely=0, relwidth=1, relheight=1)
        for view in (self.leads_page, self.interaction_page):
            if view is not None and hasattr(view, "on_show"):
                view.on_show()
        if self.leads_page is not None:
            self.leads_page.refresh()
        if self.interaction_page is not None:
            self.interaction_page.refresh()
        if self.settings_page is not None:
            self.settings_page.reload()
        if self.keywords_page is not None:
            self.keywords_page.reload()
        # place/CTkScrollableFrame/Canvas 的布局回调默认进入 Tk idle 队列。
        # 如果不在启动阶段清空，用户第一次点击 TAB 时就会看到控件从下往上
        # 逐步出现，像页面被重新刷新。这里提前完成首轮布局和画布重绘。
        self.root.update_idletasks()
        self._show_page("overview")
        self.root.update_idletasks()
        # 首轮布局/渲染已经完成；非当前页面继续留在层叠容器中，切换时
        # 只提升目标页面，避免重新挂载导致画面延迟。

    def _build_logs_page(self):
        title_row = ctk.CTkFrame(self.logs_page, fg_color="transparent")
        title_row.pack(fill="x", padx=12, pady=(12, 7))
        LineIcon(title_row, "logs", color=COLORS["text_2"], size=22, bg=COLORS["log_bg"]).pack(side="left", padx=(0, 8))
        self.log_header = ctk.CTkLabel(title_row, text="运行日志", text_color=COLORS["text_2"],
                                      anchor="w", font=FONTS["log_title"])
        self.log_header.pack(side="left")
        ctk.CTkLabel(title_row, text="● 实时", text_color=COLORS["success"],
                     font=("Microsoft YaHei UI", 10)).pack(side="right")
        panel = ctk.CTkFrame(self.logs_page, fg_color=COLORS["log_bg"], corner_radius=8)
        panel.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.log_text = tk.Text(panel, state="disabled", wrap="word",
                                font=FONTS["log"], bg=COLORS["log_bg"], fg=COLORS["log_text"],
                                insertbackground=COLORS["text"], relief="flat", bd=0,
                                padx=10, pady=10, spacing1=3, spacing3=2)
        self.log_text.pack(fill="both", expand=True)

    def _show_page(self, page):
        """切换工作台页面；已构造页面常驻，只调整前后层级。"""
        started = time.perf_counter()
        previous_page = getattr(self, "_active_page", None)
        if previous_page == page:
            current = self._page_view(page)
            if current is not None:
                current.lift()
            return

        if page in ("leads", "interaction"):
            view = self._ensure_lead_pages(page)
        else:
            view = {
            "overview": self.overview_page,
            "accounts": self.tab_acct,
            "tasks": self.tab_task,
            "keywords": getattr(self, "keywords_page", None),
            "settings": getattr(self, "settings_page", None),
            }.get(page)
        if view is None:
            return

        self._active_page = page
        try:
            # 页面已经常驻层叠容器；切换只改变 Z 顺序，不触发数据查询、
            # 控件重建或 Tcl 的重新映射。
            if not view.winfo_manager():
                view.place(relx=0, rely=0, relwidth=1, relheight=1)
        except tk.TclError:
            view.place(relx=0, rely=0, relwidth=1, relheight=1)
        lift_started = time.perf_counter()
        view.lift()
        lift_ms = round((time.perf_counter() - lift_started) * 1000, 2)
        if self.new_task_btn is not None:
            self.new_task_btn.pack_forget()
        if page == "logs":
            # 日志始终显示在左栏下方；点击时只让日志获得视觉焦点。
            self.log_panel.configure(highlightbackground="#4fc9ff")
            self.log_text.focus_set()
        else:
            pass

        for key, btn in self._nav_buttons.items():
            if hasattr(btn, "set_active"):
                btn.set_active(key == page)
            elif ctk:
                btn.configure(fg_color=COLORS["primary_soft"] if key == page else "transparent")
            else:
                btn.configure(bg=COLORS["primary_soft"] if key == page else COLORS["sidebar"])

        trace = getattr(self, "_perf_trace", None)
        if trace is not None:
            trace.emit(
                "page_switch",
                previous_page=previous_page,
                page=page,
                callback_ms=round((time.perf_counter() - started) * 1000, 2),
                lift_ms=lift_ms,
            )

    def _page_view(self, page):
        """返回主内容区页面，供切换时提升目标层。"""
        if page in ("leads", "interaction"):
            return getattr(self, f"{page}_page", None)
        return {
            "overview": getattr(self, "overview_page", None),
            "accounts": getattr(self, "tab_acct", None),
            "tasks": getattr(self, "tab_task", None),
            "keywords": getattr(self, "keywords_page", None),
            "settings": getattr(self, "settings_page", None),
        }.get(page)

    def _ensure_settings_page(self):
        if self.settings_page is None:
            self.settings_page = SettingsPage(
                self.page_host,
                self.app_config,
                on_save=self._on_settings_saved,
                inspect_bitbrowser=self._inspect_bitbrowser,
                inspect_platform_health=self._inspect_platform_health,
                inspect_live_platform_health=self._inspect_live_platform_health,
                inspect_deep_platform_health=self._inspect_deep_platform_health,
                inspect_manual_deep_platform_health=self._inspect_manual_deep_platform_health,
                export_diagnostic_package=self._export_diagnostic_package,
                test_llm_api=self._test_llm_api,
                get_llm_sample=self._get_llm_sample,
                analyze_llm_comment=self._analyze_llm_comment,
                generate_llm_reply=self._generate_llm_reply,
                analyze_llm_batch=self._analyze_llm_batch,
                get_operation_logs=self._get_operation_logs,
                export_operation_log=self._export_operation_log,
            )
        return self.settings_page

    def _ensure_keywords_page(self):
        if self.keywords_page is None:
            self.keywords_page = KeywordsPage(
                self.page_host, self.db_path, on_log=self._log,
                on_changed=self._refresh_keyword_group_options,
            )
        return self.keywords_page

    def _refresh_keyword_group_options(self):
        """后台刷新新建任务可用的关键词组，避免打开任务弹窗时查询数据库。"""
        if getattr(self, "_keyword_group_load_inflight", False):
            return
        self._keyword_group_load_inflight = True

        def worker():
            conn = None
            groups = []
            error = None
            try:
                conn = db.init_db(self.db_path, check_same_thread=False)
                from operations.keywords import KeywordGroupStore
                store = KeywordGroupStore(conn)
                groups = [store.get(item["id"]) for item in store.list_groups(enabled_only=True)]
            except Exception as exc:  # noqa: BLE001
                error = exc
            finally:
                if conn is not None:
                    conn.close()
            try:
                self._ui_call(self._apply_keyword_group_options, groups, error)
            except (tk.TclError, RuntimeError):
                pass

        threading.Thread(target=worker, name="keyword-groups-load", daemon=True).start()

    def _apply_keyword_group_options(self, groups, error=None):
        self._keyword_group_load_inflight = False
        if error:
            self._log(f"读取关键词组失败：{type(error).__name__}: {error}")
            return
        self._keyword_group_options = [item for item in groups if item]
        refresh_task_groups = getattr(self, "_keyword_group_task_refresh", None)
        if callable(refresh_task_groups):
            try:
                self.root.after_idle(refresh_task_groups)
            except (tk.TclError, RuntimeError):
                pass

    @staticmethod
    def _keyword_group_choices(groups, platform):
        choices = [("", "不使用关键词组")]
        for group in groups:
            group_platform = group.get("platform") or ""
            # 兼容早期设置页把显示文本“全部平台”直接落库的记录。
            group_platform = {"全部平台": "", "抖音": "douyin", "小红书": "xhs",
                              "微博": "weibo", "B站": "bilibili", "快手": "kuaishou"}.get(
                                  group_platform, group_platform)
            if group_platform and group_platform != platform:
                continue
            platform_cn = PLATFORM_CN.get(group_platform, "全部平台") if group_platform else "全部平台"
            label = f"{group.get('name') or '未命名'} · {platform_cn} · v{group.get('version', 1)}"
            choices.append((str(group.get("id")), label))
        return choices

    def _fill_keyword_from_group(self, group_id):
        try:
            group = next((item for item in self._keyword_group_options
                          if str(item.get("id")) == str(group_id)), None)
            if not group:
                return
            # 关键词组是任务执行时逐个搜索的预制词列表；任务表中的 keyword
            # 只保留组名，避免把多个词拼成一条错误的搜索词。
            self.var_kw.set(str(group.get("name") or "关键词组任务").strip())
        except Exception:
            pass

    def _open_keyword_group_manager(self, parent=None, focus_new=False):
        """从新建任务弹窗打开完整的关键词组新建/编辑管理器。"""
        if ctk is None:
            self._show_page("keywords")
            return
        current = getattr(self, "_keyword_group_manager_dialog", None)
        if current is not None and current.winfo_exists():
            current.deiconify()
            current.lift()
            current.focus_force()
            return
        parent = parent or self.root
        task_dialog = parent if parent is not self.root else None
        if task_dialog is not None:
            try:
                task_dialog.grab_release()
            except tk.TclError:
                pass
        manager = ctk.CTkToplevel(parent)
        manager.title("关键词组管理")
        manager.geometry("980x700")
        manager.minsize(760, 560)
        manager.transient(parent)
        manager.configure(fg_color=COLORS["window"])
        manager.overrideredirect(False)
        page = KeywordsPage(
            manager, self.db_path, on_log=self._log,
            on_changed=self._refresh_keyword_group_options,
        )
        page.pack(fill="both", expand=True)
        manager._keyword_group_page = page
        self._keyword_group_manager_dialog = manager

        # 以打开它的任务弹窗为基准居中，并限制在主软件窗口范围内。
        # 不使用系统默认位置，避免 Windows 将二级弹窗放到桌面左上角。
        try:
            owner = parent if parent.winfo_exists() else self.root
            owner.update_idletasks()
            manager.update_idletasks()
            owner_x, owner_y = owner.winfo_rootx(), owner.winfo_rooty()
            owner_w, owner_h = owner.winfo_width(), owner.winfo_height()
            manager_w = min(980, max(700, owner_w - 40))
            manager_h = min(700, max(520, owner_h - 40))
            x = owner_x + max(20, (owner_w - manager_w) // 2)
            y = owner_y + max(20, (owner_h - manager_h) // 2)
            manager.geometry(f"{manager_w}x{manager_h}+{x}+{y}")
        except tk.TclError:
            pass

        def close_manager():
            self._keyword_group_manager_dialog = None
            try:
                manager.grab_release()
                manager.destroy()
            finally:
                if task_dialog is not None and task_dialog.winfo_exists():
                    task_dialog.grab_set()
                    task_dialog.lift()
                    task_dialog.focus_force()
                    refresh_task_groups = getattr(self, "_keyword_group_task_refresh", None)
                    if callable(refresh_task_groups):
                        refresh_task_groups()

        manager.protocol("WM_DELETE_WINDOW", close_manager)
        if task_dialog is not None:
            manager.grab_set()
        manager.lift()
        manager.focus_force()
        if focus_new:
            manager.after(350, page._new)

    def _inspect_bitbrowser(self):
        if self.bb is None:
            return {"healthy": False, "error": "演示模式未连接 BitBrowser", "windows": []}
        from bitbrowser_inspector import BitBrowserInspector
        return BitBrowserInspector(self.bb).inspect()

    def _inspect_platform_health(self):
        """运行不触碰浏览器的五平台基础健康检查。"""
        from operations.platform_health import PlatformHealthChecker
        conn = db.init_db(self.db_path, check_same_thread=False)
        try:
            return PlatformHealthChecker(conn).run()
        finally:
            conn.close()

    def _inspect_live_platform_health(self):
        """运行真实浏览器只读诊断；不导航、不点击、不输入、不发送。"""
        if self.bb is None:
            return {"rows": [], "checked_at": "", "mode": "browser_unavailable"}
        from operations.browser_health import BrowserHealthChecker
        run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_dir = os.path.join(
            PROJECT_ROOT, "data", "diagnostics", f"browser_health_{run_stamp}", "screenshots"
        )
        conn = db.init_db(self.db_path, check_same_thread=False)
        try:
            return BrowserHealthChecker(
                conn, self.bb, screenshot_dir=screenshot_dir
            ).run()
        finally:
            conn.close()

    def _inspect_deep_platform_health(self, manual_sample=None):
        """运行真实浏览器深度模拟；填入测试文本后停止，绝不发送。"""
        if self.bb is None:
            return {"rows": [], "checked_at": "", "mode": "browser_unavailable",
                    "send_executed": False}
        from operations.browser_deep_diagnostic import DeepBrowserDiagnosticRunner
        conn = db.init_db(self.db_path, check_same_thread=False)
        try:
            return DeepBrowserDiagnosticRunner(conn, self.bb).run(manual_sample=manual_sample)
        finally:
            conn.close()

    def _inspect_manual_deep_platform_health(self, sample):
        return self._inspect_deep_platform_health(manual_sample=sample)

    def _export_diagnostic_package(self, health_result=None):
        """导出最近真实诊断的脱敏包，不把密钥或原始浏览器标识带出。"""
        from operations.diagnostic_package import DiagnosticPackageExporter
        result = DiagnosticPackageExporter(self.db_path).export(
            health_result=health_result
        )
        self._log(f"诊断包已生成：{result['path']}")
        return result

    def _on_settings_saved(self, bitbrowser_config, _api_config):
        """保存设置后更新当前客户端；不输出 API Key 或其它敏感字段。"""
        if self.demo:
            self._log("设置已保存（演示模式不会连接 BitBrowser）")
            return
        try:
            from bitbrowser import BitBrowserClient
            self.bb = BitBrowserClient(
                base_url=bitbrowser_config.get("base_url") or "http://127.0.0.1:54345",
                timeout=float(bitbrowser_config.get("timeout") or 20),
            )
            if self.sched is not None:
                self.sched.bb = self.bb
            for obj in (getattr(self, "collector", None), getattr(self, "live_collector", None),
                        getattr(self, "weibo_collector", None), getattr(self, "bilibili_collector", None)):
                if obj is not None and hasattr(obj, "bb"):
                    obj.bb = self.bb
            presenter = getattr(self, "_interaction_presenter", None)
            service = getattr(presenter, "_service", None)
            if service is not None:
                service._reply_adapter = BitBrowserReplyAdapter(self.bb)
            self._log("BitBrowser 连接设置已应用，请点击“检测并识别端口”确认服务状态")
        except Exception as exc:
            self._log(f"应用 BitBrowser 设置失败：{type(exc).__name__}: {exc}")

    def _test_llm_api(self, api_config):
        """只读检测智能 API，不发送回复或评论内容。"""
        from llm_api import LLMApiClient
        result = LLMApiClient.check_connection(api_config)
        detail = str(result.get("detail") or "未返回检测结果")
        self._log(f"智能 API 连通性检测：{detail}")
        return result

    def _get_llm_sample(self):
        """随机读取一条本地评论，供 API 功能测试使用。"""
        conn = db.init_db(self.db_path, check_same_thread=False)
        try:
            row = conn.execute(
                "SELECT c.id, c.platform, c.nickname, c.content, c.comment_time, "
                "v.title AS video_title, v.url AS video_url "
                "FROM comments c JOIN videos v ON v.id = c.video_id "
                "WHERE TRIM(COALESCE(c.content, '')) <> '' "
                "ORDER BY RANDOM() LIMIT 1"
            ).fetchone()
            if not row:
                return {}
            labels = {"douyin": "抖音", "xhs": "小红书", "weibo": "微博", "bilibili": "B站", "kuaishou": "快手"}
            result = dict(row)
            result["platform_label"] = labels.get(result.get("platform"), result.get("platform") or "未知平台")
            return result
        finally:
            conn.close()

    def _analyze_llm_comment(self, api_config, sample):
        from llm_api import LLMApiClient
        result = LLMApiClient.analyze_comment(api_config, sample)
        self._log(f"智能 API 评论分析测试：{result.get('detail') or '未返回结果'}")
        return result

    def _generate_llm_reply(self, api_config, sample):
        from llm_api import LLMApiClient
        result = LLMApiClient.generate_reply(api_config, sample)
        self._log(f"智能 API 回复生成测试：{result.get('detail') or '未返回结果'}")
        return result

    def _analyze_llm_batch(self, api_config, progress_callback=None):
        """读取最多 500 条本地评论，单次调用 API 分析意向；不生成回复。"""
        conn = db.init_db(self.db_path, check_same_thread=False)
        try:
            rows = conn.execute(
                "SELECT c.id, c.platform, c.nickname, c.content, c.comment_time, "
                "v.title AS video_title, v.url AS video_url "
                "FROM comments c JOIN videos v ON v.id = c.video_id "
                "WHERE TRIM(COALESCE(c.content, '')) <> '' "
                "ORDER BY c.id LIMIT 500"
            ).fetchall()
            labels = {"douyin": "抖音", "xhs": "小红书", "weibo": "微博", "bilibili": "B站", "kuaishou": "快手"}
            samples = []
            for row in rows:
                item = dict(row)
                item["platform_label"] = labels.get(item.get("platform"), item.get("platform") or "未知平台")
                samples.append(item)
        finally:
            conn.close()
        from llm_api import LLMApiClient, is_llm_eligible_comment
        eligible_samples = [item for item in samples if is_llm_eligible_comment(item)]
        filtered_count = len(samples) - len(eligible_samples)
        if filtered_count:
            self._log(
                f"智能 API 批量意向测试：已过滤 {filtered_count} 条非文本评论，"
                "不发送 LLM"
            )
        if callable(progress_callback):
            progress_callback(0, len(eligible_samples), "已读取评论，正在提交")
        result = LLMApiClient.analyze_comments_batch(
            api_config, eligible_samples, progress_callback=progress_callback
        )
        self._log(f"智能 API 批量意向测试：{result.get('detail') or '未返回结果'}")
        return result

    def _ensure_lead_pages(self, page):
        """构造并返回线索/互动页面；启动阶段会预先调用。"""
        try:
            if page == "leads":
                if self.leads_page is None:
                    conn = self._lead_conn()
                    repo = LeadRepository(conn)
                    service = LeadService(repo)
                    presenter = LeadPagePresenter(service)
                    self.leads_page = LeadsPage(self.page_host, presenter,
                                                on_assign=self._on_leads_assign,
                                                on_export=self._on_leads_export,
                                                on_owners=self._open_owners_dialog,
                                                on_to_interaction=self._on_leads_to_interaction)
                return self.leads_page
            if page == "interaction":
                if self.interaction_page is None:
                    conn = self._lead_conn()
                    lead_repo = LeadRepository(conn)
                    inter_repo = InteractionRepository(conn)
                    reply_adapter = BitBrowserReplyAdapter(self.bb) if self.bb is not None else None
                    service = InteractionService(
                        inter_repo, lead_repo, reply_adapter=reply_adapter
                    )
                    presenter = InteractionPagePresenter(service)
                    self._interaction_presenter = presenter
                    self.interaction_page = InteractionPage(
                        self.page_host, presenter,
                        on_templates=self._open_templates_dialog,
                        on_reply=(self._on_interaction_reply
                                  if service.browser_reply_enabled else None),
                        on_batch_reply=(self._on_interaction_batch_reply
                                        if reply_adapter is not None else None),
                        on_real_send_change=self._on_real_send_change,
                    )
                return self.interaction_page
        except Exception as exc:  # noqa: BLE001
            self._log(f"线索/互动页面初始化失败: {exc}")
            return None
        return None

    def _on_interaction_reply(self, draft_id: int):
        """后台定位并填入已审核回复；发送前必须再次人工确认。"""
        presenter = getattr(self, "_interaction_presenter", None)
        if presenter is None:
            self._log("互动服务尚未初始化")
            return
        self._log(f"草稿#{draft_id} 正在定位原评论并填入回复框…")

        def fill_worker():
            try:
                result = presenter.fill_browser_reply(draft_id)
            except Exception as exc:  # noqa: BLE001
                self._ui_call(lambda: self._log(f"回复填充失败: {exc}"))
                return

            def after_fill():
                self._log(f"草稿#{draft_id}: {result.message}")
                if not result.ok:
                    return
                confirmed = ask_centered_confirm(
                    self.root,
                    "确认发送回复",
                    "回复内容已填入对应评论框，是否点击发送？",
                    danger=True,
                )
                if not confirmed:
                    self._log(f"草稿#{draft_id} 已填入，用户取消发送")
                    return

                def send_worker():
                    try:
                        sent = presenter.send_browser_reply(draft_id)
                        self._ui_call(lambda: self._log(
                            f"草稿#{draft_id}: {sent.message}"))
                    except Exception as exc:  # noqa: BLE001
                        self._ui_call(lambda: self._log(
                            f"草稿#{draft_id} 发送失败: {exc}"))

                threading.Thread(target=send_worker,
                                 name=f"reply-send-{draft_id}", daemon=True).start()

            self._ui_call(after_fill)

        threading.Thread(target=fill_worker,
                         name=f"reply-fill-{draft_id}", daemon=True).start()

    def _on_real_send_change(self, enabled: bool):
        """记录会话级真实发送开关；开关状态不跨重启保存。"""
        self._real_send_enabled = bool(enabled)
        self._log(
            "⚠ 真实发送已开启：后续回复将自动点击最终发送按钮"
            if enabled else "真实发送已关闭：后续回复只填入，不点击发送"
        )

    def _on_interaction_batch_reply(self, draft_ids, account_id,
                                    real_send_enabled=False):
        """打开选定账号，逐条填入；仅在显式开关开启时点击最终发送。"""
        presenter = getattr(self, "_interaction_presenter", None)
        if presenter is None:
            self._log("互动服务尚未初始化")
            return
        draft_ids = [int(draft_id) for draft_id in draft_ids]
        real_send_enabled = bool(real_send_enabled)
        mode_label = ("真实发送；填入后自动点击最终发送按钮"
                      if real_send_enabled else "模拟回复；仅填入不发送")
        self._log(
            f"待发送池开始{mode_label}：账号#{account_id}，共 {len(draft_ids)} 条"
        )

        def worker():
            try:
                account = next(
                    (item for item in presenter.list_reply_accounts()
                     if int(item.get("id")) == int(account_id)),
                    None,
                )
                if account is None:
                    raise ValueError(f"回复账号不存在: {account_id}")
                name = account.get("name") or f"账号#{account_id}"
                wid = str(account.get("bb_window_id") or "")
                if wid and wid not in ("chrome", "chrome-bilibili"):
                    if not self._open_bb(wid, name, account.get("platform", "")):
                        raise RuntimeError(f"打开回复账号失败: {name}")
                elif wid:
                    self._log(f"复用外部 Chrome 回复账号: {name}")
                else:
                    self._log(f"账号 {name} 没有绑定浏览器窗口，继续尝试定位")

                for index, draft_id in enumerate(draft_ids, start=1):
                    try:
                        # 批量过程中关闭开关会立即阻止后续项目真实发送；
                        # 已经进入浏览器点击阶段的当前项目无法回滚。
                        send_this_item = bool(
                            real_send_enabled
                            and getattr(self, "_real_send_enabled", False)
                        )
                        if send_this_item:
                            result = presenter.send_browser_reply(
                                draft_id, account_id, real_send_enabled=True,
                            )
                        else:
                            result = presenter.simulate_browser_reply(draft_id, account_id)
                        action_label = "真实发送" if send_this_item else "模拟回复"
                        self._log(
                            f"{action_label} {index}/{len(draft_ids)} 草稿#{draft_id}: {result.message}"
                        )
                        if not result.ok:
                            presenter.mark_reply_failed(draft_id, result.message, account_id)
                    except Exception as exc:  # noqa: BLE001
                        action_label = ("真实发送" if real_send_enabled
                                        and getattr(self, "_real_send_enabled", False)
                                        else "模拟回复")
                        self._log(
                            f"{action_label} {index}/{len(draft_ids)} 草稿#{draft_id} 失败: {exc}"
                        )
                        try:
                            presenter.mark_reply_failed(draft_id, str(exc), account_id)
                        except Exception:
                            pass
                    time.sleep(0.35)
            except Exception as exc:  # noqa: BLE001
                self._log(f"待发送池回复中止: {exc}")
            finally:
                try:
                    self._ui_call(self.interaction_page.refresh)
                except Exception:
                    pass

        thread_mode = "send" if real_send_enabled else "simulate"
        threading.Thread(
            target=worker, name=f"reply-batch-{thread_mode}", daemon=True
        ).start()

    def _lead_conn(self):
        """线索/互动模块共享一个连接（懒创建，避免反复 init_db 开新连接）。"""
        if getattr(self, "_lead_db_conn", None) is None:
            self._lead_db_conn = db.init_db(self.db_path, check_same_thread=False)
        return self._lead_db_conn

    def _on_leads_assign(self, lead_ids):
        """线索中心批量分配：弹窗选择负责人（P2-2 修复）。"""
        if not lead_ids:
            self._log("未选择线索")
            return
        try:
            conn = self._lead_conn()
            owners = LeadRepository(conn).list_owners()
            if not owners:
                self._log("暂无负责人，请先点击「负责人」添加")
                return
            self._open_assign_dialog(lead_ids, owners)
        except Exception as exc:  # noqa: BLE001
            self._log(f"分配失败: {exc}")

    def _on_leads_to_interaction(self, lead_ids):
        """后台把线索中心勾选的用户加入互动中心，当前页面不切换。"""
        if not lead_ids:
            self._log("请先在线索中心勾选用户")
            return
        lead_ids = [int(lead_id) for lead_id in lead_ids]
        if getattr(self, "_interaction_add_inflight", False):
            self._log("正在后台加入互动中心，请稍候…")
            return
        self._interaction_add_inflight = True
        self._log(f"正在后台加入互动中心：{len(lead_ids)} 条，当前页面保持不变")
        threading.Thread(
            target=self._add_leads_to_interaction_worker,
            args=(lead_ids,),
            name="leads-to-interaction",
            daemon=True,
        ).start()

    def _add_leads_to_interaction_worker(self, lead_ids):
        """在线程中创建草稿，避免入库和资格判断阻塞线索中心。"""
        conn = None
        created = 0
        existing = 0
        failed = []
        try:
            # 不复用 Tk 主线程共享连接，避免列表查询与草稿写入互相阻塞。
            conn = db.init_db(self.db_path, check_same_thread=False)
            lead_repo = LeadRepository(conn)
            inter_repo = InteractionRepository(conn)
            service = InteractionService(inter_repo, lead_repo)
            open_statuses = {"draft", "pending_review", "approved", "queued"}
            for lead_id in lead_ids:
                try:
                    current = inter_repo.list_drafts(
                        lead_id=int(lead_id), page=1, page_size=20
                    )
                    if any(item.status in open_statuses
                           for item in current.get("items", [])):
                        existing += 1
                        continue
                    service.create_draft(
                        int(lead_id), template_id="greeting",
                        generation_mode="manual-selection",
                        manual_override=True,
                    )
                    created += 1
                except Exception as exc:  # noqa: BLE001 逐条隔离失败
                    failed.append(f"#{lead_id}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed.append(f"批量处理失败: {exc}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self._interaction_add_inflight = False

        def finish():
            self._log(
                f"已加入互动中心：新增 {created} 条，已有 {existing} 条，失败 {len(failed)} 条"
            )
            if failed:
                self._log("；".join(failed[:3]))
            # 当前页面不切换；若互动中心已经打开，只刷新其数据。
            if self.interaction_page is not None:
                self.interaction_page.refresh()

        try:
            self._ui_call(finish)
        except Exception:
            pass

    def _open_assign_dialog(self, lead_ids, owners):
        """分配弹窗：选择负责人并确认。"""
        top = ctk.CTkToplevel(self.root)
        top.title("分配线索")
        top.attributes("-topmost", True)
        top.configure(fg_color=COLORS["surface_2"])
        # 相对主窗口居中，不用固定屏幕坐标（8.1）
        self.root.update_idletasks()
        w, h = 420, 300
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 3
        top.geometry(f"{w}x{h}+{x}+{y}")
        top.grab_set()

        ctk.CTkLabel(top, text="选择负责人", text_color=COLORS["text"],
                     font=FONTS["section_title"], anchor="w").pack(
            fill="x", padx=20, pady=(18, 6))
        owner_var = ctk.StringVar(value=owners[0]["name"])
        menu = ctk.CTkOptionMenu(top, variable=owner_var,
                                 values=[o["name"] for o in owners],
                                 width=360, height=38, fg_color=COLORS["window"],
                                 button_color=COLORS["surface_3"],
                                 text_color=COLORS["text"], font=FONTS["body"])
        menu.pack(padx=20, pady=10)
        ctk.CTkLabel(top, text=f"将分配 {len(lead_ids)} 条线索",
                     text_color=COLORS["muted"], font=FONTS["helper"]).pack(padx=20)

        def confirm():
            name = owner_var.get()
            owner = next((o for o in owners if o["name"] == name), None)
            if not owner:
                top.destroy()
                return
            try:
                from leads.assignment import AssignmentService
                conn = self._lead_conn()
                res = AssignmentService(LeadRepository(conn)).assign(
                    lead_ids, owner["id"], "界面批量分配")
                self._log(f"分配完成: 成功 {res.success_count}，失败 {len(res.failed)}")
                if self.leads_page is not None:
                    self.leads_page.refresh()
            except Exception as exc:  # noqa: BLE001
                self._log(f"分配失败: {exc}")
            finally:
                top.destroy()

        btns = ctk.CTkFrame(top, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=18, side="bottom")
        ctk.CTkButton(btns, text="取消", width=100, height=34,
                      fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                      text_color=COLORS["text_2"], command=top.destroy).pack(side="right")
        ctk.CTkButton(btns, text="确认分配", width=120, height=34,
                      fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                      text_color="#ffffff", command=confirm).pack(side="right", padx=(0, 10))

    def _open_owners_dialog(self):
        """负责人管理弹窗：列出、新增、编辑、启用/停用（P2-2）。"""
        top = ctk.CTkToplevel(self.root)
        top.title("负责人管理")
        top.attributes("-topmost", True)
        top.configure(fg_color=COLORS["surface_2"])
        self.root.update_idletasks()
        w, h = 520, 420
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 3
        top.geometry(f"{w}x{h}+{x}+{y}")
        top.grab_set()

        conn = self._lead_conn()
        repo = LeadRepository(conn)

        def render():
            for child in body.winfo_children():
                child.destroy()
            for o in repo.list_owners(enabled_only=False):
                row = ctk.CTkFrame(body, fg_color=COLORS["surface_3"],
                                   corner_radius=8)
                row.pack(fill="x", padx=10, pady=3)
                status_txt = "启用" if o["enabled"] else "停用"
                ctk.CTkLabel(row, text=f"{o['name']} · {o.get('province') or '—'}",
                             text_color=COLORS["text"], font=FONTS["body"],
                             anchor="w").pack(side="left", padx=12, pady=8)
                ctk.CTkButton(row, text="停用" if o["enabled"] else "启用",
                              width=56, height=26, fg_color=COLORS["surface_2"],
                              hover_color=COLORS["surface_hover"],
                              text_color=COLORS["text_2"],
                              command=lambda oid=o["id"], en=o["enabled"]: (
                                  repo.update_owner(oid, enabled=not en), render())
                              ).pack(side="right", padx=6)
                ctk.CTkButton(row, text="编辑", width=56, height=26,
                              fg_color=COLORS["surface_2"], hover_color=COLORS["surface_hover"],
                              text_color=COLORS["text_2"],
                              command=lambda o=o: edit_owner(o)).pack(side="right", padx=6)

        def edit_owner(owner=None):
            """新增或编辑负责人。"""
            ed = ctk.CTkToplevel(top)
            ed.title("编辑负责人")
            ed.attributes("-topmost", True)
            ed.configure(fg_color=COLORS["surface_2"])
            ed.geometry(f"380x260+{top.winfo_rootx() + 60}+{top.winfo_rooty() + 40}")
            ed.grab_set()
            name_var = ctk.StringVar(value=owner["name"] if owner else "")
            prov_var = ctk.StringVar(value=owner.get("province") or "" if owner else "")
            contact_var = ctk.StringVar(value=owner.get("contact") or "" if owner else "")
            ctk.CTkLabel(ed, text="姓名", text_color=COLORS["muted"]).pack(padx=20, pady=(16, 2), anchor="w")
            ctk.CTkEntry(ed, textvariable=name_var, width=320, height=34,
                         fg_color=COLORS["window"], border_color=COLORS["border"]).pack(padx=20)
            ctk.CTkLabel(ed, text="省份", text_color=COLORS["muted"]).pack(padx=20, pady=(10, 2), anchor="w")
            ctk.CTkEntry(ed, textvariable=prov_var, width=320, height=34,
                         fg_color=COLORS["window"], border_color=COLORS["border"]).pack(padx=20)
            ctk.CTkLabel(ed, text="联系方式", text_color=COLORS["muted"]).pack(padx=20, pady=(10, 2), anchor="w")
            ctk.CTkEntry(ed, textvariable=contact_var, width=320, height=34,
                         fg_color=COLORS["window"], border_color=COLORS["border"]).pack(padx=20)

            def save():
                name = name_var.get().strip()
                if not name:
                    return
                if owner:
                    repo.update_owner(owner["id"], name=name, province=prov_var.get(),
                                      contact=contact_var.get())
                else:
                    repo.create_owner(name, prov_var.get(), contact_var.get())
                ed.destroy()
                render()

            ctk.CTkButton(ed, text="保存", width=100, height=34,
                          fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                          text_color="#ffffff", command=save).pack(pady=16, side="bottom")

        header = ctk.CTkFrame(top, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(16, 8))
        ctk.CTkLabel(header, text="负责人列表", text_color=COLORS["text"],
                     font=FONTS["section_title"], anchor="w").pack(side="left")
        ctk.CTkButton(header, text="＋ 新增负责人", width=110, height=32,
                      fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                      text_color="#ffffff", corner_radius=7,
                      command=lambda: edit_owner()).pack(side="right")

        body = ctk.CTkScrollableFrame(top, fg_color="transparent",
                                      scrollbar_button_color=COLORS["surface_3"])
        body.pack(fill="both", expand=True, padx=10, pady=(0, 12))
        render()

    def _open_templates_dialog(self):
        """模板管理弹窗：支持新增、编辑并持久化本地回复话术。"""
        from config_loader import templates_path
        from interactions.templates import TemplateEngine, template_label

        tpath = templates_path()
        engine = TemplateEngine.load(tpath)
        # 补默认模板（首次打开时）
        for tid, content in TemplateEngine().list_templates().items():
            if tid not in engine.list_templates():
                engine._templates[tid] = content
        top = ctk.CTkToplevel(self.root)
        top.title("回复模板")
        top.attributes("-topmost", True)
        top.configure(fg_color=COLORS["surface_2"])
        self.root.update_idletasks()
        w, h = 680, 560
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 3
        top.geometry(f"{w}x{h}+{x}+{y}")
        top.grab_set()

        header = ctk.CTkFrame(top, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(16, 8))
        ctk.CTkLabel(header, text="回复话术模板",
                     text_color=COLORS["text"], font=FONTS["section_title"],
                     anchor="w").pack(side="left")
        ctk.CTkButton(header, text="＋ 新增模板", width=112, height=32,
                      fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                      text_color="#ffffff", corner_radius=7,
                      command=lambda: add_template()).pack(side="right")
        system_variable_names = ["昵称", "平台", "省份", "城市", "产品", "联系方式提示"]
        available_vars_var = ctk.StringVar()

        def refresh_available_variables():
            names = system_variable_names + list(engine.list_custom_variables())
            available_vars_var.set("可用变量：" + "、".join(f"{{{{{name}}}}}" for name in names))

        refresh_available_variables()
        ctk.CTkLabel(top, textvariable=available_vars_var,
                     text_color=COLORS["muted"], font=FONTS["helper"],
                     anchor="w", wraplength=630, justify="left").pack(
            fill="x", padx=20, pady=(0, 8))

        list_frame = ctk.CTkScrollableFrame(
            top, fg_color=COLORS["surface_3"], corner_radius=8,
            scrollbar_button_color=COLORS["surface_2"])
        list_frame.pack(fill="both", expand=True, padx=20, pady=(0, 12))

        def reload_interaction_page():
            presenter = getattr(self, "_interaction_presenter", None)
            if presenter is not None:
                presenter.reload_templates()
            page = getattr(self, "interaction_page", None)
            if page is not None:
                page.refresh()

        def render_list():
            for child in list_frame.winfo_children():
                child.destroy()
            templates = engine.list_templates()
            for tid, content in templates.items():
                row = ctk.CTkFrame(list_frame, fg_color="transparent")
                row.pack(fill="x", padx=10, pady=4)
                text_box = ctk.CTkTextbox(row, height=64, fg_color=COLORS["window"],
                                          border_color=COLORS["border"], border_width=1,
                                          text_color=COLORS["text_2"], font=FONTS["helper"],
                                          wrap="word")
                text_box.pack(side="left", fill="both", expand=True, padx=(0, 8))
                text_box.insert("1.0", content)
                ctk.CTkLabel(row, text=template_label(tid), width=110, text_color=COLORS["text"],
                             font=FONTS["helper"], anchor="w").pack(side="left")
                # 保存：写回引擎并持久化到磁盘（P3-5）
                def save(tid=tid, tb=text_box):
                    try:
                        new_content = tb.get("1.0", "end").strip()
                        # 校验模板变量白名单
                        risks = engine.validate(tid, new_content)
                        if risks:
                            self._log(f"模板 {tid} 保存被拒: {'; '.join(risks)}")
                            return
                        engine._templates[tid] = new_content
                        engine.save(tpath)
                        reload_interaction_page()
                        self._log(f"话术模板“{template_label(tid)}”已保存")
                    except Exception as exc:  # noqa: BLE001
                        self._log(f"模板保存失败: {exc}")

                ctk.CTkButton(row, text="保存", width=56, height=26,
                              fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                              text_color="#ffffff", command=save).pack(side="left", padx=(4, 0))

        def add_template():
            ed = ctk.CTkToplevel(top)
            ed.title("新增回复话术")
            ed.attributes("-topmost", True)
            ed.configure(fg_color=COLORS["surface_2"])
            ed.geometry(f"620x620+{top.winfo_rootx() + 30}+{top.winfo_rooty() + 20}")
            ed.grab_set()
            name_var = ctk.StringVar()
            custom_name_var = ctk.StringVar()
            custom_value_var = ctk.StringVar()
            selected_token_var = ctk.StringVar(value="{{昵称}}")
            variable_hint_var = ctk.StringVar()
            status_var = ctk.StringVar()
            pending_variables = engine.list_custom_variables()

            ctk.CTkLabel(ed, text="模板名称", text_color=COLORS["muted"],
                         font=FONTS["helper"], anchor="w").pack(
                fill="x", padx=20, pady=(18, 4))
            ctk.CTkEntry(ed, textvariable=name_var, height=34,
                         fg_color=COLORS["window"], border_color=COLORS["border"],
                         text_color=COLORS["text"], placeholder_text="例如：节日问候").pack(
                fill="x", padx=20)
            ctk.CTkLabel(ed, text="话术内容", text_color=COLORS["muted"],
                         font=FONTS["helper"], anchor="w").pack(
                fill="x", padx=20, pady=(12, 4))
            content_box = ctk.CTkTextbox(
                ed, height=150, fg_color=COLORS["window"],
                border_color=COLORS["border"], border_width=1,
                text_color=COLORS["text"], font=FONTS["body"], wrap="word")
            content_box.pack(fill="x", padx=20)
            content_box.insert("1.0", "您好{{昵称}}！欢迎了解我们的服务，如需详情可以私信咨询。")

            variable_box = ctk.CTkFrame(
                ed, fg_color=COLORS["surface_3"], corner_radius=8)
            variable_box.pack(fill="both", expand=True, padx=20, pady=(12, 0))
            ctk.CTkLabel(
                variable_box, text="可用变量",
                text_color=COLORS["text"], font=FONTS["body_bold"],
                anchor="w").pack(fill="x", padx=12, pady=(10, 2))
            ctk.CTkLabel(
                variable_box,
                text="自定义变量由“名称＋替换内容”组成，保存后可在全部回复模板中使用。",
                text_color=COLORS["muted"], font=FONTS["helper"],
                anchor="w").pack(fill="x", padx=12, pady=(0, 8))

            insert_row = ctk.CTkFrame(variable_box, fg_color="transparent")
            insert_row.pack(fill="x", padx=12, pady=(0, 8))

            def token_values():
                names = system_variable_names + list(pending_variables)
                return [f"{{{{{name}}}}}" for name in names]

            token_menu = ctk.CTkOptionMenu(
                insert_row, variable=selected_token_var, values=token_values(),
                width=310, height=32, fg_color=COLORS["window"],
                button_color=COLORS["surface_hover"],
                button_hover_color=COLORS["primary_hover"],
                text_color=COLORS["text"])
            token_menu.pack(side="left", fill="x", expand=True, padx=(0, 8))

            def insert_selected_variable():
                token = selected_token_var.get().strip()
                if token:
                    content_box.insert("insert", token)
                    content_box.focus_set()

            ctk.CTkButton(
                insert_row, text="插入到话术", width=110, height=32,
                fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                text_color="#ffffff", command=insert_selected_variable).pack(side="right")

            add_row = ctk.CTkFrame(variable_box, fg_color="transparent")
            add_row.pack(fill="x", padx=12, pady=(0, 6))
            ctk.CTkEntry(
                add_row, textvariable=custom_name_var, width=145, height=32,
                fg_color=COLORS["window"], border_color=COLORS["border"],
                text_color=COLORS["text"], placeholder_text="变量名称，如门店名称"
            ).pack(side="left", padx=(0, 8))
            ctk.CTkEntry(
                add_row, textvariable=custom_value_var, height=32,
                fg_color=COLORS["window"], border_color=COLORS["border"],
                text_color=COLORS["text"], placeholder_text="替换内容，如郑州体验店"
            ).pack(side="left", fill="x", expand=True, padx=(0, 8))

            def refresh_variable_controls(selected_name=""):
                values = token_values()
                token_menu.configure(values=values)
                token = f"{{{{{selected_name}}}}}" if selected_name else ""
                if token in values:
                    selected_token_var.set(token)
                elif selected_token_var.get() not in values:
                    selected_token_var.set(values[0])
                if pending_variables:
                    variable_hint_var.set(
                        "已添加：" + "；".join(
                            f"{{{{{name}}}}}＝{value}"
                            for name, value in pending_variables.items()
                        )
                    )
                else:
                    variable_hint_var.set("尚未添加自定义变量")

            def add_custom_variable():
                variable_name = custom_name_var.get().strip()
                variable_value = custom_value_var.get().strip()
                risks = engine.validate_custom_variable(variable_name, variable_value)
                if risks:
                    status_var.set("；".join(risks))
                    return
                pending_variables[variable_name] = variable_value
                custom_name_var.set("")
                custom_value_var.set("")
                status_var.set(f"已添加变量 {{{{{variable_name}}}}}，可插入话术")
                refresh_variable_controls(variable_name)

            ctk.CTkButton(
                add_row, text="添加/更新", width=92, height=32,
                fg_color=COLORS["surface_hover"],
                hover_color=COLORS["primary_hover"],
                text_color=COLORS["text"], command=add_custom_variable
            ).pack(side="right")
            ctk.CTkLabel(
                variable_box, textvariable=variable_hint_var,
                text_color=COLORS["muted"], font=FONTS["helper"],
                anchor="w", justify="left", wraplength=555).pack(
                fill="x", padx=12, pady=(0, 8))
            refresh_variable_controls()

            def save_new():
                tid = name_var.get().strip()
                content = content_box.get("1.0", "end").strip()
                if not tid or not content:
                    status_var.set("模板名称和话术内容不能为空")
                    self._log("新增模板失败：名称和内容不能为空")
                    return
                if tid in engine._templates:
                    status_var.set(f"模板“{tid}”已存在")
                    self._log(f"新增模板失败：模板“{tid}”已存在")
                    return
                validation_engine = TemplateEngine(
                    engine.list_templates(), custom_variables=pending_variables)
                risks = validation_engine.validate(tid, content)
                if risks:
                    status_var.set("；".join(risks))
                    self._log(f"模板保存被拒: {'；'.join(risks)}")
                    return
                for variable_name, variable_value in pending_variables.items():
                    engine.set_custom_variable(variable_name, variable_value)
                engine._templates[tid] = content
                engine.save(tpath)
                ed.destroy()
                render_list()
                refresh_available_variables()
                reload_interaction_page()
                self._log(f"已新增话术模板“{tid}”")

            footer = ctk.CTkFrame(ed, fg_color="transparent")
            footer.pack(fill="x", padx=20, pady=12)
            ctk.CTkLabel(
                footer, textvariable=status_var, text_color=COLORS["danger"],
                font=FONTS["helper"], anchor="w", wraplength=420
            ).pack(side="left", fill="x", expand=True)
            ctk.CTkButton(
                footer, text="保存模板", width=110, height=34,
                fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                text_color="#ffffff", command=save_new).pack(side="right")

        render_list()
        ctk.CTkButton(top, text="关闭", width=100, height=34,
                      fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                      text_color=COLORS["text_2"], command=top.destroy
                      ).pack(pady=12, side="bottom")

    def _on_leads_export(self, lead_ids):
        """线索中心导出（MVP：导出到 data/exports 目录）。"""
        if not lead_ids:
            self._log("未选择线索")
            return
        try:
            conn = self._lead_conn()
            from leads.exporter import LeadExporter
            target = os.path.join(PROJECT_ROOT, "data", "exports")
            path = LeadExporter(LeadRepository(conn)).export(lead_ids, target)
            self._log(f"导出完成: {path}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"导出失败: {exc}")

    def _on_pane_resize(self, _event=None):
        """根据三栏实际宽度调整栏内字号，避免拖拽后文字截断。"""
        try:
            nav_w = max(220, self.sidebar.winfo_width())
            log_w = max(220, self.log_panel.winfo_width())
            nav_size = max(13, min(15, int(14 * nav_w / 250)))
            log_size = max(11, min(13, int(12 * log_w / 250)))
            signature = (nav_size, log_size)
            if signature == getattr(self, "_pane_font_signature", None):
                return
            self._pane_font_signature = signature
            for btn in self._nav_buttons.values():
                btn.configure(font=("Microsoft YaHei UI", nav_size))
            self.log_text.configure(font=("Consolas", log_size))
            self.log_header.configure(font=("Microsoft YaHei UI", max(13, log_size + 4), "bold"))
        except Exception:
            pass

    @staticmethod
    def _begin_dialog_move(dialog, event):
        dialog._drag_x = event.x_root - dialog.winfo_x()
        dialog._drag_y = event.y_root - dialog.winfo_y()
        dialog.lift()
        dialog.focus_force()

    @staticmethod
    def _queue_dialog_move(dialog, event):
        """把无边框弹窗拖动合并到约 60fps，避免每个鼠标事件都改 geometry。"""
        dialog._pending_move = (
            event.x_root - getattr(dialog, "_drag_x", 0),
            event.y_root - getattr(dialog, "_drag_y", 0),
        )
        if getattr(dialog, "_move_after", None) is None:
            try:
                dialog._move_after = dialog.after(16, GuiApp._flush_dialog_move, dialog)
            except Exception:
                dialog._move_after = None

    @staticmethod
    def _flush_dialog_move(dialog):
        dialog._move_after = None
        position = getattr(dialog, "_pending_move", None)
        if position is None:
            return
        dialog._pending_move = None
        try:
            dialog.geometry(f"+{position[0]}+{position[1]}")
        except Exception:
            pass

    def _build_overview(self):
        header = ctk.CTkFrame(self.overview_page, fg_color="transparent")
        header.pack(fill="x", padx=30, pady=(22, 18))
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left", fill="x", expand=True)
        self.overview_title = ctk.CTkLabel(titles, text="采集总览", text_color=COLORS["text"],
                                           anchor="w", font=FONTS["page_title"])
        self.overview_title.pack(fill="x")
        ctk.CTkLabel(titles, text="集中查看任务进度、账号状态与人工验证提醒",
                     text_color=COLORS["muted"], anchor="w",
                     font=FONTS["page_subtitle"]).pack(fill="x", pady=(4, 0))
        if ctk:
            ctk.CTkButton(header, text="＋  新建任务", width=142, height=42,
                          corner_radius=RADIUS["control"], fg_color=COLORS["primary"],
                          hover_color=COLORS["primary_hover"], font=("Microsoft YaHei UI", 15, "bold"),
                          command=self.open_new_task_dialog_ctk).pack(side="right")

        self.overview_stats = ctk.CTkFrame(self.overview_page, fg_color="transparent")
        self.overview_stats.pack(fill="x", padx=30)
        self.overview_banner = tk.Label(
            self.overview_page, text="", bg=COLORS["warning_bg"], fg=COLORS["warning"],
            anchor="w", justify="left", padx=18, wraplength=980,
            font=FONTS["body_bold"], height=2,
        )
        self.overview_banner.bind("<Button-1>", lambda _e: self._dismiss_human_banner(), add="+")
        panels = ctk.CTkFrame(self.overview_page, fg_color="transparent", height=520)
        self.overview_panels = panels
        panels.pack(fill="x", expand=False, padx=30, pady=(12, 24))
        panels.pack_propagate(False)
        panels.grid_rowconfigure(0, weight=1)
        panels.grid_columnconfigure(0, weight=1, uniform="overview-panels")
        panels.grid_columnconfigure(1, weight=1, uniform="overview-panels")
        self.overview_recent = ctk.CTkFrame(panels, fg_color=COLORS["surface_2"],
                                            corner_radius=RADIUS["card"], border_width=1,
                                            border_color=COLORS["border"])
        self.overview_recent.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.overview_accounts = ctk.CTkFrame(panels, fg_color=COLORS["surface_2"],
                                              corner_radius=RADIUS["card"], border_width=1,
                                              border_color=COLORS["border"])
        self.overview_accounts.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        for parent, title in ((self.overview_recent, "最近任务"), (self.overview_accounts, "账号状态")):
            title_row = ctk.CTkFrame(parent, fg_color="transparent", height=42)
            title_row.pack(fill="x", padx=20, pady=(14, 4))
            title_row.pack_propagate(False)
            ctk.CTkLabel(title_row, text=title, text_color=COLORS["text"],
                         font=FONTS["section_title"], anchor="w").pack(side="left")
            ctk.CTkLabel(title_row, text="查看全部  ›", text_color=COLORS["muted"],
                         font=FONTS["helper"], anchor="e").pack(side="right", pady=4)
        self.overview_recent_body = ctk.CTkFrame(self.overview_recent, fg_color="transparent")
        self.overview_recent_body.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self.overview_accounts_body = ctk.CTkFrame(self.overview_accounts, fg_color="transparent")
        self.overview_accounts_body.pack(fill="both", expand=True, padx=16, pady=(0, 12))

    def _refresh_overview(self, tasks, accounts):
        total_videos = sum(int(t.get("video_done", 0) or 0) for t in tasks.values())
        comments = sum(int(t.get("comments", 0) or 0) for t in tasks.values())
        running = sum(1 for t in tasks.values() if t.get("status") in ("phase_a_search", "phase_b_comments", "running"))
        waiting = len([a for a in accounts.values() if a.get("status") == "waiting_human"])
        stats = [("运行中任务", running, COLORS["info"]),
                 ("今日采集", total_videos, COLORS["text"]),
                 ("待处理评论", comments, COLORS["text"]),
                 ("需要人工处理", waiting, COLORS["warning"])]
        # 线索运营指标（来自 DashboardQueryService，不在 UI 现算 —— 8.5）
        lead_stats = self._lead_overview_stats()
        if lead_stats:
            stats += [("今日新增线索", lead_stats.get("today_new", 0), COLORS["info"]),
                      ("河南有效", lead_stats.get("henan", 0), COLORS["success"]),
                      ("高意向", lead_stats.get("high_intent", 0), COLORS["warning"]),
                      ("待审核草稿", lead_stats.get("awaiting_review", 0), COLORS["warning"])]
        if not getattr(self, "_overview_metric_cards", None):
            self._overview_metric_cards = []
            for index, (title, value, color) in enumerate(stats):
                card = MetricCard(self.overview_stats, title, f"{value:,}", color,
                                  ("▶", "↗", "◇", "!", "◆", "★", "●", "✓")[index % 8])
                card.pack(side="left", fill="both", expand=True,
                          padx=(0 if index == 0 else 6, 0 if index == len(stats) - 1 else 6))
                self._overview_metric_cards.append(card)
        else:
            for card, (_, value, _) in zip(self._overview_metric_cards, stats):
                card.update_value(f"{value:,}")
        body_sig = (
            tuple((int(tid), t.get("keyword"), t.get("platform"), t.get("status"),
                   t.get("target_count")) for tid, t in list(tasks.items())[:7]),
            tuple((str(key), a.get("name"), a.get("platform"), a.get("status"))
                  for key, a in accounts.items()),
        )
        if body_sig == getattr(self, "_overview_body_signature", None):
            return
        self._overview_body_signature = body_sig
        for body in (self.overview_recent_body, self.overview_accounts_body):
            for child in body.winfo_children():
                child.destroy()

        def separator(parent, row, columns):
            ctk.CTkFrame(parent, height=1, fg_color=COLORS["border_soft"]).grid(
                row=row, column=0, columnspan=columns, sticky="ew")

        # 最近任务：按设计图采用五列紧凑表格，而不是纵向任务卡片。
        recent = self.overview_recent_body
        for i in range(24):
            recent.grid_rowconfigure(i, weight=0)
        for col, (title, weight) in enumerate((("任务名称", 25), ("平台", 17), ("状态", 14), ("进度", 29), ("更新时间", 15))):
            recent.grid_columnconfigure(col, weight=weight, uniform="overview-recent")
            ctk.CTkLabel(recent, text=title, text_color=COLORS["muted"],
                         font=FONTS["helper"], anchor="w").grid(row=0, column=col, sticky="ew", padx=6, pady=(3, 8))
        separator(recent, 1, 5)
        for row_index, (tid, task) in enumerate(list(tasks.items())[:7]):
            grid_row = row_index * 2 + 2
            platform = PLATFORM_CN.get(task.get("platform"), task.get("platform", ""))
            status_key = task.get("status", "")
            status_text = TASK_STATUS_CN.get(status_key, status_key)
            target = max(1, int(task.get("target_count", 0) or 1))
            done = int(task.get("video_done", 0) or 0)
            ratio = min(1.0, done / target)
            ctk.CTkLabel(recent, text=str(task.get("keyword", "")), text_color=COLORS["text_2"],
                         font=FONTS["table"], anchor="w").grid(row=grid_row, column=0, sticky="ew", padx=6, pady=7)
            pcell = ctk.CTkFrame(recent, fg_color="transparent")
            pcell.grid(row=grid_row, column=1, sticky="w", padx=6, pady=5)
            PlatformBadge(pcell, platform, compact=True).pack(side="left")
            ctk.CTkLabel(pcell, text=platform, text_color=COLORS["text_2"], font=FONTS["helper"]).pack(side="left", padx=(5, 0))
            StatusBadge(recent, status_text, status_key).grid(row=grid_row, column=2, sticky="w", padx=6, pady=5)
            progress = ctk.CTkFrame(recent, fg_color="transparent")
            progress.grid(row=grid_row, column=3, sticky="ew", padx=6, pady=5)
            progress.grid_columnconfigure(0, weight=1)
            bar = ctk.CTkProgressBar(progress, height=7, corner_radius=4, fg_color=COLORS["surface_3"],
                                     progress_color=COLORS["success"] if ratio >= 1 else COLORS["primary"])
            bar.grid(row=0, column=0, sticky="ew", padx=(0, 8))
            bar.set(ratio)
            ctk.CTkLabel(progress, text=f"{int(ratio * 100)}%", text_color=COLORS["muted"],
                         font=FONTS["helper"], width=34).grid(row=0, column=1)
            stamp = task.get("updated_at") or task.get("created_at") or "--:--:--"
            if isinstance(stamp, str) and "T" in stamp:
                stamp = stamp.split("T", 1)[-1][:8]
            if isinstance(stamp, str) and " " in stamp:
                stamp = stamp.rsplit(" ", 1)[-1][:8]
            ctk.CTkLabel(recent, text=str(stamp), text_color=COLORS["muted"],
                         font=FONTS["helper"], anchor="w").grid(row=grid_row, column=4, sticky="w", padx=6, pady=5)
            separator(recent, grid_row + 1, 5)
        if not tasks:
            ctk.CTkLabel(recent, text="暂无任务", text_color=COLORS["muted"], font=FONTS["body"]).grid(row=2, column=0, columnspan=5, pady=24)

        # 账号状态：按平台聚合，展示在线/总数和状态分布。
        account_body = self.overview_accounts_body
        for i in range(24):
            account_body.grid_rowconfigure(i, weight=0)
        for col, (title, weight) in enumerate((("平台", 26), ("在线账号", 18), ("总账号", 15), ("状态分布", 41))):
            account_body.grid_columnconfigure(col, weight=weight, uniform="overview-accounts")
            ctk.CTkLabel(account_body, text=title, text_color=COLORS["muted"], font=FONTS["helper"],
                         anchor="w").grid(row=0, column=col, sticky="ew", padx=6, pady=(3, 8))
        separator(account_body, 1, 4)
        grouped = {}
        for account in accounts.values():
            platform = PLATFORM_CN.get(account.get("platform", "douyin"), "抖音")
            grouped.setdefault(platform, []).append(account)
        visible_rows = max(1, min(7, len(tasks)), len(grouped))
        panel_height = max(300, min(460, 108 + visible_rows * 48))
        self.overview_panels.configure(height=panel_height)
        platform_order = ["抖音", "小红书", "微博", "B站", "快手"]
        for row_index, platform in enumerate([p for p in platform_order if p in grouped]):
            grid_row = row_index * 2 + 2
            items = grouped[platform]
            online = sum(1 for a in items if a.get("status") not in {"closed", "dead", "disabled"})
            pcell = ctk.CTkFrame(account_body, fg_color="transparent")
            pcell.grid(row=grid_row, column=0, sticky="w", padx=6, pady=6)
            PlatformBadge(pcell, platform, compact=True).pack(side="left")
            ctk.CTkLabel(pcell, text=platform, text_color=COLORS["text_2"], font=FONTS["table"]).pack(side="left", padx=(7, 0))
            ctk.CTkLabel(account_body, text=str(online), text_color=COLORS["success"], font=FONTS["table_bold"],
                         anchor="w").grid(row=grid_row, column=1, sticky="w", padx=6, pady=6)
            ctk.CTkLabel(account_body, text=str(len(items)), text_color=COLORS["text_2"], font=FONTS["table"],
                         anchor="w").grid(row=grid_row, column=2, sticky="w", padx=6, pady=6)
            chips = ctk.CTkFrame(account_body, fg_color="transparent")
            chips.grid(row=grid_row, column=3, sticky="w", padx=6, pady=4)
            counts = {}
            for a in items:
                key = a.get("status", "")
                counts[key] = counts.get(key, 0) + 1
            running_count = sum(counts.get(key, 0) for key in ("running", "phase_a_search", "phase_b_comments", "working"))
            human_count = sum(counts.get(key, 0) for key in ("waiting_human", "dead", "need_login"))
            done_count = sum(counts.get(key, 0) for key in ("done", "idle", "ready", "active"))
            for label, count, palette_key in (("采集中", running_count, "running"),
                                               ("需人工", human_count, "waiting_human"),
                                               ("已完成", done_count, "done")):
                if count:
                    StatusBadge(chips, f"{label} {count}", palette_key).pack(side="left", padx=(0, 6))
            separator(account_body, grid_row + 1, 4)
        if not accounts:
            ctk.CTkLabel(account_body, text="暂无已绑定账号", text_color=COLORS["muted"], font=FONTS["body"]).grid(row=2, column=0, columnspan=4, pady=24)

    # ---------------- 账号管理页 ----------------
    def _lead_overview_stats(self):
        """从 DashboardQueryService 取线索运营指标（8.5：不在 UI 现算）。

        轻量只读查询；异常时返回空 dict，不影响总览其余部分。
        """
        try:
            dash = DashboardQueryService(self._lead_conn())
            return dash.summary()
        except Exception:
            return {}

    def _build_acct_tab(self):
        page_header = tk.Frame(self.tab_acct, bg=COLORS["window"])
        page_header.pack(fill="x", padx=30, pady=(22, 14))
        tk.Label(page_header, text="账号管理", bg=COLORS["window"], fg=COLORS["text"],
                 font=FONTS["page_title"], anchor="w").pack(fill="x")
        tk.Label(page_header, text="管理各平台账号及其浏览器环境，支持多账号并行采集",
                 bg=COLORS["window"], fg=COLORS["muted"], font=FONTS["page_subtitle"],
                 anchor="w").pack(fill="x", pady=(4, 0))

        frm = tk.Frame(self.tab_acct, bg=COLORS["surface"],
                       highlightthickness=1, highlightbackground=COLORS["border"])
        frm.pack(fill="x", padx=30, pady=(0, 10))
        tk.Label(frm, text="平台", bg=COLORS["surface"], fg=COLORS["muted"],
                 font=FONTS["helper"]).pack(side="left", padx=(16, 8), pady=12)
        self.var_bind_platform = tk.StringVar(value="抖音")
        if ctk:
            PlatformPicker(frm, self.var_bind_platform, ["抖音", "小红书", "微博", "B站", "快手"],
                           width=180, height=40).pack(side="left", padx=(0, 22))
        else:
            ttk.Combobox(frm, textvariable=self.var_bind_platform, values=["抖音", "小红书", "微博", "B站", "快手"], state="readonly", width=12).pack(side="left", padx=(0, 22))
        if ctk:
            ctk.CTkButton(frm, text="＋  创建浏览器", width=150, height=40,
                          corner_radius=RADIUS["control"], fg_color=COLORS["primary"],
                          hover_color=COLORS["primary_hover"], font=("Microsoft YaHei UI", 15, "bold"),
                          command=self.on_create_account).pack(side="left", padx=(0, 10))
            ctk.CTkButton(frm, text="打开浏览器", width=142, height=40,
                          corner_radius=RADIUS["control"], fg_color=COLORS["primary"],
                          hover_color=COLORS["primary_hover"], font=FONTS["body_bold"],
                          command=self.on_open_selected).pack(side="left", padx=(0, 10))
            ctk.CTkButton(frm, text="刷新窗口", width=116, height=40,
                          corner_radius=RADIUS["control"], fg_color=COLORS["surface_3"],
                          hover_color=COLORS["surface_hover"], border_width=1,
                          border_color=COLORS["border"], font=FONTS["body"],
                          command=self.refresh_window_list).pack(side="left")
        else:
            ttk.Button(frm, text="创建浏览器", command=self.on_create_account).pack(side="left", padx=3)
            ttk.Button(frm, text="打开浏览器", command=self.on_open_selected).pack(side="left", padx=3)
            ttk.Button(frm, text="刷新窗口", command=self.refresh_window_list).pack(side="left", padx=3)

        account_content = ctk.CTkFrame(self.tab_acct, fg_color="transparent")
        # 让账号列表占满页面头部和操作区之后的剩余高度，避免窗口下方出现大片空白。
        account_content.pack(fill="both", expand=True, padx=30, pady=(12, 24))
        account_content.grid_rowconfigure(0, weight=1)
        account_content.grid_columnconfigure(0, weight=1)
        # 刷新账号列表时需要动态调整内容区高度；保存引用，避免渲染阶段因
        # 只保留局部变量而触发 AttributeError，导致账号行被静默丢弃。
        self.account_content = account_content

        columns = [
            ("select", "", 5, "w"),
            ("name", "账号（昵称/ID）", 25, "w"),
            ("platform", "平台", 16, "w"),
            ("status", "状态", 15, "w", "status_key"),
            ("login", "绑定状态", 17, "w", "login_key"),
            ("bb_id", "浏览器窗口 ID", 25, "w"),
            ("menu", "", 6, "center"),
        ]
        self.account_table = ModernTable(account_content, columns, height=390)
        self.account_table.grid(row=0, column=0, sticky="nsew")

        op = ctk.CTkFrame(account_content, fg_color=COLORS["surface_2"], corner_radius=RADIUS["card"],
                          border_width=1, border_color=COLORS["border"])
        op.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        button_specs = [("打开此浏览器", self.on_open_row, "neutral"),
                        ("关闭此浏览器", self.on_close_row, "neutral"),
                        ("读取并绑定", self.on_bind, "primary"),
                        ("解除绑定", self.on_unbind, "neutral"),
                        ("安全规则", self._open_account_safety_dialog, "neutral"),
                        ("删除账号", self.on_delete_account, "danger")]
        for idx, (label, command, kind) in enumerate(button_specs):
            op.grid_columnconfigure(idx, weight=1, uniform="account-actions")
            if ctk:
                fg = COLORS["primary"] if kind == "primary" else "transparent"
                hover = COLORS["primary_hover"] if kind == "primary" else COLORS["surface_hover"]
                border = COLORS["danger"] if kind == "danger" else COLORS["border"]
                text_color = COLORS["danger"] if kind == "danger" else COLORS["text_2"]
                ctk.CTkButton(op, text=label, width=1, height=44, corner_radius=10,
                              fg_color=fg, hover_color=hover, border_width=1,
                              border_color=border, text_color=COLORS["text"] if kind == "primary" else text_color,
                              font=FONTS["body"], command=command).grid(
                                  row=0, column=idx, sticky="ew", padx=5, pady=10)
            else:
                ttk.Button(op, text=label, command=command).grid(row=0, column=idx, sticky="ew", padx=3)

        self._window_rows = []

    def _selected_account_values(self):
        row = self.account_table.get_selected() if hasattr(self, "account_table") else None
        if not row:
            return None
        return (row["name"], row["platform"], row["status"], row["login"], row["bb_id"])

    def _open_account_safety_dialog(self):
        """配置当前账号的采集/回复安全限制；只写本地规则，不操作浏览器。"""
        selected = self._selected_account_values()
        if not selected:
            messagebox.showinfo("账号安全规则", "请先在账号列表中选择一个已绑定账号。", parent=self.root)
            return
        conn = db.init_db(self.db_path, check_same_thread=False)
        try:
            row = conn.execute(
                "SELECT id, name, platform FROM accounts WHERE name = ? AND platform = ? LIMIT 1",
                (selected[0], selected[1]),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            messagebox.showwarning("账号安全规则", "当前行还没有绑定到本地账号，不能配置安全规则。",
                                   parent=self.root)
            return
        from operations.account_safety import AccountSafetyLimits, AccountSafetyStore
        account_id, account_name, platform = row["id"], row["name"], row["platform"]
        current_conn = db.init_db(self.db_path, check_same_thread=False)
        try:
            limits = AccountSafetyStore(current_conn).get(account_id)
        finally:
            current_conn.close()

        dialog = ctk.CTkToplevel(self.root)
        dialog.title(f"账号安全规则 · {account_name}")
        dialog.geometry("620x520")
        dialog.minsize(560, 480)
        dialog.transient(self.root)
        dialog.configure(fg_color=COLORS["window"])
        dialog.grab_set()
        dialog.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(dialog, text=f"账号安全规则 · {account_name}",
                     text_color=COLORS["text"], font=FONTS["page_title"]).grid(
                         row=0, column=0, columnspan=2, sticky="w", padx=24, pady=(22, 4))
        ctk.CTkLabel(dialog, text=f"平台：{PLATFORM_CN.get(platform, platform)}；达到限制后只暂停当前账号。",
                     text_color=COLORS["muted"], font=FONTS["helper"]).grid(
                         row=1, column=0, columnspan=2, sticky="w", padx=24, pady=(0, 14))
        fields = [
            ("hourly_collect_limit", "每小时采集上限（0=不限）"),
            ("daily_reply_limit", "每日回复上限（0=不限）"),
            ("min_delay_seconds", "最短操作间隔（秒）"),
            ("max_delay_seconds", "最长随机间隔（秒，0=不设）"),
            ("work_start", "工作开始（HH:MM，可留空）"),
            ("work_end", "工作结束（HH:MM，可留空）"),
            ("consecutive_failure_limit", "连续失败暂停阈值（0=不暂停）"),
        ]
        entries = {}
        for idx, (key, label) in enumerate(fields, start=2):
            ctk.CTkLabel(dialog, text=label, text_color=COLORS["text_2"],
                         font=FONTS["helper"]).grid(row=idx, column=0, sticky="w",
                                                     padx=(24, 12), pady=6)
            entry = ctk.CTkEntry(dialog, height=34, fg_color=COLORS["surface_2"],
                                 border_color=COLORS["border"], font=FONTS["body"])
            entry.grid(row=idx, column=1, sticky="ew", padx=(0, 24), pady=6)
            value = getattr(limits, key)
            if value is not None:
                entry.insert(0, str(value))
            entries[key] = entry
        enabled = tk.BooleanVar(value=bool(limits.enabled))
        ctk.CTkCheckBox(dialog, text="启用账号安全规则", variable=enabled,
                        fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                        text_color=COLORS["text_2"], font=FONTS["helper"]).grid(
                            row=9, column=0, columnspan=2, sticky="w", padx=24, pady=(8, 4))
        status = ctk.CTkLabel(dialog, text="规则仅在任务开始和每次采集前检查。",
                             text_color=COLORS["muted"], font=FONTS["helper"])
        status.grid(row=10, column=0, columnspan=2, sticky="w", padx=24, pady=(4, 10))

        def parse_limits():
            values = {}
            for key in ("hourly_collect_limit", "daily_reply_limit", "min_delay_seconds",
                        "max_delay_seconds", "consecutive_failure_limit"):
                raw = entries[key].get().strip() or "0"
                try:
                    value = int(raw)
                except ValueError as exc:
                    raise ValueError(f"{key} 必须是整数") from exc
                if value < 0:
                    raise ValueError(f"{key} 不能小于 0")
                values[key] = value
            for key in ("work_start", "work_end"):
                raw = entries[key].get().strip()
                if raw:
                    try:
                        datetime.strptime(raw, "%H:%M")
                    except ValueError as exc:
                        raise ValueError(f"{key} 请填写 HH:MM") from exc
                values[key] = raw or None
            if values["max_delay_seconds"] and values["max_delay_seconds"] < values["min_delay_seconds"]:
                raise ValueError("最长随机间隔不能小于最短操作间隔")
            values["enabled"] = bool(enabled.get())
            return AccountSafetyLimits(**values)

        def save():
            try:
                saved = parse_limits()
            except ValueError as exc:
                status.configure(text=str(exc), text_color=COLORS["danger"])
                return
            status.configure(text="正在保存…", text_color=COLORS["info"])

            def worker():
                error = None
                conn2 = None
                try:
                    conn2 = db.init_db(self.db_path, check_same_thread=False)
                    AccountSafetyStore(conn2).save(account_id, saved)
                except Exception as exc:  # noqa: BLE001
                    error = exc
                finally:
                    if conn2 is not None:
                        conn2.close()
                try:
                    self._ui_call(apply_save, error)
                except (tk.TclError, RuntimeError):
                    pass

            def apply_save(error):
                if error:
                    status.configure(text=f"保存失败：{type(error).__name__}", text_color=COLORS["danger"])
                    return
                self._log(f"账号 {account_name} 的安全规则已保存")
                status.configure(text="已保存", text_color=COLORS["success"])

            threading.Thread(target=worker, name="account-safety-save", daemon=True).start()

        footer = ctk.CTkFrame(dialog, fg_color="transparent")
        footer.grid(row=11, column=0, columnspan=2, sticky="e", padx=24, pady=(0, 20))
        ctk.CTkButton(footer, text="取消", width=90, height=34,
                      fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                      text_color=COLORS["text_2"], command=dialog.destroy).pack(side="left", padx=6)
        ctk.CTkButton(footer, text="保存规则", width=110, height=34,
                      fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                      command=save).pack(side="left")

    # ---------------- 任务管理页 ----------------
    def _build_task_tab(self):
        frm = ttk.Frame(self.tab_task, padding=10)
        frm.pack(fill="x")

        r1 = ttk.Frame(frm)
        r1.pack(fill="x", pady=3)
        ttk.Label(r1, text="平台:").pack(side="left")
        self.var_platform = tk.StringVar(value="抖音")
        ttk.Combobox(r1, textvariable=self.var_platform, values=["抖音", "小红书", "微博", "B站", "快手"],
                     state="readonly", width=8, style="TCombobox").pack(side="left", padx=(0, 12))
        ttk.Label(r1, text="模式:").pack(side="left")
        self.var_mode = tk.StringVar(value="标准")
        mode_box = ttk.Combobox(r1, textvariable=self.var_mode, values=["快速", "标准", "深度"],
                                state="readonly", width=7, style="TCombobox")
        mode_box.pack(side="left", padx=(4, 12))
        mode_box.bind("<<ComboboxSelected>>", self.on_mode_change)
        ttk.Label(r1, text="执行:").pack(side="left")
        self.var_execution_mode = tk.StringVar(value="单次采集")
        ttk.Combobox(r1, textvariable=self.var_execution_mode,
                     values=["单次采集", "定时增量监控"], state="readonly", width=11,
                     style="TCombobox").pack(side="left", padx=(4, 12))
        self.var_monitor_interval = tk.StringVar(value="3600")
        ttk.Label(r1, text="排序:").pack(side="left")
        self.var_sort = tk.StringVar(value="综合排序")
        self.sort_box = ttk.Combobox(
            r1, textvariable=self.var_sort, values=sort_labels("douyin"),
            state="readonly", width=10, style="TCombobox")
        self.sort_box.pack(side="left", padx=(4, 12))
        ttk.Label(r1, text="关键词:").pack(side="left")
        self.var_kw = tk.StringVar(value="智能快递柜")
        ttk.Entry(r1, textvariable=self.var_kw, width=28).pack(side="left", padx=(0, 8))
        ttk.Label(r1, text="(支持逗号分隔多个)", foreground="#888").pack(side="left")

        r2 = ttk.Frame(frm)
        r2.pack(fill="x", pady=3)
        ttk.Label(r2, text="参与采集的账号:").pack(side="left")
        self._task_acct_bindings = []
        self._selected_task_account_names = set()
        self.var_acct_summary = tk.StringVar(value="未选择账号")
        ttk.Button(r2, text="选择参与账号", command=self.on_choose_accounts).pack(side="left", padx=(6, 8))
        ttk.Label(r2, textvariable=self.var_acct_summary, foreground="#555").pack(side="left", padx=(0, 16))
        ttk.Label(r2, text="每批网址数:").pack(side="left")
        self.var_batch = tk.StringVar(value="10")
        ttk.Spinbox(r2, from_=1, to=50, textvariable=self.var_batch, width=4).pack(side="left", padx=(0, 12))
        ttk.Label(r2, text="冷却秒:").pack(side="left")
        self.var_cd = tk.StringVar(value="60")
        ttk.Spinbox(r2, from_=0, to=600, textvariable=self.var_cd, width=5).pack(side="left")
        ttk.Label(r2, text="目标数量:").pack(side="left", padx=(12, 0))
        self.var_target = tk.StringVar(value="500")
        self.var_only_comments = tk.BooleanVar(value=False)
        self.target_entry = ttk.Entry(r2, textvariable=self.var_target, width=8)
        self.target_entry.pack(side="left", padx=4)

        r3 = ttk.Frame(frm)
        r3.pack(fill="x", pady=6)
        ttk.Button(r3, text="➕ 创建任务", command=self.on_start).pack(side="left", padx=3)
        ttk.Label(r3, text="输出目录:").pack(side="left", padx=(18, 3))
        self.var_output_dir = tk.StringVar(
            value=os.path.join(PROJECT_ROOT, "data", "exports")
        )
        ttk.Entry(r3, textvariable=self.var_output_dir, width=34).pack(side="left", padx=3)
        ttk.Button(r3, text="选择", command=self.on_choose_output_dir).pack(side="left", padx=3)
        ttk.Label(r3, text="任务创建后，在任务卡片中选择开始、暂停、继续、停止或导出",
                  foreground="#666").pack(side="left", padx=10)

        # 旧的横向表单仅作为变量宿主；实际编辑使用卡片式弹窗。
        frm.pack_forget()
        hero = tk.Frame(self.tab_task, bg=COLORS["window"])
        hero.pack(fill="x", padx=30, pady=(22, 14))
        hero_titles = tk.Frame(hero, bg=COLORS["window"])
        hero_titles.pack(side="left", fill="x", expand=True)
        tk.Label(hero_titles, text="任务中心", bg=COLORS["window"], fg=COLORS["text"],
                 font=FONTS["page_title"], anchor="w").pack(fill="x")
        tk.Label(hero_titles, text="集中管理采集进度、账号状态与导出", bg=COLORS["window"],
                 fg=COLORS["muted"], font=FONTS["page_subtitle"], anchor="w").pack(fill="x", pady=(4, 0))
        if ctk:
            ctk.CTkButton(hero, text="＋  新建任务", width=142, height=42,
                          corner_radius=RADIUS["control"], fg_color=COLORS["primary"],
                          hover_color=COLORS["primary_hover"], font=FONTS["body_bold"],
                          command=self.open_new_task_dialog_ctk).pack(side="right")
        else:
            ttk.Button(hero, text="＋ 新建任务", command=self.open_new_task_dialog).pack(side="right")
        self.task_banner = tk.Label(
            self.tab_task, text="", bg=COLORS["warning_bg"], fg=COLORS["warning"],
            anchor="w", justify="left", padx=18, wraplength=980,
            font=FONTS["body_bold"], height=2,
        )
        self.task_banner.bind("<Button-1>", lambda _e: self._dismiss_human_banner(), add="+")
        filter_row = tk.Frame(self.tab_task, bg=COLORS["window"])
        filter_row.pack(fill="x", padx=30, pady=(12, 10))
        tk.Label(filter_row, text="任务列表", bg=COLORS["window"], fg=COLORS["text"],
                 font=FONTS["section_title"]).pack(side="left")
        tk.Label(filter_row, text="任务栏可直接开始/暂停、停止和导出",
                 bg=COLORS["window"], fg=COLORS["muted"],
                 font=FONTS["helper"]).pack(side="left", padx=14)
        # 任务卡片使用独立滚动区域；任务较多时不再挤压/裁剪底部内容。
        self.task_scroll = ctk.CTkFrame(self.tab_task, fg_color=COLORS["surface_2"], height=330,
                                        corner_radius=RADIUS["card"], border_width=1,
                                        border_color=COLORS["border"])
        self.task_scroll.pack(fill="x", padx=30, pady=(0, 14))
        self.task_scroll.pack_propagate(False)
        self.task_cards = ctk.CTkScrollableFrame(
            self.task_scroll, fg_color="transparent", corner_radius=RADIUS["card"],
            scrollbar_button_color=COLORS["surface_3"],
            scrollbar_button_hover_color=COLORS["primary"])
        self.task_cards.pack(fill="both", expand=True, padx=5, pady=5)

        # 任务列表
        self.task_tree = ttk.Treeview(self.tab_task, columns=("id", "kw", "plat", "mode", "status", "videos", "done", "comments"),
                                      show="headings", height=8)
        for c, h, w in [("id", "任务ID", 55), ("kw", "关键词", 150), ("plat", "平台", 55),
                        ("mode", "模式", 55),
                        ("status", "状态", 85), ("videos", "视频总数", 70),
                        ("done", "已完成", 60), ("comments", "评论数", 60)]:
            self.task_tree.heading(c, text=h, anchor="center")
            self.task_tree.column(c, width=w)
        self.task_tree.pack(fill="both", expand=True, padx=18, pady=(4, 8))
        self.task_tree.pack_forget()
        self.task_tree.bind("<Button-3>", self._show_task_menu)
        self.task_menu = tk.Menu(self.root, tearoff=0, bg="#18243a", fg="#eaf0fb",
                                 activebackground="#304b78", activeforeground="#ffffff",
                                 relief="flat", borderwidth=0)
        self.task_menu.add_command(label="▶ 开始采集", command=self.on_task_start)
        self.task_menu.add_command(label="⏸ 暂停采集", command=self.on_task_pause)
        self.task_menu.add_command(label="↩ 继续采集", command=self.on_task_resume)
        self.task_menu.add_command(label="⏹ 停止采集", command=self.on_task_stop)
        self.task_menu.add_separator()
        self.task_menu.add_command(label="📤 导出数据", command=self.on_task_export)
        self.task_menu.add_separator()
        self.task_menu.add_command(label="🗑 删除任务", command=self.on_task_delete)

        # 账号实时状态（中文状态）
        live_header = tk.Frame(self.tab_task, bg=COLORS["window"])
        live_header.pack(fill="x", padx=30, pady=(0, 8))
        tk.Label(live_header, text="账号实时状态", anchor="w", bg=COLORS["window"],
                 fg=COLORS["text"], font=FONTS["section_title"]).pack(side="left")
        live_columns = [
            ("name", "账号", 22, "w"), ("plat", "平台", 12, "w"),
            ("status", "状态", 16, "w", "status_key"),
            ("cd", "剩余冷却", 16, "w"), ("done", "已处理", 14, "w"),
            ("batch", "当前批次", 14, "w"),
        ]
        self.acct_live = ModernTable(self.tab_task, live_columns, height=190)
        self.acct_live.pack(fill="x", expand=False, padx=30, pady=(0, 24))

    def _on_task_mousewheel(self, event):
        """任务列表滚轮支持（Windows 鼠标滚轮）。"""
        try:
            delta = int(getattr(event, "delta", 0) or 0)
            units = -int(delta / 120) if delta else 0
            if units == 0 and delta:
                units = -1 if delta > 0 else 1
            if units:
                self.task_cards._parent_canvas.yview_scroll(units, "units")
        except Exception:
            pass
        return "break"

    def _install_scroll_router(self):
        """统一滚轮路由，避免多个 CTkScrollableFrame 的 bind_all 互相争用。"""
        try:
            self.root.unbind_all("<MouseWheel>")
            self.root.unbind_all("<Button-4>")
            self.root.unbind_all("<Button-5>")
            self.root.bind_all("<MouseWheel>", self._on_global_mousewheel, add="+")
            self.root.bind_all("<Button-4>", self._on_global_mousewheel, add="+")
            self.root.bind_all("<Button-5>", self._on_global_mousewheel, add="+")
            self._wheel_pending = {}
            self._wheel_after = None
        except Exception:
            pass

    def _on_global_mousewheel(self, event):
        """将滚轮事件交给鼠标所在的唯一滚动框，并合并高频事件。"""
        try:
            widget = self.root.winfo_containing(event.x_root, event.y_root)
            scrollable = None
            while widget is not None:
                if hasattr(widget, "_parent_canvas"):
                    scrollable = widget
                    break
                widget = getattr(widget, "master", None)
            if scrollable is None:
                return "break"
            if getattr(event, "num", None) == 4:
                units = -1
            elif getattr(event, "num", None) == 5:
                units = 1
            else:
                delta = int(getattr(event, "delta", 0) or 0)
                # Windows 标准滚轮 delta=120；按 CustomTkinter 的比例换算，
                # 不能压缩成 1 个 unit，否则任务列表会表现为滚动极慢。
                units = -int(delta / 6) if delta else 0
                if units == 0 and delta:
                    units = -1 if delta > 0 else 1
            if not units:
                return "break"
            self._wheel_pending[scrollable] = self._wheel_pending.get(scrollable, 0) + units
            if self._wheel_after is None:
                self._wheel_after = self.root.after(8, self._flush_mousewheel)
        except Exception:
            pass
        return "break"

    def _flush_mousewheel(self):
        try:
            pending, self._wheel_pending = self._wheel_pending, {}
            self._wheel_after = None
            for scrollable, units in pending.items():
                if units:
                    scrollable._parent_canvas.yview_scroll(units, "units")
        except Exception:
            self._wheel_after = None

    # ---------------- 数据导出页 ----------------
    def _build_exp_tab(self):
        frm = ttk.Frame(self.tab_exp, padding=10)
        frm.pack(fill="x")
        ttk.Label(frm, text="平台:").pack(side="left")
        self.exp_platform = tk.StringVar(value="抖音")
        ttk.Combobox(frm, textvariable=self.exp_platform, values=["抖音", "小红书", "微博", "B站", "快手"],
                     state="readonly", width=8, style="TCombobox").pack(side="left", padx=(0, 10))
        ttk.Label(frm, text="采集结果文件:").pack(side="left")
        self.exp_input = tk.StringVar(value=os.path.join(PROJECT_ROOT, "out", "抖音-智能快递柜", "videos.json"))
        ttk.Entry(frm, textvariable=self.exp_input, width=46).pack(side="left", padx=(0, 10))
        ttk.Button(frm, text="📤 导出4类CSV", command=self.on_export).pack(side="left")

        frm2 = ttk.Frame(self.tab_exp, padding=(10, 0))
        frm2.pack(fill="x")
        ttk.Label(frm2, text="数据库任务:").pack(side="left")
        self.exp_task = tk.StringVar(value="")
        self.exp_task_combo = ttk.Combobox(frm2, textvariable=self.exp_task,
                                           state="readonly", width=42, style="TCombobox")
        self.exp_task_combo.pack(side="left", padx=(6, 10))
        ttk.Button(frm2, text="📤 导出选中任务", command=self.on_export_task).pack(side="left")
        self._export_task_rows = []

        self.exp_log = tk.Text(self.tab_exp, height=14, state="disabled",
                               font=("Microsoft YaHei UI", 16))
        self.exp_log.pack(fill="both", expand=True, padx=10, pady=10)

    # ------------------------------------------------------------------ #
    # 账号管理操作
    # ------------------------------------------------------------------ #
    def _list_windows(self):
        """列出 BitBrowser 所有窗口。"""
        if self.bb is None:
            return []
        try:
            d = self.bb.list_browsers(page=0, page_size=100)
            return d.get("list", [])
        except Exception as e:
            self._log(f"读取窗口列表失败: {e}")
            return []

    def _mapping(self):
        """动态列出 BitBrowser 窗口（每个窗口=一个独立浏览器进程=一个账号候选）。
        平台按窗口 platform 字段或名称推断。"""
        wins = []
        if self.bb is not None:
            try:
                d = self.bb.list_browsers(page=0, page_size=100)
                wins = d.get("list", [])
            except Exception:
                wins = []
        out = []
        for w in wins:
            nm = w.get("name") or f"窗口{w.get('seq')}"
            wid = w.get("id")
            platform = w.get("platform") or ""
            # 平台识别：支持内部名 douyin/xhs 或站点URL / 窗口名推断
            pl = platform.lower()
            if "xiaohongshu" in pl:
                platform = "xhs"
            elif "douyin" in pl:
                platform = "douyin"
            elif "weibo" in pl:
                platform = "weibo"
            elif "bilibili" in pl or "b23.tv" in pl:
                platform = "bilibili"
            elif "kuaishou" in pl or "gifshow" in pl:
                platform = "kuaishou"
            else:
                if "小红" in nm:
                    platform = "xhs"
                elif "抖音" in nm or "douyin" in nm.lower():
                    platform = "douyin"
                elif "微博" in nm or "weibo" in nm.lower():
                    platform = "weibo"
                elif "B站" in nm or "哔哩" in nm or "bilibili" in nm.lower():
                    platform = "bilibili"
                elif "快手" in nm or "kuaishou" in nm.lower() or "gifshow" in nm.lower():
                    platform = "kuaishou"
                else:
                    platform = ""
            out.append({"name": nm, "id": wid, "seq": w.get("seq"), "platform": platform})
        return out

    def _window_status(self, wid):
        """查询窗口是否已打开(有进程)。"""
        try:
            pids = self.bb.pids(wid)
            return "打开" if pids.get(wid) else "关闭"
        except Exception:
            return "关闭"

    def _detect_login(self, wid, platform, name):
        """检测已打开窗口的登录态：连 CDP 看页面是否已登录。
        返回 "已登录" / "未登录" / "未检测"(连不上)。"""
        try:
            fname = ("dy_window.txt" if "抖音" in name else
                     ("xhs_window.txt" if "小红书" in name else
                      ("kuaishou_window.txt" if "快手" in name else None)))
            if not fname:
                return "未检测"
            path = os.path.join(PROJECT_ROOT, "data", fname)
            if not os.path.exists(path):
                return "未检测"
            lines = [l.strip() for l in open(path, encoding="utf-8") if l.strip()]
            ws = lines[-1]
            import asyncio
            from cdp import CdpSession
            c = CdpSession(ws)
            async def probe():
                await c.connect()
                sid = await c.attach_page()
                url = await c.eval("location.href", sid)
                # 抖音/小红书：检测「登录后/请登录/登录」关键提示
                if "抖音" in name:
                    r = await c.eval('''(function(){
                      const t = document.body ? document.body.innerText : '';
                      const hasLoginBtn = !!document.querySelector('[class*="login"], [class*="Login"]');
                      const showLoginPrompt = t.includes('登录后即可') || t.includes('一键登录') || t.includes('登录其他账号');
                      return {hasLoginBtn, showLoginPrompt, tLen:t.length};
                    })()''', sid)
                    await c.close()
                    if r.get("showLoginPrompt"):
                        return "未登录"
                    return "已登录"
                else:
                    r = await c.eval('''(function(){
                      const t = document.body ? document.body.innerText : '';
                      return {showLogin: t.includes('扫码登录') || t.includes('请登录') || t.includes('登录后查看')};
                    })()''', sid)
                    await c.close()
                    return "未登录" if r.get("showLogin") else "已登录"
            try:
                loop = asyncio.new_event_loop()
                res = loop.run_until_complete(probe())
                loop.close()
                return res
            except Exception:
                return "未检测"
        except Exception:
            return "未检测"

    def refresh_window_list(self):
        """刷新账号管理页：绑定账号(昵称/ID)从 DB 读，未绑定窗口显示窗口名。

        注意：list_browsers / pids 是同步的 BitBrowser HTTP 请求，可能较慢或超时，
        一律放到后台线程执行，只把结果调度回主线程重建表格，避免 GUI 卡死。
        """
        if getattr(self, "_winlist_inflight", False):
            return  # 防止周期刷新叠加出堆积的慢请求
        self._winlist_inflight = True
        threading.Thread(target=self._refresh_window_list_worker, daemon=True).start()

    def _schedule_window_refresh(self):
        """维持唯一的账号状态定时刷新，避免重复 after 回调堆积。"""
        try:
            old = getattr(self, "_window_refresh_after", None)
            if old is not None:
                self.root.after_cancel(old)
            self._window_refresh_after = self.root.after(15000, self.refresh_window_list)
        except tk.TclError:
            pass

    def _refresh_window_list_worker(self):
        """后台线程：读取窗口列表 + 状态 + 绑定，产出 rows 后交回主线程渲染。"""
        try:
            if self.demo:
                demo_rows = [
                    ("抖音1号", "抖音", "打开", "已绑定", "dy-231"),
                    ("抖音2号", "抖音", "空闲", "已绑定", "dy-232"),
                    ("抖音3号", "抖音", "需人工", "已绑定", "dy-233"),
                    ("小红书1号", "小红书", "打开", "已绑定", "xhs-221"),
                    ("小红书2号", "小红书", "空闲", "已绑定", "xhs-222"),
                    ("微博1号", "微博", "未打开", "未绑定", "wb-211"),
                ]
                self._ui_call(self._apply_window_rows, demo_rows,
                                [row[4] for row in demo_rows])
                return
            wins = self._mapping()
            bound = self._bound_accounts()
            rows = []
            for w in wins:
                wid = w["id"]
                plat = w.get("platform") or ""
                plat_cn = PLATFORM_CN.get(plat, "未绑定")
                try:
                    st = "打开" if plat == "weibo" else self._window_status(wid)
                except Exception:
                    st = "关闭"
                b = None
                for pk, items in bound.items():
                    for it in items:
                        if it.get("wid") == wid:
                            b = (pk, it)
                            break
                    if b is not None:
                        break
                if b is not None:
                    _, v = b
                    nick = v.get("name") or ""
                    label = nick or f"{plat_cn}·已绑定"
                    login = "已绑定"
                else:
                    label = w["name"]
                    login = ("未绑定" if st == "打开" else "未打开")
                rows.append((label, plat_cn, st, login, wid))
            # BitBrowser 可能只返回当前窗口，不能因此丢掉数据库里已经绑定的账号。
            # 将 DB 绑定记录补进来；浏览器未出现在窗口列表时显示为“未打开”。
            known_ids = {str(w["id"]) for w in wins}
            for pk, items in bound.items():
                plat_cn = PLATFORM_CN.get(pk, pk)
                for item in items:
                    wid = str(item.get("wid") or "")
                    if not wid or wid in known_ids:
                        continue
                    name = item.get("name") or f"{plat_cn}·已绑定"
                    rows.append((f"{plat_cn}·{name}", plat_cn, "未打开", "已绑定", wid))
                    known_ids.add(wid)
            window_ids = [w["id"] for w in wins] + [
                str(item.get("wid")) for pk, items in bound.items() for item in items
                if item.get("wid") and str(item.get("wid")) not in {str(w["id"]) for w in wins}
            ]
        except Exception as e:
            self._log(f"账号刷新失败：{type(e).__name__}: {e}")
            rows, window_ids = [], []
        finally:
            self._winlist_inflight = False
        try:
            self._ui_call(self._apply_window_rows, rows, window_ids)
        except Exception as e:
            self._log(f"账号表格渲染失败：{type(e).__name__}: {e}")

    def _apply_window_rows(self, rows, window_ids):
        """主线程：把后台算好的窗口行重建进账号管理表格。"""
        try:
            display_rows = []
            for r in rows:
                display_rows.append({
                    "_identity": str(r[4]), "name": r[0], "platform": r[1],
                    # 浏览器窗口 ID 单独放在最后一列，不重复显示在昵称/ID下方。
                    "subname": "",
                    "status": r[2], "status_key": r[2], "login": r[3],
                    "login_key": r[3], "bb_id": r[4], "select": "", "menu": "",
                })
            # 账号页下方仍有可用空间，扩大列表可视区，减少账号还没填满就出现
            # 大片留白的情况；账号更多时仍由表格自身滚动。
            table_height = min(500, max(420, 56 + len(display_rows) * 50))
            row_signature = (tuple(tuple(sorted(row.items())) for row in display_rows), tuple(str(x) for x in window_ids))
            if row_signature == getattr(self, "_window_rows_signature", None):
                self._schedule_window_refresh()
                return
            self._window_rows_signature = row_signature
            self.account_table.configure(height=table_height)
            if not self.account_table.update_rows(display_rows):
                self.account_table.set_rows(display_rows)
            self._window_rows = window_ids
            self._schedule_window_refresh()
        except Exception as e:
            self._log(f"账号表格渲染失败：{type(e).__name__}: {e}")

    def _bound_accounts(self):
        """从 scheduler accounts(DB) 读已绑定账号，按平台分组。"""
        bound = {}
        status_ok = False
        try:
            rep = self.sched.status_report()
            for _account_key, a in rep.get("accounts", {}).items():
                name = a.get("name", "")
                # 已绑定 = name 是昵称(非窗口名/演示账号名)
                if name in ("演示账号1", "演示账号2", "演示账号3"):
                    continue
                plat = a.get("platform") or "douyin"
                wid = a.get("bb_window_id") or ""
                bound.setdefault(plat, []).append({"name": name, "wid": wid})
            status_ok = True
        except Exception as e:
            self._log(f"读取账号状态失败，尝试数据库兜底：{e}")
        if status_ok:
            return bound
        # 运行时状态读取失败时，直接从本地数据库补齐绑定关系。
        # 账号展示不能依赖 BitBrowser 是否在线。
        try:
            conn = db.init_db(self.db_path, check_same_thread=False)
            cur = conn.execute("SELECT name, bb_window_id, platform FROM accounts ORDER BY id")
            existing = {(item.get("name"), str(item.get("wid") or ""))
                        for items in bound.values() for item in items}
            for row in cur.fetchall():
                # 同时兼容 sqlite3.Row 与普通 tuple，避免数据库连接配置差异导致整批账号被吞掉。
                try:
                    name = row["name"]
                    wid = str(row["bb_window_id"] or "")
                    plat = row["platform"] or "douyin"
                except (TypeError, IndexError):
                    name, wid, plat = row[0], str(row[1] or ""), row[2] or "douyin"
                if name in ("演示账号1", "演示账号2", "演示账号3") or not wid:
                    continue
                if (name, wid) not in existing:
                    bound.setdefault(plat, []).append({"name": name, "wid": wid})
            conn.close()
        except Exception as e:
            self._log(f"数据库账号读取失败：{type(e).__name__}: {e}")
        return bound

    def _read_account_quick(self, platform, name, window_id=None):
        """从指定窗口读取已登录账号；严禁回退到同平台的其它窗口。"""
        try:
            import account_reader
            fname = ("dy_window.txt" if platform == "douyin" else
                     ("xhs_window.txt" if platform == "xhs" else
                     ("weibo_chrome_window.txt" if platform == "weibo" else
                      ("bilibili_chrome_window.txt" if platform == "bilibili" else None))))
            if not fname:
                return {}
            # 拿最新 ws：尝试 bb.open_browser（已打开会复用并返回当前 ws）
            ws = None
            selected = next((w for w in self._mapping() if w["id"] == window_id), None)
            if selected and self._window_status(selected["id"]) == "打开":
                page_url = ("https://creator.xiaohongshu.com/new/note-manager?source=official" if platform == "xhs"
                            else ("https://weibo.com/" if platform == "weibo"
                                  else ("https://www.bilibili.com/" if platform == "bilibili"
                                        else "https://www.douyin.com/user/self?from_nav=1")))
                d = self.bb.open_browser(selected["id"], ignore_default_urls=True,
                                         new_page_url=page_url)
                ws = d.get("ws")
                if ws:
                    with open(os.path.join(PROJECT_ROOT, "data", fname), "w", encoding="utf-8") as f:
                        f.write(selected["id"] + "\n" + ws + "\n")
            if not ws:
                path = os.path.join(PROJECT_ROOT, "data", fname)
                if os.path.exists(path):
                    lines = [l.strip() for l in open(path, encoding="utf-8") if l.strip()]
                    ws = lines[-1] if lines else None
            if not ws:
                return {}
            return account_reader.read_account(platform, ws)
        except Exception:
            return {}

    def _open_bb(self, wid, name, platform=""):
        """打开一个 BitBrowser 窗口并导航到平台页（单界面）。
        平台用 platform 字段/URL 判断（兼容简称），打开后用 CDP 导航，避免工作台。"""
        try:
            # 判断平台：platform(URL) 优先，其次 name
            p = (platform or "").lower()
            if p in ("xhs", "xiaohongshu") or "xiaohongshu" in p or "小红" in name:
                page_url = "https://creator.xiaohongshu.com/new/note-manager?source=official"
                is_xhs = True
            elif p in ("douyin", "dy") or "douyin" in p or "抖音" in name or "douyin" in name.lower():
                page_url = "https://www.douyin.com/user/self?from_nav=1"
                is_xhs = False
            elif p in ("weibo", "wb") or "微博" in name or "weibo" in name.lower():
                page_url = "https://weibo.com/"
                is_xhs = False
            elif p in ("bilibili", "bili") or "B站" in name or "哔哩" in name or "bilibili" in name.lower():
                page_url = "https://www.bilibili.com/"
                is_xhs = False
            elif p in ("kuaishou", "ks") or "快手" in name or "kuaishou" in name.lower() or "gifshow" in name.lower():
                page_url = "https://www.kuaishou.com/new-reco"
                is_xhs = False
            else:
                page_url = "https://www.baidu.com/"
                is_xhs = False
            # 打开（ignore_default_urls 避免叠加多URL；不用 newPageUrl 因为不可靠）
            d = self.bb.open_browser(wid, ignore_default_urls=True)
            ws = d.get("ws")
            # 打开后导航到目标页
            try:
                import asyncio
                from cdp import CdpSession
                loop = asyncio.new_event_loop()
                async def _nav():
                    c = CdpSession(ws)
                    await c.connect()
                    sid = await c.attach_page()
                    await c.cmd("Page.navigate", {"url": page_url}, session_id=sid)
                    await asyncio.sleep(4)
                    await c.close()
                loop.run_until_complete(_nav())
                loop.close()
            except Exception:
                pass
            # 写 ws 到对应平台窗口文件
            if is_xhs:
                self._write_ws("xhs_window.txt", wid, ws)
            elif p in ("weibo", "wb") or "微博" in name or "weibo" in name.lower():
                self._write_ws("weibo_chrome_window.txt", wid, ws)
            elif p in ("bilibili", "bili") or "B站" in name or "哔哩" in name or "bilibili" in name.lower():
                self._write_ws("bilibili_chrome_window.txt", wid, ws)
            elif p in ("kuaishou", "ks") or "快手" in name or "kuaishou" in name.lower() or "gifshow" in name.lower():
                self._write_ws("kuaishou_window.txt", wid, ws)
            else:
                self._write_ws("dy_window.txt", wid, ws)
            self._log(f"🌐 已打开浏览器: {name}（单界面）")
            return True
        except Exception as e:
            self._log(f"打开 {name} 失败: {e}")
            return False

    def _write_ws(self, fname, wid, ws):
        path = os.path.join(PROJECT_ROOT, "data", fname)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(wid + "\n" + ws + "\n")
        except Exception:
            pass

    def on_batch_open(self):
        """按数量批量打开浏览器（后台执行，不阻塞 GUI）。"""
        if self.demo:
            messagebox.showinfo("演示模式", "演示模式无需打开浏览器")
            return
        n = 1
        self._log("正在批量打开浏览器…")
        threading.Thread(target=self._batch_open_worker, args=(n,), daemon=True).start()

    def _batch_open_worker(self, n):
        opened = 0
        try:
            for w in self._mapping():
                if opened >= n:
                    break
                wid = w["id"]
                try:
                    if self._window_status(wid) == "打开":
                        continue
                except Exception:
                    continue
                if self._open_bb(wid, w["name"], w.get("platform", "")):
                    opened += 1
                    time.sleep(1)  # 避免并发打开冲突
        except Exception:
            pass
        try:
            self._ui_call(lambda: self._log(
                f"批量打开完成，本次新开 {opened} 个浏览器。请在弹出的浏览器中登录账号。"))
            self._ui_call(self.refresh_window_list)
        except Exception:
            pass

    def on_open_row(self):
        vals = self._selected_account_values()
        if not vals:
            messagebox.showinfo("提示", "请先选中一个窗口")
            return
        wid = vals[4]
        name = vals[0]
        if self.demo:
            messagebox.showinfo("演示模式", "演示模式无需打开浏览器")
            return
        self._log(f"正在打开浏览器: {name}…")
        # 后台线程里先查该窗口平台，再打开（网络+CDP 均不占用主线程）
        threading.Thread(target=self._open_row_worker, args=(wid, name), daemon=True).start()

    def _open_row_worker(self, wid, name):
        try:
            if wid == "chrome":
                self._ui_call(lambda: self._log("🌐 微博 Chrome 会话已连接，无需重复打开。"))
                return
            win = next((w for w in self._mapping() if w["id"] == wid), None)
            plat = win.get("platform", "") if win else ""
            self._open_bb(wid, name, plat)
        except Exception:
            pass
        try:
            self._ui_call(self.refresh_window_list)
        except Exception:
            pass

    def on_close_row(self):
        vals = self._selected_account_values()
        if not vals:
            return
        wid = vals[4]
        threading.Thread(target=self._close_row_worker, args=(wid,), daemon=True).start()

    def _close_row_worker(self, wid):
        try:
            if wid in ("chrome", "chrome-bilibili"):
                label = "微博" if wid == "chrome" else "B站"
                self._ui_call(lambda: self._log(f"ℹ️ {label} Chrome 会话由外部 Chrome 管理，未关闭浏览器。"))
                return
            self.bb.close_browser(wid)
            self._ui_call(lambda: self._log(f"⛔ 已关闭浏览器 {wid}"))
        except Exception as e:
            self._ui_call(lambda: self._log(f"关闭失败: {e}"))
        try:
            self._ui_call(self.refresh_window_list)
        except Exception:
            pass

    def on_open_selected(self):
        """按 BitBrowser 窗口自身的平台打开，不让下拉框强制改站点。"""
        if self.demo:
            messagebox.showinfo("演示模式", "演示模式无需打开浏览器")
            return
        self._log("正在按窗口平台打开浏览器…")
        threading.Thread(target=self._open_selected_worker, daemon=True).start()

    def _open_selected_worker(self):
        opened = False
        try:
            # 顶部操作固定一次打开一个浏览器，避免批量数量控件干扰账号绑定流程。
            limit = 1
            for w in self._mapping():
                if opened >= limit:
                    break
                if self._window_status(w["id"]) == "打开":
                    continue
                if self._open_bb(w["id"], w["name"], w.get("platform") or ""):
                    opened += 1
        except Exception:
            pass
        try:
            if opened:
                self._ui_call(lambda: self._log(
                    f"🌐 已按窗口平台打开 {opened} 个浏览器，请登录后点「读取并绑定」"))
            else:
                self._ui_call(lambda: messagebox.showinfo(
                    "提示", "未找到可打开的比特浏览器窗口，请先创建账号浏览器"))
            self._ui_call(self.refresh_window_list)
        except Exception:
            pass

    def on_create_account(self):
        """创建账号对应的独立 BitBrowser profile 窗口（后台执行）。"""
        if self.demo:
            messagebox.showinfo("演示模式", "演示模式无需创建浏览器")
            return
        plat_cn = self.var_bind_platform.get()
        plat = PLATFORM_EN.get(plat_cn, "douyin")
        # 浏览器只是承载登录环境，真实账号名称由“读取并绑定”从线上提取。
        # BitBrowser 接口要求创建时有名称，这里仅生成内部临时名称，不展示给用户填写。
        acct_name = f"{plat_cn}浏览器-{time.strftime('%H%M%S')}"
        self._log(f"正在创建账号浏览器: {acct_name}…")
        threading.Thread(target=self._create_account_worker,
                         args=(plat, plat_cn, acct_name), daemon=True).start()

    def _create_account_worker(self, plat, plat_cn, acct_name):
        try:
            platform_url = ("https://creator.xiaohongshu.com/new/note-manager?source=official" if plat == "xhs"
                            else ("https://weibo.com/" if plat == "weibo"
                                  else ("https://www.bilibili.com/" if plat == "bilibili"
                                        else ("https://www.kuaishou.com/new-reco" if plat == "kuaishou"
                                              else "https://www.douyin.com/user/self?from_nav=1"))))
            w = self.bb.create_window(name=acct_name, platform=platform_url, url=platform_url)
            wid = w.get("id")
            self._ui_call(lambda: self._log(
                f"➕ 已创建账号浏览器: {acct_name}（平台={plat_cn}，窗口={wid}）"))
            self._ui_call(lambda: messagebox.showinfo(
                "创建成功",
                f"浏览器已创建。\n点「打开浏览器」打开它，登录后点「读取并绑定」即可自动获取账号名称。"))
        except Exception as e:
            self._ui_call(lambda: messagebox.showerror("创建失败", str(e)))
        try:
            self._ui_call(self.refresh_window_list)
        except Exception:
            pass

    def on_bind(self):
        """读取选中窗口已登录账号的昵称/ID 并绑定（持久化到DB，后台执行）。"""
        vals = self._selected_account_values()
        if not vals:
            messagebox.showinfo("提示", "请先选中要绑定的窗口")
            return
        wid = vals[4]
        label = vals[0]
        plat = vals[1]
        plat_en = PLATFORM_EN.get(plat, "douyin")
        self._log(f"正在读取 {plat} 账号登录信息…")
        threading.Thread(target=self._bind_worker,
                         args=(wid, label, plat, plat_en), daemon=True).start()

    def _bind_worker(self, wid, label, plat, plat_en):
        try:
            win = next((w for w in self._mapping() if w["id"] == wid), None)
            if not win:
                return
            acc = self._read_account_quick(plat_en, win["name"], window_id=wid)
        except Exception:
            acc = {}
        if not acc.get("logged_in"):
            self._ui_call(lambda: messagebox.showwarning(
                "提示", "该窗口未登录或读取失败，请确认已在浏览器中登录"))
            try:
                self._ui_call(self.refresh_window_list)
            except Exception:
                pass
            return
        nick = acc.get("nick") or acc.get("uid") or acc.get("sec_uid") or ""
        if not nick:
            nick = f"{plat}账号"
        try:
            self.sched.add_account(nick, bb_window_id=wid, platform=plat_en)
            self._ui_call(lambda: self._log(f"✅ 已绑定账号: {plat}·{nick}"))
            self._ui_call(lambda: messagebox.showinfo(
                "绑定成功", f"{plat}·{nick} 已绑定，可用于采集任务"))
        except Exception as e:
            self._ui_call(lambda: messagebox.showerror("绑定失败", str(e)))
        try:
            self._ui_call(self.refresh_window_list)
        except Exception:
            pass

    def on_unbind(self):
        """解除选中窗口的账号绑定。"""
        vals = self._selected_account_values()
        if not vals:
            return
        name = vals[0]
        if "·" in name:
            name = name.split("·", 1)[1]
        try:
            self.sched.remove_account(name)
            self._log(f"🗑 已解除绑定: {name}")
        except Exception as e:
            self._log(f"解除绑定失败: {e}")
        self.refresh_window_list()
        self._log(f"✅ 账号已解除绑定: {name}")

    def on_delete_account(self):
        """删除选中的 BitBrowser profile，并清理对应的绑定记录（后台执行）。"""
        vals = self._selected_account_values()
        if not vals:
            messagebox.showinfo("提示", "请先选中要删除的账号")
            return
        if self.demo:
            messagebox.showinfo("演示模式", "演示模式不能删除真实账号")
            return
        wid = str(vals[4])
        display_name = str(vals[0])
        if wid in ("chrome", "chrome-bilibili"):
            try:
                name = "微博 Chrome" if wid == "chrome" else "B站 Chrome"
                self.sched.remove_account(name)
                self._log(f"🗑 已移除{name}账号绑定，未关闭 Chrome。")
                self.refresh_window_list()
            except Exception as e:
                self._log(f"移除微博 Chrome 绑定失败: {e}")
            return
        ok = messagebox.askyesno(
            "确认删除账号",
            f"确定删除账号「{display_name}」及其 BitBrowser 浏览器窗口吗？\n"
            "此操作会删除登录态和 profile，无法恢复。",
            icon="warning",
        )
        if not ok:
            return
        self._log(f"正在删除账号: {display_name}…")
        threading.Thread(target=self._delete_account_worker,
                         args=(wid, display_name), daemon=True).start()

    def _delete_account_worker(self, wid, display_name):
        bound_account = None
        try:
            accounts = self.sched.status_report().get("accounts", {})
            bound_account = next((a for a in accounts.values()
                                  if str(a.get("bb_window_id") or "") == wid), None)
            if bound_account:
                display_name = bound_account.get("name") or display_name
        except Exception:
            pass
        try:
            if bound_account:
                blocker = self.sched.account_removal_blocker(bound_account.get("id"))
                if blocker:
                    raise RuntimeError(blocker)
            try:
                if self._window_status(wid) == "打开":
                    self.bb.close_browser(wid)
                    self._ui_call(lambda: self._log(
                        f"正在关闭账号窗口，等待进程退出: {display_name}"))
                    time.sleep(5)
            except Exception:
                pass
            self.bb.delete_browser(wid)
            if bound_account:
                self.sched.remove_account(account_id=bound_account.get("id"))
            self._ui_call(lambda: self._log(f"🗑 已删除账号及浏览器窗口: {display_name}"))
            self._ui_call(lambda: messagebox.showinfo("删除完成", f"账号「{display_name}」已删除"))
        except Exception as e:
            self._ui_call(lambda: self._log(f"删除账号失败: {e}"))
            self._ui_call(lambda: messagebox.showerror("删除失败", str(e)))
        try:
            self._ui_call(self.refresh_window_list)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 任务操作
    # ------------------------------------------------------------------ #
    def open_new_task_dialog_ctk(self):
        """使用 CustomTkinter 绘制圆角深色新建任务窗口。"""
        if ctk is None:
            return self.open_new_task_dialog()
        self.var_keyword_group_id.set("")
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("新建采集任务")
        dialog.geometry("900x780")
        dialog.minsize(700, 560)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.lift()
        dialog.configure(fg_color=COLORS["surface_2"])
        # 使用 Windows 原生标题栏和窗口输入上下文。无边框窗口会让部分
        # 中文输入法无法取得 Tk Entry 的 caret，候选栏可能落到桌面原点。
        dialog.overrideredirect(False)
        # 弹窗按主窗口实时位置居中，并限制在主窗口范围内。
        self.root.update_idletasks()
        root_x, root_y = self.root.winfo_rootx(), self.root.winfo_rooty()
        root_w, root_h = self.root.winfo_width(), self.root.winfo_height()
        dialog_w = min(900, max(700, root_w - 40))
        dialog_h = min(780, max(560, root_h - 40))
        x = root_x + max(20, (root_w - dialog_w) // 2)
        y = root_y + max(20, (root_h - dialog_h) // 2)
        dialog.geometry(f"{dialog_w}x{dialog_h}+{x}+{y}")
        ctk.CTkLabel(dialog, text="配置平台、采集模式、账号和输出目录",
                     text_color=COLORS["muted"], font=FONTS["helper"]).pack(
                         anchor="w", padx=30, pady=(18, 12))
        # 内容区可滚动，避免窗口高度不足时账号和输出目录被截断。
        body = ctk.CTkScrollableFrame(dialog, fg_color=COLORS["surface_2"],
                                      corner_radius=RADIUS["dialog"],
                                      scrollbar_fg_color=COLORS["surface_2"],
                                      scrollbar_button_color=COLORS["border"],
                                      scrollbar_button_hover_color=COLORS["surface_hover"])
        body.pack(fill="both", expand=True, padx=22, pady=(0, 12))
        top = ctk.CTkFrame(body, fg_color="transparent")
        top.pack(fill="x", padx=22, pady=(20, 10))
        ctk.CTkLabel(top, text="1. 基础信息", text_color=COLORS["text"],
                     font=FONTS["body_bold"]).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 5))
        ctk.CTkLabel(top, text="平台", text_color=COLORS["muted"], font=FONTS["helper"]).grid(row=1, column=0, sticky="w", padx=(0, 12))
        ctk.CTkLabel(top, text="任务关键词/名称（选择关键词组后按词逐个执行）", text_color=COLORS["muted"], font=FONTS["helper"]).grid(row=1, column=1, sticky="w")
        ctk.CTkLabel(top, text="搜索排序", text_color=COLORS["muted"], font=FONTS["helper"]).grid(row=3, column=0, sticky="w", padx=(0, 12), pady=(12, 0))
        sort_box = ctk.CTkComboBox(
            top, variable=self.var_sort, values=sort_labels("douyin"), height=38,
            fg_color=COLORS["window"], border_color=COLORS["border"],
            button_color=COLORS["surface_3"], button_hover_color=COLORS["surface_hover"],
            dropdown_fg_color=COLORS["surface_2"], dropdown_hover_color=COLORS["primary_soft"],
            text_color=COLORS["text"], font=FONTS["body"])
        sort_box.grid(row=4, column=0, sticky="ew", padx=(0, 12), pady=(5, 0))
        current_sort_platform = PLATFORM_EN.get(self.var_platform.get(), "douyin")
        current_sort_labels = sort_labels(current_sort_platform)
        sort_box.configure(values=current_sort_labels)
        if self.var_sort.get() not in current_sort_labels:
            self.var_sort.set(current_sort_labels[0])
        ctk.CTkLabel(top, text="排序规则按平台分别提供", text_color=COLORS["muted"], font=FONTS["helper"]).grid(row=3, column=1, sticky="w", pady=(12, 0))
        def rebuild_ctk_task_accounts(_value=None):
            # 平台一变，清空旧平台选择，只展示当前平台账号。
            self._selected_task_account_names = set()
            for child in accs.winfo_children():
                child.destroy()
            vars_by_name.clear()
            current_platform = PLATFORM_EN.get(self.var_platform.get(), "douyin")
            labels = sort_labels(current_platform)
            sort_box.configure(values=labels)
            if self.var_sort.get() not in labels:
                self.var_sort.set(labels[0])
            rebuild_keyword_groups()
            group = [x for x in self._task_acct_bindings if x.get("platform") == current_platform]
            for col, item in enumerate(group):
                var = tk.BooleanVar(value=False)
                vars_by_name[item["name"]] = var
                status = ACCT_STATUS_CN.get(item.get("status", "idle"), item.get("status", "可用"))
                ctk.CTkCheckBox(accs,
                                text=f"{item['name']}  ·  {PLATFORM_CN.get(item['platform'], item['platform'])}  ·  {status}",
                                variable=var, fg_color=COLORS["primary"],
                                hover_color=COLORS["primary_hover"], text_color=COLORS["text_2"],
                                font=FONTS["helper"], command=update_selected_count).grid(
                                    row=col // 2, column=col % 2, sticky="ew", padx=12, pady=6)
            if not group:
                ctk.CTkLabel(accs, text=f"暂无已绑定的{self.var_platform.get()}账号",
                             text_color=COLORS["warning"], font=FONTS["helper"]).grid(
                                 row=0, column=0, columnspan=2, sticky="w", padx=14, pady=10)
            update_selected_count()

        ctk.CTkComboBox(top, values=["抖音", "小红书", "微博", "B站", "快手"], variable=self.var_platform,
                        height=38, fg_color=COLORS["window"], border_color=COLORS["border"],
                        button_color=COLORS["surface_3"], button_hover_color=COLORS["surface_hover"],
                        dropdown_fg_color=COLORS["surface_2"], dropdown_hover_color=COLORS["primary_soft"],
                        command=rebuild_ctk_task_accounts,
                        text_color=COLORS["text"], font=FONTS["body"]).grid(row=2, column=0, sticky="ew", padx=(0, 12), pady=(5, 0))
        keyword_entry = ctk.CTkEntry(
            top, textvariable=self.var_kw, height=38, fg_color=COLORS["window"],
            border_color=COLORS["border"], text_color=COLORS["text"],
            font=FONTS["body"])
        keyword_entry.grid(row=2, column=1, sticky="ew", pady=(5, 0))
        ctk.CTkLabel(top, text="关键词组", text_color=COLORS["muted"], font=FONTS["helper"]).grid(
            row=3, column=1, sticky="w", pady=(12, 0))
        keyword_group_box = ctk.CTkComboBox(
            top, values=["不使用关键词组"], height=38,
            fg_color=COLORS["window"], border_color=COLORS["border"],
            button_color=COLORS["surface_3"], button_hover_color=COLORS["surface_hover"],
            dropdown_fg_color=COLORS["surface_2"], dropdown_hover_color=COLORS["primary_soft"],
            text_color=COLORS["text"], font=FONTS["body"])
        group_row = ctk.CTkFrame(top, fg_color="transparent")
        group_row.grid(row=4, column=1, sticky="ew", pady=(5, 0))
        group_row.grid_columnconfigure(0, weight=1)
        keyword_group_box.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            group_row, text="新建关键词组", width=106, height=38,
            fg_color=COLORS["surface_3"], hover_color=COLORS["primary_soft"],
            text_color=COLORS["text_2"], font=FONTS["helper"],
            command=lambda: self._open_keyword_group_manager(dialog, focus_new=True),
        ).grid(row=0, column=1, padx=(8, 0))
        ctk.CTkButton(
            group_row, text="管理词组", width=82, height=38,
            fg_color="transparent", hover_color=COLORS["primary_soft"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text_2"], font=FONTS["helper"],
            command=lambda: self._open_keyword_group_manager(dialog),
        ).grid(row=0, column=2, padx=(6, 0))

        def rebuild_keyword_groups(_value=None):
            current_platform = PLATFORM_EN.get(self.var_platform.get(), "douyin")
            choices = self._keyword_group_choices(self._keyword_group_options, current_platform)
            labels = [label for _gid, label in choices]
            keyword_group_box.configure(values=labels)
            matched = next(((gid, label) for gid, label in choices
                            if str(gid) == str(self.var_keyword_group_id.get())), None)
            if matched is None:
                self.var_keyword_group_id.set("")
                selected = choices[0][1]
            else:
                selected = matched[1]
            keyword_group_box.set(selected)

        def select_keyword_group(label):
            current_platform = PLATFORM_EN.get(self.var_platform.get(), "douyin")
            choices = self._keyword_group_choices(self._keyword_group_options, current_platform)
            selected = next((gid for gid, text in choices if text == label), "")
            self.var_keyword_group_id.set(str(selected))
            if selected:
                self._fill_keyword_from_group(selected)

        keyword_group_box.configure(command=select_keyword_group)
        rebuild_keyword_groups()
        self._keyword_group_task_refresh = rebuild_keyword_groups
        dialog.after_idle(keyword_entry.focus_set)
        top.columnconfigure(0, weight=1); top.columnconfigure(1, weight=2)
        ctk.CTkLabel(body, text="2. 采集模式", text_color=COLORS["text"],
                     font=FONTS["body_bold"]).pack(anchor="w", padx=22, pady=(7, 6))
        modes = ctk.CTkFrame(body, fg_color="transparent")
        modes.pack(fill="x", padx=22, pady=(0, 12))
        mode_help = {"快速": "测试关键词质量 · 最多100条", "标准": "常规采集分发 · 可自定义数量", "深度": "持续翻页采集 · 上限10000条"}
        mode_cards = {}
        def sync_mode_cards():
            current = self.var_mode.get()
            for name, mode_card in mode_cards.items():
                selected_now = name == current
                mode_card.configure(
                    fg_color=COLORS["primary_soft"] if selected_now else COLORS["surface"],
                    border_color=COLORS["primary"] if selected_now else COLORS["border"],
                )
        def choose_mode(mode):
            self.var_mode.set(mode)
            sync_mode_cards()
            self.on_mode_change()
        for col, mode in enumerate(("快速", "标准", "深度")):
            selected = self.var_mode.get() == mode
            card = ctk.CTkFrame(modes, fg_color=COLORS["primary_soft"] if selected else COLORS["surface"],
                                border_width=1, border_color=COLORS["primary"] if selected else COLORS["border"],
                                corner_radius=RADIUS["card"])
            card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 7, 0))
            mode_cards[mode] = card
            radio = ctk.CTkRadioButton(card, text=mode, variable=self.var_mode, value=mode,
                                       command=lambda m=mode: choose_mode(m), fg_color=COLORS["primary"],
                                       hover_color=COLORS["primary_hover"], text_color=COLORS["text"],
                                       font=FONTS["body_bold"])
            radio.pack(anchor="w", padx=13, pady=(10, 3))
            help_label = ctk.CTkLabel(card, text=mode_help[mode], text_color=COLORS["muted"],
                                      font=FONTS["helper"])
            help_label.pack(anchor="w", padx=13, pady=(0, 10))
            # 卡片、模式文字和说明文字都可以点击，不再要求精准点击左上角圆圈。
            def bind_mode_click(widget):
                targets = [widget]
                for attr in ("_canvas", "_label"):
                    target = getattr(widget, attr, None)
                    if target is not None:
                        targets.append(target)
                for target in targets:
                    target.bind("<Button-1>", lambda _e, m=mode: choose_mode(m), add="+")
                for child in widget.winfo_children():
                    bind_mode_click(child)
            bind_mode_click(card)
            modes.columnconfigure(col, weight=1)
        params = ctk.CTkFrame(body, fg_color="transparent")
        params.pack(fill="x", padx=22, pady=(0, 12))
        for col, text_ in enumerate(("每关键词目标数量", "每采集数量（条）", "冷却时长（秒）")):
            ctk.CTkLabel(params, text=text_, text_color=COLORS["muted"],
                         font=FONTS["helper"]).grid(row=0, column=col, sticky="w", padx=(0, 12))
        self.target_entry = ctk.CTkEntry(params, textvariable=self.var_target, height=36,
                                         fg_color=COLORS["window"], border_color=COLORS["border"])
        self.target_entry.grid(row=1, column=0, sticky="ew", padx=(0, 12), pady=(5, 0))
        batch_entry = ctk.CTkEntry(
            params, textvariable=self.var_batch, height=36,
            fg_color=COLORS["window"], border_color=COLORS["border"])
        batch_entry.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=(5, 0))
        cooldown_entry = ctk.CTkEntry(
            params, textvariable=self.var_cd, height=36,
            fg_color=COLORS["window"], border_color=COLORS["border"])
        cooldown_entry.grid(row=1, column=2, sticky="ew", padx=(0, 12), pady=(5, 0))
        ctk.CTkCheckBox(params, text="仅保留有评论", variable=self.var_only_comments,
                        fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                        text_color=COLORS["text_2"], font=FONTS["helper"]).grid(
                            row=1, column=3, sticky="w", pady=(5, 0))
        for col in range(3):
            params.columnconfigure(col, weight=1)
        ctk.CTkLabel(params, text="执行方式", text_color=COLORS["muted"],
                     font=FONTS["helper"]).grid(row=2, column=0, sticky="w", padx=(0, 12), pady=(8, 0))
        ctk.CTkComboBox(params, variable=self.var_execution_mode,
                        values=["单次采集", "定时增量监控"], height=36,
                        fg_color=COLORS["window"], border_color=COLORS["border"],
                        button_color=COLORS["surface_3"], button_hover_color=COLORS["surface_hover"],
                        dropdown_fg_color=COLORS["surface_2"], dropdown_hover_color=COLORS["primary_soft"],
                        text_color=COLORS["text"], font=FONTS["body"]).grid(
                            row=3, column=0, sticky="ew", padx=(0, 12), pady=(5, 0))
        ctk.CTkLabel(params, text="监控间隔（秒）", text_color=COLORS["muted"],
                     font=FONTS["helper"]).grid(row=2, column=1, sticky="w", padx=(0, 12), pady=(8, 0))
        ctk.CTkEntry(params, textvariable=self.var_monitor_interval, height=36,
                     fg_color=COLORS["window"], border_color=COLORS["border"]).grid(
                         row=3, column=1, sticky="ew", padx=(0, 12), pady=(5, 0))
        self.on_mode_change()
        ctk.CTkLabel(body, text="3. 采集内容", text_color=COLORS["text"],
                     font=FONTS["body_bold"]).pack(anchor="w", padx=22, pady=(2, 6))
        type_box = ctk.CTkFrame(body, fg_color=COLORS["surface"], corner_radius=RADIUS["control"])
        type_box.pack(fill="x", padx=22, pady=(0, 12))
        collect_vars = {}
        for idx, (key, label) in enumerate(COLLECT_TYPE_OPTIONS):
            var = tk.BooleanVar(value=key in self._selected_collect_types)
            collect_vars[key] = var
            ctk.CTkCheckBox(type_box, text=label, variable=var, fg_color=COLORS["primary"],
                            hover_color=COLORS["primary_hover"], text_color=COLORS["text_2"],
                            font=FONTS["helper"]).grid(
                                row=idx // 4, column=idx % 4, sticky="w", padx=12, pady=7)
        account_head = ctk.CTkFrame(body, fg_color="transparent")
        account_head.pack(fill="x", padx=22, pady=(2, 6))
        ctk.CTkLabel(account_head, text="4. 参与采集的账号", text_color=COLORS["text"],
                     font=FONTS["body_bold"]).pack(side="left")
        selected_count_var = tk.StringVar(value="已选择 0")
        ctk.CTkLabel(account_head, textvariable=selected_count_var, text_color=COLORS["muted"],
                     font=FONTS["helper"]).pack(side="right")
        accs = ctk.CTkScrollableFrame(body, fg_color=COLORS["surface"],
                                      corner_radius=RADIUS["control"], height=130,
                                      scrollbar_fg_color=COLORS["surface"],
                                      scrollbar_button_color=COLORS["border"],
                                      scrollbar_button_hover_color=COLORS["surface_hover"])
        accs.pack(fill="x", padx=22, pady=(0, 10))
        vars_by_name = {}
        def update_selected_count():
            selected_count_var.set(f"已选择 {sum(1 for var in vars_by_name.values() if var.get())} / {len(vars_by_name)}")
        current_platform = PLATFORM_EN.get(self.var_platform.get(), "douyin")
        for col, item in enumerate([x for x in self._task_acct_bindings if x.get("platform") == current_platform]):
            var = tk.BooleanVar(value=item["name"] in self._selected_task_account_names)
            vars_by_name[item["name"]] = var
            status = ACCT_STATUS_CN.get(item.get("status", "idle"), item.get("status", "可用"))
            ctk.CTkCheckBox(accs,
                            text=f"{item['name']}  ·  {PLATFORM_CN.get(item['platform'], item['platform'])}  ·  {status}",
                            variable=var, fg_color=COLORS["primary"],
                            hover_color=COLORS["primary_hover"], text_color=COLORS["text_2"],
                            font=FONTS["helper"], command=update_selected_count).grid(
                                row=col // 2, column=col % 2, sticky="ew", padx=12, pady=6)
        accs.columnconfigure(0, weight=1)
        accs.columnconfigure(1, weight=1)
        update_selected_count()
        if not vars_by_name:
            ctk.CTkLabel(accs, text=f"暂无已绑定的{self.var_platform.get()}账号",
                         text_color=COLORS["warning"], font=FONTS["helper"]).pack(anchor="w", padx=14, pady=10)
        ctk.CTkLabel(body, text="5. 输出目录", text_color=COLORS["text"],
                     font=FONTS["body_bold"]).pack(anchor="w", padx=22, pady=(0, 6))
        outrow = ctk.CTkFrame(body, fg_color="transparent")
        outrow.pack(fill="x", padx=22, pady=(0, 18))
        output_entry = ctk.CTkEntry(
            outrow, textvariable=self.var_output_dir, height=36,
            fg_color=COLORS["window"], border_color=COLORS["border"])
        output_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(outrow, text="选择目录", width=105, height=36,
                      corner_radius=RADIUS["control"], fg_color=COLORS["surface_3"],
                      hover_color=COLORS["surface_hover"], command=self.on_choose_output_dir).pack(side="left", padx=(9, 0))
        footer = ctk.CTkFrame(dialog, fg_color="transparent")
        footer.pack(fill="x", padx=24, pady=(0, 20))
        ctk.CTkLabel(footer, text="创建后可在任务卡片中开始、暂停、停止或导出",
                     text_color=COLORS["muted"], font=FONTS["helper"]).pack(side="left")
        def close_task_dialog():
            if getattr(self, "_keyword_group_task_refresh", None) is rebuild_keyword_groups:
                self._keyword_group_task_refresh = None
            dialog.destroy()

        def create_and_close():
            self._selected_collect_types = {key for key, var in collect_vars.items() if var.get()}
            self._selected_task_account_names = {n for n, v in vars_by_name.items() if v.get()}
            self._update_account_summary()
            if self.on_start(): close_task_dialog()
        ctk.CTkButton(footer, text="创建任务", width=120, height=38,
                      corner_radius=RADIUS["control"], fg_color=COLORS["primary"],
                      hover_color=COLORS["primary_hover"], font=FONTS["body_bold"],
                      command=create_and_close).pack(side="right")
        ctk.CTkButton(footer, text="取消", width=92, height=38,
                      corner_radius=RADIUS["control"], fg_color="transparent",
                      hover_color=COLORS["surface_hover"], border_width=1,
                      border_color=COLORS["border"], text_color=COLORS["text_2"],
                      command=close_task_dialog).pack(side="right", padx=(0, 10))

    def open_new_task_dialog(self):
        """打开卡片式新建任务弹窗。"""
        self.var_keyword_group_id.set("")
        dialog = tk.Toplevel(self.root)
        dialog.title("新建采集任务")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.focus_force()
        dialog.lift()
        dialog.geometry("760x650")
        dialog.configure(bg="#131d31")

        def close_legacy_task_dialog():
            self._keyword_group_task_refresh = None
            dialog.destroy()

        head = ttk.Frame(dialog, padding=(22, 16))
        head.pack(fill="x")
        ttk.Label(head, text="新建采集任务", font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        ttk.Button(head, text="关闭", command=close_legacy_task_dialog).pack(side="right")
        head.bind("<Button-1>", lambda event: self._begin_dialog_move(dialog, event), add="+")
        head.bind("<B1-Motion>", lambda event: self._queue_dialog_move(dialog, event), add="+")
        for child in head.winfo_children():
            child.bind("<Button-1>", lambda event: self._begin_dialog_move(dialog, event), add="+")
            child.bind("<B1-Motion>", lambda event: self._queue_dialog_move(dialog, event), add="+")
        dialog.bind("<Button-1>", lambda _e: dialog.lift(), add="+")
        body = ttk.Frame(dialog, padding=(24, 4, 24, 18))
        body.pack(fill="both", expand=True)

        grid = ttk.Frame(body)
        grid.pack(fill="x", pady=(0, 14))
        ttk.Label(grid, text="采集平台").grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Label(grid, text="任务关键词/名称（选择关键词组后按词逐个执行）").grid(row=0, column=1, sticky="w")
        ttk.Label(grid, text="关键词组").grid(row=0, column=2, sticky="w")
        platform_box = ttk.Combobox(grid, textvariable=self.var_platform, values=["抖音", "小红书", "微博", "B站", "快手"],
                                    state="readonly", width=18, style="TCombobox")
        platform_box.grid(row=1, column=0, sticky="ew", padx=(0, 12))
        ttk.Entry(grid, textvariable=self.var_kw, width=34).grid(row=1, column=1, sticky="ew")
        keyword_group_box = ttk.Combobox(grid, state="readonly", width=32)
        keyword_group_box.grid(row=1, column=2, sticky="ew", padx=(12, 0))
        ttk.Button(grid, text="新建关键词组", width=12,
                   command=lambda: self._open_keyword_group_manager(dialog, focus_new=True)).grid(
                       row=1, column=3, sticky="ew", padx=(8, 0))
        ttk.Button(grid, text="管理词组", width=9,
                   command=lambda: self._open_keyword_group_manager(dialog)).grid(
                       row=1, column=4, sticky="ew", padx=(6, 0))
        ttk.Label(grid, text="搜索排序").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=(10, 0))
        sort_box = ttk.Combobox(grid, textvariable=self.var_sort,
                                values=sort_labels("douyin"), state="readonly", width=18)
        sort_box.grid(row=3, column=0, sticky="ew", padx=(0, 12))
        current_sort_platform = PLATFORM_EN.get(self.var_platform.get(), "douyin")
        current_sort_labels = sort_labels(current_sort_platform)
        sort_box["values"] = current_sort_labels
        if self.var_sort.get() not in current_sort_labels:
            self.var_sort.set(current_sort_labels[0])
        ttk.Label(grid, text="排序规则按平台分别提供", foreground="#91a2bd").grid(
            row=2, column=1, sticky="w", pady=(10, 0))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=2)
        grid.columnconfigure(2, weight=2)
        grid.columnconfigure(3, weight=0)
        grid.columnconfigure(4, weight=0)

        def rebuild_legacy_keyword_groups(_event=None):
            current_platform = PLATFORM_EN.get(self.var_platform.get(), "douyin")
            choices = self._keyword_group_choices(self._keyword_group_options, current_platform)
            keyword_group_box["values"] = [label for _gid, label in choices]
            matched = next(((gid, label) for gid, label in choices
                            if str(gid) == str(self.var_keyword_group_id.get())), None)
            if matched is None:
                self.var_keyword_group_id.set("")
                selected = choices[0][1]
            else:
                selected = matched[1]
            keyword_group_box.set(selected)

        def select_legacy_keyword_group(_event=None):
            current_platform = PLATFORM_EN.get(self.var_platform.get(), "douyin")
            choices = self._keyword_group_choices(self._keyword_group_options, current_platform)
            selected = next((gid for gid, label in choices if label == keyword_group_box.get()), "")
            self.var_keyword_group_id.set(str(selected))
            if selected:
                self._fill_keyword_from_group(selected)

        keyword_group_box.bind("<<ComboboxSelected>>", select_legacy_keyword_group)
        rebuild_legacy_keyword_groups()
        self._keyword_group_task_refresh = rebuild_legacy_keyword_groups

        ttk.Label(body, text="采集模式").pack(anchor="w", pady=(0, 7))
        modes = ttk.Frame(body)
        modes.pack(fill="x", pady=(0, 14))
        mode_help = {
            "快速": "测试关键词质量\n默认最多 100 条",
            "标准": "常规采集与分发\n目标数量可修改",
            "深度": "持续翻页采集\n上限 10,000 条",
        }
        for col, mode in enumerate(("快速", "标准", "深度")):
            card = ttk.Frame(modes, padding=10, relief="solid", borderwidth=1)
            card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 0))
            ttk.Radiobutton(card, text=mode, variable=self.var_mode, value=mode,
                            command=self.on_mode_change).pack(anchor="w")
            ttk.Label(card, text=mode_help[mode], foreground="#91a2bd").pack(anchor="w", pady=(5, 0))
            modes.columnconfigure(col, weight=1)

        grid2 = ttk.Frame(body)
        grid2.pack(fill="x", pady=(0, 14))
        ttk.Label(grid2, text="每关键词目标数量").grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Label(grid2, text="每批数量 / 冷却秒数").grid(row=0, column=1, sticky="w")
        self.target_entry = ttk.Entry(grid2, textvariable=self.var_target, width=18)
        self.target_entry.grid(row=1, column=0, sticky="ew", padx=(0, 12))
        batch_box = ttk.Frame(grid2)
        batch_box.grid(row=1, column=1, sticky="ew")
        ttk.Spinbox(batch_box, from_=1, to=50, textvariable=self.var_batch, width=9).pack(side="left")
        ttk.Spinbox(batch_box, from_=0, to=600, textvariable=self.var_cd, width=9).pack(side="left", padx=8)
        grid2.columnconfigure(0, weight=1)
        grid2.columnconfigure(1, weight=1)
        self.on_mode_change()

        ttk.Label(body, text="执行方式").pack(anchor="w", pady=(0, 5))
        ttk.Combobox(body, textvariable=self.var_execution_mode,
                     values=["单次采集", "定时增量监控"], state="readonly", width=20).pack(
                         anchor="w", pady=(0, 12))
        interval_row = ttk.Frame(body)
        interval_row.pack(fill="x", pady=(0, 12))
        ttk.Label(interval_row, text="监控间隔（秒）").pack(side="left")
        ttk.Spinbox(interval_row, from_=60, to=604800,
                    textvariable=self.var_monitor_interval, width=12).pack(side="left", padx=(10, 0))
        ttk.Label(interval_row, text="仅定时增量监控生效，最短 60 秒").pack(side="left", padx=10)

        ttk.Label(body, text="采集类型（默认全选，仅勾选项会参与采集）").pack(anchor="w", pady=(0, 7))
        type_frame = ttk.Frame(body)
        type_frame.pack(fill="x", pady=(0, 14))
        collect_vars = {}
        for idx, (key, label) in enumerate(COLLECT_TYPE_OPTIONS):
            var = tk.BooleanVar(value=key in self._selected_collect_types)
            collect_vars[key] = var
            ttk.Checkbutton(type_frame, text=label, variable=var).grid(
                row=idx // 2, column=idx % 2, sticky="w", padx=(0, 18), pady=4)

        ttk.Label(body, text="参与采集的账号").pack(anchor="w", pady=(0, 7))
        accounts_frame = ttk.Frame(body)
        accounts_frame.pack(fill="x", pady=(0, 14))
        vars_by_name = {}
        def rebuild_legacy_task_accounts(_event=None):
            self._selected_task_account_names = set()
            for child in accounts_frame.winfo_children():
                child.destroy()
            vars_by_name.clear()
            current_platform = PLATFORM_EN.get(self.var_platform.get(), "douyin")
            labels = sort_labels(current_platform)
            sort_box["values"] = labels
            if self.var_sort.get() not in labels:
                self.var_sort.set(labels[0])
            group = [x for x in self._task_acct_bindings if x.get("platform") == current_platform]
            for col, item in enumerate(group):
                var = tk.BooleanVar(value=False)
                vars_by_name[item["name"]] = var
                ttk.Checkbutton(accounts_frame,
                                text=f"{item['name']} · {PLATFORM_CN.get(item['platform'], item['platform'])}",
                                variable=var).grid(row=col // 2, column=col % 2,
                                                    sticky="w", padx=(0, 18), pady=4)
            if not group:
                ttk.Label(accounts_frame, text=f"暂无已绑定的{self.var_platform.get()}账号",
                          foreground="#f1b45f").grid(row=0, column=0, columnspan=2, sticky="w")
            rebuild_legacy_keyword_groups()

        platform_box.bind("<<ComboboxSelected>>", rebuild_legacy_task_accounts)
        current_platform = PLATFORM_EN.get(self.var_platform.get(), "douyin")
        for col, item in enumerate([x for x in self._task_acct_bindings if x.get("platform") == current_platform]):
            var = tk.BooleanVar(value=item["name"] in self._selected_task_account_names)
            vars_by_name[item["name"]] = var
            ttk.Checkbutton(accounts_frame, text=f"{item['name']} · {PLATFORM_CN.get(item['platform'], item['platform'])}",
                            variable=var).grid(row=col // 2, column=col % 2, sticky="w", padx=(0, 18), pady=4)
        if not vars_by_name:
            ttk.Label(accounts_frame, text=f"暂无已绑定的{self.var_platform.get()}账号", foreground="#f1b45f").grid(
                row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(body, text="输出目录").pack(anchor="w", pady=(0, 7))
        output_row = ttk.Frame(body)
        output_row.pack(fill="x")
        ttk.Entry(output_row, textvariable=self.var_output_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(output_row, text="选择目录", command=self.on_choose_output_dir).pack(side="left", padx=(8, 0))

        foot = ttk.Frame(dialog, padding=(24, 14))
        foot.pack(fill="x")
        ttk.Label(foot, text="创建后可在任务卡片中开始、暂停、停止或导出", foreground="#91a2bd").pack(side="left")

        def create_and_close():
            self._selected_collect_types = {key for key, var in collect_vars.items() if var.get()}
            self._selected_task_account_names = {n for n, v in vars_by_name.items() if v.get()}
            self._update_account_summary()
            if self.on_start():
                close_legacy_task_dialog()

        ttk.Button(foot, text="创建任务", command=create_and_close).pack(side="right")

    def on_start(self):
        """按当前表单创建任务；实际开始/暂停/停止由任务卡片按钮控制。"""
        kw = self.var_kw.get().strip()
        keyword_group_id = None
        try:
            if str(self.var_keyword_group_id.get()).strip():
                keyword_group_id = int(self.var_keyword_group_id.get())
                if not kw:
                    group = next((item for item in self._keyword_group_options
                                  if int(item.get("id")) == keyword_group_id), None)
                    kw = str((group or {}).get("name") or "关键词组任务").strip()
        except (TypeError, ValueError):
            keyword_group_id = None
        if not kw:
            messagebox.showwarning("提示", "请输入关键词")
            return False
        platform_cn = self.var_platform.get()
        platform = PLATFORM_EN.get(platform_cn, "douyin")
        try:
            batch = int(self.var_batch.get())
            cd = int(self.var_cd.get())
            if not 1 <= batch <= 10000:
                raise ValueError
            if not 0 <= cd <= 86400:
                raise ValueError
        except ValueError:
            messagebox.showwarning("提示", "每采集数量需为 1–10000，冷却时长需为 0–86400 秒")
            return False
        mode = MODE_EN.get(self.var_mode.get(), "standard")
        search_sort = sort_key(platform, self.var_sort.get())
        try:
            target_count = 100 if mode == "fast" else int(self.var_target.get())
            if not 1 <= target_count <= 10000:
                raise ValueError
        except ValueError:
            messagebox.showwarning("提示", "目标数量必须是 1–10000 的整数")
            return False
        try:
            monitor_interval = int(self.var_monitor_interval.get() or 3600)
            if not 60 <= monitor_interval <= 604800:
                raise ValueError
        except ValueError:
            messagebox.showwarning("提示", "监控间隔必须是 60–604800 秒")
            return False
        selected_names = set(self._selected_task_account_names)
        selected = [dict(x) for x in self._task_acct_bindings
                    if x.get("name") in selected_names]
        if not selected:
            messagebox.showwarning("提示", "请先在“参与采集的账号”中勾选至少一个账号")
            return False
        # 新建任务时重新读取调度器中的账号快照，不能直接相信轮询缓存里的
        # wid。账号刚绑定/刷新窗口后，旧缓存可能仍携带历史占位 ID，
        # 从而覆盖数据库中的真实 BitBrowser 窗口绑定。
        try:
            # 这里运行在 Tk 主线程，不能同步查询共享数据库；使用状态刷新线程
            # 已发布的最新快照，避免创建任务时被采集锁卡住整个界面。
            live_accounts = (getattr(self, "_latest_report", {}) or {}).get("accounts", {})
            live_by_key = {
                (a.get("name", key), a.get("platform", "douyin")): a
                for key, a in live_accounts.items()
            }
            refreshed = []
            for item in selected:
                live = live_by_key.get((item.get("name"), item.get("platform")))
                if live is None:
                    continue
                item["wid"] = live.get("bb_window_id")
                item["status"] = live.get("status", item.get("status", "idle"))
                refreshed.append(item)
            selected = refreshed
        except Exception as exc:  # noqa: BLE001
            self._log(f"创建任务前刷新账号绑定失败，已停止创建：{exc}")
            messagebox.showwarning("账号状态未刷新", "账号绑定状态读取失败，请刷新账号列表后再创建任务。")
            return False
        if not selected:
            messagebox.showwarning("账号状态已变化", "所选账号已不存在，请刷新账号列表后重新选择。")
            return False
        if not self._selected_collect_types:
            messagebox.showwarning("提示", "请至少选择一种采集类型")
            return False
        matched = [x for x in selected if x.get("platform") == platform]
        if not matched:
            messagebox.showwarning(
                "账号平台不匹配",
                f"当前任务平台为“{platform_cn}”，请勾选一个已绑定的{platform_cn}账号。",
            )
            return False
        try:
            tid = self.sched.create_task(kw, platform=platform, batch_size=batch,
                                         cooldown_seconds=cd, collect_mode=mode,
                                         target_count=target_count,
                                         collect_types=sorted(self._selected_collect_types),
                                         task_accounts=[x["name"] for x in matched],
                                         only_with_comments=(self.var_only_comments.get() or
                                                              platform in ("weibo", "bilibili")),
                                         output_dir=os.path.abspath(
                                             self.var_output_dir.get().strip() or
                                             os.path.join(PROJECT_ROOT, "data", "exports")),
                                         search_sort=search_sort,
                                         execution_mode=("monitoring" if self.var_execution_mode.get() == "定时增量监控" else "once"),
                                         keyword_group_id=keyword_group_id,
                                         monitor_interval_seconds=monitor_interval)
            for item in matched:
                self.sched.add_account(item["name"], bb_window_id=item.get("wid"),
                                       platform=item["platform"])
            group_text = f" 关键词组#{keyword_group_id}" if keyword_group_id else ""
            quantity_text = f"每关键词={target_count}" if keyword_group_id else f"数量={target_count}"
            self._log(f"➕ 任务#{tid} 已创建: 平台={platform_cn} 关键词={kw}{group_text} 排序={self.var_sort.get()} 账号="
                      f"{[x['name'] for x in matched]} {quantity_text}")
            if self.var_execution_mode.get() == "定时增量监控":
                try:
                    self.sched.start_monitoring()
                except Exception as exc:
                    self._log(f"监控后台启动失败，任务仍已创建：{exc}")
            self.refresh_all()
            self.var_keyword_group_id.set("")
            return True
        except Exception as e:
            messagebox.showerror("错误", str(e))
            return False

    def on_choose_output_dir(self):
        current = self.var_output_dir.get().strip() or PROJECT_ROOT
        selected = filedialog.askdirectory(title="选择导出目录", initialdir=current)
        if selected:
            self.var_output_dir.set(os.path.abspath(selected))

    def on_mode_change(self, _event=None):
        def set_entry_state(state):
            if hasattr(self.target_entry, "configure") and ctk is not None and isinstance(self.target_entry, ctk.CTkEntry):
                self.target_entry.configure(state=state)
            else:
                self.target_entry.state([state == "disabled" and "disabled" or "!disabled"])
        if MODE_EN.get(self.var_mode.get(), "standard") == "fast":
            self.var_target.set("100")
            set_entry_state("disabled")
        else:
            if self.var_target.get() == "100":
                self.var_target.set("500")
            set_entry_state("normal")

    def on_choose_accounts(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("选择参与采集的账号")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("430x360")
        ttk.Label(dialog, text="按平台排序，可多选账号：").pack(anchor="w", padx=12, pady=8)
        body = ttk.Frame(dialog)
        body.pack(fill="both", expand=True, padx=12)
        vars_by_name = {}
        for platform in ("douyin", "xhs", "weibo", "bilibili", "kuaishou"):
            group = [x for x in self._task_acct_bindings if x.get("platform") == platform]
            if not group:
                continue
            ttk.Label(body, text=PLATFORM_CN.get(platform, platform),
                      font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", pady=(6, 2))
            for item in group:
                var = tk.BooleanVar(value=item["name"] in self._selected_task_account_names)
                vars_by_name[item["name"]] = var
                ttk.Checkbutton(body, text=item["name"], variable=var).pack(anchor="w", padx=12)
        btns = ttk.Frame(dialog)
        btns.pack(fill="x", padx=12, pady=10)
        def confirm():
            self._selected_task_account_names = {n for n, v in vars_by_name.items() if v.get()}
            self._update_account_summary()
            dialog.destroy()
        ttk.Button(btns, text="确定", command=confirm).pack(side="right", padx=4)
        ttk.Button(btns, text="取消", command=dialog.destroy).pack(side="right", padx=4)

    def _update_account_summary(self):
        selected = [x for x in self._task_acct_bindings
                    if x.get("name") in self._selected_task_account_names]
        self.var_acct_summary.set("、".join(x["name"] for x in selected) if selected else "未选择账号")

    def _selected_task_id(self):
        if getattr(self, "_task_selected_id", None) is not None:
            return int(self._task_selected_id)
        sel = self.task_tree.selection()
        if not sel:
            return None
        try:
            return int(self.task_tree.item(sel[0])["values"][0])
        except (ValueError, IndexError, TypeError):
            return None

    def _show_task_menu(self, event):
        row = self.task_tree.identify_row(event.y)
        if not row:
            return
        self.task_tree.selection_set(row)
        self.task_tree.focus(row)
        # 右键任务列表时同步覆盖卡片/上一次弹窗留下的任务 ID，
        # 避免操作菜单误作用于上一个任务。
        try:
            self._task_selected_id = int(self.task_tree.item(row)["values"][0])
        except (ValueError, IndexError, TypeError):
            self._task_selected_id = None
        if ctk:
            self._show_task_popup(event.x_root, event.y_root)
            return
        try:
            self.task_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.task_menu.grab_release()

    def _show_task_popup(self, x, y):
        """圆角深色任务操作菜单，替代系统白色右键菜单。"""
        # 右键连续打开菜单时只保留一个实例，避免旧菜单残留，造成
        # 第一次点击实际执行了动作、第二次点击才关掉另一个菜单的错觉。
        self._close_task_popup()
        popup = ctk.CTkToplevel(self.root)
        self._task_popup = popup
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.geometry(f"190x300+{x}+{y}")
        popup.configure(fg_color=COLORS["surface_2"])
        box = ctk.CTkFrame(popup, fg_color=COLORS["surface_2"],
                           corner_radius=RADIUS["card"], border_width=1,
                           border_color=COLORS["border"])
        box.pack(fill="both", expand=True, padx=1, pady=1)
        current_status = ""
        try:
            # 右键菜单必须瞬时打开；状态由后台刷新快照提供，不能在主线程查库。
            tasks = (getattr(self, "_latest_report", {}) or {}).get("tasks", {})
            task_id = self._selected_task_id()
            selected = tasks.get(task_id, tasks.get(str(task_id), {}))
            current_status = selected.get("status", "")
        except Exception:
            pass
        human_waiting = self._task_has_waiting_human(task_id)
        running_states = {"phase_a_search", "phase_b_comments", "running"}
        paused_states = {"paused", "incomplete", "failed", "no_account", "waiting_account"}
        pending_states = {"pending"}
        resumable_stopped = {"stopped", "aborted"}
        done_states = {"done"}
        actions = [
            ("▷  开始任务", self.on_task_start, current_status in pending_states and not human_waiting),
            ("Ⅱ  暂停任务", self.on_task_pause, current_status in running_states),
            ("▷  继续任务", self.on_task_resume,
             current_status in paused_states | resumable_stopped or
             (current_status == "pending" and human_waiting)),
            ("■  停止任务", self.on_task_stop, current_status in running_states | paused_states),
            ("⇩  导出数据", self.on_task_export, current_status in paused_states | resumable_stopped | done_states),
            ("□  打开输出目录", self.on_open_selected_task_folder, current_status in paused_states | resumable_stopped | done_states),
            # 删除操作内部会先停止当前任务，因此采集中也必须保持可用。
            # 之前这里把采集中任务置灰，用户无法进入确认删除流程。
            ("▱  删除任务", self.on_task_delete, True),
        ]
        for index, (label, command, enabled) in enumerate(actions):
            if index in (4, 6):
                ctk.CTkFrame(box, height=1, fg_color=COLORS["border_soft"],
                             corner_radius=0).pack(fill="x", padx=10, pady=3)
            is_danger = "删除" in label
            text_color = COLORS["danger"] if is_danger else COLORS["text_2"]
            hover = COLORS["danger_bg"] if is_danger else COLORS["surface_hover"]
            ctk.CTkButton(box, text=label, anchor="w", height=30, corner_radius=7,
                          fg_color="transparent", hover_color=hover, text_color=text_color,
                          text_color_disabled=COLORS["subtle"], font=FONTS["helper"],
                          state="normal" if enabled else "disabled",
                          command=lambda cmd=command: self._run_popup_action(popup, cmd)).pack(
                              fill="x", padx=8, pady=1)
        # 不再显示关闭叉号；点击弹窗外部即可关闭。
        # 菜单按钮本身必须保留点击事件，不能被全局关闭绑定提前销毁。
        def close_popup(event=None):
            try:
                if event is not None:
                    # 用鼠标屏幕坐标判断是否真的点在菜单内部；不依赖 grab 的事件重定向。
                    inside = popup.winfo_containing(event.x_root, event.y_root)
                    if inside is not None and inside.winfo_toplevel() == popup:
                        return
                self._close_task_popup(popup)
            except tk.TclError:
                pass
        # 不能在 Button-1 按下阶段销毁弹窗，否则 CTkButton 还没收到释放事件，
        # 菜单命令就会被提前取消；在 ButtonRelease 阶段关闭外部点击即可。
        popup.bind_all("<ButtonRelease-1>", close_popup, add="+")
        popup.bind("<Escape>", close_popup)
        popup.bind("<Destroy>", lambda _e: popup.unbind_all("<ButtonRelease-1>"))
        # 不使用 grab_set：无边框菜单使用 grab 时，外部点击会被重定向回菜单，
        # 导致“点击空白区域关闭”无法生效。
        popup.focus_force()

    def _close_task_popup(self, popup=None):
        """立即关闭任务操作菜单，并解除其全局鼠标监听。"""
        target = popup or getattr(self, "_task_popup", None)
        if target is None:
            return
        try:
            # 先解绑再销毁，避免当前 ButtonRelease 事件继续命中已销毁菜单。
            target.unbind_all("<ButtonRelease-1>")
        except tk.TclError:
            pass
        try:
            if target.winfo_exists():
                target.withdraw()
                target.destroy()
        except tk.TclError:
            pass
        if target is getattr(self, "_task_popup", None):
            self._task_popup = None

    def _run_popup_action(self, popup, command):
        # 先让菜单消失，再执行动作；否则动作触发的刷新/弹窗可能抢在
        # ButtonRelease 的全局关闭处理前运行，导致菜单要点第二次才消失。
        self._close_task_popup(popup)
        command()

    def on_open_selected_task_folder(self):
        tid = self._selected_task_id()
        if tid is None:
            return
        threading.Thread(target=self._open_task_folder_worker, args=(tid,), daemon=True).start()

    def _open_task_folder_worker(self, tid):
        try:
            task = self.sched.status_report().get("tasks", {}).get(tid)
            if task:
                label = self._export_task_label(task, tid)
                base = (task.get("output_dir") or self.var_output_dir.get().strip()
                        or os.path.join(PROJECT_ROOT, "data", "exports"))
                self._open_output_folder(os.path.join(base, label))
        except Exception as e:
            self._log(f"打开输出目录失败: {e}")

    def on_task_start(self):
        tid = self._selected_task_id()
        if tid is None:
            self._log("请先选中一个任务")
            return
        # 兼容旧版本已经落库为 pending 的人工验证任务：即使按钮尚未
        # 刷新为“继续”，也必须先恢复原账号，不能直接让调度器挑账号 B。
        if self._task_has_waiting_human(tid):
            threading.Thread(target=self._resume_task_worker, args=(tid,), daemon=True).start()
            return
        threading.Thread(target=self._start_task_worker, args=(tid,), daemon=True).start()

    def _task_has_waiting_human(self, tid):
        try:
            return bool(self.sched.waiting_accounts_for_task(int(tid)))
        except Exception:
            return False

    def _start_task_worker(self, tid):
        try:
            self.sched.start(tid)
            self._ui_call(lambda: self._log(f"▶ 任务#{tid} 已开始采集"))
            self._ui_call(self.refresh_all)
        except Exception as e:
            self._ui_call(lambda: self._log(f"任务#{tid} 启动失败: {e}"))

    def on_task_pause(self):
        tid = self._selected_task_id()
        if tid is None:
            return
        self.sched.pause(tid)   # 只暂停任务#tid
        self._log(f"⏸ 任务#{tid} 已暂停")
        self.refresh_all()

    def on_task_resume(self):
        tid = self._selected_task_id()
        if tid is None:
            return
        self._log(f"↩ 正在继续任务#{tid}…")
        # status_report/resolve/resume/start 都在后台线程执行，避免卡住主线程。
        threading.Thread(target=self._resume_task_worker, args=(tid,), daemon=True).start()

    def _resume_task_worker(self, tid):
        try:
            task = self.sched.get_task(tid) or {}
            # “暂无更多视频”不是永久终态。用户明确点击“继续采集”时，
            # 放开搜索闸门重新查一轮；普通重启/后台恢复仍不会重复搜索。
            force_search = bool(int(task.get("search_exhausted", 0) or 0))
            bound_names = task.get("task_accounts") or "[]"
            if isinstance(bound_names, str):
                try:
                    bound_names = json.loads(bound_names)
                except Exception:
                    bound_names = []
            rep = self.sched.status_report()
            waiting_ids = self.sched.waiting_accounts_for_task(tid)
            for _name, a in rep.get("accounts", {}).items():
                # 只恢复当前任务绑定的账号；status_report 的 key 是账号名，
                # resolve_human 需要传 accounts.id，不能把账号名当作 id。
                if (a.get("status") == "waiting_human" and
                        (a.get("id") in waiting_ids or
                         (not waiting_ids and (not bound_names or a.get("name") in bound_names)))):
                    self.sched.resolve_human(a.get("id"))
            self.sched.resume(tid)   # 只继续任务#tid
            self.sched.start(tid, force_search=force_search)
            self._ui_call(lambda: self._log(f"↩ 任务#{tid} 已继续"))
            self._ui_call(self.refresh_all)
        except Exception as e:
            self._ui_call(lambda: self._log(f"任务#{tid} 继续失败: {e}"))

    def on_task_stop(self):
        tid = self._selected_task_id()
        if tid is None:
            return
        def stop_worker():
            try:
                self.sched.stop_task(tid)   # 只停止任务#tid，其它任务继续
                self._ui_call(lambda: self._log(f"⏹ 任务#{tid} 已停止"))
            except Exception as e:
                self._ui_call(lambda: self._log(f"停止任务#{tid}失败: {e}"))
        threading.Thread(target=stop_worker, daemon=True).start()

    def on_task_export(self):
        tid = self._selected_task_id()
        if tid is None:
            self._log("请先选中一个任务")
            return
        self.on_export_task(tid)

    def on_task_delete(self):
        tid = self._selected_task_id()
        if tid is None:
            self._log("请先选中一个任务")
            return
        task = self.sched.get_task(tid)
        if not task:
            self._log(f"任务#{tid} 不存在或已删除")
            self.refresh_all()
            return
        running = task.get("status") in {
            "phase_a_search", "phase_b_comments", "running", "paused",
        }
        prompt = f"确定删除任务#{tid}「{task.get('keyword', '')}」及其采集数据吗？"
        if running:
            prompt += "\n任务当前仍在运行，删除前会先停止任务。"
        if not messagebox.askyesno("确认删除任务", prompt, icon="warning"):
            return
        threading.Thread(target=self._delete_task_worker, args=(tid, running), daemon=True).start()

    def _delete_task_worker(self, tid, running):
        try:
            if running:
                self.sched.stop_task(tid)   # 只停被删的任务，不影响其它任务
            if self.sched.delete_task(tid):
                self._ui_call(lambda: self._log(f"🗑 已删除任务#{tid}及其采集数据"))
            else:
                self._ui_call(lambda: self._log(f"任务#{tid}不存在或已删除"))
            self._ui_call(self.refresh_all)
        except Exception as e:
            self._ui_call(lambda: self._log(f"删除任务失败: {e}"))

    def on_pause(self):
        self.sched.pause()
        self._log("⏸ 已暂停（断点可恢复）")

    def on_stop(self):
        threading.Thread(target=self.sched.shutdown, daemon=True).start()
        self._log("⏹ 正在停止当前任务…")

    def on_resume(self):
        threading.Thread(target=self._resume_worker, daemon=True).start()

    def _resume_worker(self):
        try:
            rep = self.sched.status_report()
            frozen = [a.get("id") for a in rep["accounts"].values()
                      if a.get("status") == "waiting_human"]
            for aid in frozen:
                self.sched.resolve_human(aid)
                self._log("↩ 已恢复人工冻结账号")
            self.sched.resume()
            self._log("↩ 已继续")
            self.refresh_all()
        except Exception as e:
            self._log(f"继续失败: {e}")

    def on_export(self, into_log=False):
        platform_cn = self.exp_platform.get()
        platform = PLATFORM_EN.get(platform_cn, "douyin")
        input_path = self.exp_input.get().strip()
        if not os.path.exists(input_path):
            self._exp(f"文件不存在: {input_path}")
            return
        try:
            import export_report
            items = export_report.load_items(platform, input_path)
            task = "douyin" if platform in ("douyin", "dy") else "xhs"
            outdir = os.path.dirname(input_path)
            res = export_report.export(items, outdir, task)
            lines = [f"导出完成：作品{res['n_videos']} 评论{res['n_comments']} 聚合用户{res['n_users']}"]
            csv_vn = {"videos": "作品清单", "comments": "全部评论", "intent_leads": "意向明细", "user_leads": "高意向用户"}
            for k in ("videos", "comments", "intent_leads", "user_leads"):
                lines.append(f"  · {csv_vn[k]}: {os.path.basename(res[k])}")
            for ln in lines:
                self._exp(ln)
                if into_log:
                    self._log(ln)
        except Exception as e:
            self._exp(f"导出失败: {e}")
            import traceback
            self._exp(traceback.format_exc())

    def on_export_task(self, task_id):
        """直接从 SQLite 导出指定任务，不依赖中间 JSON 文件（后台执行）。"""
        tid = int(task_id)
        self._log(f"正在导出任务#{tid}…")
        threading.Thread(target=self._export_task_worker, args=(tid,), daemon=True).start()

    def _export_task_worker(self, tid):
        try:
            task = self.sched.status_report().get("tasks", {}).get(tid)
            if not task:
                self._log(f"任务#{tid} 不存在")
                return
            platform = task.get("platform", "douyin")
            import json
            import export_report
            conn = self.sched.conn
            with self.sched._conn_lock:
                videos = [dict(row) for row in conn.execute(
                    "SELECT * FROM videos WHERE task_id = ? ORDER BY id", (tid,)
                ).fetchall()]
                comments_by_video = {
                    v["id"]: [dict(row) for row in conn.execute(
                        "SELECT * FROM comments WHERE video_id = ? ORDER BY id", (v["id"],)
                    ).fetchall()]
                    for v in videos
                }
            items = []
            for v in videos:
                comments = comments_by_video.get(v["id"], [])
                parsed_comments = []
                for c in comments:
                    extra = {}
                    try:
                        if c["extra"]:
                            extra = json.loads(c["extra"])
                    except Exception:
                        extra = {}
                    parsed_comments.append({
                        "uid": c["user_id"] or "",
                        "nickname": c["nickname"] or "",
                        "text": c["content"] or "",
                        "digg": extra.get("digg_count", extra.get("digg", 0)) if isinstance(extra, dict) else 0,
                        "ctime_str": c["comment_time"] or "",
                        "region": extra.get("region", "") if isinstance(extra, dict) else "",
                        "homepage": extra.get("homepage", "") if isinstance(extra, dict) else "",
                    })
                extra = {}
                try:
                    if v["extra"]:
                        extra = json.loads(v["extra"])
                except Exception:
                    extra = {}
                items.append({
                    "kind": platform,
                    "pid": v["vid"],
                    "title": v["title"] or "",
                    "author": v["author"] or "",
                    "pub_str": extra.get("create_time", "") if isinstance(extra, dict) else "",
                    "collected_at": v["collected_at"] or "",
                    "url": v["url"] or "",
                    "comments": parsed_comments,
                })
            label = self._export_task_label(task, tid)
            base_dir = (task.get("output_dir") or self.var_output_dir.get().strip()
                        or os.path.join(PROJECT_ROOT, "data", "exports"))
            outdir = os.path.join(os.path.abspath(base_dir), label)
            result = export_report.export_unified(items, outdir, label)
            self._log(
                f"任务#{tid} 导出完成：作品{result['n_videos']} 评论{result['n_comments']} "
                f"高意向用户{result['n_users']}，文件：{result['path']}"
            )
            self._open_output_folder(outdir)
        except Exception as e:
            import traceback
            self._log(f"任务#{tid} 导出失败: {e}")
            self._log(traceback.format_exc())

    def _open_output_folder(self, path):
        """导出完成后打开目录；Explorer 已存在时尽量激活已有窗口。"""
        path = os.path.abspath(path)
        os.makedirs(path, exist_ok=True)
        try:
            # 优先查找已打开的 Explorer 窗口并置前，避免重复打开目录。
            try:
                from win32com.client import Dispatch
                import urllib.parse
                import ctypes
                wanted = os.path.normcase(path).rstrip("\\/")
                for win in Dispatch("Shell.Application").Windows():
                    location = urllib.parse.unquote(str(getattr(win, "LocationURL", "")))
                    if location.lower().startswith("file:///"):
                        location = location[8:].replace("/", "\\")
                    if os.path.normcase(location).rstrip("\\/") == wanted:
                        hwnd = int(getattr(win, "HWND", 0) or 0)
                        if hwnd:
                            ctypes.windll.user32.ShowWindow(hwnd, 9)
                            ctypes.windll.user32.SetForegroundWindow(hwnd)
                            return
            except Exception:
                # 未安装 pywin32 时回退到系统 Explorer。
                pass
            os.startfile(path)
        except Exception as e:
            self._log(f"打开导出目录失败: {e}")

    @staticmethod
    def _export_task_label(task, tid):
        """导出命名：日期 + 关键词 + 批次 + 平台。"""
        created = str(task.get("created_at") or "")[:10].replace("-", "")
        date = created if len(created) == 8 else time.strftime("%Y%m%d")
        keyword = str(task.get("keyword") or "未命名关键词").strip()
        platform = PLATFORM_CN.get(task.get("platform", "douyin"), task.get("platform", "平台"))
        label = f"{date}_{keyword}_批次{tid}_{platform}"
        return re.sub(r'[\\/:*?"<>|]+', "_", label).strip(" .")

    def _update_monitor_clock(self):
        """更新顶部监控时间显示；实际心跳仍由 _heartbeat_loop 写入文件。"""
        try:
            if getattr(self, "monitor_label", None) is not None:
                self.monitor_label.configure(
                    text=f"监控正常 · {time.strftime('%H:%M')}"
                )
                self.root.after(1000, self._update_monitor_clock)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    # 心跳（供监控判断主线程是否卡死）
    # ------------------------------------------------------------------ #
    def _heartbeat_loop(self):
        """后台线程：把主线程最近活动时间 + 运行指标写入心跳文件。

        卡死判据：只要 GUI 主线程正常跑 Tk 事件循环，_render_report 就会持续
        刷新 self._last_activity；监控脚本读到心跳 ts 不再前进即判定界面卡死。
        """
        path = os.path.join(PROJECT_ROOT, "data", "gui_heartbeat.txt")
        while not self._heartbeat_stop.is_set():
            try:
                n_threads = threading.active_count()
                with open(path, "w", encoding="utf-8") as f:
                    f.write(
                        f"pid={os.getpid()} ts={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self._last_activity))} "
                        f"threads={n_threads}\n"
                    )
            except Exception:
                pass
            self._heartbeat_stop.wait(1.0)

    # ------------------------------------------------------------------ #
    # 主线程 UI 调度
    # ------------------------------------------------------------------ #
    def _ui_call(self, callback, *args):
        """把 UI 回调放入队列；该方法本身不从后台线程访问 Tk。"""
        if getattr(self, "_closing", False):
            return False
        with self._ui_dispatch_queue_lock:
            if len(self._ui_dispatch_queue) >= self._ui_dispatch_limit:
                self._ui_dispatch_dropped += 1
                return False
            self._ui_dispatch_queue.append((time.monotonic(), callback, args))
        return True

    def _ui_call_later(self, delay_ms, callback, *args):
        """延迟 UI 回调；延迟也由主线程队列泵处理，避免跨线程 root.after。"""
        if getattr(self, "_closing", False):
            return False
        with self._ui_dispatch_queue_lock:
            if len(self._ui_dispatch_queue) >= self._ui_dispatch_limit:
                self._ui_dispatch_dropped += 1
                return False
            deadline = time.monotonic() + max(0, int(delay_ms)) / 1000.0
            self._ui_dispatch_queue.append((deadline, callback, args))
        return True

    def _drain_ui_dispatch_queue(self):
        """主线程批量执行后台完成通知，始终保留一个定时器。"""
        self._ui_dispatch_after = None
        if getattr(self, "_closing", False):
            return
        now = time.monotonic()
        callbacks = []
        with self._ui_dispatch_queue_lock:
            remaining = deque()
            while self._ui_dispatch_queue:
                deadline, callback, args = self._ui_dispatch_queue.popleft()
                if deadline <= now and len(callbacks) < self._ui_dispatch_batch_size:
                    callbacks.append((callback, args))
                else:
                    remaining.append((deadline, callback, args))
            self._ui_dispatch_queue = remaining
            dropped = self._ui_dispatch_dropped
            self._ui_dispatch_dropped = 0
        for callback, args in callbacks:
            try:
                callback(*args)
            except Exception as exc:  # noqa: BLE001
                # 单个 UI 回调失败不能终止队列泵，否则后续所有操作都会失去反馈。
                try:
                    self._operation_log.append(
                        f"UI 回调失败：{type(exc).__name__}: {exc}",
                        source="gui",
                        event="ui_callback_failed",
                        action="dispatch",
                        details={"callback": getattr(callback, "__name__", repr(callback))},
                    )
                except Exception:
                    pass
        if dropped:
            try:
                self._operation_log.append(
                    f"UI 队列高峰期丢弃 {dropped} 条展示回调",
                    source="gui", event="ui_queue_dropped", action="dispatch",
                    details={"dropped": dropped},
                )
            except Exception:
                pass
        try:
            self._ui_dispatch_after = self.root.after(30, self._drain_ui_dispatch_queue)
        except Exception:
            self._ui_dispatch_after = None

    # ------------------------------------------------------------------ #
    # 实时轮询（节流，避免卡顿）
    # ------------------------------------------------------------------ #
    def _poll_loop(self):
        """轮询循环：只发起刷新请求，不为每轮创建线程。"""
        while not self._poll_stop.is_set():
            self._refresh_requested.set()
            self._poll_stop.wait(2.0)

    def _refresh_worker_loop(self):
        """单一状态刷新线程；请求合并，避免刷新线程和 Tk 回调无限累积。"""
        while not self._poll_stop.is_set():
            if not self._refresh_requested.wait(0.5):
                continue
            self._refresh_requested.clear()
            if self._poll_stop.is_set():
                return
            self._refresh_data()

    def _refresh_data(self):
        """后台线程：取 status_report 快照，交给主线程的合并队列渲染。"""
        if not self._refresh_guard.acquire(blocking=False):
            return  # 防止轮询、按钮刷新同时读取数据库
        self._refresh_inflight = True
        try:
            rep = self.sched.status_report()
        except Exception as e:
            self._log(f"状态刷新失败：{type(e).__name__}: {e}")
            rep = None
        finally:
            self._refresh_inflight = False
            self._refresh_guard.release()
            self._publish_latest_report(rep)

    def refresh_all(self):
        """兼容入口：合并一次异步刷新请求（不阻塞调用线程）。"""
        if not getattr(self, "_closing", False):
            self._refresh_requested.set()

    def _publish_latest_report(self, rep):
        """只保留最新报表，防止后台刷新结果在 UI 队列中排队。"""
        with self._refresh_report_lock:
            self._pending_report = rep
            if self._report_delivery_scheduled:
                return
            self._report_delivery_scheduled = True
        if not self._ui_call(self._deliver_latest_report):
            with self._refresh_report_lock:
                self._report_delivery_scheduled = False

    def _deliver_latest_report(self):
        with self._refresh_report_lock:
            rep = self._pending_report
            self._pending_report = None
            self._report_delivery_scheduled = False
        if rep is not None and not getattr(self, "_closing", False):
            self._render_report(rep)
        with self._refresh_report_lock:
            should_schedule = (
                self._pending_report is not None
                and not self._report_delivery_scheduled
                and not getattr(self, "_closing", False)
            )
            if should_schedule:
                self._report_delivery_scheduled = True
        if should_schedule and not self._ui_call(self._deliver_latest_report):
            with self._refresh_report_lock:
                self._report_delivery_scheduled = False

    def _render_report(self, rep):
        # 主线程在此推进心跳时间戳：只要 UI 正常刷新，这里就会持续更新。
        self._last_activity = time.time()
        self._latest_report = rep
        try:
            self._refresh_inflight = False
        except Exception:
            pass
        if rep is None:
            return
        # 轮询线程可能在 UI 构建完成前首次触发；此时控件尚未就绪，直接跳过。
        if not hasattr(self, "task_cards") or not hasattr(self, "acct_live"):
            return
        tasks = rep.get("tasks", {})
        accounts = rep.get("accounts", {})
        overview_sig = (
            tuple(sorted((int(tid), t.get("status"), t.get("video_done"),
                          t.get("target_count"), t.get("comments"))
                         for tid, t in tasks.items())),
            tuple(sorted((account_key, a.get("status"), a.get("platform"),
                          a.get("processed_count"))
                         for account_key, a in accounts.items())),
        )
        if overview_sig != self._last_overview_signature:
            try:
                self._refresh_overview(tasks, accounts)
                self._last_overview_signature = overview_sig
            except Exception as e:
                self._log(f"总览刷新失败：{type(e).__name__}: {e}")

        # 任务卡片列表：只有数据确实变化时才重绘，避免轮询导致整块内容跳动。
        selected_task = self._selected_task_id()
        task_sig = tuple(sorted((int(tid), t.get("keyword"), t.get("platform"),
                                 t.get("collect_mode"), t.get("search_sort"),
                                 t.get("target_count"),
                                 (t.get("monitoring_rule") or {}).get("enabled"),
                                 (t.get("monitoring_rule") or {}).get("interval_seconds"),
                                 (t.get("monitoring_rule") or {}).get("next_run_at"))
                               for tid, t in tasks.items()))
        if task_sig != self._last_task_card_signature:
            for child in self.task_cards.winfo_children():
                child.destroy()
            self._task_card_refs = {}
            for row_index, (tid, t) in enumerate(tasks.items()):
                self._render_task_card(int(tid), t, selected_task == int(tid),
                                       alternate=(row_index % 2 == 1))
            self._last_task_card_signature = task_sig
        self._update_task_cards_live(tasks)

        # 账号实时状态（中文）
        acct_sig = tuple(sorted((account_key, a.get("status"), a.get("cooldown_left_seconds"),
                                 a.get("processed_count"), a.get("batch_count"))
                                for account_key, a in accounts.items()))
        if acct_sig != self._last_account_signature:
            live_rows = []
            for account_key, a in accounts.items():
                name = a.get("name", account_key)
                st = ACCT_STATUS_CN.get(a["status"], a["status"])
                plat = PLATFORM_CN.get(a.get("platform", "douyin"), "抖音")
                live_rows.append({
                    "_identity": account_key, "name": name, "plat": plat, "status": st,
                    "status_key": a.get("status", ""),
                    "cd": f"{a.get('cooldown_left_seconds', 0)} 秒",
                    "done": a["processed_count"], "batch": a["batch_count"],
                })
            if not self.acct_live.update_rows(live_rows):
                self.acct_live.set_rows(live_rows)
            self._last_account_signature = acct_sig

        # P7 人工接管警示
        wh = rep.get("totals", {}).get("waiting_human", [])
        if wh:
            human_key = ",".join(sorted(wh))
            if human_key != self._human_banner_key:
                self._human_banner_key = human_key
                self._human_banner_dismissed = False
            self.banner.pack_forget()
            active = getattr(self, "_active_page", "overview")
            alert = self.overview_banner if active == "overview" else (
                self.task_banner if active == "tasks" else None)
            if alert is not None and not self._human_banner_dismissed:
                alert.config(
                    text=f"⚠ 需要人工处理: {', '.join(wh)} · 处理完成后点击任务卡片中的“继续”",
                    bg=COLORS["warning_bg"], fg=COLORS["warning"])
                if not alert.winfo_manager():
                    alert.pack(fill="x", padx=30, pady=(12, 0),
                               before=self.overview_panels if active == "overview" else self.task_scroll)
            self._notify_human(wh)
        else:
            if self.banner.winfo_manager():
                self.banner.pack_forget()
            for alert in (getattr(self, "overview_banner", None), getattr(self, "task_banner", None)):
                if alert is not None:
                    alert.pack_forget()
            self.banner.config(text="", bg=COLORS["warning_bg"], fg=COLORS["warning"])
            self._human_banner_key = ""
            self._human_banner_dismissed = False

        # 刷新账号下拉（已激活账号）
        self._refresh_acct_combo(accounts)

    def _render_task_card(self, tid, task, selected=False, alternate=False):
        bg = COLORS["primary_soft"] if selected else COLORS["surface"]
        card = ctk.CTkFrame(self.task_cards, fg_color=bg, corner_radius=10,
                            border_width=1,
                            border_color=COLORS["primary"] if selected else COLORS["border_soft"])
        card.pack(fill="x", padx=3, pady=5)
        self._task_card_refs[int(tid)] = card
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=15, pady=(12, 8))
        ctk.CTkLabel(top, text=f"#{tid}", width=44, height=28, corner_radius=8,
                     fg_color=COLORS["surface_3"], text_color=COLORS["muted"],
                     font=FONTS["table_bold"]).pack(side="left", padx=(0, 12))
        plat = PLATFORM_CN.get(task.get("platform", "douyin"), task.get("platform", ""))
        mode = MODE_CN.get(task.get("collect_mode", "standard"), "标准")
        sort_text = sort_label(task.get("platform", "douyin"), task.get("search_sort", "default"))
        PlatformBadge(top, plat).pack(side="left", padx=(0, 12))
        info = ctk.CTkFrame(top, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True)
        status = task.get("status", "")
        human_waiting = self._task_has_waiting_human(tid)
        title_label = ctk.CTkLabel(
            info,
            text=f"{task.get('keyword', '')}", text_color=COLORS["text"], font=FONTS["card_title"],
            anchor="w", justify="left")
        title_label.pack(fill="x")
        error_label = ctk.CTkLabel(info, text=f"最近异常：{task['error_message']}" if task.get("error_message") else "",
                                   text_color=COLORS["danger"], anchor="w", justify="left",
                                   wraplength=520, font=FONTS["helper"])
        if task.get("error_message"):
            error_label.pack(fill="x", pady=(3, 0))
        # 进度分母必须是任务目标数，不能使用当前已采集数量，
        # 否则采到 15 条时会错误显示为 15/15、100%。
        target = max(1, int(task.get("effective_target_count") or task.get("target_count", 100) or 100))
        done = int(task.get("video_done", 0) or 0)
        collected_total = max(done, int(task.get("videos_total", 0) or 0))
        ratio = min(1.0, done / target)
        keyword_count = int(task.get("keyword_count") or 0)
        target_text = (f"目标 {target} 条（{keyword_count} 个关键词，每词 {task.get('target_per_keyword', target)} 条）"
                       if keyword_count > 1 else f"目标 {target} 条")
        summary_label = ctk.CTkLabel(info, text=f"{plat}  ·  {mode}模式  ·  排序 {sort_text}  ·  {target_text}  ·  评论 {task.get('comments', 0)}",
                                     text_color=COLORS["muted"], anchor="w",
                                     font=FONTS["helper"])
        summary_label.pack(fill="x", pady=(2, 0))
        progress_box = ctk.CTkFrame(top, fg_color="transparent", width=175, height=44)
        progress_box.pack(side="left", padx=(14, 12))
        progress_box.pack_propagate(False)
        progress_label = ctk.CTkLabel(progress_box, text=f"{done} / {collected_total}", text_color=COLORS["text_2"],
                                      font=FONTS["table_bold"])
        progress_label.pack(fill="x")
        no_more_videos = self._task_needs_more_after_exhausted(task)
        status_text = self._task_display_status(task)
        status_badge = StatusBadge(top, status_text, status)
        status_badge.pack(side="left")
        # 任务操作直接放在任务栏，避免依赖右键菜单。
        running_states = {"phase_a_search", "phase_b_comments", "running"}
        paused_states = {"paused", "incomplete", "failed", "no_account", "waiting_account"}
        stopped_states = {"stopped", "aborted"}
        # 运行状态优先级最高：即使上一轮曾确认“暂无更多视频”，
        # 当前确实正在工作时按钮也必须是“暂停”，不能显示“继续采集”。
        if status in running_states:
            action_text, action_command, action_enabled = "暂停", self.on_task_pause, True
        elif human_waiting:
            # 人工验证/登录恢复必须显示“继续”，不能被“暂无更多视频”的补采按钮覆盖。
            action_text, action_command, action_enabled = "继续", self.on_task_resume, True
        elif status == "paused":
            # 按钮显示下一步动作：暂停后的下一步是开始，而不是“继续”。
            action_text, action_command, action_enabled = "开始", self.on_task_resume, True
        elif no_more_videos:
            action_text, action_command, action_enabled = "继续采集", self.on_task_resume, True
        elif status in paused_states | stopped_states:
            action_text, action_command, action_enabled = "开始", self.on_task_resume, True
        elif status == "pending" and human_waiting:
            action_text, action_command, action_enabled = "继续", self.on_task_resume, True
        elif status == "pending":
            action_text, action_command, action_enabled = "开始", self.on_task_start, True
        else:
            action_text, action_command, action_enabled = "开始", self.on_task_start, False
        # 操作区独立占一行，避免标题、进度、状态和四个按钮挤在同一行时
        # 把最右侧的“监控设置”裁切掉。
        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", padx=15, pady=(0, 8))
        actions_right = ctk.CTkFrame(actions, fg_color="transparent")
        actions_right.pack(side="right")
        action_button = ctk.CTkButton(actions_right, text=action_text, width=58, height=30, corner_radius=7,
                                      fg_color=COLORS["primary"] if action_enabled else COLORS["surface_3"],
                                      hover_color=COLORS["primary_hover"] if action_enabled else COLORS["surface_3"],
                                      text_color=COLORS["text"] if action_enabled else COLORS["subtle"],
                                      state="normal" if action_enabled else "disabled", font=FONTS["helper"],
                                      command=lambda i=tid, cmd=action_command: self._run_task_card_action(i, cmd))
        action_button.pack(side="left", padx=(0, 4))
        stop_enabled = status in running_states | paused_states
        stop_button = ctk.CTkButton(actions_right, text="停止", width=58, height=30, corner_radius=7,
                                    fg_color="transparent", hover_color=COLORS["danger_bg"],
                                    border_width=1, border_color=COLORS["danger"] if stop_enabled else COLORS["border"],
                                    text_color=COLORS["danger"] if stop_enabled else COLORS["subtle"],
                                    state="normal" if stop_enabled else "disabled", font=FONTS["helper"],
                                    command=lambda i=tid: self._run_task_card_action(i, self.on_task_stop))
        stop_button.pack(side="left", padx=(0, 4))
        export_enabled = status in paused_states | stopped_states | {"done"}
        export_button = ctk.CTkButton(actions_right, text="导出", width=58, height=30, corner_radius=7,
                                      fg_color="transparent", hover_color=COLORS["surface_hover"],
                                      border_width=1, border_color=COLORS["border"],
                                      text_color=COLORS["text_2"] if export_enabled else COLORS["subtle"],
                                      state="normal" if export_enabled else "disabled", font=FONTS["helper"],
                                      command=lambda i=tid: self._run_task_card_action(i, self.on_task_export))
        export_button.pack(side="left", padx=(0, 4))
        monitor_rule = task.get("monitoring_rule") or {}
        monitor_enabled = bool(int(monitor_rule.get("enabled", 0) or 0))
        monitor_button = ctk.CTkButton(
            actions_right, text="监控设置" if not monitor_enabled else "增量监控",
            width=94, height=30, corner_radius=7,
            fg_color=COLORS["info_bg"] if monitor_enabled else "transparent",
            hover_color=COLORS["primary_soft"], border_width=1,
            border_color=COLORS["info"] if monitor_enabled else COLORS["border"],
            text_color=COLORS["info"] if monitor_enabled else COLORS["text_2"],
            font=FONTS["helper"],
            command=lambda i=tid: self._open_monitoring_dialog(i))
        monitor_button.pack(side="left", padx=(5, 0))
        # 第二行只放统一长度的进度条，减少卡片高度并保证多任务可浏览。
        bar = ctk.CTkProgressBar(card, height=6, corner_radius=3,
                                 fg_color=COLORS["surface_3"],
                                 progress_color=COLORS["success"] if ratio >= 1 else COLORS["primary"])
        bar.pack(fill="x", padx=15, pady=(0, 12))
        bar.set(ratio)
        self._task_card_refs[int(tid)] = {
             "card": card, "summary": summary_label, "error": error_label,
             "progress": progress_label, "bar": bar, "status": status_badge,
             "action": action_button, "stop": stop_button, "export": export_button,
             "monitor": monitor_button,
         }
        # 绑定整棵任务卡片控件树，进度条/标签/状态块均可右键。
        self._bind_task_card_widgets(card, tid)

    def _open_monitoring_dialog(self, task_id):
        """打开单任务增量监控设置；默认使用平台对应的最新排序。"""
        task_id = int(task_id)
        report = getattr(self, "_latest_report", {}) or {}
        task = (report.get("tasks", {}) or {}).get(task_id)
        if task is None:
            try:
                task = self.sched.get_task(task_id) or {}
            except Exception as exc:  # noqa: BLE001
                self._log(f"读取任务#{task_id}监控设置失败：{exc}")
                return
        rule = task.get("monitoring_rule") or {}
        platform = task.get("platform", "douyin")
        interval = int(rule.get("interval_seconds") or 3600)
        interval = max(60, min(604800, interval))
        enabled = bool(int(rule.get("enabled", 0) or 0)) if rule else True
        current_key = task.get("search_sort") or latest_sort_key(platform)
        if not rule:
            current_key = latest_sort_key(platform)
        sort_var = tk.StringVar(value=sort_label(platform, current_key))
        enabled_var = tk.BooleanVar(value=enabled)
        hours_var = tk.StringVar(value=str(interval // 3600))
        minutes_var = tk.StringVar(value=str((interval % 3600) // 60))

        if ctk is None:
            return self._open_monitoring_dialog_legacy(
                task_id, task, enabled_var, hours_var, minutes_var, sort_var
            )
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("定时增量监控")
        dialog.geometry("540x420")
        dialog.minsize(480, 360)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(fg_color=COLORS["surface_2"])
        ctk.CTkLabel(dialog, text=f"任务#{task_id} · {task.get('keyword', '')}",
                     text_color=COLORS["text"], font=FONTS["section_title"]).pack(
                         anchor="w", padx=28, pady=(24, 4))
        ctk.CTkLabel(dialog, text="设置后由后台自动触发增量采集，不改变已有历史数据。",
                     text_color=COLORS["muted"], font=FONTS["helper"]).pack(
                         anchor="w", padx=28, pady=(0, 18))
        body = ctk.CTkFrame(dialog, fg_color=COLORS["surface"], corner_radius=RADIUS["card"])
        body.pack(fill="both", expand=True, padx=22, pady=(0, 14))
        ctk.CTkCheckBox(body, text="启用定时增量监控", variable=enabled_var,
                        fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                        text_color=COLORS["text"], font=FONTS["body"]).pack(
                            anchor="w", padx=20, pady=(20, 18))
        interval_row = ctk.CTkFrame(body, fg_color="transparent")
        interval_row.pack(fill="x", padx=20, pady=(0, 18))
        ctk.CTkLabel(interval_row, text="每隔", text_color=COLORS["text_2"],
                     font=FONTS["body"]).pack(side="left")
        ctk.CTkEntry(interval_row, textvariable=hours_var, width=72, height=36,
                     fg_color=COLORS["window"], border_color=COLORS["border"]).pack(side="left", padx=8)
        ctk.CTkLabel(interval_row, text="小时", text_color=COLORS["muted"],
                     font=FONTS["helper"]).pack(side="left")
        ctk.CTkEntry(interval_row, textvariable=minutes_var, width=72, height=36,
                     fg_color=COLORS["window"], border_color=COLORS["border"]).pack(side="left", padx=8)
        ctk.CTkLabel(interval_row, text="分钟执行一次", text_color=COLORS["muted"],
                     font=FONTS["helper"]).pack(side="left")
        ctk.CTkLabel(body, text="增量监控搜索排序", text_color=COLORS["text_2"],
                     font=FONTS["helper"]).pack(anchor="w", padx=20, pady=(0, 5))
        ctk.CTkComboBox(body, variable=sort_var, values=sort_labels(platform), height=36,
                        fg_color=COLORS["window"], border_color=COLORS["border"],
                        button_color=COLORS["surface_3"], button_hover_color=COLORS["surface_hover"],
                        dropdown_fg_color=COLORS["surface_2"], dropdown_hover_color=COLORS["primary_soft"],
                        text_color=COLORS["text"], font=FONTS["body"]).pack(fill="x", padx=20, pady=(0, 10))
        ctk.CTkLabel(body, text="默认排序为最新；各平台会自动映射到对应的最新排序规则（快手当前使用综合排序）。",
                     text_color=COLORS["subtle"], font=FONTS["helper"], wraplength=450,
                     justify="left").pack(anchor="w", padx=20, pady=(0, 16))
        footer = ctk.CTkFrame(dialog, fg_color="transparent")
        footer.pack(fill="x", padx=24, pady=(0, 20))
        status_label = ctk.CTkLabel(footer, text="", text_color=COLORS["danger"], font=FONTS["helper"])
        status_label.pack(side="left")

        def save():
            try:
                hours = int(hours_var.get().strip() or 0)
                minutes = int(minutes_var.get().strip() or 0)
                if hours < 0 or minutes < 0 or minutes > 59:
                    raise ValueError
                total = hours * 3600 + minutes * 60
                if not 60 <= total <= 604800:
                    raise ValueError
            except ValueError:
                status_label.configure(text="间隔需为 0–168 小时、0–59 分钟，且总时长为 1 分钟至 7 天")
                return
            selected_sort = sort_key(platform, sort_var.get(), latest_sort_key(platform))
            try:
                self.sched.configure_monitoring(
                    task_id, interval_seconds=total, enabled=bool(enabled_var.get()),
                    search_sort=selected_sort,
                )
                self._log(
                    f"任务#{task_id} 增量监控已{'启用' if enabled_var.get() else '停用'}："
                    f"每 {hours} 小时 {minutes} 分钟，排序={sort_label(platform, selected_sort)}"
                )
                dialog.destroy()
                self.refresh_all()
            except Exception as exc:  # noqa: BLE001
                status_label.configure(text=f"保存失败：{type(exc).__name__}: {exc}")

        ctk.CTkButton(footer, text="保存设置", width=112, height=36,
                      fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                      font=FONTS["body_bold"], command=save).pack(side="right")
        ctk.CTkButton(footer, text="取消", width=88, height=36,
                      fg_color="transparent", hover_color=COLORS["surface_hover"],
                      border_width=1, border_color=COLORS["border"],
                      text_color=COLORS["text_2"], font=FONTS["body"],
                      command=dialog.destroy).pack(side="right", padx=(0, 10))

    def _open_monitoring_dialog_legacy(self, task_id, task, enabled_var,
                                       hours_var, minutes_var, sort_var):
        """无 CustomTkinter 时的简化兼容弹窗。"""
        dialog = tk.Toplevel(self.root)
        dialog.title("定时增量监控")
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"任务#{task_id} · {task.get('keyword', '')}").pack(anchor="w")
        ttk.Checkbutton(body, text="启用定时增量监控", variable=enabled_var).pack(anchor="w", pady=10)
        row = ttk.Frame(body)
        row.pack(anchor="w", pady=6)
        ttk.Label(row, text="每隔").pack(side="left")
        ttk.Entry(row, textvariable=hours_var, width=6).pack(side="left", padx=5)
        ttk.Label(row, text="小时").pack(side="left")
        ttk.Entry(row, textvariable=minutes_var, width=6).pack(side="left", padx=5)
        ttk.Label(row, text="分钟").pack(side="left")
        ttk.Label(body, text="搜索排序").pack(anchor="w", pady=(10, 2))
        ttk.Combobox(body, textvariable=sort_var, values=sort_labels(task.get("platform", "douyin")),
                     state="readonly", width=24).pack(anchor="w")
        def save():
            try:
                total = int(hours_var.get()) * 3600 + int(minutes_var.get()) * 60
                if not 60 <= total <= 604800:
                    raise ValueError
                platform = task.get("platform", "douyin")
                self.sched.configure_monitoring(task_id, total, bool(enabled_var.get()),
                                                sort_key(platform, sort_var.get(), latest_sort_key(platform)))
                dialog.destroy()
                self.refresh_all()
            except Exception as exc:
                messagebox.showerror("保存失败", str(exc), parent=dialog)
        ttk.Button(body, text="保存设置", command=save).pack(anchor="e", pady=(16, 0))

    @staticmethod
    def _task_needs_more_after_exhausted(task) -> bool:
        """上一轮已确认没有更多视频、但尚未达到目标时允许人工补采。"""
        try:
            target = max(1, int(task.get("effective_target_count") or task.get("target_count", 100) or 100))
            done = int(task.get("video_done", 0) or 0)
            exhausted = bool(int(task.get("search_exhausted", 0) or 0))
            return exhausted and done < target
        except (TypeError, ValueError):
            return False

    def _task_display_status(self, task) -> str:
        status = task.get("status", "")
        if self._task_needs_more_after_exhausted(task):
            return "暂无更多视频"
        if status in ("phase_a_search", "phase_b_comments", "running"):
            return "采集中"
        return TASK_STATUS_CN.get(status, status)

    def _run_task_card_action(self, task_id, command):
        """执行任务卡片按钮命令前锁定对应任务，避免操作到上一次右键任务。"""
        self._task_selected_id = int(task_id)
        command()

    def _update_task_cards_live(self, tasks):
        """仅更新任务卡片中的进度/评论数字，不重建卡片控件。"""
        for tid, task in tasks.items():
            ref = getattr(self, "_task_card_refs", {}).get(int(tid))
            if not isinstance(ref, dict):
                continue
            target = max(1, int(task.get("effective_target_count") or task.get("target_count", 100) or 100))
            done = int(task.get("video_done", 0) or 0)
            collected_total = max(done, int(task.get("videos_total", 0) or 0))
            plat = PLATFORM_CN.get(task.get("platform", "douyin"), task.get("platform", ""))
            mode = MODE_CN.get(task.get("collect_mode", "standard"), "标准")
            keyword_count = int(task.get("keyword_count") or 0)
            target_text = (f"目标 {target} 条（{keyword_count} 个关键词，每词 {task.get('target_per_keyword', target)} 条）"
                           if keyword_count > 1 else f"目标 {target} 条")
            ref["summary"].configure(
                text=f"{plat}  ·  {mode}模式  ·  {target_text}  ·  评论 {task.get('comments', 0)}")
            ref["progress"].configure(text=f"{done} / {collected_total}")
            ref["bar"].set(min(1.0, done / target))
            status = task.get("status", "")
            human_waiting = self._task_has_waiting_human(tid)
            no_more_videos = self._task_needs_more_after_exhausted(task)
            status_text = self._task_display_status(task)
            fg, bg = status_palette(status)
            ref["status"].configure(text=status_text, text_color=fg, fg_color=bg)
            running_states = {"phase_a_search", "phase_b_comments", "running"}
            paused_states = {"paused", "incomplete", "failed", "no_account", "waiting_account"}
            stopped_states = {"stopped", "aborted"}
            # 与首次构建卡片保持相同优先级，实时刷新时不能把“暂停”改回“继续采集”。
            if status in running_states:
                action_text, action_command, action_enabled = "暂停", self.on_task_pause, True
            elif human_waiting:
                # 实时刷新同样优先显示人工恢复动作，避免按钮在验证完成前后跳成“继续采集”。
                action_text, action_command, action_enabled = "继续", self.on_task_resume, True
            elif status == "paused":
                # 与首次构建卡片一致：暂停状态的下一步动作显示为“开始”。
                action_text, action_command, action_enabled = "开始", self.on_task_resume, True
            elif no_more_videos:
                action_text, action_command, action_enabled = "继续采集", self.on_task_resume, True
            elif status in paused_states | stopped_states:
                action_text, action_command, action_enabled = "开始", self.on_task_resume, True
            elif status == "pending" and human_waiting:
                action_text, action_command, action_enabled = "继续", self.on_task_resume, True
            elif status == "pending":
                action_text, action_command, action_enabled = "开始", self.on_task_start, True
            else:
                action_text, action_command, action_enabled = "开始", self.on_task_start, False
            ref["action"].configure(text=action_text, state="normal" if action_enabled else "disabled",
                                     fg_color=COLORS["primary"] if action_enabled else COLORS["surface_3"],
                                     hover_color=COLORS["primary_hover"] if action_enabled else COLORS["surface_3"],
                                     text_color=COLORS["text"] if action_enabled else COLORS["subtle"],
                                     command=lambda i=int(tid), cmd=action_command: self._run_task_card_action(i, cmd))
            stop_enabled = status in running_states | paused_states
            ref["stop"].configure(state="normal" if stop_enabled else "disabled",
                                   border_color=COLORS["danger"] if stop_enabled else COLORS["border"],
                                   text_color=COLORS["danger"] if stop_enabled else COLORS["subtle"])
            export_enabled = status in paused_states | stopped_states | {"done"}
            ref["export"].configure(state="normal" if export_enabled else "disabled",
                                     text_color=COLORS["text_2"] if export_enabled else COLORS["subtle"])
            err = task.get("error_message") or ""
            if err:
                ref["error"].configure(text=f"最近异常：{err}")
                if not ref["error"].winfo_ismapped():
                    ref["error"].pack(fill="x", pady=(3, 0), before=ref["summary"])
            elif ref["error"].winfo_ismapped():
                ref["error"].pack_forget()

    def _bind_task_card_widgets(self, widget, task_id):
        widget.bind("<Button-1>", lambda _e, i=task_id: self._select_task_card(i), add="+")
        widget.bind("<Button-3>", lambda e, i=task_id: self._show_task_card_menu(e, i), add="+")
        for child in widget.winfo_children():
            self._bind_task_card_widgets(child, task_id)

    def _select_task_card(self, task_id):
        task_id = int(task_id)
        self._task_selected_id = task_id
        # 只更新选中态，不触发 refresh_all()，避免点击时销毁并重建整组卡片造成闪烁。
        for tid, ref in getattr(self, "_task_card_refs", {}).items():
            card = ref.get("card") if isinstance(ref, dict) else ref
            selected = int(tid) == task_id
            try:
                card.configure(
                    fg_color=COLORS["primary_soft"] if selected else COLORS["surface"],
                    border_color=COLORS["primary"] if selected else COLORS["border_soft"],
                )
            except Exception:
                pass

    def _show_task_card_menu(self, event, task_id):
        self._task_selected_id = int(task_id)
        if ctk:
            self._show_task_popup(event.x_root, event.y_root)
            return
        try:
            self.task_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.task_menu.grab_release()

    def _refresh_acct_combo(self, accounts):
        """更新账号选择弹窗的数据，按平台排序并保留已选账号。"""
        demo_names = {"演示账号1", "演示账号2", "演示账号3"}
        next_bindings = sorted([
            {"id": a.get("id"), "name": a.get("name", account_key),
             "platform": a.get("platform", "douyin"),
             "wid": a.get("bb_window_id"), "status": a.get("status", "idle")}
            for account_key, a in accounts.items()
            if self.demo or (
                a.get("name", account_key) not in demo_names
                and not str(a.get("bb_window_id") or "").strip().lower().startswith("demo-")
            )
        ], key=lambda x: (0 if x["platform"] == "douyin" else 1, x["name"]))
        self._task_acct_bindings = next_bindings
        self._selected_task_account_names &= {x["name"] for x in next_bindings}
        self._update_account_summary()

    def _refresh_export_tasks(self, tasks):
        """刷新可直接从数据库导出的任务列表，并保留当前选择。"""
        rows = []
        values = []
        for tid, task in tasks.items():
            if task.get("status") not in ("done", "failed"):
                continue
            if not task.get("videos_total"):
                continue
            rows.append((int(tid), task.get("platform", "douyin"), task.get("keyword", "")))
            values.append(f"任务#{tid} · {task.get('keyword', '')} · {task.get('status', '')}")
        old = self.exp_task.get()
        self._export_task_rows = rows
        self.exp_task_combo["values"] = values
        if old in values:
            self.exp_task.set(old)
        elif values:
            self.exp_task_combo.current(0)
        else:
            self.exp_task.set("")

    def _notify_human(self, names):
        """P7：弹出系统通知 + 日志提示。仅首次触发时弹一次。"""
        key = ",".join(names)
        if getattr(self, "_last_human_notify", "") == key:
            return
        self._last_human_notify = key
        try:
            self.root.bell()
        except Exception:
            pass
        self._log(f"🚨 需要人工介入: {key} — 请到对应浏览器处理验证/登录")

    def _dismiss_human_banner(self):
        """隐藏人工提示，不改变任务冻结状态；继续操作仍需点击任务卡片。"""
        self._human_banner_dismissed = True
        if self.banner.winfo_manager():
            self.banner.pack_forget()
        for alert in (getattr(self, "overview_banner", None), getattr(self, "task_banner", None)):
            if alert is not None and alert.winfo_manager():
                alert.pack_forget()

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #
    def _on_debug_trace(self, trace_record):
        """接收所有组件的结构化事件，并汇总到统一操作日志。"""
        try:
            record = self._operation_log.append_trace(trace_record)
        except Exception:
            return
        # Network.getResponseBody 在响应已被浏览器回收时会返回这个预期错误。
        # 原始 CDP 文件仍保留完整记录，但不把高频、不可恢复的噪声推到 GUI。
        fields = trace_record.get("fields") if isinstance(trace_record, dict) else {}
        if (
            trace_record.get("component") == "cdp"
            and trace_record.get("event") in {"command_failed", "command_ignored"}
            and isinstance(fields, dict)
            and fields.get("method") == "Network.getResponseBody"
            and (
                fields.get("reason") == "response_body_expired"
                or "No resource with given identifier found" in str(
                    fields.get("exception_message") or ""
                )
            )
        ):
            return
        self._queue_log_record(record)

    def _append_operation_log_record(self, record):
        """把结构化日志追加到界面；不再次写入存储，避免递归。"""
        try:
            settings_page = getattr(self, "settings_page", None)
            if settings_page is not None:
                settings_page.append_operation_log(record)
        except Exception:
            pass

    def _append_operation_log_records(self, records):
        """批量追加结构化日志，避免每条记录都触发一次控件重排。"""
        if not records:
            return
        try:
            settings_page = getattr(self, "settings_page", None)
            if settings_page is None:
                return
            append_many = getattr(settings_page, "append_operation_logs", None)
            if callable(append_many):
                append_many(records)
            else:
                for record in records:
                    settings_page.append_operation_log(record)
        except Exception:
            pass

    def _queue_log_record(self, record, *, legacy_text=None):
        """把日志加入单一 UI 队列；落盘与界面渲染彻底解耦。"""
        if not isinstance(record, dict):
            return
        with self._log_display_queue_lock:
            if len(self._log_display_queue) >= self._log_display_limit:
                # 仅跳过界面展示，OperationLog 已经把完整记录写入文件。
                self._log_display_dropped += 1
            else:
                self._log_display_queue.append((record, legacy_text))
            if self._log_flush_after is not None or getattr(self, "_closing", False):
                return
            self._log_flush_after = True
            if not self._ui_call_later(80, self._flush_log_display_queue):
                self._log_flush_after = None

    def _flush_log_display_queue(self):
        """主线程按批次刷新日志，最多每次处理 200 条并主动让出事件循环。"""
        with self._log_display_queue_lock:
            self._log_flush_after = None
            items = []
            for _ in range(min(self._log_flush_batch_size, len(self._log_display_queue))):
                items.append(self._log_display_queue.popleft())
            dropped = self._log_display_dropped
            self._log_display_dropped = 0

        records = [record for record, _ in items]
        legacy_lines = [text for _, text in items if text]
        if legacy_lines:
            try:
                self.log_text.config(state="normal")
                self.log_text.insert("end", "".join(f"{line}\n" for line in legacy_lines))
                # 界面只保留最近 2000 行，完整日志仍在 JSONL 文件中。
                line_count = int(self.log_text.index("end-1c").split(".")[0])
                if line_count > 2000:
                    self.log_text.delete("1.0", f"{line_count - 2000}.0")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
            except Exception:
                pass
        if records:
            self._append_operation_log_records(records)
        if dropped:
            try:
                self.log_text.config(state="normal")
                self.log_text.insert(
                    "end",
                    f"[日志界面] 高峰期跳过展示 {dropped} 条，完整记录已保存到日志文件\n",
                )
                self.log_text.see("end")
                self.log_text.config(state="disabled")
            except Exception:
                pass

        with self._log_display_queue_lock:
            has_more = bool(self._log_display_queue)
            if has_more and self._log_flush_after is None and not getattr(self, "_closing", False):
                self._log_flush_after = True
                if not self._ui_call_later(80, self._flush_log_display_queue):
                    self._log_flush_after = None

    def _current_page_for_log(self):
        return str(getattr(self, "_active_page", "unknown") or "unknown")

    def _queue_audit_record(self, message, details):
        """非阻塞地提交界面审计记录，文件写入由后台线程完成。"""
        item = (str(message or ""), dict(details or {}))
        try:
            self._audit_queue.put_nowait(item)
        except queue.Full:
            # 不让审计队列反过来阻塞用户操作；完整业务日志仍继续写入。
            self._audit_dropped = int(getattr(self, "_audit_dropped", 0)) + 1

    def _audit_log_loop(self):
        """顺序落盘点击/滚轮审计，退出时先排空队列再结束。"""
        audit_queue = getattr(self, "_audit_queue", None)
        stop = getattr(self, "_audit_stop", None)
        if audit_queue is None or stop is None:
            return
        while not stop.is_set() or not audit_queue.empty():
            try:
                message, details = audit_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._operation_log.append(
                    message,
                    source="ui",
                    event=str(details.pop("event", "ui_audit")),
                    action=str(details.pop("action", "audit")),
                    details=details,
                )
            except Exception:
                pass
            finally:
                audit_queue.task_done()

    @staticmethod
    def _widget_log_snapshot(widget):
        """读取控件公开状态，失败时返回空值，绝不影响点击处理。"""
        result = {
            "class": "",
            "path": "",
            "text": "",
            "value": "",
            "state": "",
        }
        try:
            result["class"] = str(widget.winfo_class())
            result["path"] = str(widget)
        except Exception:
            return result
        for option, key in (("text", "text"), ("value", "value"), ("state", "state")):
            try:
                value = widget.cget(option)
                result[key] = str(value)[:500]
            except Exception:
                pass
        return result

    def _audit_ui_click(self, event):
        """记录鼠标点击目标与坐标；不修改 Tk 事件返回值。"""
        try:
            widget = event.widget
            snapshot = self._widget_log_snapshot(widget)
            self._queue_audit_record("界面点击", {
                "event": "ui_click",
                "action": "mouse_release",
                "button": 1,
                "page": self._current_page_for_log(),
                "x": int(getattr(event, "x", 0)),
                "y": int(getattr(event, "y", 0)),
                "x_root": int(getattr(event, "x_root", 0)),
                "y_root": int(getattr(event, "y_root", 0)),
                "widget": snapshot,
            })
        except Exception:
            pass

    def _audit_ui_wheel(self, event):
        """记录滚轮方向和目标控件，辅助排查评论区滚动/页面跳动。"""
        try:
            widget = event.widget
            self._queue_audit_record("界面滚轮", {
                "event": "ui_wheel",
                "action": "mouse_wheel",
                "delta": int(getattr(event, "delta", 0)),
                "page": self._current_page_for_log(),
                "widget": self._widget_log_snapshot(widget),
            })
        except Exception:
            pass

    def _hourly_operation_log_snapshot(self):
        """每小时保存一份可直接发送/回看的完整日志快照。"""
        try:
            result = self._operation_log.save_hourly_snapshot()
            self._log(
                f"后台操作日志已自动保存：{result['count']}条，"
                f"文本快照={result['text']}"
            )
            settings_page = getattr(self, "settings_page", None)
            if settings_page is not None:
                settings_page._reload_operation_logs()
        except Exception as exc:
            # 快照失败也写入现有实时日志，便于发现磁盘/权限问题。
            self._log(f"后台操作日志自动保存失败：{type(exc).__name__}: {exc}")
        try:
            self._operation_log_hourly_after = self.root.after(
                3600000, self._hourly_operation_log_snapshot
            )
        except Exception:
            self._operation_log_hourly_after = None

    def _get_operation_logs(self, limit=1000):
        return self._operation_log.recent(limit)

    def _export_operation_log(self):
        """由设置页调用，导出当前已脱敏的后台操作记录。"""
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出后台操作日志",
            initialdir=os.path.join(PROJECT_ROOT, "data", "logs"),
            initialfile=f"后台操作日志_{time.strftime('%Y%m%d_%H%M%S')}.txt",
            defaultextension=".txt",
            filetypes=[("文本日志", "*.txt"), ("全部文件", "*.*")],
        )
        if not path:
            return ""
        try:
            exported = self._operation_log.export_text(path)
        except Exception as exc:  # noqa: BLE001
            self._log(f"后台操作日志导出失败：{type(exc).__name__}: {exc}")
            raise
        self._log(f"后台操作日志已导出：{exported}")
        return exported

    def _log(self, msg):
        msg = self._sanitize_log(msg)
        record = self._operation_log.append(
            msg,
            source="gui",
            event="ui_message",
            action="log",
            details={
                "thread": threading.current_thread().name,
                "page": self._current_page_for_log(),
            },
        )
        self._queue_log_record(
            record,
            legacy_text=f"[{time.strftime('%H:%M:%S')}] {msg}",
        )

    @staticmethod
    def _sanitize_log(msg):
        """日志脱敏与截断（P3-4，方案 15：日志不输出完整客户留言）。"""
        if msg is None:
            return ""
        text = str(msg)
        # 敏感字段值脱敏
        for key in ("cookie", "token", "password", "window_id", "bb_window_id"):
            if key in text.lower():
                text = re.sub(rf"(?i)({key}\s*[=:]\s*)[^\s,;]+", rf"\1***", text)
        # 截断 200 字
        if len(text) > 200:
            text = text[:200] + "…"
        return text

    def _exp(self, msg):
        if threading.current_thread() is not threading.main_thread():
            try:
                self._ui_call(self._exp, msg)
            except Exception:
                pass
            return
        try:
            self.exp_log.config(state="normal")
            self.exp_log.insert("end", msg + "\n")
            self.exp_log.see("end")
            self.exp_log.config(state="disabled")
        except Exception:
            pass
        # 旧实验面板的后台反馈也纳入统一操作日志，方便导出复盘。
        self._log(f"[实验] {msg}")

    def _on_close(self):
        if getattr(self, "_closing", False):
            return
        self._closing = True
        try:
            if self._operation_log_hourly_after is not None:
                self.root.after_cancel(self._operation_log_hourly_after)
                self._operation_log_hourly_after = None
        except Exception:
            pass
        try:
            with self._log_display_queue_lock:
                self._log_flush_after = None
        except Exception:
            pass
        try:
            self._refresh_requested.set()
            pending_ui_after = getattr(self, "_ui_dispatch_after", None)
            self._ui_dispatch_after = None
            if pending_ui_after is not None:
                self.root.after_cancel(pending_ui_after)
            with self._ui_dispatch_queue_lock:
                self._ui_dispatch_queue.clear()
        except Exception:
            pass
        try:
            # 先停止并排空审计写入队列，再保存小时快照，确保退出前的
            # 点击/滚轮记录不会落在快照之后。
            audit_stop = getattr(self, "_audit_stop", None)
            if audit_stop is not None:
                audit_stop.set()
            audit_writer = getattr(self, "_audit_writer", None)
            if (audit_writer is not None and audit_writer.is_alive()
                    and audit_writer is not threading.current_thread()):
                audit_writer.join(timeout=1.0)
        except Exception:
            pass
        try:
            self._operation_log.save_hourly_snapshot()
        except Exception:
            pass
        unregister_trace_sink(getattr(self, "_trace_sink", None))
        self._uninstall_native_move_size_hook()
        self._save_preferences()
        self._poll_stop.set()
        try:
            self._heartbeat_stop.set()
        except Exception:
            pass
        self.root.withdraw()
        self._close_done = threading.Event()

        def cleanup():
            try:
                if self.sched is not None:
                    self.sched.shutdown(close_connections=True)
                if getattr(self, "live_collector", None) is not None:
                    self.live_collector.shutdown()
                # 关闭线索/互动模块共享连接（页面资源释放）
                if getattr(self, "_lead_db_conn", None) is not None:
                    try:
                        self._lead_db_conn.close()
                    except Exception:
                        pass
                    self._lead_db_conn = None
            except Exception:
                pass
            finally:
                self._close_done.set()

        def finish_close():
            if self._close_done.is_set():
                self.root.destroy()
            else:
                self.root.after(50, finish_close)

        threading.Thread(target=cleanup, name="gui-shutdown", daemon=True).start()
        finish_close()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    demo = "--demo" in sys.argv
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DouyinXhsCollector.GUI")
        except Exception:
            pass
    if not _acquire_instance_lock():
        # 不弹模态框：监控器/双击启动时，模态框会留下隐藏进程并阻塞自动重启。
        try:
            with open(os.path.join(PROJECT_ROOT, "data", "gui_startup.log"), "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 已有实例运行，本次启动退出。\n")
        except Exception:
            pass
        return
    if ctk is not None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        root = ctk.CTk()
    else:
        root = tk.Tk()
    app = GuiApp(root, demo=demo)
    try:
        root.update_idletasks()
        # 保持用户保存的窗口尺寸，不再启动时强制最大化。
        root.state("normal")
        if os.name == "nt":
            import ctypes
            hwnd = int(ctypes.windll.user32.FindWindowW(None, "多账号采集平台") or root.winfo_id())
            ico = os.path.join(PROJECT_ROOT, "assets", "app_icon.ico")
            hicon = ctypes.windll.user32.LoadImageW(0, ico, 1, 0, 0, 0x00000010 | 0x00000040)
            if hicon:
                ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 1, hicon)  # WM_SETICON large
                ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 0, hicon)  # WM_SETICON small
    except tk.TclError:
        pass
    root.mainloop()


if __name__ == "__main__":
    main()
