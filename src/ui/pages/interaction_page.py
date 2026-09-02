# -*- coding: utf-8 -*-
"""互动中心页面（技术方案 Agent F / 8.4）。

- ``InteractionPagePresenter``：纯逻辑层，草稿列表/审核/勿扰操作。
- ``InteractionPage``：Tk 视图。开放草稿生成、审核和待发送模拟填充；
  已发送/已回复/失败仍为只读结果页，真实发送保持人工确认与开关控制。
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from typing import Any, Callable, Dict, List, Optional

import customtkinter as ctk

from leads.models import DraftStatus

from ..components.status_badge import (
    DraftStatusBadge, IntentBadge, PoolBadge,
)
from ..theme import COLORS, FONTS, RADIUS, SPACE
from ..dialogs import ask_centered_confirm
from ..widgets import PlatformBadge
log = logging.getLogger(__name__)

PLATFORM_LABELS = {
    "douyin": "抖音", "xhs": "小红书", "weibo": "微博", "bilibili": "B站",
}
PLATFORM_VALUES = {label: key for key, label in PLATFORM_LABELS.items()}


# =====================================================================
# Presenter（纯逻辑）
# =====================================================================
class InteractionPagePresenter:
    """互动中心 Presenter：加载草稿、审核、勿扰。不依赖 Tk。"""

    # 互动中心只保留运营实际使用的四个阶段。
    TABS = [
        ("draft", "待生成", True),
        ("queued", "待发送", True),
        ("sent", "已回复", True),
        ("failed", "失败", True),
    ]

    def __init__(self, service, *, page_size: int = 30):
        self._service = service
        self._page_size = page_size

    # ------------------------------------------------------------------
    def list_drafts(self, status: str, page: int = 1, *,
                    platform: Optional[str] = None,
                    account_id: Optional[int] = None) -> Dict[str, Any]:
        """加载指定状态的草稿列表。"""
        if status == "sent":
            return self._service.list_drafts(
                statuses=[DraftStatus.SENT, "replied"], platform=platform,
                account_id=account_id, page=page, page_size=self._page_size,
            )
        return self._service.list_drafts(status=status, page=page,
                                         platform=platform, account_id=account_id,
                                         page_size=self._page_size)

    def approve(self, draft_id: int, content: str, reviewer: str = "ui-user"):
        self._service.review(draft_id, "approved", reviewer, content)
        self._service.stage_for_send(draft_id)

    def reject(self, draft_id: int, content: str, reviewer: str = "ui-user"):
        self._service.review(draft_id, "rejected", reviewer, content)

    def submit(self, draft_id: int):
        self._service.submit_for_review(draft_id)

    def submit_with_content(self, draft_id: int, content: str):
        self._service.update_draft_content(draft_id, content)
        self._service.submit_for_review(draft_id)

    def enter_send_page(self, draft_id: int, content: str):
        self._service.move_draft_to_send(draft_id, content)

    def delete(self, draft_id: int):
        self._service.delete_draft(draft_id)

    def return_failed_to_draft(self, draft_id: int):
        self._service.return_failed_to_draft(draft_id)

    def update_content(self, draft_id: int, content: str):
        self._service.update_draft_content(draft_id, content)

    def assign_reply_account(self, draft_id: int, account_id: int):
        self._service.assign_reply_account(draft_id, account_id)

    def mark_reply_failed(self, draft_id: int, message: str,
                          account_id: Optional[int] = None):
        self._service.mark_reply_failed(draft_id, message, account_id)

    def list_templates(self) -> Dict[str, str]:
        return self._service.list_templates()

    def render_template(self, template_id: str, lead_id: int) -> str:
        return self._service.render_template_for_lead(template_id, lead_id)

    def reload_templates(self) -> None:
        self._service.reload_templates()

    def suppress(self, lead_id: int, reason: str, actor: str = "ui-user"):
        self._service.add_suppression(lead_id, reason, actor=actor)

    def list_reply_accounts(self, include_unavailable: bool = False) -> List[dict]:
        return self._service.list_reply_accounts(include_unavailable=include_unavailable)

    def fill_browser_reply(self, draft_id: int, account_id: Optional[int] = None):
        return self._service.fill_browser_reply(draft_id, account_id)

    def simulate_browser_reply(self, draft_id: int, account_id: Optional[int] = None):
        return self._service.simulate_browser_reply(draft_id, account_id)

    def send_browser_reply(self, draft_id: int, account_id: Optional[int] = None,
                           *, real_send_enabled: bool = False):
        return self._service.send_browser_reply(
            draft_id, account_id, confirm=True,
            real_send_enabled=real_send_enabled,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def to_card(draft) -> Dict[str, Any]:
        """把 DraftDetail 转成审核卡片数据。"""
        lead = draft.lead or {}
        platform = {
            "douyin": "抖音", "xhs": "小红书", "weibo": "微博", "bilibili": "B站",
        }.get(lead.get("platform") or draft.channel, "未知平台")
        province = lead.get("region_province") or "未知地区"
        confidence = lead.get("region_confidence") or 0
        region = f"{province}（可信度{confidence}）" if confidence else province
        return {
            "draft_id": draft.id,
            "lead_id": draft.lead_id,
            "nickname": lead.get("nickname") or "未知",
            "platform": platform,
            "region": region,
            "comment_time": lead.get("last_interaction_at") or "暂无评论时间",
            "intent": lead.get("intent_level") or "unknown",
            "pool": lead.get("pool") or "unclassified",
            "template_id": draft.template_id,
            "content": draft.content,
            "status": draft.status,
            "reviewed_by": draft.reviewed_by,
            "reviewed_at": draft.reviewed_at,
            "reply_account_id": draft.reply_account_id,
            "reply_account_name": draft.reply_account_name,
            "reply_account_platform": draft.reply_account_platform,
            "original_comment": (draft.source_content or "").strip() or "暂无评论原文",
            "failure_reason": (draft.failure_reason or "").strip(),
        }


# =====================================================================
# Tk 视图
# =====================================================================
class InteractionPage(ctk.CTkFrame):
    """互动中心页面：审核草稿，并提供待发送池的逐条模拟填充。"""

    def __init__(self, master, presenter: InteractionPagePresenter,
                 *, on_templates=None, on_reply=None, on_batch_reply=None,
                 on_real_send_change=None):
        super().__init__(master, fg_color=COLORS["window"])
        self._presenter = presenter
        self._on_templates = on_templates
        self._on_reply = on_reply
        self._on_batch_reply = on_batch_reply
        self._on_real_send_change = on_real_send_change
        self._tab = "draft"
        self._cards: List[Dict[str, Any]] = []
        self._entries: Dict[int, Any] = {}
        self._template_maps: Dict[int, Dict[str, str]] = {}
        self._selected_draft_ids = set()
        self._selection_vars: Dict[int, tk.BooleanVar] = {}
        self._refresh_request = 0
        self._refresh_inflight = False
        self._render_request = 0
        self._render_after = None
        self._visible = False
        self._rendered_signature = None
        self._pending_render_signature = None
        self._account_map: Dict[str, int] = {}
        self._account_maps_by_platform: Dict[str, Dict[str, int]] = {}
        self._bulk_account_map: Dict[str, int] = {}
        self._filter_account_map: Dict[str, int] = {}
        self._account_records: List[dict] = []
        self._send_account_var = ctk.StringVar(value="正在加载账号…")
        self._sent_platform_var = ctk.StringVar(value="全部平台")
        self._sent_account_var = ctk.StringVar(value="全部账号")
        # 安全默认：每次启动均关闭，不从上次会话自动恢复。
        self._real_send_var = tk.BooleanVar(value=False)
        self._build()

    # ------------------------------------------------------------------
    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=SPACE["xl"], pady=(SPACE["lg"], 0))
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(titles, text="互动中心", text_color=COLORS["text"],
                     font=FONTS["page_title"], anchor="w").pack(anchor="w")
        self._subtitle = ctk.CTkLabel(
            titles, text="完整回复内容与人工审核 · 当前仅填入，不点击发送",
            text_color=COLORS["muted"], font=FONTS["page_subtitle"],
            anchor="w")
        self._subtitle.pack(anchor="w", pady=(2, 0))
        ctk.CTkButton(header, text="模板", width=72, height=34,
                      fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                      text_color=COLORS["text"], corner_radius=RADIUS["control"],
                      font=FONTS["helper"],
                      command=lambda: self._on_templates and self._on_templates()
                      ).pack(side="right", pady=(4, 0))
        self._real_send_switch = ctk.CTkSwitch(
            header, text="真实发送", variable=self._real_send_var,
            width=112, height=30, progress_color=COLORS["danger"],
            button_color="#ffffff", button_hover_color="#ffffff",
            text_color=COLORS["text"], font=FONTS["helper"],
            command=self._toggle_real_send,
        )
        self._real_send_switch.pack(side="right", padx=(0, SPACE["md"]), pady=(4, 0))

        # 页签
        tabs = ctk.CTkFrame(self, fg_color="transparent")
        tabs.grid(row=1, column=0, sticky="ew", padx=SPACE["xl"], pady=SPACE["md"])
        self._tab_buttons: Dict[str, ctk.CTkButton] = {}
        self._tab_enabled: Dict[str, bool] = {}
        for idx, (key, label, enabled) in enumerate(self._presenter.TABS):
            suffix = "" if enabled else "（未启用）"
            btn = ctk.CTkButton(
                tabs, text=f"{label}{suffix}", width=96, height=34,
                fg_color=COLORS["surface_3"] if enabled else COLORS["surface_2"],
                hover_color=COLORS["surface_hover"],
                text_color=COLORS["text"] if enabled else COLORS["muted"],
                corner_radius=RADIUS["control"], font=FONTS["helper"],
                state="normal" if enabled else "disabled",
                command=lambda k=key: self._switch_tab(k),
            )
            btn.grid(row=0, column=idx, padx=(0, SPACE["sm"]))
            self._tab_buttons[key] = btn
            self._tab_enabled[key] = enabled

        # 顶部操作栏：待发送提供账号和批量回复，已回复提供筛选。
        bulk = ctk.CTkFrame(self, fg_color=COLORS["surface_2"],
                            corner_radius=RADIUS["control"])
        bulk.grid(row=2, column=0, sticky="ew", padx=SPACE["xl"],
                  pady=(0, SPACE["sm"]))
        bulk.grid_columnconfigure(0, weight=1)
        self._bulk_hint = ctk.CTkLabel(bulk, text="当前页批量操作",
                                       text_color=COLORS["muted"],
                                       font=FONTS["helper"], anchor="w")
        self._bulk_hint.grid(row=0, column=0, sticky="w", padx=SPACE["md"],
                             pady=SPACE["xs"])
        self._bulk_buttons: Dict[str, Any] = {}
        for col, (key, label, color, hover, text_color) in enumerate((
            ("submit", "一键提交审核", COLORS["primary"], COLORS["primary_hover"], "#ffffff"),
            ("approve", "一键批准", COLORS["success"], "#3ddb9b", "#06130d"),
            ("reject", "一键拒绝", COLORS["surface_3"], COLORS["danger_bg"], COLORS["danger"]),
        ), start=1):
            button = ctk.CTkButton(
                bulk, text=label, width=112, height=30,
                fg_color=color, hover_color=hover, text_color=text_color,
                corner_radius=RADIUS["control"], font=FONTS["helper"],
                command=lambda action=key: self._bulk_action(action),
            )
            button.grid(row=0, column=col, padx=(0, SPACE["xs"]), pady=SPACE["xs"])
            self._bulk_buttons[key] = button
        self._send_account_menu = ctk.CTkOptionMenu(
            bulk, variable=self._send_account_var, values=["正在加载账号…"],
            width=190, height=30, fg_color=COLORS["window"],
            button_color=COLORS["surface_3"], button_hover_color=COLORS["surface_hover"],
            dropdown_fg_color=COLORS["surface_2"], dropdown_hover_color=COLORS["surface_hover"],
            text_color=COLORS["text"], font=FONTS["helper"], dropdown_font=FONTS["helper"],
            command=lambda _value: self._update_send_button_state(),
        )
        self._send_account_menu.grid(row=0, column=1, padx=(0, SPACE["xs"]), pady=SPACE["xs"])
        self._sent_platform_menu = ctk.CTkOptionMenu(
            bulk, variable=self._sent_platform_var,
            values=["全部平台", "抖音", "小红书", "B站", "微博"],
            width=140, height=30, fg_color=COLORS["window"],
            button_color=COLORS["surface_3"], button_hover_color=COLORS["surface_hover"],
            dropdown_fg_color=COLORS["surface_2"], dropdown_hover_color=COLORS["surface_hover"],
            text_color=COLORS["text"], font=FONTS["helper"], dropdown_font=FONTS["helper"],
            command=lambda _value: self.refresh(),
        )
        self._sent_platform_menu.grid(row=0, column=1, padx=(0, SPACE["xs"]), pady=SPACE["xs"])
        self._sent_account_filter_menu = ctk.CTkOptionMenu(
            bulk, variable=self._sent_account_var, values=["全部账号"],
            width=220, height=30, fg_color=COLORS["window"],
            button_color=COLORS["surface_3"], button_hover_color=COLORS["surface_hover"],
            dropdown_fg_color=COLORS["surface_2"], dropdown_hover_color=COLORS["surface_hover"],
            text_color=COLORS["text"], font=FONTS["helper"], dropdown_font=FONTS["helper"],
            command=lambda _value: self.refresh(),
        )
        self._sent_account_filter_menu.grid(row=0, column=2, padx=(0, SPACE["xs"]), pady=SPACE["xs"])
        self._bulk_buttons["send"] = ctk.CTkButton(
            bulk, text="一键回复", width=132, height=30,
            fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
            text_color="#ffffff", corner_radius=RADIUS["control"], font=FONTS["helper"],
            command=self._send_queued,
        )
        self._bulk_buttons["send"].grid(row=0, column=2, padx=(0, SPACE["xs"]), pady=SPACE["xs"])
        self._update_bulk_actions()

        # 卡片区
        self._body = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color=COLORS["surface_3"])
        self._body.grid(row=3, column=0, sticky="nsew", padx=SPACE["xl"],
                        pady=(0, SPACE["lg"]))
        self._body.grid_columnconfigure(0, weight=1)

    # ------------------------------------------------------------------
    def _switch_tab(self, key: str):
        if not self._tab_enabled.get(key, False):
            return
        self._tab = key
        if key != "queued":
            self._selected_draft_ids.clear()
        for k, btn in self._tab_buttons.items():
            active = k == key
            btn.configure(
                fg_color=COLORS["primary_soft"] if active else COLORS["surface_3"],
                text_color=COLORS["text"] if active else COLORS["muted"],
            )
        self._update_bulk_actions()
        self.refresh()

    def real_send_enabled(self) -> bool:
        return bool(self._real_send_var.get())

    def _toggle_real_send(self):
        """真实发送为会话级高风险开关，开启时只确认一次。"""
        enabled = self.real_send_enabled()
        if enabled:
            confirmed = ask_centered_confirm(
                self.winfo_toplevel(),
                "开启真实发送",
                "开启后，点击“发送”或“一键回复”会在填入成功后自动点击平台最终发送按钮。\n\n是否确认开启？",
                danger=True,
            )
            if not confirmed:
                self._real_send_var.set(False)
                enabled = False
        self._subtitle.configure(
            text=("警告：真实发送已开启 · 回复填入后将自动点击最终发送按钮"
                  if enabled else "完整回复内容与人工审核 · 当前仅填入，不点击发送"),
            text_color=COLORS["danger"] if enabled else COLORS["muted"],
        )
        if self._on_real_send_change is not None:
            self._on_real_send_change(enabled)
        self._update_bulk_actions()

    def _update_bulk_actions(self):
        """按四个状态页签显示对应的顶部操作。"""
        for button in self._bulk_buttons.values():
            button.grid_remove()
        self._send_account_menu.grid_remove()
        self._sent_platform_menu.grid_remove()
        self._sent_account_filter_menu.grid_remove()
        if self._tab == DraftStatus.DRAFT:
            self._bulk_hint.configure(text="待生成：先检查并修改完整回复内容，再进入发送页")
        elif self._tab == DraftStatus.QUEUED:
            self._send_account_menu.grid(row=0, column=1,
                                          padx=(0, SPACE["xs"]), pady=SPACE["xs"])
            self._bulk_buttons["send"].grid(row=0, column=2,
                                             padx=(0, SPACE["xs"]), pady=SPACE["xs"])
            self._bulk_buttons["send"].configure(state="disabled")
            self._bulk_hint.configure(
                text=("真实发送已开启：填入成功后自动点击最终发送按钮"
                      if self.real_send_enabled()
                      else "勾选待发送内容后选择账号；当前仅填入，不点击发送")
            )
            self._refresh_bulk_account_menu()
            self._load_reply_accounts_async(include_unavailable=False)
        elif self._tab == "sent":
            self._sent_platform_menu.grid(row=0, column=1,
                                           padx=(0, SPACE["xs"]), pady=SPACE["xs"])
            self._sent_account_filter_menu.grid(row=0, column=2,
                                                 padx=(0, SPACE["xs"]), pady=SPACE["xs"])
            self._bulk_hint.configure(text="已回复：按平台和回复账号筛选")
            self._load_reply_accounts_async(include_unavailable=True)
        elif self._tab == "failed":
            self._bulk_hint.configure(text="失败：保留失败记录，便于定位原因")

    def _load_reply_accounts_async(self, include_unavailable: bool = False):
        def worker():
            try:
                accounts = self._presenter.list_reply_accounts(include_unavailable)
                error = None
            except Exception as exc:  # noqa: BLE001
                accounts, error = [], exc
            try:
                self.after(0, self._apply_reply_accounts, accounts, error)
            except Exception:
                pass

        threading.Thread(target=worker, name="interaction-accounts", daemon=True).start()

    def _apply_reply_accounts(self, accounts, error=None):
        if error is not None:
            log.warning("加载回复账号失败: %s", error)
            self._account_map = {}
            self._account_maps_by_platform = {}
            self._bulk_account_map = {}
            self._filter_account_map = {}
            self._send_account_var.set("账号加载失败")
            self._send_account_menu.configure(values=["账号加载失败"], state="disabled")
            self._bulk_buttons["send"].configure(state="disabled")
            return
        self._account_records = list(accounts or [])
        self._account_map = {}
        self._account_maps_by_platform = {}
        self._filter_account_map = {}
        filter_labels = ["全部账号"]
        for account in accounts:
            platform_key = str(account.get("platform") or "")
            platform = PLATFORM_LABELS.get(platform_key, "未知平台")
            label = f"{platform} · {account.get('name') or '未命名账号'}"
            account_id = int(account["id"])
            self._filter_account_map[label] = account_id
            filter_labels.append(label)
            if account.get("status") not in ("waiting_human", "dead", "frozen"):
                self._account_map[label] = account_id
                self._account_maps_by_platform.setdefault(platform_key, {})[label] = account_id
        self._refresh_bulk_account_menu()
        self._sent_account_filter_menu.configure(values=filter_labels)
        if self._sent_account_var.get() not in filter_labels:
            self._sent_account_var.set("全部账号")
        if self._tab == "queued":
            # 账号清单变化后，待发送卡片的下拉框也必须重建。
            self._rendered_signature = None
            self.refresh()

    @staticmethod
    def _platform_key(value: str) -> str:
        text = str(value or "")
        return PLATFORM_VALUES.get(text, text)

    def _account_map_for_platform(self, platform: str) -> Dict[str, int]:
        return self._account_maps_by_platform.get(self._platform_key(platform), {})

    def _selected_queued_cards(self) -> List[Dict[str, Any]]:
        return [
            card for card in self._cards
            if int(card["draft_id"]) in self._selected_draft_ids
        ]

    def _refresh_bulk_account_menu(self):
        """批量账号下拉只展示当前勾选内容所属平台的账号。"""
        if not hasattr(self, "_send_account_menu"):
            return
        selected = self._selected_queued_cards()
        platforms = {self._platform_key(card.get("platform")) for card in selected}
        previous = self._send_account_var.get()
        self._bulk_account_map = {}
        if not selected:
            values = ["请先选择内容"]
            state = "disabled"
        elif len(platforms) != 1:
            values = ["请按平台分别选择"]
            state = "disabled"
        else:
            platform_key = next(iter(platforms))
            self._bulk_account_map = dict(
                self._account_maps_by_platform.get(platform_key, {})
            )
            if self._bulk_account_map:
                values = list(self._bulk_account_map)
                state = "normal"
            else:
                platform_name = PLATFORM_LABELS.get(platform_key, "该平台")
                values = [f"暂无可用{platform_name}账号"]
                state = "disabled"
        self._send_account_menu.configure(values=values, state=state)
        self._send_account_var.set(previous if previous in values else values[0])
        self._update_send_button_state()

    # ------------------------------------------------------------------
    def refresh(self):
        """后台查库，主线程按小批次创建卡片，避免页签切换卡住。"""
        if not self._visible:
            return
        self._refresh_request += 1
        if self._refresh_inflight:
            return
        self._refresh_inflight = True
        request = self._refresh_request
        tab = self._tab
        platform = None
        account_id = None
        if tab == "sent":
            platform = {
                "全部平台": None, "抖音": "douyin", "小红书": "xhs",
                "B站": "bilibili", "微博": "weibo", "快手": "kuaishou",
            }.get(self._sent_platform_var.get())
            if self._sent_account_var.get() != "全部账号":
                account_id = self._filter_account_map.get(self._sent_account_var.get())

        def worker():
            data = None
            cards = []
            error = None
            try:
                data = self._presenter.list_drafts(
                    tab, platform=platform, account_id=account_id,
                )
                cards = [self._presenter.to_card(item)
                         for item in data.get("items", [])]
            except Exception as exc:  # noqa: BLE001
                error = exc
            try:
                self.after(0, self._apply_refresh, request, cards, error)
            except Exception:
                self._refresh_inflight = False

        threading.Thread(target=worker, name="interaction-page-refresh", daemon=True).start()

    def _apply_refresh(self, request, cards, error=None):
        self._refresh_inflight = False
        if request != self._refresh_request:
            if self._visible:
                self.after_idle(self.refresh)
            return
        # 页面切走后不销毁/重建卡片，避免隐藏页面占用 Tk 主线程。
        if not self._visible:
            self._cards = list(cards or [])
            return
        next_cards = list(cards or [])
        next_signature = tuple(
            (card.get("draft_id"), card.get("status"), card.get("content"),
             card.get("reply_account_id"), card.get("template_id"),
             card.get("original_comment"), card.get("failure_reason"))
            for card in next_cards
        )
        # 数据未变化时保留输入框、下拉框和卡片控件，切回不再整块闪烁。
        if error is None and next_signature == self._rendered_signature:
            self._cards = next_cards
            return
        if self._render_after is not None:
            try:
                self.after_cancel(self._render_after)
            except Exception:
                pass
            self._render_after = None
        for child in self._body.winfo_children():
            child.destroy()
        self._render_request += 1
        render_request = self._render_request
        self._cards = next_cards
        self._pending_render_signature = next_signature
        self._entries = {}
        self._template_maps = {}
        visible_ids = {int(card["draft_id"]) for card in self._cards}
        self._selected_draft_ids.intersection_update(visible_ids)
        self._selection_vars = {}
        self._refresh_bulk_account_menu()
        if error is not None:
            log.warning("刷新互动中心失败: %s", error)
            ctk.CTkLabel(self._body, text=f"加载失败: {error}",
                         text_color=COLORS["danger"],
                         font=FONTS["body"]).grid(row=0, column=0, pady=40)
            return
        if not self._cards:
            ctk.CTkLabel(self._body, text="当前页签暂无内容",
                         text_color=COLORS["muted"],
                         font=FONTS["body"]).grid(row=0, column=0, pady=40)
            self._rendered_signature = next_signature
            return
        self._render_cursor = 0
        self._render_cards_in_batches(render_request)

    def _render_cards_in_batches(self, render_request):
        self._render_after = None
        if not self._visible:
            return
        if render_request != self._render_request:
            return
        # 每轮只创建少量卡片，把 Tk 事件循环还给窗口拖拽和页签点击。
        end = min(len(self._cards), self._render_cursor + 2)
        while self._render_cursor < end:
            self._render_card(self._cards[self._render_cursor], self._render_cursor)
            self._render_cursor += 1
        if self._render_cursor < len(self._cards):
            self._render_after = self.after_idle(
                self._render_cards_in_batches, render_request
            )
        else:
            self._rendered_signature = self._pending_render_signature

    # ------------------------------------------------------------------
    def _render_card(self, card: Dict[str, Any], idx: Optional[int] = None):
        idx = len(self._cards) - 1 if idx is None else idx
        box = ctk.CTkFrame(self._body, fg_color=COLORS["surface_2"],
                           corner_radius=RADIUS["card"],
                           border_width=1, border_color=COLORS["border"])
        box.grid(row=idx, column=0, sticky="ew", padx=2, pady=(2, SPACE["sm"]))
        box.grid_columnconfigure(1, weight=1)

        status = card["status"]

        # 顶部：待发送提供多选，其余页签显示用户和状态
        head = ctk.CTkFrame(box, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, sticky="ew",
                  padx=SPACE["lg"], pady=(SPACE["md"], 0))
        head.grid_columnconfigure(0, weight=1)
        if status == DraftStatus.QUEUED:
            select_var = tk.BooleanVar(value=int(card["draft_id"]) in self._selected_draft_ids)
            self._selection_vars[int(card["draft_id"])] = select_var
            head.grid_columnconfigure(1, weight=1)
            ctk.CTkCheckBox(
                head, text="选择", variable=select_var, width=68, height=28,
                fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                border_color=COLORS["border"], text_color=COLORS["text_2"],
                font=FONTS["helper"], command=lambda d=card["draft_id"], v=select_var:
                    self._toggle_selected(d, v),
            ).grid(row=0, column=0, sticky="w", padx=(0, SPACE["sm"]))
            ctk.CTkLabel(head, text=card["nickname"], text_color=COLORS["text"],
                         font=FONTS["card_title"], anchor="w").grid(row=0, column=1, sticky="w")
            DraftStatusBadge(head, status).grid(row=0, column=2, sticky="e")
        else:
            ctk.CTkLabel(head, text=card["nickname"], text_color=COLORS["text"],
                         font=FONTS["card_title"], anchor="w").grid(row=0, column=0, sticky="w")
            DraftStatusBadge(head, status).grid(row=0, column=1, sticky="e")

        # 元信息行
        meta = ctk.CTkFrame(box, fg_color="transparent")
        meta.grid(row=1, column=0, columnspan=2, sticky="ew",
                  padx=SPACE["lg"], pady=(SPACE["xs"], 0))
        PlatformBadge(meta, card["platform"], compact=True).pack(
            side="left", padx=(0, SPACE["xs"])
        )
        ctk.CTkLabel(meta, text=f"{card['platform']} · {card['region']}",
                     text_color=COLORS["muted"], font=FONTS["helper"],
                     anchor="w").pack(side="left")
        PoolBadge(meta, card["pool"]).pack(side="left", padx=SPACE["sm"])
        ctk.CTkLabel(meta, text=f"评论时间：{card['comment_time']}",
                     text_color=COLORS["muted"], font=FONTS["helper"],
                     anchor="w").pack(side="left", padx=SPACE["sm"])
        IntentBadge(meta, card["intent"]).pack(side="left", padx=SPACE["sm"])

        # 原评论必须与回复草稿同时可见，审核时不能只看生成后的话术。
        ctk.CTkLabel(box, text="评论原文", text_color=COLORS["muted"],
                     font=FONTS["helper"], anchor="w").grid(
            row=2, column=0, columnspan=2, sticky="w",
            padx=SPACE["lg"], pady=(SPACE["md"], 2))
        original_box = ctk.CTkFrame(
            box, fg_color=COLORS["surface"], corner_radius=RADIUS["small"],
        )
        original_box.grid(
            row=3, column=0, columnspan=2, sticky="ew",
            padx=SPACE["lg"], pady=(0, SPACE["xs"]),
        )
        ctk.CTkLabel(
            original_box, text=card.get("original_comment") or "暂无评论原文",
            text_color=COLORS["text_2"], font=FONTS["body"],
            anchor="w", justify="left", wraplength=920,
        ).pack(fill="x", padx=SPACE["md"], pady=SPACE["sm"])

        if status == DraftStatus.FAILED:
            ctk.CTkLabel(box, text="失败原因", text_color=COLORS["danger"],
                         font=FONTS["helper"], anchor="w").grid(
                row=4, column=0, columnspan=2, sticky="w",
                padx=SPACE["lg"], pady=(SPACE["sm"], 2))
            reason_box = ctk.CTkFrame(
                box, fg_color=COLORS["danger_bg"], corner_radius=RADIUS["small"],
            )
            reason_box.grid(
                row=5, column=0, columnspan=2, sticky="ew",
                padx=SPACE["lg"], pady=(0, SPACE["xs"]),
            )
            ctk.CTkLabel(
                reason_box,
                text=card.get("failure_reason") or "未记录具体失败原因",
                text_color=COLORS["danger"], font=FONTS["body"],
                anchor="w", justify="left", wraplength=920,
            ).pack(fill="x", padx=SPACE["md"], pady=SPACE["sm"])

        # 下拉框直接显示完整话术，不再只显示“问候话术/价格咨询”等名称。
        ctk.CTkLabel(box, text="完整回复内容（可编辑）", text_color=COLORS["primary"],
                     font=FONTS["helper"], anchor="w").grid(
            row=6, column=0, columnspan=2, sticky="w", padx=SPACE["lg"], pady=(SPACE["md"], 2))
        template_row = ctk.CTkFrame(box, fg_color="transparent")
        template_row.grid(row=7, column=0, columnspan=2, sticky="ew",
                          padx=SPACE["lg"], pady=(0, SPACE["xs"]))
        ctk.CTkLabel(template_row, text="回复话术", text_color=COLORS["muted"],
                     font=FONTS["helper"], anchor="w").pack(side="left", padx=(0, SPACE["sm"]))
        templates = self._presenter.list_templates()
        template_map = {
            " ".join(str(content or "").splitlines()).strip(): tid
            for tid, content in templates.items()
            if str(content or "").strip()
        }
        self._template_maps[card["draft_id"]] = template_map
        template_values = list(template_map) or ["暂无预制话术"]
        selected_template = ""
        if card.get("template_id") in templates:
            selected_template = " ".join(
                str(templates[card["template_id"]] or "").splitlines()
            ).strip()
        if selected_template not in template_values:
            selected_template = template_values[0]
        template_var = ctk.StringVar(value=selected_template)
        ctk.CTkOptionMenu(
            template_row, variable=template_var, values=template_values,
            width=520, height=30, fg_color=COLORS["window"],
            button_color=COLORS["surface_3"], button_hover_color=COLORS["surface_hover"],
            dropdown_fg_color=COLORS["surface_2"], dropdown_hover_color=COLORS["surface_hover"],
            text_color=COLORS["text"], font=FONTS["helper"], dropdown_font=FONTS["helper"],
            command=lambda label, d=card, tv=template_var: self._apply_template(label, d, tv),
        ).pack(side="left")

        entry = ctk.CTkTextbox(
            box, height=120, fg_color=COLORS["window"], border_color=COLORS["border"],
            border_width=1, text_color=COLORS["text_2"], font=FONTS["body"],
            wrap="word",
        )
        entry.grid(row=8, column=0, columnspan=2, sticky="ew",
                   padx=SPACE["lg"], pady=(0, SPACE["sm"]))
        entry.insert("1.0", card["content"])
        self._entries[card["draft_id"]] = entry

        # 操作按钮（按四个状态显示）
        ops = ctk.CTkFrame(box, fg_color="transparent")
        ops.grid(row=9, column=0, columnspan=2, sticky="ew",
                 padx=SPACE["lg"], pady=(0, SPACE["md"]))
        if status == DraftStatus.DRAFT:
            ctk.CTkButton(ops, text="进入发送页", width=108, height=32,
                          fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                          text_color="#ffffff", corner_radius=RADIUS["control"],
                          command=lambda d=card, e=entry: self._enter_send_page(d, e)
                          ).pack(side="left", padx=(0, SPACE["sm"]))
            self._delete_button(ops, card["draft_id"])
        elif status == DraftStatus.QUEUED:
            account_var = ctk.StringVar(value="选择账号")
            platform_accounts = self._account_map_for_platform(card.get("platform"))
            labels = list(platform_accounts)
            if not labels:
                labels = [f"暂无可用{card.get('platform') or '该平台'}账号"]
            current_label = next(
                (label for label, aid in platform_accounts.items()
                 if aid == card.get("reply_account_id")),
                "选择账号",
            )
            if current_label in labels:
                account_var.set(current_label)
            menu = ctk.CTkOptionMenu(
                ops, variable=account_var, values=labels, width=250, height=32,
                fg_color=COLORS["window"], button_color=COLORS["surface_3"],
                button_hover_color=COLORS["surface_hover"],
                dropdown_fg_color=COLORS["surface_2"],
                dropdown_hover_color=COLORS["surface_hover"],
                text_color=COLORS["text"], font=FONTS["helper"],
                dropdown_font=FONTS["helper"],
                state="normal" if platform_accounts else "disabled",
            )
            menu.pack(side="left", padx=(0, SPACE["sm"]))
            ctk.CTkButton(
                ops, text="发送", width=76, height=32,
                fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                text_color="#ffffff", corner_radius=RADIUS["control"],
                state="normal" if platform_accounts else "disabled",
                command=lambda d=card, e=entry, v=account_var: self._send_single(d, e, v),
            ).pack(side="left", padx=(0, SPACE["sm"]))
            self._delete_button(ops, card["draft_id"])
        elif status == "sent":
            account_text = card.get("reply_account_name") or "未记录账号"
            ctk.CTkLabel(ops, text=f"回复账号：{account_text}",
                         text_color=COLORS["muted"], font=FONTS["helper"]).pack(side="left")
        elif status == "failed":
            ctk.CTkButton(
                ops, text="退回待生成", width=108, height=32,
                fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                text_color="#ffffff", corner_radius=RADIUS["control"],
                command=lambda d=card, e=entry: self._return_failed_to_draft(d, e),
            ).pack(side="left", padx=(0, SPACE["sm"]))
            self._delete_button(ops, card["draft_id"])

    def _apply_template(self, label: str, card: Dict[str, Any], _var=None):
        template_id = self._template_maps.get(card["draft_id"], {}).get(label)
        if not template_id:
            return
        entry = self._entries.get(card["draft_id"])
        if entry is None:
            return
        try:
            content = self._presenter.render_template(template_id, card["lead_id"])
            entry.delete("1.0", "end")
            entry.insert("1.0", content)
        except Exception as exc:  # noqa: BLE001
            log.warning("切换回复话术失败: %s", exc)

    @staticmethod
    def _entry_content(entry) -> str:
        return entry.get("1.0", "end").strip()

    def _toggle_selected(self, draft_id: int, value: tk.BooleanVar):
        draft_id = int(draft_id)
        if value.get():
            self._selected_draft_ids.add(draft_id)
        else:
            self._selected_draft_ids.discard(draft_id)
        self._refresh_bulk_account_menu()

    def _update_send_button_state(self):
        if self._tab != DraftStatus.QUEUED:
            return
        selected = self._selected_queued_cards()
        platforms = {self._platform_key(card.get("platform")) for card in selected}
        account_id = self._bulk_account_map.get(self._send_account_var.get())
        enabled = bool(
            selected and len(platforms) == 1
            and account_id is not None and self._on_batch_reply
        )
        self._bulk_buttons["send"].configure(state="normal" if enabled else "disabled")
        if not selected:
            hint = "请先勾选待发送内容"
        elif len(platforms) != 1:
            hint = "所选内容来自不同平台，请按平台分别批量回复"
        elif not self._bulk_account_map:
            platform_name = PLATFORM_LABELS.get(next(iter(platforms)), "该平台")
            hint = f"{platform_name}暂无可用回复账号"
        else:
            platform_name = PLATFORM_LABELS.get(next(iter(platforms)), "当前平台")
            mode = ("将真实发送" if self.real_send_enabled()
                    else "仅填入、不发送")
            hint = f"已选择 {len(selected)} 条{platform_name}内容；当前模式：{mode}"
        self._bulk_hint.configure(text=hint)

    def _delete_button(self, parent, draft_id: int):
        ctk.CTkButton(
            parent, text="删除", width=76, height=32,
            fg_color=COLORS["surface_3"], hover_color=COLORS["danger_bg"],
            text_color=COLORS["danger"], corner_radius=RADIUS["control"],
            command=lambda d=draft_id: self._delete_card(d),
        ).pack(side="left")

    def _delete_card(self, draft_id: int):
        try:
            self._presenter.delete(draft_id)
            self._selected_draft_ids.discard(int(draft_id))
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            log.warning("删除互动内容失败: %s", exc)

    def _return_failed_to_draft(self, card: Dict[str, Any], entry):
        try:
            content = self._entry_content(entry)
            if content:
                self._presenter.update_content(card["draft_id"], content)
            self._presenter.return_failed_to_draft(card["draft_id"])
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            log.warning("退回待生成失败: %s", exc)

    def _enter_send_page(self, card: Dict[str, Any], entry):
        try:
            self._presenter.enter_send_page(
                card["draft_id"], self._entry_content(entry)
            )
            self._switch_tab(DraftStatus.QUEUED)
        except Exception as exc:  # noqa: BLE001
            log.warning("进入发送页失败: %s", exc)

    def _send_single(self, card: Dict[str, Any], entry, account_var):
        account_id = self._account_map_for_platform(card.get("platform")).get(
            account_var.get()
        )
        if account_id is None:
            log.warning("发送失败：请先选择回复账号")
            return
        try:
            self._presenter.update_content(card["draft_id"], self._entry_content(entry))
            self._presenter.assign_reply_account(card["draft_id"], account_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("保存发送账号失败: %s", exc)
            return
        if self._on_batch_reply is None:
            log.warning("当前未配置浏览器回复适配器")
            return
        self._on_batch_reply(
            [card["draft_id"]], account_id, self.real_send_enabled()
        )

    def _send_queued(self):
        account_id = self._bulk_account_map.get(self._send_account_var.get())
        draft_ids = [card["draft_id"] for card in self._cards
                     if int(card["draft_id"]) in self._selected_draft_ids]
        if not draft_ids:
            self._bulk_hint.configure(text="请先勾选待发送内容")
            return
        if account_id is None:
            self._bulk_hint.configure(text="请先选择回复账号")
            return
        if self._on_batch_reply is None:
            self._bulk_hint.configure(text="当前未配置浏览器模拟回复适配器")
            return
        try:
            for draft_id in draft_ids:
                entry = self._entries.get(draft_id)
                if entry is not None:
                    self._presenter.update_content(draft_id, self._entry_content(entry))
                self._presenter.assign_reply_account(draft_id, account_id)
        except Exception as exc:  # noqa: BLE001
            self._bulk_hint.configure(text=f"保存发送内容失败：{exc}")
            return
        self._bulk_buttons["send"].configure(state="disabled")
        real_send = self.real_send_enabled()
        self._bulk_hint.configure(
            text=(f"已开始真实发送，共 {len(draft_ids)} 条"
                  if real_send else f"已开始模拟回复，共 {len(draft_ids)} 条；仅填入不发送")
        )
        self._on_batch_reply(draft_ids, account_id, real_send)

    def _submit_card(self, card: Dict[str, Any], entry):
        try:
            self._presenter.submit_with_content(card["draft_id"], self._entry_content(entry))
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            log.warning("提交审核失败: %s", exc)

    def _bulk_action(self, action: str):
        """批量处理当前页，逐条隔离错误并在完成后刷新。"""
        success = 0
        failed = 0
        for card in list(self._cards):
            entry = self._entries.get(card["draft_id"])
            content = self._entry_content(entry) if entry is not None else card["content"]
            try:
                if action == "approve":
                    self._presenter.approve(card["draft_id"], content)
                elif action == "reject":
                    self._presenter.reject(card["draft_id"], content)
                elif action == "submit":
                    self._presenter.submit_with_content(card["draft_id"], content)
                success += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.warning("批量处理草稿 %s 失败: %s", card["draft_id"], exc)
        self._bulk_hint.configure(text=f"本次处理成功 {success} 条，失败 {failed} 条")
        self.refresh()

    # ------------------------------------------------------------------
    def _approve(self, card: Dict[str, Any], entry):
        try:
            self._presenter.approve(card["draft_id"], entry.get("1.0", "end").strip())
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            log.warning("批准失败: %s", exc)

    def _reject(self, card: Dict[str, Any], entry):
        try:
            self._presenter.reject(card["draft_id"], entry.get("1.0", "end").strip())
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            log.warning("拒绝失败: %s", exc)

    def _suppress(self, card: Dict[str, Any]):
        try:
            self._presenter.suppress(card["lead_id"], "用户在互动中心标记勿扰")
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            log.warning("加入勿扰失败: %s", exc)

    # ------------------------------------------------------------------
    def on_show(self):
        """标记页面可渲染；切换页签时不再自动查库重绘。"""
        self._visible = True

    def on_hide(self):
        self._visible = False
        self._refresh_request += 1
        self._render_request += 1
        if self._render_after is not None:
            try:
                self.after_cancel(self._render_after)
            except Exception:
                pass
            self._render_after = None
