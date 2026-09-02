# -*- coding: utf-8 -*-
"""线索中心顶部筛选栏。

平台、地区、意向保持在同一行；评论的准确时间直接显示在列表中。
"""

from __future__ import annotations

import customtkinter as ctk
from typing import Callable, Dict, List, Optional

from ..theme import COLORS, FONTS, RADIUS, SPACE


class FilterBar(ctk.CTkFrame):
    def __init__(
        self,
        master,
        *,
        platforms: List[str],
        intents: List[str],
        provinces: Optional[List[str]] = None,
        task_options: Optional[Dict[str, str]] = None,
        labels: Optional[Dict[str, Dict[str, str]]] = None,
        on_change: Callable[[Dict[str, str]], None],
    ):
        super().__init__(master, fg_color=COLORS["surface_2"],
                         corner_radius=RADIUS["card"],
                         border_width=1, border_color=COLORS["border"])
        self._on_change = on_change
        self._vars: Dict[str, ctk.StringVar] = {}
        self._raw_values: Dict[str, Dict[str, str]] = {}
        self._display_values: Dict[str, Dict[str, str]] = {}
        self._menus: Dict[str, ctk.CTkOptionMenu] = {}
        self._suspend_change = False
        self._labels = dict(labels or {})
        if task_options:
            self._labels["task"] = dict(task_options)
        self._build(platforms, provinces or [], intents, task_options or {})

    # ------------------------------------------------------------------
    def _build(self, platforms, provinces, intents, task_options):
        """单行筛选区：任务、平台、地区和意向。"""
        for col in range(8):
            self.grid_columnconfigure(col, weight=0)

        options = [
            ("任务", "task", list(task_options)),
            ("平台", "platform", platforms),
            ("地区", "province", provinces),
            ("意向", "intent", intents),
        ]
        for index, (label, key, values) in enumerate(options):
            col = index * 2
            self._add_label(col, label, row=0)
            self._add_option(col + 1, values, key, row=0)

    def _add_label(self, col: int, text: str, *, row: int):
        ctk.CTkLabel(self, text=text, text_color=COLORS["muted"],
                     font=FONTS["helper"]).grid(
            row=row, column=col, padx=(SPACE["md"], SPACE["xs"]), pady=SPACE["sm"])

    def _add_option(self, col: int, values: List[str], key: str, *, row: int):
        var = ctk.StringVar(value="全部")
        self._vars[key] = var
        labels = self._labels.get(key, {})
        display_to_raw = {"全部": ""}
        raw_to_display = {"": "全部"}
        for raw in values:
            display = labels.get(raw, raw)
            display_to_raw[display] = raw
            raw_to_display[raw] = display
        self._raw_values[key] = display_to_raw
        self._display_values[key] = raw_to_display
        menu = ctk.CTkOptionMenu(
            self, variable=var, values=list(display_to_raw),
            width=220 if key == "task" else 110, height=36,
            fg_color=COLORS["window"], button_color=COLORS["surface_3"],
            button_hover_color=COLORS["surface_hover"],
            text_color=COLORS["text"], dropdown_fg_color=COLORS["surface_2"],
            dropdown_hover_color=COLORS["surface_hover"],
            font=FONTS["body"], dropdown_font=FONTS["body"],
        )
        menu.grid(row=row, column=col, padx=(0, SPACE["md"]), pady=SPACE["sm"])
        self._menus[key] = menu
        var.trace_add("write", lambda *_a, k=key: self._fire_change(k))
        return var

    def set_options(self, key: str, values: List[str], labels: Optional[Dict[str, str]] = None):
        """后台刷新动态下拉选项，不重建筛选栏。"""
        if key not in self._vars or key not in self._menus:
            return
        labels = labels or {}
        old_display = self._vars[key].get()
        old_raw = self._raw_values.get(key, {}).get(old_display, "")
        display_to_raw = {"全部": ""}
        raw_to_display = {"": "全部"}
        for raw in values:
            raw = str(raw)
            display = str(labels.get(raw, raw))
            display_to_raw[display] = raw
            raw_to_display[raw] = display
        if display_to_raw == self._raw_values.get(key):
            return
        self._raw_values[key] = display_to_raw
        self._display_values[key] = raw_to_display
        self._suspend_change = True
        try:
            self._menus[key].configure(values=list(display_to_raw))
            next_display = raw_to_display.get(old_raw, "全部")
            if self._vars[key].get() != next_display:
                self._vars[key].set(next_display)
        finally:
            self._suspend_change = False

    # ------------------------------------------------------------------
    def _fire_change(self, key: str):
        if self._suspend_change:
            return
        self._on_change(self.current())

    def current(self) -> Dict[str, str]:
        """返回当前筛选值（key → value，'全部' 表示不限）。"""
        return {k: self._raw_values.get(k, {}).get(v.get(), v.get())
                for k, v in self._vars.items()}

    def set(self, values: Dict[str, str]):
        self._suspend_change = True
        try:
            for k, v in values.items():
                if k in self._vars:
                    next_display = self._display_values.get(k, {}).get(v, v)
                    if self._vars[k].get() != next_display:
                        self._vars[k].set(next_display)
        finally:
            self._suspend_change = False
