# -*- coding: utf-8 -*-
"""设置页：BitBrowser 连接诊断和后续 AI/API 配置。"""

from __future__ import annotations

import threading
import tkinter as tk
import os
from typing import Callable

import customtkinter as ctk

from ..theme import COLORS, FONTS, RADIUS, SPACE
from ..widgets import PlatformBadge
from operation_log import OperationLog


_HEALTH_CHECK_LABELS = {
    "page_connected": "页面连接",
    "domain_match": "平台地址匹配",
    "platform_home": "平台首页",
    "login_required": "登录状态",
    "login_entry": "登录入口",
    "captcha": "验证码",
    "search_input": "搜索输入框",
    "search_surface": "搜索区域",
    "comment_area": "评论区",
    "reply_button": "回复按钮",
    "reply_input": "回复输入框",
    "scroll": "滚轮响应",
    "sample_available": "测试数据",
    "target_page": "目标作品",
    "comment_target": "目标评论",
    "reply_filled": "回复框填充",
    "send_executed": "发送操作",
}
_HEALTH_STATUS_LABELS = {
    "ok": "正常", "warning": "需检查", "human_required": "需人工",
    "login_required": "需登录", "failed": "失败", "no_data": "暂无数据",
    "no_account": "未绑定账号", "unknown": "未知",
}
_HEALTH_VALUE_LABELS = {
    "ok": "正常", "not_scrollable": "无足够滚动空间", "locked": "滚轮被拦截",
    "unknown": "未知", "advanced": "已滚动", "at_end_or_not_scrollable": "已到末尾或不可滚动",
}


def _health_check_text(key, value):
    """把诊断内部字段转换为面向用户的中文描述。"""
    label = _HEALTH_CHECK_LABELS.get(str(key), str(key))
    if isinstance(value, bool):
        display = "是" if value else "否"
    else:
        display = _HEALTH_VALUE_LABELS.get(str(value), _HEALTH_STATUS_LABELS.get(str(value), str(value)))
    return f"{label}：{display}"


class SettingsPage(ctk.CTkFrame):
    def __init__(self, master, config, *, on_save: Callable | None = None,
                 inspect_bitbrowser: Callable | None = None,
                 inspect_platform_health: Callable | None = None,
                 inspect_live_platform_health: Callable | None = None,
                 inspect_deep_platform_health: Callable | None = None,
                 inspect_manual_deep_platform_health: Callable | None = None,
                 export_diagnostic_package: Callable | None = None,
                 test_llm_api: Callable | None = None,
                 get_llm_sample: Callable | None = None,
                 analyze_llm_comment: Callable | None = None,
                 generate_llm_reply: Callable | None = None,
                 analyze_llm_batch: Callable | None = None,
                 get_operation_logs: Callable | None = None,
                 export_operation_log: Callable | None = None):
        super().__init__(master, fg_color=COLORS["window"])
        self._config = config
        self._on_save = on_save
        self._inspect_bitbrowser = inspect_bitbrowser
        self._inspect_platform_health = inspect_platform_health
        self._inspect_live_platform_health = inspect_live_platform_health
        self._inspect_deep_platform_health = inspect_deep_platform_health
        self._inspect_manual_deep_platform_health = inspect_manual_deep_platform_health
        self._export_diagnostic_package_callback = export_diagnostic_package
        self._test_llm_api_callback = test_llm_api
        self._get_llm_sample_callback = get_llm_sample
        self._analyze_llm_comment_callback = analyze_llm_comment
        self._generate_llm_reply_callback = generate_llm_reply
        self._analyze_llm_batch_callback = analyze_llm_batch
        self._get_operation_logs_callback = get_operation_logs
        self._export_operation_log_callback = export_operation_log
        self._api_sample = {}
        self._sample_meta = None
        self._sample_content = None
        self._api_test_output = None
        self._api_batch_progress = None
        self._api_batch_progress_label = None
        self._last_live_health_result = None
        self._port_rows = None
        self._check_rows = None
        self._platform_rows = None
        self._platform_status = None
        self._operation_log_text = None
        self._operation_log_status = None
        self._operation_log_auto_scroll = tk.BooleanVar(value=True)
        self._build()
        self.reload()

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=SPACE["xl"], pady=(SPACE["lg"], SPACE["md"]))
        ctk.CTkLabel(header, text="设置", text_color=COLORS["text"],
                     font=FONTS["page_title"], anchor="w").pack(anchor="w")
        ctk.CTkLabel(header, text="连接诊断、端口识别和后续智能能力配置",
                     text_color=COLORS["muted"], font=FONTS["page_subtitle"],
                     anchor="w").pack(anchor="w", pady=(2, 0))

        body = ctk.CTkScrollableFrame(self, fg_color="transparent",
                                      scrollbar_button_color=COLORS["surface_3"])
        body.grid(row=1, column=0, sticky="nsew", padx=SPACE["xl"], pady=(0, SPACE["lg"]))
        body.grid_columnconfigure(0, weight=1)

        bit_card = ctk.CTkFrame(body, fg_color=COLORS["surface_2"],
                                corner_radius=RADIUS["card"], border_width=1,
                                border_color=COLORS["border"])
        bit_card.grid(row=0, column=0, sticky="ew", pady=(0, SPACE["md"]))
        bit_card.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(bit_card, text="BitBrowser 连接", text_color=COLORS["text"],
                     font=FONTS["section_title"]).grid(row=0, column=0, columnspan=3,
                                                       sticky="w", padx=SPACE["lg"], pady=(SPACE["md"], 2))
        ctk.CTkLabel(bit_card, text="首次使用先检测本地服务；识别结果只读，不会自动打开或关闭窗口。",
                     text_color=COLORS["muted"], font=FONTS["helper"]).grid(
                         row=1, column=0, columnspan=3, sticky="w", padx=SPACE["lg"], pady=(0, SPACE["sm"]))
        ctk.CTkLabel(bit_card, text="本地 API 地址", text_color=COLORS["text_2"],
                     font=FONTS["helper"]).grid(row=2, column=0, sticky="w", padx=(SPACE["lg"], SPACE["sm"]), pady=SPACE["sm"])
        self._bb_url = ctk.CTkEntry(bit_card, height=36, fg_color=COLORS["window"],
                                    border_color=COLORS["border"], font=FONTS["body"])
        self._bb_url.grid(row=2, column=1, sticky="ew", padx=SPACE["sm"], pady=SPACE["sm"])
        self._bb_timeout = ctk.CTkEntry(bit_card, width=90, height=36, fg_color=COLORS["window"],
                                        border_color=COLORS["border"], font=FONTS["body"])
        self._bb_timeout.grid(row=2, column=2, sticky="w", padx=(SPACE["sm"], SPACE["lg"]), pady=SPACE["sm"])
        ctk.CTkLabel(bit_card, text="超时秒数", text_color=COLORS["muted"],
                     font=FONTS["helper"]).grid(row=3, column=2, sticky="w", padx=(SPACE["sm"], SPACE["lg"]), pady=(0, SPACE["sm"]))
        self._bb_status = ctk.CTkLabel(bit_card, text="尚未检测", text_color=COLORS["muted"],
                                       font=FONTS["helper"], anchor="w")
        self._bb_status.grid(row=3, column=0, columnspan=2, sticky="w", padx=SPACE["lg"], pady=(0, SPACE["sm"]))
        actions = ctk.CTkFrame(bit_card, fg_color="transparent")
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", padx=SPACE["lg"], pady=(0, SPACE["md"]))
        ctk.CTkButton(actions, text="保存连接设置", width=128, height=34,
                      fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                      font=FONTS["helper"], command=self._save_bitbrowser).pack(side="left")
        ctk.CTkButton(actions, text="检测并识别端口", width=140, height=34,
                      fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                      text_color=COLORS["text"], font=FONTS["helper"],
                      command=self._detect).pack(side="left", padx=SPACE["sm"])

        ctk.CTkLabel(bit_card, text="健康诊断", text_color=COLORS["text_2"],
                     font=FONTS["card_title"]).grid(row=5, column=0, columnspan=3,
                                                     sticky="w", padx=SPACE["lg"], pady=(0, SPACE["sm"]))
        self._check_rows = ctk.CTkFrame(bit_card, fg_color=COLORS["window"], corner_radius=RADIUS["small"])
        self._check_rows.grid(row=6, column=0, columnspan=3, sticky="ew", padx=SPACE["lg"], pady=(0, SPACE["sm"]))
        self._render_checks([])

        self._port_rows = ctk.CTkFrame(bit_card, fg_color=COLORS["window"], corner_radius=RADIUS["small"])
        self._port_rows.grid(row=7, column=0, columnspan=3, sticky="ew", padx=SPACE["lg"], pady=(0, SPACE["lg"]))
        self._port_rows.grid_columnconfigure(1, weight=1)
        self._render_ports([])

        health_card = ctk.CTkFrame(body, fg_color=COLORS["surface_2"],
                                   corner_radius=RADIUS["card"], border_width=1,
                                   border_color=COLORS["border"])
        health_card.grid(row=1, column=0, sticky="ew", pady=(0, SPACE["md"]))
        health_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(health_card, text="五平台健康诊断", text_color=COLORS["text"],
                     font=FONTS["section_title"]).grid(
                         row=0, column=0, sticky="w", padx=SPACE["lg"], pady=(SPACE["md"], 2))
        ctk.CTkLabel(health_card,
                     text="基础检查不打开浏览器；真实诊断检查平台首页。深度诊断使用已有或手动测试数据，填入测试内容后停止，不点击发送。",
                     text_color=COLORS["muted"], font=FONTS["helper"]).grid(
                         row=1, column=0, sticky="w", padx=SPACE["lg"], pady=(0, SPACE["sm"]))
        health_actions = ctk.CTkFrame(health_card, fg_color="transparent")
        health_actions.grid(row=2, column=0, sticky="ew", padx=SPACE["lg"], pady=(0, SPACE["sm"]))
        ctk.CTkButton(health_actions, text="运行五平台检查", width=140, height=34,
                      fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                      font=FONTS["helper"], command=self._check_platform_health).pack(side="left")
        ctk.CTkButton(health_actions, text="真实浏览器诊断", width=140, height=34,
                      fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                      text_color=COLORS["text"], font=FONTS["helper"],
                      command=self._check_live_platform_health).pack(side="left", padx=SPACE["sm"])
        ctk.CTkButton(health_actions, text="导出诊断包", width=112, height=34,
                      fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                      text_color=COLORS["text"], font=FONTS["helper"],
                      command=self._export_diagnostic_package).pack(side="left")
        ctk.CTkButton(health_actions, text="一键深度诊断", width=128, height=34,
                      fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                      font=FONTS["helper"],
                      command=self._check_deep_platform_health).pack(side="left", padx=(SPACE["sm"], 0))
        ctk.CTkButton(health_actions, text="手动测试", width=92, height=34,
                      fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                      text_color=COLORS["text"], font=FONTS["helper"],
                      command=self._open_manual_deep_dialog).pack(side="left", padx=(SPACE["sm"], 0))
        self._platform_status = ctk.CTkLabel(
            health_actions, text="尚未检查", text_color=COLORS["muted"],
            font=FONTS["helper"], anchor="w")
        self._platform_status.pack(side="left", padx=SPACE["md"])
        self._platform_rows = ctk.CTkFrame(health_card, fg_color=COLORS["window"],
                                           corner_radius=RADIUS["small"])
        self._platform_rows.grid(row=3, column=0, sticky="ew", padx=SPACE["lg"], pady=(0, SPACE["lg"]))
        self._render_platform_health([])

        api_card = ctk.CTkFrame(body, fg_color=COLORS["surface_2"],
                                corner_radius=RADIUS["card"], border_width=1,
                                border_color=COLORS["border"])
        api_card.grid(row=2, column=0, sticky="ew", pady=(0, SPACE["md"]))
        api_card.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(api_card, text="智能回复与评论分析 API", text_color=COLORS["text"],
                     font=FONTS["section_title"]).grid(row=0, column=0, columnspan=2,
                                                       sticky="w", padx=SPACE["lg"], pady=(SPACE["md"], 2))
        ctk.CTkLabel(api_card, text="仅保存连接信息，当前不会自动调用；后续接入回复生成和评论分析时使用。",
                     text_color=COLORS["muted"], font=FONTS["helper"]).grid(
                         row=1, column=0, columnspan=2, sticky="w", padx=SPACE["lg"], pady=(0, SPACE["sm"]))
        self._api_enabled = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(api_card, text="启用 API 配置（不代表立即调用）", variable=self._api_enabled,
                        fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                        text_color=COLORS["text_2"], font=FONTS["helper"]).grid(
                            row=2, column=0, columnspan=2, sticky="w", padx=SPACE["lg"], pady=(0, SPACE["sm"]))
        self._api_provider = self._field(api_card, 3, "服务商")
        self._api_base_url = self._field(api_card, 4, "API 地址")
        self._api_key = self._field(api_card, 5, "API Key", secret=True)
        self._api_model = self._field(api_card, 6, "模型名称")
        api_actions = ctk.CTkFrame(api_card, fg_color="transparent")
        api_actions.grid(row=7, column=0, columnspan=2, sticky="ew",
                         padx=SPACE["lg"], pady=(0, SPACE["sm"]))
        ctk.CTkButton(api_actions, text="保存 API 信息", width=128, height=34,
                      fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                      font=FONTS["helper"], command=self._save_api).pack(side="left")
        ctk.CTkButton(api_actions, text="检测 API 连通性", width=140, height=34,
                      fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                      text_color=COLORS["text"], font=FONTS["helper"],
                      command=self._test_api).pack(side="left", padx=SPACE["sm"])
        self._api_status = ctk.CTkLabel(api_actions, text="尚未检测",
                                        text_color=COLORS["muted"], font=FONTS["helper"], anchor="w")
        self._api_status.pack(side="left", padx=SPACE["sm"])
        ctk.CTkLabel(api_card, text="安全提示：API Key 只保存在本机 data/config/app_config.json，不会写入运行日志。",
                     text_color=COLORS["warning"], font=FONTS["helper"]).grid(
                         row=8, column=0, columnspan=2, sticky="w", padx=SPACE["lg"], pady=(0, SPACE["md"]))

        test_card = ctk.CTkFrame(body, fg_color=COLORS["surface_2"],
                                 corner_radius=RADIUS["card"], border_width=1,
                                 border_color=COLORS["border"])
        test_card.grid(row=3, column=0, sticky="ew", pady=(0, SPACE["md"]))
        test_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(test_card, text="API 功能测试", text_color=COLORS["text"],
                     font=FONTS["section_title"]).grid(
                         row=0, column=0, sticky="w", padx=SPACE["lg"], pady=(SPACE["md"], 2))
        ctk.CTkLabel(test_card,
                     text="随机读取一条本地评论，测试智能分析和完整回复草稿；只展示结果，不进入互动中心，不执行发送。",
                     text_color=COLORS["muted"], font=FONTS["helper"]).grid(
                         row=1, column=0, sticky="w", padx=SPACE["lg"], pady=(0, SPACE["sm"]))
        sample_actions = ctk.CTkFrame(test_card, fg_color="transparent")
        sample_actions.grid(row=2, column=0, sticky="ew", padx=SPACE["lg"], pady=(0, SPACE["xs"]))
        ctk.CTkButton(sample_actions, text="随机抽取评论", width=128, height=34,
                      fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                      text_color=COLORS["text"], font=FONTS["helper"],
                      command=self._load_api_sample).pack(side="left")
        self._sample_meta = ctk.CTkLabel(sample_actions, text="尚未抽取测试评论",
                                         text_color=COLORS["muted"], font=FONTS["helper"], anchor="w")
        self._sample_meta.pack(side="left", padx=SPACE["sm"])
        ctk.CTkLabel(test_card, text="评论原文（可编辑）", text_color=COLORS["text_2"],
                     font=FONTS["helper"]).grid(row=3, column=0, sticky="w",
                                                 padx=SPACE["lg"], pady=(SPACE["xs"], 2))
        self._sample_content = ctk.CTkTextbox(test_card, height=88, fg_color=COLORS["window"],
                                               border_width=1, border_color=COLORS["border"],
                                               font=FONTS["body"])
        self._sample_content.grid(row=4, column=0, sticky="ew", padx=SPACE["lg"], pady=(0, SPACE["sm"]))
        feature_actions = ctk.CTkFrame(test_card, fg_color="transparent")
        feature_actions.grid(row=5, column=0, sticky="ew", padx=SPACE["lg"], pady=(0, SPACE["sm"]))
        ctk.CTkButton(feature_actions, text="分析评论", width=112, height=34,
                      fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                      font=FONTS["helper"], command=lambda: self._run_api_feature("analyze")).pack(side="left")
        ctk.CTkButton(feature_actions, text="批量分析500条意向", width=158, height=34,
                      fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                      font=FONTS["helper"],
                      command=lambda: self._run_api_feature("batch")).pack(side="left", padx=SPACE["sm"])
        ctk.CTkButton(feature_actions, text="回复使用模板", width=128, height=34,
                      fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                      text_color=COLORS["muted"], font=FONTS["helper"], state="disabled").pack(
                          side="left", padx=SPACE["sm"])
        progress_row = ctk.CTkFrame(test_card, fg_color="transparent")
        progress_row.grid(row=6, column=0, sticky="ew", padx=SPACE["lg"], pady=(0, SPACE["sm"]))
        progress_row.grid_columnconfigure(0, weight=1)
        self._api_batch_progress = ctk.CTkProgressBar(
            progress_row, height=10, fg_color=COLORS["window"],
            progress_color=COLORS["primary"])
        self._api_batch_progress.grid(row=0, column=0, sticky="ew", padx=(0, SPACE["sm"]))
        self._api_batch_progress.set(0)
        self._api_batch_progress_label = ctk.CTkLabel(
            progress_row, text="批量分析尚未开始", width=170,
            text_color=COLORS["muted"], font=FONTS["helper"], anchor="e")
        self._api_batch_progress_label.grid(row=0, column=1, sticky="e")
        self._api_test_output = ctk.CTkTextbox(test_card, height=150, fg_color=COLORS["window"],
                                               border_width=1, border_color=COLORS["border"],
                                               font=FONTS["body"])
        self._api_test_output.grid(row=7, column=0, sticky="ew", padx=SPACE["lg"], pady=(0, SPACE["lg"]))
        self._api_test_output.insert("1.0", "测试结果会显示在这里。")
        self._api_test_output.configure(state="disabled")

        log_card = ctk.CTkFrame(body, fg_color=COLORS["surface_2"],
                                corner_radius=RADIUS["card"], border_width=1,
                                border_color=COLORS["border"])
        log_card.grid(row=4, column=0, sticky="ew", pady=(0, SPACE["md"]))
        log_card.grid_columnconfigure(0, weight=1)
        log_header = ctk.CTkFrame(log_card, fg_color="transparent")
        log_header.grid(row=0, column=0, sticky="ew", padx=SPACE["lg"],
                        pady=(SPACE["md"], 2))
        ctk.CTkLabel(log_header, text="后台操作日志", text_color=COLORS["text"],
                     font=FONTS["section_title"]).pack(side="left")
        ctk.CTkLabel(log_header, text="● 实时记录", text_color=COLORS["success"],
                     font=FONTS["helper"]).pack(side="left", padx=(SPACE["sm"], 0))
        ctk.CTkCheckBox(
            log_header, text="自动滚动", variable=self._operation_log_auto_scroll,
            fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
            text_color=COLORS["text_2"], font=FONTS["helper"],
        ).pack(side="right")
        log_actions = ctk.CTkFrame(log_card, fg_color="transparent")
        log_actions.grid(row=1, column=0, sticky="ew", padx=SPACE["lg"],
                         pady=(0, SPACE["sm"]))
        ctk.CTkButton(
            log_actions, text="刷新日志", width=96, height=32,
            fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
            text_color=COLORS["text"], font=FONTS["helper"],
            command=self._reload_operation_logs,
        ).pack(side="left")
        ctk.CTkButton(
            log_actions, text="导出日志", width=96, height=32,
            fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
            font=FONTS["helper"], command=self._export_operation_log,
        ).pack(side="left", padx=SPACE["sm"])
        self._operation_log_status = ctk.CTkLabel(
            log_actions, text="日志文件：data/logs/operation_events.jsonl",
            text_color=COLORS["muted"], font=FONTS["helper"], anchor="w",
        )
        self._operation_log_status.pack(side="left", padx=SPACE["sm"])
        ctk.CTkLabel(
            log_card,
            text="记录任务、账号、浏览器、诊断、采集、回复和异常反馈；敏感信息会按现有规则脱敏。",
            text_color=COLORS["muted"], font=FONTS["helper"], anchor="w",
        ).grid(row=2, column=0, sticky="ew", padx=SPACE["lg"], pady=(0, SPACE["sm"]))
        self._operation_log_text = ctk.CTkTextbox(
            log_card, height=260, fg_color=COLORS["log_bg"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["log_text"], font=FONTS["log"], wrap="word",
        )
        self._operation_log_text.grid(row=3, column=0, sticky="ew",
                                      padx=SPACE["lg"], pady=(0, SPACE["lg"]))
        self._operation_log_text.configure(state="disabled")

    @staticmethod
    def _field(parent, row, label, secret=False):
        ctk.CTkLabel(parent, text=label, text_color=COLORS["text_2"],
                     font=FONTS["helper"]).grid(row=row, column=0, sticky="w",
                                                padx=(SPACE["lg"], SPACE["sm"]), pady=SPACE["xs"])
        entry = ctk.CTkEntry(parent, height=34, fg_color=COLORS["window"],
                             border_color=COLORS["border"], font=FONTS["body"],
                             show="•" if secret else None)
        entry.grid(row=row, column=1, sticky="ew", padx=(SPACE["sm"], SPACE["lg"]), pady=SPACE["xs"])
        return entry

    @staticmethod
    def _set_entry(entry, value):
        entry.delete(0, "end")
        entry.insert(0, str(value or ""))

    def reload(self):
        bb = self._config.bitbrowser()
        api = self._config.llm_api()
        self._set_entry(self._bb_url, bb.get("base_url", "http://127.0.0.1:54345"))
        self._set_entry(self._bb_timeout, bb.get("timeout", 20))
        self._api_enabled.set(bool(api.get("enabled", False)))
        self._set_entry(self._api_provider, api.get("provider"))
        self._set_entry(self._api_base_url, api.get("base_url"))
        self._set_entry(self._api_key, api.get("api_key"))
        self._set_entry(self._api_model, api.get("model"))
        self._reload_operation_logs()

    def _reload_operation_logs(self):
        if self._operation_log_text is None:
            return
        records = []
        if callable(self._get_operation_logs_callback):
            try:
                records = self._get_operation_logs_callback(1000) or []
            except Exception as exc:  # noqa: BLE001
                records = [{"timestamp": "", "message": f"读取操作日志失败：{type(exc).__name__}: {exc}"}]
        self._operation_log_text.configure(state="normal")
        self._operation_log_text.delete("1.0", "end")
        for record in records:
            self._operation_log_text.insert("end", OperationLog.format_record(record) + "\n")
        if self._operation_log_auto_scroll.get():
            self._operation_log_text.see("end")
        self._operation_log_text.configure(state="disabled")
        if self._operation_log_status is not None:
            self._operation_log_status.configure(text=f"已载入 {len(records)} 条")

    def append_operation_log(self, record):
        """由主窗口实时推送一条新日志；仅在 UI 线程修改控件。"""
        self.append_operation_logs([record])

    def append_operation_logs(self, records):
        """批量追加实时日志，降低高频日志下的控件重排开销。"""
        if self._operation_log_text is None:
            return
        try:
            self._operation_log_text.configure(state="normal")
            text = "".join(
                OperationLog.format_record(record) + "\n"
                for record in records if isinstance(record, dict)
            )
            if not text:
                self._operation_log_text.configure(state="disabled")
                return
            self._operation_log_text.insert("end", text)
            line_count = int(self._operation_log_text.index("end-1c").split(".")[0])
            if line_count > 2000:
                self._operation_log_text.delete("1.0", f"{line_count - 2000}.0")
            if self._operation_log_auto_scroll.get():
                self._operation_log_text.see("end")
            self._operation_log_text.configure(state="disabled")
            if self._operation_log_status is not None:
                current = int(float(self._operation_log_text.index("end-1c").split(".")[0])) - 1
                self._operation_log_status.configure(text=f"实时记录中 · {max(0, current)} 条")
        except tk.TclError:
            pass

    def _export_operation_log(self):
        if not callable(self._export_operation_log_callback):
            if self._operation_log_status is not None:
                self._operation_log_status.configure(text="导出功能不可用", text_color=COLORS["warning"])
            return
        try:
            path = self._export_operation_log_callback()
        except Exception as exc:  # noqa: BLE001
            if self._operation_log_status is not None:
                self._operation_log_status.configure(
                    text=f"导出失败：{type(exc).__name__}", text_color=COLORS["danger"]
                )
            return
        if path and self._operation_log_status is not None:
            self._operation_log_status.configure(
                text=f"已导出：{os.path.basename(path)}", text_color=COLORS["success"]
            )

    def _collect_api(self):
        return {
            "enabled": bool(self._api_enabled.get()),
            "provider": self._api_provider.get().strip(),
            "base_url": self._api_base_url.get().strip().rstrip("/"),
            "api_key": self._api_key.get(),
            "model": self._api_model.get().strip(),
        }

    def _save_bitbrowser(self):
        try:
            timeout = max(1, min(120, int(self._bb_timeout.get().strip() or 20)))
        except ValueError:
            self._bb_status.configure(text="超时秒数必须是 1–120 的整数", text_color=COLORS["danger"])
            return
        bitbrowser = {"base_url": self._bb_url.get().strip().rstrip("/"), "timeout": timeout}
        try:
            self._config.update_section("bitbrowser", bitbrowser)
            if callable(self._on_save):
                self._on_save(bitbrowser, self._config.llm_api())
        except Exception as exc:  # noqa: BLE001
            self._bb_status.configure(
                text=f"设置保存失败：{type(exc).__name__}",
                text_color=COLORS["danger"],
            )
            return
        self._bb_status.configure(text="设置已保存；请重新检测端口", text_color=COLORS["success"])

    def _save_api(self):
        api = self._collect_api()
        try:
            self._config.update_section("llm_api", api)
        except Exception as exc:  # noqa: BLE001
            self._api_status.configure(text=f"API 信息保存失败：{type(exc).__name__}",
                                       text_color=COLORS["danger"])
            return
        self._api_status.configure(text="API 信息已保存，请检测连通性", text_color=COLORS["success"])

    def _test_api(self):
        if not callable(self._test_llm_api_callback):
            self._api_status.configure(text="API 检测功能不可用", text_color=COLORS["warning"])
            return
        config = self._collect_api()
        self._api_status.configure(text="正在检测 API 连通性…", text_color=COLORS["info"])

        def worker():
            try:
                result = self._test_llm_api_callback(config) or {}
                error = None
            except Exception as exc:  # noqa: BLE001
                result, error = {}, exc
            try:
                self.after(0, self._apply_api_test, result, error)
            except tk.TclError:
                pass

        threading.Thread(target=worker, name="llm-api-test", daemon=True).start()

    def _apply_api_test(self, result, error=None):
        if error:
            self._api_status.configure(text=f"API 检测失败：{type(error).__name__}",
                                       text_color=COLORS["danger"])
            return
        detail = str(result.get("detail") or "未返回检测结果")
        elapsed = result.get("elapsed_ms")
        suffix = f" · {elapsed} ms" if elapsed is not None else ""
        if result.get("healthy"):
            self._api_status.configure(text=f"正常：{detail}{suffix}",
                                       text_color=COLORS["success"])
        else:
            self._api_status.configure(text=f"失败：{detail}{suffix}",
                                       text_color=COLORS["danger"])

    def _set_api_test_output(self, text):
        if self._api_test_output is None:
            return
        self._api_test_output.configure(state="normal")
        self._api_test_output.delete("1.0", "end")
        self._api_test_output.insert("1.0", str(text or ""))
        self._api_test_output.configure(state="disabled")

    def _load_api_sample(self):
        if not callable(self._get_llm_sample_callback):
            self._sample_meta.configure(text="测试数据读取功能不可用", text_color=COLORS["warning"])
            return
        self._sample_meta.configure(text="正在读取…", text_color=COLORS["info"])

        def worker():
            try:
                sample = self._get_llm_sample_callback() or {}
                error = None
            except Exception as exc:  # noqa: BLE001
                sample, error = {}, exc
            try:
                self.after(0, self._apply_api_sample, sample, error)
            except tk.TclError:
                pass

        threading.Thread(target=worker, name="llm-api-sample", daemon=True).start()

    def _apply_api_sample(self, sample, error=None):
        if error:
            self._sample_meta.configure(text=f"读取失败：{type(error).__name__}",
                                        text_color=COLORS["danger"])
            return
        if not sample or not sample.get("content"):
            self._api_sample = {}
            self._sample_meta.configure(text="暂无评论数据，请先运行采集任务",
                                        text_color=COLORS["warning"])
            self._set_api_test_output("暂无可测试的评论数据。")
            return
        self._api_sample = dict(sample)
        platform = sample.get("platform_label") or sample.get("platform") or "未知平台"
        nickname = sample.get("nickname") or "未知用户"
        self._sample_meta.configure(text=f"{platform} · {nickname}", text_color=COLORS["success"])
        self._sample_content.configure(state="normal")
        self._sample_content.delete("1.0", "end")
        self._sample_content.insert("1.0", str(sample.get("content") or ""))
        self._sample_content.configure(state="normal")
        self._set_api_test_output("评论已载入，可以点击“分析评论”或“生成回复草稿”。")

    def _run_api_feature(self, feature):
        if feature == "analyze":
            callback = self._analyze_llm_comment_callback
        elif feature == "batch":
            callback = self._analyze_llm_batch_callback
        else:
            callback = self._generate_llm_reply_callback
        if not callable(callback):
            self._set_api_test_output("API 功能测试不可用。")
            return
        if feature == "batch":
            config = self._collect_api()
            self._apply_api_batch_progress(0, 1, "准备读取评论")
            self._set_api_test_output("正在读取评论并批量分析，最多一次提交 500 条，请稍候…")

            def batch_worker():
                def progress_callback(done, total, phase):
                    try:
                        self.after(0, self._apply_api_batch_progress, done, total, phase)
                    except tk.TclError:
                        pass

                try:
                    result = callback(config, progress_callback) or {}
                    error = None
                except Exception as exc:  # noqa: BLE001
                    result, error = {}, exc
                try:
                    self.after(0, self._apply_api_feature, "批量分析500条意向", result, error)
                except tk.TclError:
                    pass

            threading.Thread(target=batch_worker, name="llm-api-batch", daemon=True).start()
            return
        content = self._sample_content.get("1.0", "end").strip() if self._sample_content else ""
        if not content:
            self._set_api_test_output("请先随机抽取一条评论，或在评论原文框中输入内容。")
            return
        sample = dict(self._api_sample or {})
        sample["content"] = content
        config = self._collect_api()
        label = "分析评论" if feature == "analyze" else "生成回复草稿"
        self._set_api_test_output(f"正在{label}，请稍候…")

        def worker():
            try:
                result = callback(config, sample) or {}
                error = None
            except Exception as exc:  # noqa: BLE001
                result, error = {}, exc
            try:
                self.after(0, self._apply_api_feature, label, result, error)
            except tk.TclError:
                pass

        threading.Thread(target=worker, name=f"llm-api-{feature}", daemon=True).start()

    def _apply_api_feature(self, label, result, error=None):
        if error:
            if label == "批量分析500条意向":
                self._apply_api_batch_progress(0, 1, f"失败：{type(error).__name__}")
            self._set_api_test_output(f"{label}失败：{type(error).__name__}")
            return
        result = result or {}
        if not result.get("healthy"):
            if label == "批量分析500条意向":
                self._apply_api_batch_progress(0, result.get("requested_count") or 1,
                                               f"失败：{result.get('status') or '未知错误'}")
            output = f"{label}失败：{result.get('detail') or '未知错误'}"
            if label == "批量分析500条意向" and result.get("raw_preview"):
                output += (
                    f"\n\n模型原始返回片段（仅用于排查，已截取前 4000 字符）：\n\n"
                    f"{result['raw_preview']}"
                )
            self._set_api_test_output(output)
            return
        if label == "批量分析500条意向":
            items = result.get("items") or []
            self._apply_api_batch_progress(
                result.get("returned_count") or len(items),
                result.get("requested_count") or len(items) or 1,
                "批量分析完成",
            )
            counts = {}
            for item in items:
                intent = str(item.get("意向") or item.get("intent") or "未知")
                counts[intent] = counts.get(intent, 0) + 1
            preview = []
            for item in items[:20]:
                preview.append(
                    f"#{item.get('编号', item.get('id', '—'))} · "
                    f"{item.get('意向', item.get('intent', '未知'))} · "
                    f"{item.get('需求', item.get('need', ''))} · "
                    f"{item.get('依据', item.get('reason', ''))}"
                )
            elapsed = result.get("elapsed_ms")
            summary = "、".join(f"{key}{value}条" for key, value in counts.items()) or "无分类结果"
            output = (f"批量分析完成：请求 {result.get('requested_count', 0)} 条，"
                      f"返回 {result.get('returned_count', 0)} 条\n"
                      f"意向统计：{summary}\n"
                      f"\n前 20 条结果预览：\n" + "\n".join(preview))
            if elapsed is not None:
                output += f"\n\n耗时：{elapsed} ms"
            self._set_api_test_output(output)
            return

        elapsed = result.get("elapsed_ms")
        suffix = f"\n\n耗时：{elapsed} ms" if elapsed is not None else ""
        thinking = str(result.get("thinking") or "").strip()
        text = str(result.get("text") or "API 未返回文本").strip()
        if label == "生成回复草稿" and thinking:
            display = (f"思维链（仅测试展示，不会进入正式回复）：\n\n{thinking}\n\n"
                       f"正式回复：\n\n{text}{suffix}")
        else:
            display = f"{label}结果：\n\n{text}{suffix}"
        self._set_api_test_output(display)

    def _apply_api_batch_progress(self, done, total, phase):
        """只在主线程更新批量分析进度，避免后台线程直接操作 Tk 控件。"""
        if self._api_batch_progress is None or self._api_batch_progress_label is None:
            return
        try:
            total_value = max(1, int(total or 1))
            done_value = max(0, min(total_value, int(done or 0)))
            self._api_batch_progress.set(done_value / total_value)
            self._api_batch_progress_label.configure(
                text=f"{phase} · {done_value}/{total_value}"
            )
        except (TypeError, ValueError, tk.TclError):
            pass

    def _detect(self):
        if not callable(self._inspect_bitbrowser):
            self._bb_status.configure(text="当前为演示模式，未连接 BitBrowser", text_color=COLORS["warning"])
            return
        self._bb_status.configure(text="正在检测 BitBrowser 服务和端口…", text_color=COLORS["info"])
        def worker():
            result = self._inspect_bitbrowser()
            try:
                self.after(0, self._apply_detection, result)
            except tk.TclError:
                pass
        threading.Thread(target=worker, name="bitbrowser-inspect", daemon=True).start()

    def _apply_detection(self, result):
        if result.get("healthy"):
            text = f"服务正常 · 识别到 {len(result.get('windows', []))} 个窗口"
            color = COLORS["success"]
        else:
            text = f"BitBrowser 不可用：{result.get('error') or '未知错误'}"
            color = COLORS["danger"]
        self._bb_status.configure(text=text, text_color=color)
        self._render_checks(result.get("checks", []))
        self._render_ports(result.get("windows", []))

    def _check_platform_health(self):
        if not callable(self._inspect_platform_health):
            self._platform_status.configure(text="当前不可用", text_color=COLORS["warning"])
            return
        self._platform_status.configure(text="正在检查…", text_color=COLORS["info"])

        def worker():
            result = None
            error = None
            try:
                result = self._inspect_platform_health()
            except Exception as exc:  # noqa: BLE001
                error = exc
            try:
                self.after(0, self._apply_platform_health, result, error)
            except tk.TclError:
                pass

        threading.Thread(target=worker, name="platform-health", daemon=True).start()

    def _apply_platform_health(self, result, error=None):
        if error:
            self._platform_status.configure(
                text=f"检查失败：{type(error).__name__}", text_color=COLORS["danger"])
            self._render_platform_health([])
            return
        result = result or {}
        checked_at = result.get("checked_at") or "刚刚"
        self._platform_status.configure(
            text=f"检查完成 · {checked_at}", text_color=COLORS["success"])
        self._render_platform_health(result.get("rows", []))

    def _check_live_platform_health(self):
        if not callable(self._inspect_live_platform_health):
            self._platform_status.configure(text="当前未连接 BitBrowser", text_color=COLORS["warning"])
            return
        self._platform_status.configure(
            text="正在连接已绑定浏览器；只读不发送…", text_color=COLORS["info"])

        def worker():
            result = None
            error = None
            try:
                result = self._inspect_live_platform_health()
            except Exception as exc:  # noqa: BLE001
                error = exc
            try:
                self.after(0, self._apply_live_platform_health, result, error)
            except tk.TclError:
                pass

        threading.Thread(target=worker, name="platform-live-health", daemon=True).start()

    def _check_deep_platform_health(self):
        if not callable(self._inspect_deep_platform_health):
            self._platform_status.configure(text="当前未连接 BitBrowser", text_color=COLORS["warning"])
            return
        self._platform_status.configure(
            text="正在抽取本地评论并执行深度诊断；只填不发…", text_color=COLORS["info"])

        def worker():
            result = None
            error = None
            try:
                result = self._inspect_deep_platform_health()
            except Exception as exc:  # noqa: BLE001
                error = exc
            try:
                self.after(0, self._apply_deep_platform_health, result, error)
            except tk.TclError:
                pass

        threading.Thread(target=worker, name="platform-deep-health", daemon=True).start()

    def _apply_deep_platform_health(self, result, error=None):
        if error:
            self._platform_status.configure(
                text=f"深度诊断失败：{type(error).__name__}", text_color=COLORS["danger"])
            self._render_platform_health([])
            return
        result = result or {}
        self._last_live_health_result = result
        checked_at = result.get("checked_at") or "刚刚"
        send_text = "未发送" if result.get("send_executed") is False else "请检查"
        self._platform_status.configure(
            text=f"深度诊断完成 · {checked_at} · {send_text}", text_color=COLORS["success"])
        self._render_platform_health(result.get("rows", []))

    def _open_manual_deep_dialog(self):
        if not callable(self._inspect_manual_deep_platform_health):
            self._platform_status.configure(text="当前未连接 BitBrowser", text_color=COLORS["warning"])
            return
        dialog = ctk.CTkToplevel(self)
        dialog.title("手动输入深度诊断数据")
        dialog.geometry("660x560")
        dialog.minsize(580, 480)
        dialog.transient(self.winfo_toplevel())
        dialog.configure(fg_color=COLORS["window"])
        ctk.CTkLabel(dialog, text="手动测试", text_color=COLORS["text"],
                     font=FONTS["section_title"]).pack(anchor="w", padx=22, pady=(18, 2))
        ctk.CTkLabel(dialog, text="没有采集数据时，可手动提供作品和评论；流程仍只填入、不发送。",
                     text_color=COLORS["muted"], font=FONTS["helper"]).pack(
                         anchor="w", padx=22, pady=(0, 14))
        body = ctk.CTkFrame(dialog, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22)
        body.grid_columnconfigure(1, weight=1)
        platform_var = tk.StringVar(value="抖音")
        ctk.CTkLabel(body, text="平台", text_color=COLORS["text_2"],
                     font=FONTS["helper"]).grid(row=0, column=0, sticky="w", pady=8)
        ctk.CTkOptionMenu(body, variable=platform_var,
                          values=["抖音", "小红书", "微博", "B站"],
                          fg_color=COLORS["surface_3"], button_color=COLORS["primary"],
                          font=FONTS["helper"]).grid(row=0, column=1, sticky="ew", padx=(16, 0), pady=8)
        ctk.CTkLabel(body, text="作品地址", text_color=COLORS["text_2"],
                     font=FONTS["helper"]).grid(row=1, column=0, sticky="w", pady=8)
        url_entry = ctk.CTkEntry(body, height=36, fg_color=COLORS["window"],
                                 border_color=COLORS["border"], font=FONTS["body"])
        url_entry.grid(row=1, column=1, sticky="ew", padx=(16, 0), pady=8)
        ctk.CTkLabel(body, text="评论用户昵称", text_color=COLORS["text_2"],
                     font=FONTS["helper"]).grid(row=2, column=0, sticky="w", pady=8)
        nickname_entry = ctk.CTkEntry(body, height=36, fg_color=COLORS["window"],
                                      border_color=COLORS["border"], font=FONTS["body"])
        nickname_entry.grid(row=2, column=1, sticky="ew", padx=(16, 0), pady=8)
        ctk.CTkLabel(body, text="评论原文", text_color=COLORS["text_2"],
                     font=FONTS["helper"]).grid(row=3, column=0, sticky="nw", pady=8)
        comment_box = ctk.CTkTextbox(body, height=150, fg_color=COLORS["window"],
                                     border_width=1, border_color=COLORS["border"],
                                     font=FONTS["body"])
        comment_box.grid(row=3, column=1, sticky="nsew", padx=(16, 0), pady=8)
        body.grid_rowconfigure(3, weight=1)
        status = ctk.CTkLabel(dialog, text="", text_color=COLORS["danger"],
                              font=FONTS["helper"], anchor="w")
        status.pack(fill="x", padx=22, pady=(0, 6))
        actions = ctk.CTkFrame(dialog, fg_color="transparent")
        actions.pack(fill="x", padx=22, pady=(0, 18))

        def start():
            platform = {"抖音": "douyin", "小红书": "xhs", "微博": "weibo", "B站": "bilibili"}[platform_var.get()]
            sample = {
                "platform": platform,
                "url": url_entry.get().strip(),
                "nickname": nickname_entry.get().strip(),
                "content": comment_box.get("1.0", "end").strip(),
            }
            if not sample["url"] or not sample["nickname"] or not sample["content"]:
                status.configure(text="作品地址、评论用户昵称和评论原文都不能为空")
                return
            status.configure(text="正在执行，只填不发…", text_color=COLORS["info"])
            start_button.configure(state="disabled")

            def worker():
                result = None
                error = None
                try:
                    result = self._inspect_manual_deep_platform_health(sample)
                except Exception as exc:  # noqa: BLE001
                    error = exc
                try:
                    self.after(0, finish, result, error)
                except tk.TclError:
                    pass

            threading.Thread(target=worker, name="platform-manual-deep-health", daemon=True).start()

        def finish(result, error):
            try:
                dialog.destroy()
            except tk.TclError:
                pass
            self._apply_deep_platform_health(result, error)

        ctk.CTkButton(actions, text="取消", width=90, height=34,
                      fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                      text_color=COLORS["text"], command=dialog.destroy).pack(side="right")
        start_button = ctk.CTkButton(actions, text="开始测试", width=110, height=34,
                                     fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                                     command=start)
        start_button.pack(side="right", padx=(0, 10))
        try:
            dialog.update_idletasks()
            owner = self.winfo_toplevel()
            x = owner.winfo_rootx() + max(20, (owner.winfo_width() - dialog.winfo_width()) // 2)
            y = owner.winfo_rooty() + max(20, (owner.winfo_height() - dialog.winfo_height()) // 2)
            dialog.geometry(f"{dialog.winfo_width()}x{dialog.winfo_height()}+{x}+{y}")
            dialog.deiconify()
            dialog.lift()
            dialog.focus_force()
        except tk.TclError:
            pass

    def _apply_live_platform_health(self, result, error=None):
        if error:
            self._platform_status.configure(
                text=f"真实诊断失败：{type(error).__name__}", text_color=COLORS["danger"])
            self._render_platform_health([])
            return
        result = result or {}
        self._last_live_health_result = result
        checked_at = result.get("checked_at") or "刚刚"
        self._platform_status.configure(
            text=f"真实诊断完成 · {checked_at}", text_color=COLORS["success"])
        self._render_platform_health(result.get("rows", []))

    def _render_platform_health(self, rows):
        if self._platform_rows is None:
            return
        for child in self._platform_rows.winfo_children():
            child.destroy()
        headers = ("平台", "状态", "采集适配器", "回复适配器", "账号", "说明", "详情")
        for col, label in enumerate(headers):
            ctk.CTkLabel(self._platform_rows, text=label, text_color=COLORS["muted"],
                         font=FONTS["helper"], anchor="w").grid(
                             row=0, column=col, sticky="ew", padx=8, pady=6)
        for col in range(len(headers)):
            self._platform_rows.grid_columnconfigure(col, weight=1 if col in (0, 5) else 0)
        if not rows:
            ctk.CTkLabel(self._platform_rows, text="点击“运行五平台检查”后显示结果。",
                         text_color=COLORS["subtle"], font=FONTS["helper"]).grid(
                             row=1, column=0, columnspan=len(headers), sticky="w", padx=8, pady=10)
            return
        for idx, row in enumerate(rows, start=1):
            status = row.get("status") or "unknown"
            color = (COLORS["success"] if status == "ok" else
                     COLORS["warning"] if status == "warning" else COLORS["danger"])
            values = (row.get("platform_label") or row.get("platform") or "—",
                      row.get("status_label") or "未知", row.get("adapter") or "—",
                      row.get("reply") or "—", str(row.get("accounts", 0)),
                      row.get("detail") or "—")
            for col, value in enumerate(values):
                if col == 0:
                    platform_cell = ctk.CTkFrame(self._platform_rows, fg_color="transparent")
                    platform_cell.grid(row=idx, column=col, sticky="ew", padx=8, pady=4)
                    PlatformBadge(platform_cell, str(value), compact=True).pack(side="left")
                    ctk.CTkLabel(platform_cell, text=str(value),
                                 text_color=COLORS["text_2"], font=FONTS["helper"],
                                 anchor="w").pack(side="left", padx=(6, 0))
                    continue
                ctk.CTkLabel(self._platform_rows, text=str(value),
                             text_color=color if col == 1 else COLORS["text_2"],
                             font=FONTS["helper"], anchor="w").grid(
                                 row=idx, column=col, sticky="ew", padx=8, pady=5)
            ctk.CTkButton(
                self._platform_rows, text="查看", width=52, height=26,
                fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                text_color=COLORS["text"], font=FONTS["helper"],
                command=lambda item=row: self._show_platform_details(item),
            ).grid(row=idx, column=6, sticky="w", padx=8, pady=4)

    def _show_platform_details(self, row):
        """展示单个平台/账号的逐项诊断结果，不阻塞主窗口。"""
        owner = self.winfo_toplevel()
        dialog = ctk.CTkToplevel(owner)
        dialog.title(f"{row.get('platform_label') or '平台'}诊断详情")
        dialog.geometry("760x560")
        dialog.minsize(620, 420)
        dialog.transient(owner)
        dialog.configure(fg_color=COLORS["window"])
        ctk.CTkLabel(dialog, text="诊断详情", text_color=COLORS["text"],
                     font=FONTS["section_title"]).pack(anchor="w", padx=20, pady=(18, 2))
        ctk.CTkLabel(dialog, text="仅展示诊断状态；不会显示 Cookie、Token 或 CDP 地址。",
                     text_color=COLORS["muted"], font=FONTS["helper"]).pack(
                         anchor="w", padx=20, pady=(0, 10))
        box = ctk.CTkTextbox(dialog, fg_color=COLORS["surface_2"],
                             text_color=COLORS["text_2"], font=FONTS["helper"])
        box.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        lines = [
            f"平台：{row.get('platform_label') or row.get('platform') or '—'}",
            f"总体状态：{row.get('status_label') or '未知'}",
            f"说明：{row.get('detail') or '—'}",
            "",
        ]
        details = row.get("account_details") or []
        if not details:
            lines.append("未绑定账号，因此没有逐账号浏览器检查结果。")
        for index, item in enumerate(details, start=1):
            lines.extend([
                f"账号 {index}：{item.get('account') or '—'}",
                f"状态：{_HEALTH_STATUS_LABELS.get(str(item.get('status')), str(item.get('status') or '—'))}",
                f"结果：{item.get('detail') or '—'}",
            ])
            checks = item.get("checks") or {}
            if checks:
                lines.append("检查项：")
                lines.extend(f"  · {_health_check_text(key, value)}"
                             for key, value in checks.items())
            sample = item.get("sample") or {}
            if sample:
                lines.extend([
                    f"测试作品：{sample.get('video_url') or '—'}",
                    f"测试用户：{sample.get('nickname') or '—'}",
                    f"评论摘要：{sample.get('comment_preview') or '—'}",
                ])
            lines.append(f"截图：{'已生成（已模糊）' if item.get('screenshot_path') else '未生成'}")
            lines.append("")
        box.insert("1.0", "\n".join(lines))
        box.see("1.0")
        box.configure(state="disabled")
        ctk.CTkButton(dialog, text="关闭", width=90, height=32,
                      command=dialog.destroy).pack(anchor="e", padx=20, pady=(0, 16))
        # 二级窗口必须以主软件为基准定位，避免被 Windows 放到桌面左上角或屏幕外。
        try:
            owner.update_idletasks()
            dialog.update_idletasks()
            width = min(760, max(620, owner.winfo_width() - 40))
            height = min(560, max(420, owner.winfo_height() - 40))
            x = owner.winfo_rootx() + max(20, (owner.winfo_width() - width) // 2)
            y = owner.winfo_rooty() + max(20, (owner.winfo_height() - height) // 2)
            dialog.geometry(f"{width}x{height}+{x}+{y}")
            dialog.deiconify()
            dialog.lift()
            dialog.focus_force()
        except tk.TclError:
            pass

    def _export_diagnostic_package(self):
        if not callable(self._export_diagnostic_package_callback):
            self._platform_status.configure(text="导出功能不可用", text_color=COLORS["warning"])
            return
        self._platform_status.configure(text="正在生成脱敏诊断包…", text_color=COLORS["info"])

        def worker():
            result = None
            error = None
            try:
                result = self._export_diagnostic_package_callback(self._last_live_health_result)
            except Exception as exc:  # noqa: BLE001
                error = exc
            try:
                self.after(0, self._apply_diagnostic_package, result, error)
            except tk.TclError:
                pass

        threading.Thread(target=worker, name="diagnostic-package", daemon=True).start()

    def _apply_diagnostic_package(self, result, error=None):
        if error:
            self._platform_status.configure(
                text=f"诊断包导出失败：{type(error).__name__}", text_color=COLORS["danger"])
            return
        path = str((result or {}).get("path") or "")
        name = os.path.basename(path) if path else "已生成"
        count = int((result or {}).get("screenshot_count") or 0)
        self._platform_status.configure(
            text=f"已导出 {name} · 模糊截图 {count} 张", text_color=COLORS["success"])

    def _render_checks(self, rows):
        if self._check_rows is None:
            return
        for child in self._check_rows.winfo_children():
            child.destroy()
        headers = ("检查项", "状态", "说明", "耗时")
        for col, label in enumerate(headers):
            ctk.CTkLabel(self._check_rows, text=label, text_color=COLORS["muted"],
                         font=FONTS["helper"], anchor="w").grid(
                             row=0, column=col, sticky="ew", padx=8, pady=6)
        self._check_rows.grid_columnconfigure(2, weight=1)
        if not rows:
            ctk.CTkLabel(self._check_rows, text="点击“检测并识别端口”后显示诊断结果。",
                         text_color=COLORS["subtle"], font=FONTS["helper"]).grid(
                             row=1, column=0, columnspan=4, sticky="w", padx=8, pady=10)
            return
        for idx, row in enumerate(rows, start=1):
            status = row.get("status") or "未知"
            color = COLORS["success"] if status == "正常" else COLORS["danger"]
            values = (row.get("name") or "—", status, row.get("detail") or "—",
                      f"{row.get('elapsed_ms', 0)} ms")
            for col, value in enumerate(values):
                ctk.CTkLabel(self._check_rows, text=str(value),
                             text_color=color if col == 1 else COLORS["text_2"],
                             font=FONTS["helper"], anchor="w").grid(
                                 row=idx, column=col, sticky="ew", padx=8, pady=5)

    def _render_ports(self, rows):
        for child in self._port_rows.winfo_children():
            child.destroy()
        headers = ("窗口", "窗口 ID", "进程", "调试端口", "状态")
        for col, label in enumerate(headers):
            ctk.CTkLabel(self._port_rows, text=label, text_color=COLORS["muted"],
                         font=FONTS["helper"], anchor="w").grid(
                             row=0, column=col, sticky="ew", padx=8, pady=6)
        for col in range(len(headers)):
            self._port_rows.grid_columnconfigure(col, weight=1 if col == 0 else 0)
        if not rows:
            ctk.CTkLabel(self._port_rows, text="尚未识别到窗口，请确认 BitBrowser 已启动后点击检测。",
                         text_color=COLORS["subtle"], font=FONTS["helper"]).grid(
                             row=1, column=0, columnspan=5, sticky="w", padx=8, pady=12)
            return
        for idx, row in enumerate(rows, start=1):
            values = (row.get("name") or "未命名", row.get("id") or "—",
                      row.get("pid") or "—", row.get("port") or "—",
                      "已打开" if row.get("opened") else "未打开")
            color = COLORS["success"] if row.get("opened") else COLORS["muted"]
            for col, value in enumerate(values):
                ctk.CTkLabel(self._port_rows, text=str(value), text_color=color if col == 4 else COLORS["text_2"],
                             font=FONTS["helper"], anchor="w").grid(
                                 row=idx, column=col, sticky="ew", padx=8, pady=5)
