# -*- coding: utf-8 -*-
"""线索中心页面（技术方案 Agent E / 8.3）。

- ``LeadPagePresenter``：纯逻辑层，把筛选状态 → LeadQuery → 表格行/详情。
  不依赖 Tk，可直接单测（验收红线：业务判断不写入控件回调）。
- ``LeadsPage``：Tk 视图，只做渲染和事件转发。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional
import webbrowser

import customtkinter as ctk

from leads.models import IntentLevel, LeadStatus, Page, Pool
from leads.repository import LeadQuery
from leads.service import LeadService
from debug_trace import DebugTrace

from ..components.data_table import DataTable
from ..components.detail_drawer import DetailDrawer
from ..components.filter_bar import FilterBar
from ..components.status_badge import (
    INTENT_LABELS, LEAD_STATUS_LABELS, POOL_LABELS,
)
from ..theme import COLORS, FONTS, RADIUS, SPACE

log = logging.getLogger(__name__)


def _open_url_in_chrome(url: str) -> None:
    """使用本机 Chrome 打开原作地址；未找到 Chrome 时回退到系统浏览器。"""
    normalized = str(url or "").strip()
    if not normalized:
        return
    if not normalized.startswith(("http://", "https://")):
        normalized = "https://" + normalized

    candidates = [
        shutil.which("chrome"),
        os.path.join(os.environ.get("PROGRAMFILES", ""),
                     "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""),
                     "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Google", "Chrome", "Application", "chrome.exe"),
    ]
    chrome_path = next((path for path in candidates if path and os.path.exists(path)), None)
    try:
        if chrome_path:
            subprocess.Popen([chrome_path, normalized], close_fds=True)
        else:
            webbrowser.open(normalized)
        log.info("打开原作地址: %s", normalized)
    except Exception as exc:  # noqa: BLE001
        log.warning("打开原作地址失败: %s, url=%s", exc, normalized)


# =====================================================================
# Presenter（纯逻辑，无 Tk 依赖）
# =====================================================================
class LeadPagePresenter:
    """线索中心 Presenter：筛选状态 → 查询 → 表格行/详情数据。"""

    PLATFORMS = ["抖音", "小红书", "微博", "B站", "快手"]
    PLATFORM_VALUES = {"抖音": "douyin", "小红书": "xhs", "微博": "weibo", "B站": "bilibili", "快手": "kuaishou"}
    PLATFORM_LABELS = {"douyin": "抖音", "xhs": "小红书", "weibo": "微博", "bilibili": "B站", "kuaishou": "快手"}
    POOLS = list(Pool.ALL)
    INTENTS = list(IntentLevel.ALL)
    FILTER_LABELS = {
        "pool": POOL_LABELS,
        "intent": INTENT_LABELS,
    }

    def __init__(self, service: LeadService, *, page_size: int = 50):
        self._service = service
        self._page_size = page_size
        self._filters: Dict[str, str] = {}
        self._keyword = ""

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def set_filter(self, key: str, value: str):
        if value in ("", "全部"):
            self._filters.pop(key, None)
        else:
            self._filters[key] = value

    def set_keyword(self, keyword: str):
        self._keyword = keyword

    def clear_filters(self):
        self._filters = {}
        self._keyword = ""

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def load_page(self, page: int = 1) -> Page:
        """按当前筛选条件查询一页线索。"""
        q = self._build_query(page)
        return self._service.list_leads(q)

    def load_detail(self, lead_id: int) -> Dict[str, Any]:
        """线索详情（含证据/分配/审计），转为抽屉可渲染的字典。"""
        detail = self._service.get_detail(lead_id)
        return {
            "lead": detail.lead,
            "evidence": detail.evidence,
            "assignments": detail.assignments,
            "timeline": detail.timeline,
            "audit": detail.audit,
        }

    def list_provinces(self) -> List[str]:
        return self._service.list_provinces()

    def task_options(self) -> Dict[str, str]:
        """返回任务编号到界面显示文本的映射。"""
        options: Dict[str, str] = {}
        for task in self._service.list_tasks():
            task_id = str(task.get("id"))
            platform = self.PLATFORM_LABELS.get(
                str(task.get("platform") or ""), str(task.get("platform") or "未知平台")
            )
            keyword = str(task.get("keyword") or "未命名关键词").strip()
            options[task_id] = f"任务#{task_id} · {platform} · {keyword}"
        return options

    # ------------------------------------------------------------------
    # 表格行组装（纯函数，便于单测）
    # ------------------------------------------------------------------
    @staticmethod
    def _two_line_text(value: Any, max_chars: int) -> str:
        """把列表文本限制在约两行，详情抽屉仍使用数据库中的完整内容。"""
        text = " ".join(str(value or "").split())
        if len(text) <= max_chars:
            return text
        return text[:max_chars - 1].rstrip() + "…"

    @staticmethod
    def to_row(summary) -> Dict[str, Any]:
        """把 LeadSummary 转成线索列表行数据，列表只保留用户需要的五列。"""
        full_comment = (summary.summary_text or "").strip() or "暂无评论内容"
        comment = LeadPagePresenter._two_line_text(full_comment, 76)
        comment_time = (summary.comment_time or summary.last_interaction_at or "").strip()
        if "T" in comment_time:
            comment_time = comment_time.replace("T", " ")
        if len(comment_time) > 19:
            comment_time = comment_time[:19]
        source_url_value = (summary.source_url or "").strip()
        source_url = "点击查看" if source_url_value else "暂无原作地址"
        user_full = (summary.nickname or summary.platform_user_id or "-").strip()
        user = LeadPagePresenter._two_line_text(user_full, 22)
        return {
            "_identity": str(summary.id),
            "id": summary.id,
            "platform": LeadPagePresenter.PLATFORM_LABELS.get(summary.platform, "未知平台"),
            "user": user,
            "user_full": user_full,
            "comment": comment,
            "comment_time": comment_time or "暂无时间",
            "source_url": source_url or "暂无原作地址",
            "source_url_value": source_url_value,
            # 保留旧 Presenter 字段供导出/旧调用方兼容；不再放入当前列表列。
            "summary": full_comment,
            "region": f"{summary.region_province or '未知地区'}（可信度{summary.region_confidence}）",
            "intent": INTENT_LABELS.get(summary.intent_level, "未知"),
            "owner": summary.owner_name or "未分配",
            "status": LEAD_STATUS_LABELS.get(summary.status, "未知状态"),
        }

    # ------------------------------------------------------------------
    def _build_query(self, page: int) -> LeadQuery:
        f = self._filters
        mapping = {
            "task": "task_id",
            "platform": "platform",
            "province": "province",
            "pool": "pool",
            "intent": "intent_level",
        }
        kwargs: Dict[str, Any] = {"page": page, "page_size": self._page_size}
        for ui_key, q_key in mapping.items():
            v = f.get(ui_key)
            if v:
                if ui_key == "platform":
                    v = self.PLATFORM_VALUES.get(v, v)
                elif ui_key == "task":
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        continue
                kwargs[q_key] = v
        if self._keyword:
            kwargs["keyword"] = self._keyword
        return LeadQuery(**kwargs)


# =====================================================================
# Tk 视图
# =====================================================================
COLUMNS = [
    ("select", "选择", 0, "w", None, None, 64),
    ("user", "用户", 1, "w", None, "wrap", 180),
    ("comment", "评论内容", 4, "w", None, "wrap", 520),
    ("comment_time", "评论时间", 1, "w", None, None, 165),
    ("source_url", "原作地址", 1, "w", None, "link", 120),
]


class LeadsPage(ctk.CTkFrame):
    """线索中心页面。可嵌入主窗口内容区。"""

    def __init__(self, master, presenter: LeadPagePresenter,
                 *, on_assign=None, on_export=None, on_owners=None,
                 on_to_interaction=None):
        super().__init__(master, fg_color=COLORS["window"])
        self._presenter = presenter
        self._on_assign = on_assign
        self._on_export = on_export
        self._on_owners = on_owners
        self._on_to_interaction = on_to_interaction
        self._page = 1
        self._refresh_request = 0
        self._refresh_inflight = False
        self._visible = False
        self._last_refresh_completed = 0.0
        self._filter_after = None
        self._perf_trace = DebugTrace("gui_performance")
        self._manual_refresh_pending = False
        self._build()

    # ------------------------------------------------------------------
    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # 标题
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=SPACE["xl"], pady=(SPACE["lg"], 0))
        header.grid_columnconfigure(0, weight=1)
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(title_box, text="线索中心", text_color=COLORS["text"],
                     font=FONTS["page_title"], anchor="w").pack(anchor="w")
        ctk.CTkLabel(title_box, text="评论 → 用户级线索；时效仅供参考，由人工控制",
                     text_color=COLORS["muted"], font=FONTS["page_subtitle"],
                     anchor="w").pack(anchor="w", pady=(2, 0))

        # 操作按钮
        self._refresh_button = ctk.CTkButton(
            header, text="↻ 刷新列表", width=112, height=36,
            fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
            text_color="#ffffff", corner_radius=RADIUS["control"],
            command=self._manual_refresh,
        )
        self._refresh_button.grid(row=0, column=1, padx=SPACE["sm"])
        ctk.CTkButton(header, text="导出", width=88, height=36,
                      fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                      text_color=COLORS["text"], corner_radius=RADIUS["control"],
                      command=lambda: self._on_export and self._on_export(
                          self._selected_ids())).grid(row=0, column=2, padx=SPACE["sm"])
        ctk.CTkButton(header, text="加入互动中心", width=120, height=36,
                      fg_color=COLORS["success"], hover_color="#3ddb9b",
                      text_color="#06130d", corner_radius=RADIUS["control"],
                      command=lambda: self._on_to_interaction and self._on_to_interaction(
                          self._checked_ids())).grid(row=0, column=3, padx=SPACE["sm"])

        # 筛选栏
        self._filter_bar = FilterBar(
            self, platforms=self._presenter.PLATFORMS,
            provinces=self._presenter.list_provinces(),
            intents=self._presenter.INTENTS,
            task_options=self._presenter.task_options(),
            labels=self._presenter.FILTER_LABELS,
            on_change=self._on_filter_change,
        )
        self._filter_bar.grid(row=1, column=0, sticky="ew", padx=SPACE["xl"],
                              pady=SPACE["md"])

        # 表格 + 抽屉容器
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=2, column=0, sticky="nsew", padx=SPACE["xl"],
                     pady=(0, SPACE["lg"]))
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(0, weight=1)

        self._table = DataTable(content, COLUMNS,
                                on_select=self._on_select_row,
                                on_cell_click=self._on_cell_click,
                                on_page=self._on_page)
        self._table.grid(row=0, column=0, sticky="nsew")

        # 抽屉以 content 为父容器（相对停靠，不用屏幕坐标）
        self._drawer = DetailDrawer(content, width=380)

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _on_filter_change(self, filters: Dict[str, str]):
        for k, v in filters.items():
            self._presenter.set_filter(k, v)
        self._page = 1
        # 先让下拉框完成收起与重绘；连续选择只执行最后一次后台查询。
        if self._filter_after is not None:
            try:
                self.after_cancel(self._filter_after)
            except Exception:
                pass
        self._filter_after = self.after(60, self._apply_filter_refresh)

    def _apply_filter_refresh(self):
        self._filter_after = None
        self.refresh()

    def _on_search(self, keyword: str):
        self._presenter.set_keyword(keyword)
        self._page = 1
        self.refresh()

    def _on_page(self, page: int):
        self._page = page
        self.refresh()

    def _on_select_row(self, row):
        if row is None:
            return
        try:
            detail = self._presenter.load_detail(int(row["id"]))
            self._drawer.open(detail)
        except Exception as exc:  # noqa: BLE001
            log.warning("加载线索详情失败: %s", exc)

    def _on_cell_click(self, row, key):
        if key != "source_url":
            return
        source_url = row.get("source_url_value") or ""
        if source_url:
            _open_url_in_chrome(source_url)

    def _selected_ids(self) -> List[int]:
        """优先取勾选行；无勾选时退回单选行（P2-1 多选支持）。"""
        checked = self._table.get_checked()
        if checked:
            return [int(row["id"]) for row in checked]
        row = self._table.get_selected()
        return [int(row["id"])] if row else []

    def _checked_ids(self) -> List[int]:
        """加入互动中心必须来自明确勾选，不能用单击行替代。"""
        return [int(row["id"]) for row in self._table.get_checked()]

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------
    def _manual_refresh(self):
        """只由用户点击触发查库，页面切换本身不触发刷新。"""
        if self._manual_refresh_pending or self._refresh_inflight:
            return
        self._manual_refresh_pending = True
        self._refresh_button.configure(text="刷新中…", state="disabled")
        self.refresh()

    def refresh(self):
        """后台查库，主线程只负责应用已准备好的行数据。"""
        if not self._visible:
            self._manual_refresh_pending = False
            return
        self._refresh_request += 1
        if self._refresh_inflight:
            return
        self._refresh_inflight = True
        request = self._refresh_request
        page_number = self._page
        threading.Thread(
            target=self._refresh_worker,
            args=(request, page_number),
            name="leads-page-refresh",
            daemon=True,
        ).start()

    def _refresh_worker(self, request: int, page_number: int):
        page = None
        rows = []
        task_options = {}
        error = None
        query_started = time.perf_counter()
        try:
            page = self._presenter.load_page(page_number)
            rows = [self._presenter.to_row(item) for item in page.items]
            task_options = self._presenter.task_options()
        except Exception as exc:  # noqa: BLE001
            error = exc
        query_ms = (time.perf_counter() - query_started) * 1000
        try:
            self.after(0, self._apply_refresh, request, page, rows, error, query_ms, task_options)
        except Exception:
            self._refresh_inflight = False

    def _apply_refresh(self, request, page, rows, error, query_ms=0.0, task_options=None):
        render_started = time.perf_counter()
        self._refresh_inflight = False
        self._manual_refresh_pending = False
        self._refresh_button.configure(text="↻ 刷新列表", state="normal")
        if request != self._refresh_request:
            # 筛选条件在查库期间发生变化，只再发起一次最新请求。
            if self._visible:
                self.after_idle(self.refresh)
            return
        # 页面已经切走时不在主线程重建表格；下次显示时再按缓存策略刷新。
        if not self._visible:
            return
        if error is not None or page is None:
            log.warning("刷新线索列表失败: %s", error)
            self._table.set_rows([])
            self._table.set_page(0, 1, 50)
            return
        if task_options is not None:
            self._filter_bar.set_options("task", list(task_options), task_options)
        # 差量更新优先，结构变化（行数/身份变化）时回退整表重建（P3-1）。
        if not self._table.update_rows(rows):
            self._table.set_rows(rows)
        self._table.set_page(page.total, page.page, page.page_size)
        self._last_refresh_completed = time.monotonic()
        self._perf_trace.emit(
            "leads_refresh",
            rows=len(rows), total=page.total, page=page.page,
            query_ms=round(query_ms, 2),
            render_ms=round((time.perf_counter() - render_started) * 1000, 2),
            filters=self._filter_bar.current(),
        )

    def on_show(self):
        """只显示驻留页面；查询由顶部“刷新列表”按钮或筛选操作触发。"""
        self._visible = True

    def on_hide(self):
        """页面切走时停止把后台查询结果应用到隐藏表格。"""
        self._visible = False
        self._refresh_request += 1
        if self._filter_after is not None:
            try:
                self.after_cancel(self._filter_after)
            except Exception:
                pass
            self._filter_after = None
