# -*- coding: utf-8 -*-
"""工作台内统一弹窗。

系统 messagebox 在部分 Windows/DPI/多屏组合下会脱离主窗口显示。这里使用
应用自己的 Toplevel，并以真实主窗口坐标计算位置，保证确认弹窗留在软件界面内。
"""

from __future__ import annotations

import tkinter as tk

try:
    import customtkinter as ctk
except ImportError:  # pragma: no cover - 运行环境通常已安装
    ctk = None

from ui.theme import COLORS, FONTS, RADIUS, SPACE


def _center_in_owner(dialog, owner, width: int, height: int) -> None:
    owner.update_idletasks()
    dialog.update_idletasks()
    owner_x, owner_y = owner.winfo_rootx(), owner.winfo_rooty()
    owner_w = max(width, owner.winfo_width())
    owner_h = max(height, owner.winfo_height())
    x = owner_x + max(0, (owner_w - width) // 2)
    y = owner_y + max(0, (owner_h - height) // 3)
    screen_w, screen_h = owner.winfo_screenwidth(), owner.winfo_screenheight()
    x = max(0, min(x, max(0, screen_w - width)))
    y = max(0, min(y, max(0, screen_h - height)))
    dialog.geometry(f"{width}x{height}+{x}+{y}")


def ask_centered_confirm(owner, title: str, message: str,
                         *, danger: bool = False) -> bool:
    """显示留在主窗口范围内的确认弹窗，并返回用户选择。"""
    dialog = ctk.CTkToplevel(owner) if ctk is not None else tk.Toplevel(owner)
    dialog.title(title)
    dialog.resizable(False, False)
    dialog.transient(owner)
    width, height = 540, 250
    _center_in_owner(dialog, owner, width, height)

    result = {"confirmed": False}

    if ctk is not None:
        body = ctk.CTkFrame(dialog, fg_color=COLORS["surface"], corner_radius=0)
        body.pack(fill="both", expand=True)
        ctk.CTkLabel(
            body, text=title, text_color=COLORS["text"],
            font=FONTS["section_title"], anchor="w",
        ).pack(fill="x", padx=SPACE["xl"], pady=(SPACE["lg"], SPACE["sm"]))
        ctk.CTkLabel(
            body, text=message, text_color=COLORS["text_2"],
            font=FONTS["body"], justify="left", anchor="w", wraplength=480,
        ).pack(fill="x", padx=SPACE["xl"], pady=(0, SPACE["lg"]))
        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.pack(fill="x", padx=SPACE["xl"], pady=(0, SPACE["lg"]))

        def finish(confirmed: bool):
            result["confirmed"] = bool(confirmed)
            dialog.destroy()

        ctk.CTkButton(
            actions, text="取消", width=100, height=34,
            fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
            text_color=COLORS["text_2"], corner_radius=RADIUS["control"],
            command=lambda: finish(False),
        ).pack(side="right", padx=(SPACE["sm"], 0))
        ctk.CTkButton(
            actions, text="确认发送", width=120, height=34,
            fg_color=COLORS["danger"] if danger else COLORS["primary"],
            hover_color=COLORS["danger_bg"] if danger else COLORS["primary_hover"],
            text_color="#ffffff", corner_radius=RADIUS["control"],
            command=lambda: finish(True),
        ).pack(side="right")
    else:
        body = tk.Frame(dialog, bg="#20242d")
        body.pack(fill="both", expand=True)
        tk.Label(body, text=title, bg="#20242d", fg="#ffffff",
                 font=("Microsoft YaHei UI", 12, "bold"), anchor="w").pack(
                     fill="x", padx=24, pady=(22, 10))
        tk.Label(body, text=message, bg="#20242d", fg="#d8dde8",
                 justify="left", anchor="w", wraplength=480).pack(
                     fill="x", padx=24, pady=(0, 24))
        actions = tk.Frame(body, bg="#20242d")
        actions.pack(fill="x", padx=24, pady=(0, 20))

        def finish(confirmed: bool):
            result["confirmed"] = bool(confirmed)
            dialog.destroy()

        tk.Button(actions, text="取消", width=10, command=lambda: finish(False)).pack(
            side="right", padx=(8, 0))
        tk.Button(actions, text="确认发送", width=12, command=lambda: finish(True)).pack(
            side="right")

    # 内容控件完成布局后，CTk 可能会重新计算 Toplevel 的默认 geometry；
    # 此时再定位一次，并在下一帧做最后校正，避免窗口回到屏幕左上角。
    dialog.update_idletasks()
    _center_in_owner(dialog, owner, width, height)

    def reposition_after_layout():
        try:
            if dialog.winfo_exists():
                _center_in_owner(dialog, owner, width, height)
        except tk.TclError:
            pass

    dialog.after_idle(reposition_after_layout)
    dialog.after(80, reposition_after_layout)
    dialog.protocol("WM_DELETE_WINDOW", lambda: (result.__setitem__("confirmed", False), dialog.destroy()))
    dialog.grab_set()
    dialog.focus_force()
    owner.wait_window(dialog)
    return bool(result["confirmed"])
