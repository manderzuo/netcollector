# -*- coding: utf-8 -*-
"""线索数据表：单画布表格 + 分页条。

不包含业务判断；行数据由 Presenter 组装后传入。画布渲染避免筛选时
销毁、创建数百个独立控件，使平台和标签切换保持即时响应。
"""

from __future__ import annotations

import tkinter as tk
from typing import Any, Callable, Dict, List, Optional

import customtkinter as ctk

from ..theme import COLORS, FONTS, RADIUS, SPACE


class LeadCanvasTable(ctk.CTkFrame):
    """单画布线索表。

    线索页每次筛选最多显示 50 行。用数百个 CTkLabel/Frame 表示这些行会让
    主线程停顿接近一秒；Canvas 重绘保持相同交互，但只操作一个原生控件。
    """

    HEADER_HEIGHT = 44
    ROW_HEIGHT = 66

    def __init__(self, master, columns, *, height=420, on_select=None,
                 on_cell_click=None):
        super().__init__(
            master, fg_color=COLORS["surface_2"], corner_radius=RADIUS["card"],
            border_width=1, border_color=COLORS["border"], height=height,
        )
        self.columns = list(columns)
        self.on_select = on_select
        self.on_cell_click = on_cell_click
        self.rows: List[Dict[str, Any]] = []
        self.selected_index: Optional[int] = None
        self._checked_ids = set()
        self._layout = []
        self._redraw_after = None
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        canvas_args = {
            "highlightthickness": 0,
            "bd": 0,
            "takefocus": 0,
        }
        self.header = tk.Canvas(
            self, height=self.HEADER_HEIGHT, bg=COLORS["surface"], **canvas_args,
        )
        self.header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=1, pady=(1, 0))
        self.body = tk.Canvas(
            self, bg=COLORS["surface_2"], yscrollincrement=18, **canvas_args,
        )
        self.body.grid(row=1, column=0, sticky="nsew", padx=(1, 0), pady=(0, 1))
        self.scrollbar = ctk.CTkScrollbar(
            self, orientation="vertical", command=self.body.yview,
            button_color=COLORS["surface_3"],
            button_hover_color=COLORS["primary"], width=12,
        )
        self.scrollbar.grid(row=1, column=1, sticky="ns", padx=(0, 2), pady=(0, 2))
        self.body.configure(yscrollcommand=self.scrollbar.set)
        self.header.bind("<Configure>", self._schedule_redraw, add="+")
        self.body.bind("<Configure>", self._schedule_redraw, add="+")
        self.body.bind("<Button-1>", self._on_click, add="+")
        self.body.bind("<MouseWheel>", self._on_mousewheel, add="+")

    def _schedule_redraw(self, _event=None):
        if self._redraw_after is not None:
            try:
                self.after_cancel(self._redraw_after)
            except Exception:
                pass
        self._redraw_after = self.after_idle(self._redraw)

    def _column_widths(self, width):
        """按窗口宽度压缩列，同时保持表头和数据使用同一组边界。"""
        width = max(710, int(width) - 4)
        preferred = [64, 180, 520, 165, 120]
        minimum = [56, 130, 280, 145, 99]
        if len(self.columns) != len(preferred):
            weights = [max(0, int(spec[2])) for spec in self.columns]
            base = [self._fixed_width(spec) or 80 for spec in self.columns]
            extra = max(0, width - sum(base))
            total_weight = sum(weights) or 1
            return [value + int(extra * weight / total_weight)
                    for value, weight in zip(base, weights)]
        if width >= sum(preferred):
            widths = list(preferred)
            widths[2] += width - sum(preferred)
            return widths
        span = sum(preferred) - sum(minimum)
        ratio = max(0.0, min(1.0, (width - sum(minimum)) / max(1, span)))
        widths = [int(low + (high - low) * ratio)
                  for low, high in zip(minimum, preferred)]
        widths[2] += width - sum(widths)
        return widths

    @staticmethod
    def _fixed_width(spec):
        if len(spec) > 6 and isinstance(spec[6], (int, float)):
            return int(spec[6])
        return None

    def _make_layout(self, width):
        x = 0
        layout = []
        for spec, column_width in zip(self.columns, self._column_widths(width)):
            layout.append((spec, x, x + column_width))
            x += column_width
        self._layout = layout
        return layout, x

    def _redraw(self):
        self._redraw_after = None
        if not self.winfo_exists():
            return
        width = max(self.header.winfo_width(), self.body.winfo_width(), 710)
        layout, content_width = self._make_layout(width)
        self.header.delete("all")
        self.body.delete("all")
        self.header.configure(scrollregion=(0, 0, content_width, self.HEADER_HEIGHT))

        for spec, left, right in layout:
            anchor = spec[3] if len(spec) > 3 else "w"
            x = (left + right) / 2 if anchor == "center" else left + 14
            self.header.create_text(
                x, self.HEADER_HEIGHT / 2, text=spec[1],
                anchor="center" if anchor == "center" else "w",
                fill=COLORS["muted"], font=FONTS["table_bold"],
            )

        for index, row in enumerate(self.rows):
            top = index * self.ROW_HEIGHT
            bottom = top + self.ROW_HEIGHT
            identity = str(row.get("_identity", ""))
            base = COLORS["surface_2"] if index % 2 == 0 else COLORS["surface"]
            fill = COLORS["primary_soft"] if index == self.selected_index else base
            outline = COLORS["primary"] if index == self.selected_index else fill
            self.body.create_rectangle(
                6, top + 3, content_width - 6, bottom - 2,
                fill=fill, outline=outline, width=1,
            )
            for spec, left, right in layout:
                key = spec[0]
                value = str(row.get(key, ""))
                center_y = (top + bottom) / 2
                if key == "select":
                    size = 18
                    box_left = left + 15
                    box_top = center_y - size / 2
                    checked = identity in self._checked_ids
                    self.body.create_rectangle(
                        box_left, box_top, box_left + size, box_top + size,
                        fill=COLORS["primary"] if checked else COLORS["surface"],
                        outline=COLORS["primary"] if checked else COLORS["border"],
                        width=1,
                    )
                    if checked:
                        self.body.create_line(
                            box_left + 4, box_top + 9, box_left + 8, box_top + 13,
                            box_left + 15, box_top + 5,
                            fill="#ffffff", width=2, smooth=True,
                        )
                    continue
                anchor = spec[3] if len(spec) > 3 else "w"
                x = (left + right) / 2 if anchor == "center" else left + 14
                wrap = len(spec) > 5 and spec[5] == "wrap"
                link = len(spec) > 5 and spec[5] == "link"
                if wrap:
                    chars_per_line = max(8, int((right - left - 28) / 13))
                    max_chars = chars_per_line * 2
                    if len(value) > max_chars:
                        value = value[:max_chars - 1].rstrip() + "…"
                self.body.create_text(
                    x, center_y, text=value,
                    anchor="center" if anchor == "center" else "w",
                    justify="left", width=max(1, right - left - 28) if wrap else 0,
                    fill=(COLORS["primary"] if link and row.get(f"{key}_value")
                          else row.get(f"{key}_color", COLORS["text_2"])),
                    font=FONTS["table"],
                )

        total_height = max(1, len(self.rows) * self.ROW_HEIGHT)
        self.body.configure(scrollregion=(0, 0, content_width, total_height))

    def set_rows(self, rows):
        previous = self.get_selected()
        previous_id = str(previous.get("_identity", "")) if previous else None
        self.rows = list(rows)
        visible_ids = {str(row.get("_identity", "")) for row in self.rows}
        self._checked_ids.intersection_update(visible_ids)
        self.selected_index = next(
            (index for index, row in enumerate(self.rows)
             if previous_id is not None and str(row.get("_identity", "")) == previous_id),
            None,
        )
        self.body.yview_moveto(0)
        self._redraw()

    def update_rows(self, rows):
        self.set_rows(rows)
        return True

    def _on_click(self, event):
        index = int(self.body.canvasy(event.y) // self.ROW_HEIGHT)
        if index < 0 or index >= len(self.rows):
            return
        x = self.body.canvasx(event.x)
        key = next((spec[0] for spec, left, right in self._layout
                    if left <= x < right), "")
        row = self.rows[index]
        identity = str(row.get("_identity", ""))
        self.selected_index = index
        if key == "select":
            if identity in self._checked_ids:
                self._checked_ids.remove(identity)
            else:
                self._checked_ids.add(identity)
        self._redraw()
        if key == "source_url" and row.get("source_url_value"):
            if self.on_cell_click:
                self.on_cell_click(row, key)
        elif key != "select" and self.on_select:
            self.on_select(row)

    def _on_mousewheel(self, event):
        delta = -1 if event.delta > 0 else 1
        self.body.yview_scroll(delta * 3, "units")
        return "break"

    def get_selected(self):
        if self.selected_index is None or self.selected_index >= len(self.rows):
            return None
        return self.rows[self.selected_index]

    def get_checked(self):
        return [row for row in self.rows
                if str(row.get("_identity", "")) in self._checked_ids]


class DataTable(ctk.CTkFrame):
    """ModernTable + 分页条的组合控件。

    用法：
        table = DataTable(parent, columns=[(key, 标题, 权重, anchor, status_key)],
                          on_select=fn, on_page=fn)
        table.set_page(total, page, page_size)
        table.set_rows(rows)
    """

    def __init__(self, master, columns, *, on_select=None, on_cell_click=None, on_page=None,
                 height=420):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._table = LeadCanvasTable(
            self, columns, height=height, on_select=on_select,
            on_cell_click=on_cell_click,
        )
        self._table.grid(row=0, column=0, sticky="nsew")

        self._pager = self._build_pager()
        self._pager.grid(row=1, column=0, sticky="ew", pady=(SPACE["sm"], 0))
        self._on_page = on_page
        self._page = 1
        self._total = 0
        self._page_size = 50

    # ------------------------------------------------------------------
    def _build_pager(self):
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid_columnconfigure(0, weight=1)
        self._info = ctk.CTkLabel(bar, text="", text_color=COLORS["muted"],
                                  font=FONTS["helper"], anchor="w")
        self._info.grid(row=0, column=0, sticky="w", padx=SPACE["sm"])
        self._prev = ctk.CTkButton(bar, text="‹", width=36, height=28,
                                   fg_color=COLORS["surface_3"],
                                   hover_color=COLORS["surface_hover"],
                                   text_color=COLORS["text"], corner_radius=7,
                                   command=self._go_prev)
        self._prev.grid(row=0, column=1, padx=2)
        self._page_label = ctk.CTkLabel(bar, text="1/1", text_color=COLORS["text_2"],
                                        font=FONTS["helper"], width=56)
        self._page_label.grid(row=0, column=2, padx=2)
        self._next = ctk.CTkButton(bar, text="›", width=36, height=28,
                                   fg_color=COLORS["surface_3"],
                                   hover_color=COLORS["surface_hover"],
                                   text_color=COLORS["text"], corner_radius=7,
                                   command=self._go_next)
        self._next.grid(row=0, column=3, padx=2)
        return bar

    # ------------------------------------------------------------------
    def set_page(self, total: int, page: int, page_size: int):
        self._total = total
        self._page = max(1, page)
        self._page_size = max(1, page_size)
        pages = max(1, (total + page_size - 1) // page_size)
        self._info.configure(text=f"共 {total} 条")
        self._page_label.configure(text=f"{self._page}/{pages}")
        self._prev.configure(state="normal" if self._page > 1 else "disabled")
        self._next.configure(state="normal" if self._page < pages else "disabled")

    def set_rows(self, rows: List[Dict[str, Any]]):
        self._table.set_rows(rows)

    def update_rows(self, rows: List[Dict[str, Any]]) -> bool:
        """差量更新已有行；返回 False 表示结构变化需 set_rows（P3-1）。"""
        return self._table.update_rows(rows)

    def get_selected(self):
        return self._table.get_selected()

    def get_checked(self):
        """返回当前页通过复选框勾选的行。"""
        return self._table.get_checked()

    # ------------------------------------------------------------------
    def _go_prev(self):
        if self._page > 1 and self._on_page:
            self._on_page(self._page - 1)

    def _go_next(self):
        if self._on_page:
            self._on_page(self._page + 1)
