"""Reusable CustomTkinter widgets for the desktop workbench."""

import os
import tkinter as tk
import customtkinter as ctk
try:
    from PIL import Image, ImageChops, ImageDraw
except ImportError:  # customtkinter normally bundles/depends on Pillow
    Image = None
    ImageChops = None

from .theme import COLORS, FONTS, RADIUS, status_palette


PLATFORM_STYLE = {
    "抖音": ("♪", "#05070b", "#f4f6fa"),
    "小红书": ("小红", "#ff304f", "#ffffff"),
    "微博": ("●", "#f04b5d", "#ffffff"),
    "B站": ("哔", "#ef79a8", "#ffffff"),
}
_PLATFORM_IMAGES = {}
_NAV_IMAGES = {}
NAV_ICON_BLUE = "#56B4FF"


def _platform_image(platform, size):
    if Image is None or platform not in {"抖音", "小红书", "微博", "B站"}:
        return None
    key = (platform, size)
    if key in _PLATFORM_IMAGES:
        return _PLATFORM_IMAGES[key]
    filename = {
        "抖音": "platform_douyin.png",
        "小红书": "platform_xhs.jpg",
        "微博": "platform_weibo.webp",
        "B站": "platform_bilibili.png",
    }[platform]
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "assets", filename))
    if not os.path.exists(path):
        return None
    try:
        source = Image.open(path).convert("RGBA")
        # 先在高分辨率画布上等比例缩放，再交给 CTkImage 显示，避免
        # “原图 → 96px → 20px”的重复缩放造成锯齿；同时不拉伸微博的宽图标。
        work_size = max(256, size * 12)
        source.thumbnail((work_size - 4, work_size - 4), Image.Resampling.LANCZOS)
        img = Image.new("RGBA", (work_size, work_size), (0, 0, 0, 0))
        img.alpha_composite(
            source,
            ((work_size - source.width) // 2, (work_size - source.height) // 2),
        )
        mask = Image.new("L", (work_size, work_size), 0)
        draw = ImageDraw.Draw(mask)
        radius = max(10, work_size // 6)
        draw.rounded_rectangle((1, 1, work_size - 2, work_size - 2), radius=radius, fill=255)
        img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
        result = ctk.CTkImage(light_image=img, dark_image=img, size=(size - 2, size - 2))
        _PLATFORM_IMAGES[key] = result
        return result
    except Exception:
        return None


def _nav_image(key, size, color=None):
    """加载高清侧栏导航图标，去白底并统一成单色。"""
    if Image is None or ImageChops is None:
        return None
    filename = {
        "overview": "nav_overview.webp",
        "tasks": "nav_tasks.bmp",
        "accounts": "nav_accounts.webp",
        "leads": "nav_leads.webp",
        "interaction": "nav_interaction.webp",
        "settings": "nav_settings.jpg",
    }.get(key)
    if not filename:
        return None
    # 侧栏图标统一使用蓝色；活动状态由导航背景和文字颜色表达。
    color = color or NAV_ICON_BLUE
    cache_key = (key, size, color)
    if cache_key in _NAV_IMAGES:
        return _NAV_IMAGES[cache_key]
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "assets", filename))
    if not os.path.exists(path):
        return None
    try:
        source = Image.open(path).convert("RGBA")
        # 保留更高的中间分辨率，再交给 CTkImage 缩放到 25px，避免先缩小
        # 到 96px 后二次缩放造成明显锯齿。
        work_size = max(256, size * 12)
        source.thumbnail((work_size, work_size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (work_size, work_size), (0, 0, 0, 0))
        canvas.alpha_composite(source, ((work_size - source.width) // 2, (work_size - source.height) // 2))
        # 用户提供的图标可能带浅灰底、水印或压缩纹理；用前景阈值提取
        # alpha，过滤浅色背景，只保留深色/彩色主体，同时保留主体边缘抗锯齿。
        rgb = canvas.convert("RGB")
        white = Image.new("RGB", rgb.size, (255, 255, 255))
        diff = ImageChops.difference(rgb, white)
        red, green, blue = diff.split()
        mask = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        # 20 以下通常是底色波动/水印，阈值以上快速过渡到不透明，
        # 让主体轮廓清楚；彩色图标即使亮度较高也能保留下来。
        mask = mask.point(lambda value: 0 if value < 20 else min(255, (value - 20) * 6))
        mask = ImageChops.multiply(mask, canvas.getchannel("A"))
        tinted = Image.new("RGBA", canvas.size, color)
        tinted.putalpha(mask)
        result = ctk.CTkImage(light_image=tinted, dark_image=tinted, size=(size, size))
        _NAV_IMAGES[cache_key] = result
        return result
    except Exception:
        return None


class PlatformBadge(ctk.CTkFrame):
    def __init__(self, master, platform, *, compact=False):
        self.compact = compact
        size = 24 if compact else 28
        super().__init__(master, width=size, height=size, corner_radius=7, fg_color="transparent")
        self.pack_propagate(False)
        self._label = ctk.CTkLabel(self, fg_color="transparent",
                                   font=("Microsoft YaHei UI", 9 if compact else 10, "bold"),
                                   anchor="center")
        self._label.pack(fill="both", expand=True)
        self.set_platform(platform)

    def set_platform(self, platform):
        icon, _bg, fg = PLATFORM_STYLE.get(platform, ("•", COLORS["surface_3"], COLORS["text"]))
        image = _platform_image(platform, (24 if self.compact else 28) - 2)
        self._label.configure(text="" if image else icon, image=image, text_color=fg)


class PlatformPicker(ctk.CTkFrame):
    """带平台图标的紧凑下拉选择器。"""
    def __init__(self, master, variable, values, *, width=180, height=40):
        super().__init__(master, width=width, height=height, corner_radius=RADIUS["control"],
                         fg_color=COLORS["window"], border_width=1, border_color=COLORS["border"])
        self.variable = variable
        self.values = list(values)
        self._popup = None
        self.pack_propagate(False)
        self.grid_columnconfigure(1, weight=1)
        self.badge = PlatformBadge(self, variable.get(), compact=True)
        self.badge.grid(row=0, column=0, padx=(8, 2), pady=7)
        self.label = ctk.CTkLabel(self, text=variable.get(), anchor="w", text_color=COLORS["text"],
                                  font=FONTS["body"])
        self.label.grid(row=0, column=1, sticky="ew", padx=(0, 4))
        self.arrow = ctk.CTkLabel(self, text="⌄", width=24, text_color=COLORS["muted"],
                                  font=("Segoe UI", 16))
        self.arrow.grid(row=0, column=2, padx=(0, 6))
        for widget in (self, self.badge, self.label, self.arrow):
            widget.bind("<Button-1>", self._toggle, add="+")
        self.variable.trace_add("write", self._sync)

    def _sync(self, *_):
        value = self.variable.get()
        self.label.configure(text=value)
        self.badge.set_platform(value)

    def _toggle(self, _event=None):
        if self._popup is not None and self._popup.winfo_exists():
            self._close_popup()
            return
        popup = ctk.CTkToplevel(self)
        self._popup = popup
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(fg_color=COLORS["surface_2"])
        popup.geometry(f"{self.winfo_width()}x{len(self.values) * 42 + 8}+{self.winfo_rootx()}+{self.winfo_rooty() + self.winfo_height() + 4}")
        body = ctk.CTkFrame(popup, fg_color=COLORS["surface_2"], corner_radius=RADIUS["control"],
                           border_width=1, border_color=COLORS["border"])
        body.pack(fill="both", expand=True, padx=1, pady=1)
        for value in self.values:
            row = ctk.CTkFrame(body, height=40, fg_color="transparent", corner_radius=7)
            row.pack(fill="x", padx=4, pady=1)
            badge = PlatformBadge(row, value, compact=True)
            badge.pack(side="left", padx=(6, 3), pady=4)
            text = ctk.CTkLabel(row, text=value, anchor="w", text_color=COLORS["text"],
                               font=FONTS["body"])
            text.pack(side="left", fill="x", expand=True)
            for widget in (row, badge, text):
                widget.bind("<Enter>", lambda _e, r=row: r.configure(fg_color=COLORS["primary_soft"]), add="+")
                widget.bind("<Leave>", lambda _e, r=row: r.configure(fg_color="transparent"), add="+")
                widget.bind("<Button-1>", lambda _e, v=value: self._choose(v), add="+")
        popup.bind("<FocusOut>", lambda _e: self.after(80, self._close_if_unfocused), add="+")
        popup.focus_force()

    def _choose(self, value):
        self.variable.set(value)
        self._close_popup()

    def _close_if_unfocused(self):
        if self._popup is not None and self._popup.winfo_exists():
            try:
                if self._popup.focus_displayof() is None:
                    self._close_popup()
            except Exception:
                self._close_popup()

    def _close_popup(self):
        if self._popup is not None and self._popup.winfo_exists():
            self._popup.destroy()
        self._popup = None


class LineIcon(ctk.CTkCanvas):
    """Small vector icon drawn in-process so it stays sharp at every scale."""
    def __init__(self, master, kind, color=None, size=26, bg=None):
        super().__init__(master, width=size, height=size, bg=bg or COLORS["sidebar"],
                         highlightthickness=0, bd=0)
        self.kind, self.color, self.size = kind, color or COLORS["text_2"], size
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    def _draw(self):
        self.delete("all")
        c, s = self.color, self.size
        w = max(1.5, s / 11)
        if self.kind == "overview":
            self.create_oval(4, 4, s - 4, s - 4, fill=c, outline="")
            self.create_oval(8, 8, s - 8, s - 8, fill=COLORS["primary_soft"], outline="")
        elif self.kind == "brand":
            q = 6
            for x1, y1, x2, y2 in ((q, q, q + 6, q), (q, q, q, q + 6),
                                    (s - q, q, s - q - 6, q), (s - q, q, s - q, q + 6),
                                    (q, s - q, q + 6, s - q), (q, s - q, q, s - q - 6),
                                    (s - q, s - q, s - q - 6, s - q), (s - q, s - q, s - q, s - q - 6)):
                self.create_line(x1, y1, x2, y2, fill=c, width=w + 1)
            self.create_oval(s / 2 - 2, s / 2 - 2, s / 2 + 2, s / 2 + 2, fill=COLORS["primary"])
        elif self.kind == "tasks":
            self.create_rectangle(5, 4, s - 5, s - 3, outline=c, width=w)
            self.create_line(9, 9, s - 9, 9, fill=c, width=w)
            self.create_line(9, 14, s - 9, 14, fill=c, width=w)
            self.create_line(9, 19, s - 12, 19, fill=c, width=w)
            self.create_rectangle(s / 2 - 4, 2, s / 2 + 4, 6, fill=COLORS["sidebar"], outline=c, width=w)
        elif self.kind == "accounts":
            # 双人轮廓：两个头像 + 两个肩部，保持与设计图一致的识别度。
            self.create_oval(4, 4, 10, 10, outline=c, width=w)
            self.create_oval(14, 4, 20, 10, outline=c, width=w)
            self.create_line(2, 20, 2, 17, 4, 14, 10, 14, 13, 17, 13, 20,
                             fill=c, width=w, joinstyle="round")
            self.create_line(12, 20, 12, 17, 15, 14, 20, 14, 23, 17, 23, 20,
                             fill=c, width=w, joinstyle="round")
        elif self.kind == "user":
            self.create_oval(s / 2 - 4, 3, s / 2 + 4, 11, outline=c, width=w)
            self.create_line(5, 22, 5, 18, 8, 14, s - 8, 14, s - 5, 18, s - 5, 22,
                             fill=c, width=w, joinstyle="round")
        elif self.kind == "collect":
            self.create_line(7, s - 6, 7, 14, fill=c, width=w + 1)
            self.create_line(s / 2, s - 6, s / 2, 8, fill=c, width=w + 1)
            self.create_line(s - 7, s - 6, s - 7, 4, fill=c, width=w + 1)
        elif self.kind == "warning":
            self.create_polygon(s / 2, 3, s - 4, s - 4, 4, s - 4, outline=c, fill="", width=w)
            self.create_line(s / 2, 9, s / 2, 16, fill=c, width=w)
            self.create_oval(s / 2 - 1, 19, s / 2 + 1, 21, fill=c, outline=c)
        elif self.kind == "logs":
            self.create_rectangle(5, 3, s - 5, s - 3, outline=c, width=w)
            for y in (9, 14, 19):
                self.create_line(9, y, s - 9, y, fill=c, width=w)
        elif self.kind == "settings":
            # 设置页没有额外图片资源，使用矢量齿轮，确保在任意缩放下清晰。
            center = s / 2
            outer = s / 2 - 5
            inner = s / 2 - 2
            self.create_oval(center - outer, center - outer, center + outer,
                             center + outer, outline=c, width=w)
            self.create_oval(center - inner, center - inner, center + inner,
                             center + inner, outline=c, width=w)
            for angle in range(0, 360, 45):
                import math
                rad = math.radians(angle)
                x1 = center + (outer - 1) * math.cos(rad)
                y1 = center + (outer - 1) * math.sin(rad)
                x2 = center + (outer + 3) * math.cos(rad)
                y2 = center + (outer + 3) * math.sin(rad)
                self.create_line(x1, y1, x2, y2, fill=c, width=w,
                                 capstyle="round")
        elif self.kind == "keywords":
            self.create_rectangle(5, 5, s - 5, s - 5, outline=c, width=w)
            self.create_line(9, 10, s - 9, 10, fill=c, width=w)
            self.create_line(9, 15, s - 9, 15, fill=c, width=w)
            self.create_line(9, 20, s - 13, 20, fill=c, width=w)
            self.create_oval(s - 9, s - 9, s - 5, s - 5, fill=c, outline=c)


class NavButton(ctk.CTkFrame):
    def __init__(self, master, key, text, command):
        super().__init__(master, height=48, corner_radius=9, fg_color="transparent")
        self.key = key
        self._command = command
        self._active = None
        self.grid_columnconfigure(1, weight=1)
        # 侧栏图标放大，保持导航项总高度不变，避免影响点击区域和布局。
        self._icon_size = 30
        self._nav_image = _nav_image(key, self._icon_size, NAV_ICON_BLUE)
        self.icon = (ctk.CTkLabel(self, text="", image=self._nav_image,
                                  width=self._icon_size, height=self._icon_size,
                                  fg_color="transparent")
                     if self._nav_image is not None else LineIcon(
                         self, key, color=NAV_ICON_BLUE, size=self._icon_size))
        self.icon.grid(row=0, column=0, padx=(13, 10), pady=9)
        self.label = ctk.CTkLabel(self, text=text, anchor="w", text_color=COLORS["muted"],
                                  font=FONTS["body"])
        self.label.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        for widget in (self, self.icon, self.label):
            widget.bind("<Button-1>", lambda _e: self._command(), add="+")

    def set_active(self, active):
        active = bool(active)
        if self._active == active:
            return
        self._active = active
        active_bg = COLORS["primary_soft"] if active else COLORS["sidebar"]
        self.configure(fg_color=active_bg)
        self.label.configure(text_color=COLORS["text"] if active else COLORS["muted"])
        if hasattr(self.icon, "color"):
            self.icon.color = NAV_ICON_BLUE
            self.icon.configure(bg=active_bg)
            self.icon._draw()
        else:
            self.icon.configure(fg_color=active_bg, image=self._nav_image)

    def configure(self, **kwargs):
        font = kwargs.pop("font", None)
        if font and hasattr(self, "label"):
            self.label.configure(font=font)
        super().configure(**kwargs)


class StatusBadge(ctk.CTkLabel):
    def __init__(self, master, text, status="", **kwargs):
        fg, bg = status_palette(status)
        super().__init__(
            master, text=text, height=26, corner_radius=13,
            fg_color=bg, text_color=fg, font=FONTS["helper"],
            padx=10, **kwargs,
        )


class ModernTable(ctk.CTkFrame):
    """Card-like, scrollable table with selectable rows and cell badges."""

    def __init__(self, master, columns, *, height=300, on_select=None,
                 on_cell_click=None):
        super().__init__(
            master, fg_color=COLORS["surface_2"], corner_radius=RADIUS["card"],
            border_width=1, border_color=COLORS["border"], height=height,
        )
        self.columns = columns
        self.on_select = on_select
        self.on_cell_click = on_cell_click
        self.rows = []
        self.selected_index = None
        self._row_widgets = []
        self._cell_widgets = []
        self._check_vars = {}
        self._row_base_colors = []
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=0, height=44)
        header.grid(row=0, column=0, sticky="ew", padx=1, pady=(1, 0))
        header.grid_propagate(False)
        self._configure_columns(header)
        for col, spec in enumerate(columns):
            label_kwargs = {
                "text": spec[1],
                "anchor": spec[3] if len(spec) > 3 else "w",
                "text_color": COLORS["muted"],
                "font": FONTS["table_bold"],
            }
            fixed_width = self._fixed_column_width(spec)
            if fixed_width is not None:
                label_kwargs["width"] = max(1, fixed_width - 28)
            ctk.CTkLabel(header, **label_kwargs).grid(
                row=0, column=col, sticky="ew", padx=14)

        self.body = ctk.CTkScrollableFrame(
            self, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=COLORS["surface_3"],
            scrollbar_button_hover_color=COLORS["primary"],
        )
        self.body.grid(row=1, column=0, sticky="nsew", padx=1, pady=(0, 1))
        self.body.grid_columnconfigure(0, weight=1)

    def _configure_columns(self, frame):
        for col, spec in enumerate(self.columns):
            # 表头和每条数据行是不同的 Tk 容器；仅设置 weight 会让长文本
            # 在不同容器中各自撑开列宽。使用同一组 minsize，保证所有横向边界一致。
            fixed_width = self._fixed_column_width(spec)
            if fixed_width is not None:
                minsize = fixed_width
            elif spec[2] == 0:
                minsize = 56
            else:
                minsize = 80
            config = {
                "weight": 0 if fixed_width is not None else int(spec[2]),
                "minsize": minsize,
            }
            # 固定列不能再加入 uniform 比例组，否则 Tk 会把零权重的
            # 复选框列重新分配成异常宽度；无固定宽度的旧表格仍保留比例布局。
            if fixed_width is None:
                config["uniform"] = "modern-table"
            frame.grid_columnconfigure(col, **config)

    @staticmethod
    def _fixed_column_width(spec):
        if len(spec) > 6 and isinstance(spec[6], (int, float)):
            return int(spec[6])
        return None

    def set_rows(self, rows):
        next_rows = list(rows)
        # 常见筛选场景已经拥有 50 行控件池。结果身份或数量变化时复用
        # 现有控件，避免销毁并重建几百个 CustomTkinter 控件。
        if self._row_widgets and len(next_rows) <= len(self._row_widgets):
            self._reuse_row_pool(next_rows)
            return
        previous = self.get_selected()
        self.rows = next_rows
        self.selected_index = None
        for child in self.body.winfo_children():
            child.destroy()
        self._row_widgets = []
        self._cell_widgets = []
        self._check_vars = {}
        for index, row in enumerate(self.rows):
            has_wrap = any(len(spec) > 5 and spec[5] == "wrap"
                           for spec in self.columns)
            wrap_specs = [spec for spec in self.columns
                          if len(spec) > 5 and spec[5] == "wrap"]
            line_counts = []
            for spec in wrap_specs:
                width = self._fixed_column_width(spec) or 390
                chars_per_line = max(8, int((width - 28) / 10))
                value_text = str(row.get(spec[0], ""))
                line_counts.append(max(1, (len(value_text) + chars_per_line - 1)
                                      // chars_per_line))
            line_count = min(2, max(line_counts or [1]))
            row_height = min(70, max(48, 22 * line_count + 14)) if has_wrap else 48
            base_color = COLORS["surface_2"] if index % 2 == 0 else COLORS["surface"]
            row_frame = ctk.CTkFrame(
                self.body, fg_color=base_color, corner_radius=10, height=row_height,
            )
            row_frame.grid(row=index, column=0, sticky="ew", padx=8, pady=(5 if index == 0 else 2, 2))
            row_frame.grid_propagate(False)
            self._configure_columns(row_frame)
            cell_widgets = {}
            for col, spec in enumerate(self.columns):
                key = spec[0]
                value = row.get(key, "")
                anchor = spec[3] if len(spec) > 3 else "w"
                status_key = spec[4] if len(spec) > 4 else None
                if key == "select":
                    check_var = tk.BooleanVar(value=False)
                    self._check_vars[index] = check_var
                    widget = ctk.CTkCheckBox(row_frame, text="", width=22, height=22,
                                             checkbox_width=18, checkbox_height=18,
                                             border_width=1, fg_color=COLORS["primary"],
                                             hover_color=COLORS["primary_hover"],
                                             border_color=COLORS["border"], text_color=COLORS["text"],
                                             variable=check_var)
                    widget.grid(row=0, column=col, padx=14)
                elif key == "name":
                    cell = ctk.CTkFrame(row_frame, fg_color="transparent")
                    cell.grid(row=0, column=col, sticky="w", padx=14)
                    ctk.CTkLabel(cell, text=str(value), text_color=COLORS["text_2"],
                                 font=FONTS["table"], anchor="w").pack(anchor="w")
                    if row.get("subname"):
                        ctk.CTkLabel(cell, text=str(row["subname"]), text_color=COLORS["muted"],
                                     font=FONTS["helper"], anchor="w").pack(anchor="w", pady=(1, 0))
                    widget = cell
                elif key == "menu":
                    widget = ctk.CTkLabel(row_frame, text="•••", text_color=COLORS["muted"],
                                         font=("Segoe UI", 16), anchor="center")
                    widget.grid(row=0, column=col, padx=14)
                elif key == "platform":
                    cell = ctk.CTkFrame(row_frame, fg_color="transparent")
                    cell.grid(row=0, column=col, sticky="w", padx=14)
                    PlatformBadge(cell, str(value), compact=True).pack(side="left")
                    ctk.CTkLabel(cell, text=str(value), text_color=COLORS["text_2"],
                                 font=FONTS["table"], anchor="w").pack(side="left", padx=(8, 0))
                    widget = cell
                elif len(spec) > 5 and spec[5] == "link":
                    fixed_width = self._fixed_column_width(spec) or 120
                    target = str(row.get(f"{key}_value") or "").strip()
                    widget = ctk.CTkButton(
                        row_frame, text=str(value), width=max(1, fixed_width - 28),
                        height=28, fg_color="transparent",
                        hover_color=COLORS["primary_soft"],
                        text_color=COLORS["primary"] if target else COLORS["muted"],
                        font=FONTS["table"], anchor="w", corner_radius=6,
                        state="normal" if target else "disabled",
                        command=lambda i=index, k=key: self._cell_click(i, k),
                    )
                    widget.grid(row=0, column=col, sticky="w", padx=14)
                elif status_key:
                    widget = StatusBadge(row_frame, str(value), str(row.get(status_key, "")))
                    widget.grid(row=0, column=col, padx=14, pady=10,
                                sticky="w" if anchor == "w" else "")
                else:
                    color = row.get(f"{key}_color", COLORS["text_2"])
                    wrap = len(spec) > 5 and spec[5] == "wrap"
                    fixed_width = self._fixed_column_width(spec)
                    label_kwargs = {
                        "text": str(value),
                        "anchor": anchor,
                        "text_color": color,
                        "font": FONTS["table"],
                    }
                    if fixed_width is not None:
                        label_kwargs["width"] = max(1, fixed_width - 28)
                    if wrap:
                        label_kwargs.update({
                            "wraplength": max(1, (fixed_width or 390) - 28),
                            "justify": "left",
                            "height": max(1, row_height - 10),
                        })
                    widget = ctk.CTkLabel(row_frame, **label_kwargs)
                    widget.grid(row=0, column=col, sticky="ew", padx=14,
                                pady=(5, 5) if wrap else 0)
                # 链接按钮自己负责选中并回调，避免点击地址时同时打开详情抽屉。
                if not (len(spec) > 5 and spec[5] == "link"):
                    widget.bind("<Button-1>", lambda _e, i=index: self.select(i), add="+")
                cell_widgets[key] = widget
            row_frame.bind("<Button-1>", lambda _e, i=index: self.select(i), add="+")
            self._row_widgets.append(row_frame)
            self._cell_widgets.append(cell_widgets)
            self._row_base_colors.append(base_color)
            if previous and row.get("_identity") == previous.get("_identity"):
                self.selected_index = index
        self._paint_selection()

    @staticmethod
    def _row_height(row, columns):
        """计算一行需要的高度，最多显示两行文本。"""
        has_wrap = any(len(spec) > 5 and spec[5] == "wrap" for spec in columns)
        if not has_wrap:
            return 48
        line_counts = []
        for spec in columns:
            if len(spec) <= 5 or spec[5] != "wrap":
                continue
            width = ModernTable._fixed_column_width(spec) or 390
            chars_per_line = max(8, int((width - 28) / 10))
            value_text = str(row.get(spec[0], ""))
            line_counts.append(max(1, (len(value_text) + chars_per_line - 1)
                                   // chars_per_line))
        line_count = min(2, max(line_counts or [1]))
        return min(70, max(48, 22 * line_count + 14))

    def _reuse_row_pool(self, rows):
        """把新数据写入已有行控件；多余控件隐藏，后续筛选可再次复用。"""
        previous_selected = self.get_selected()
        selected_identity = (str(previous_selected.get("_identity", ""))
                             if previous_selected else None)
        checked_identities = {
            str(self.rows[index].get("_identity", ""))
            for index, var in self._check_vars.items()
            if index < len(self.rows) and var.get()
        }
        next_rows = list(rows)
        self.rows = next_rows
        self.selected_index = None

        for index, row in enumerate(next_rows):
            row_frame = self._row_widgets[index]
            row_frame.grid()
            row_height = self._row_height(row, self.columns)
            base_color = COLORS["surface_2"] if index % 2 == 0 else COLORS["surface"]
            self._row_base_colors[index] = base_color
            row_frame.configure(height=row_height, fg_color=base_color)
            refs = self._cell_widgets[index]
            identity = str(row.get("_identity", ""))

            for spec in self.columns:
                key = spec[0]
                value = row.get(key, "")
                widget = refs.get(key)
                if widget is None:
                    continue
                if key == "select":
                    self._check_vars[index].set(identity in checked_identities)
                elif key == "name":
                    labels = [child for child in widget.winfo_children()
                              if isinstance(child, ctk.CTkLabel)]
                    if labels:
                        labels[0].configure(text=str(value))
                    subname = str(row.get("subname") or "")
                    if len(labels) > 1:
                        labels[1].configure(text=subname)
                    elif subname:
                        ctk.CTkLabel(
                            widget, text=subname, text_color=COLORS["muted"],
                            font=FONTS["helper"], anchor="w",
                        ).pack(anchor="w", pady=(1, 0))
                elif key == "platform":
                    children = widget.winfo_children()
                    if children and hasattr(children[0], "set_platform"):
                        children[0].set_platform(str(value))
                    if len(children) > 1:
                        children[1].configure(text=str(value))
                elif len(spec) > 5 and spec[5] == "link":
                    target = str(row.get(f"{key}_value") or "").strip()
                    widget.configure(
                        text=str(value),
                        text_color=COLORS["primary"] if target else COLORS["muted"],
                        state="normal" if target else "disabled",
                    )
                elif len(spec) > 4 and spec[4]:
                    fg, bg = status_palette(str(row.get(spec[4], "")))
                    widget.configure(text=str(value), text_color=fg, fg_color=bg)
                elif key != "menu":
                    kwargs = {
                        "text": str(value),
                        "text_color": row.get(f"{key}_color", COLORS["text_2"]),
                    }
                    if len(spec) > 5 and spec[5] == "wrap":
                        kwargs["height"] = max(1, row_height - 10)
                    widget.configure(**kwargs)

            if selected_identity is not None and identity == selected_identity:
                self.selected_index = index

        for index in range(len(next_rows), len(self._row_widgets)):
            self._row_widgets[index].grid_remove()
            if index in self._check_vars:
                self._check_vars[index].set(False)

        self._paint_selection()
        try:
            self.body._parent_canvas.yview_moveto(0)
        except Exception:
            pass

    def update_rows(self, rows):
        """复用已有行控件；控件池不足时才返回 False 触发扩容重建。"""
        next_rows = list(rows)
        if len(next_rows) > len(self._row_widgets):
            return False
        if not self._row_widgets and next_rows:
            return False
        if not self._row_widgets:
            self.rows = []
            return True
        self._reuse_row_pool(next_rows)
        return True

    def select(self, index):
        self.selected_index = index
        self._paint_selection()
        if self.on_select:
            self.on_select(self.get_selected())

    def _cell_click(self, index, key):
        """点击特殊单元格时先同步行选中，再交给页面处理。"""
        self.select(index)
        if self.on_cell_click and 0 <= index < len(self.rows):
            self.on_cell_click(self.rows[index], key)

    def _paint_selection(self):
        for index, frame in enumerate(self._row_widgets[:len(self.rows)]):
            selected = index == self.selected_index
            base_color = self._row_base_colors[index] if index < len(self._row_base_colors) else COLORS["surface_2"]
            frame.configure(
                fg_color=COLORS["primary_soft"] if selected else base_color,
                border_width=1 if selected else 0,
                border_color=COLORS["primary"] if selected else COLORS["surface_2"],
            )

    def get_selected(self):
        if self.selected_index is None or self.selected_index >= len(self.rows):
            return None
        return self.rows[self.selected_index]

    def get_checked(self):
        """返回所有被勾选的行（select 列复选框）。"""
        checked = []
        for index, var in self._check_vars.items():
            if var.get() and index < len(self.rows):
                checked.append(self.rows[index])
        return checked


class MetricCard(ctk.CTkFrame):
    def __init__(self, master, title, value, accent, icon="•"):
        super().__init__(master, fg_color=COLORS["surface_2"], corner_radius=RADIUS["card"],
                         border_width=1, border_color=COLORS["border"])
        icon_box = ctk.CTkFrame(self, width=42, height=42, corner_radius=21,
                               fg_color=COLORS["surface_3"])
        icon_box.pack(anchor="w", padx=18, pady=(16, 8))
        icon_box.pack_propagate(False)
        icon_kind = {"▶": "tasks", "↗": "collect", "◇": "warning", "!": "user"}.get(icon, "overview")
        LineIcon(icon_box, icon_kind, color=accent, size=28, bg=COLORS["surface_3"]).pack(expand=True)
        self.value_label = ctk.CTkLabel(self, text=str(value), text_color=COLORS["text"],
                                        font=FONTS["stat_value"], anchor="w")
        self.value_label.pack(fill="x", padx=18)
        ctk.CTkLabel(self, text=title, text_color=COLORS["muted"],
                     font=FONTS["helper"], anchor="w").pack(fill="x", padx=18, pady=(3, 16))

    def update_value(self, value):
        self.value_label.configure(text=str(value))
