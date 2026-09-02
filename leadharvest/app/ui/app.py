# -*- coding: utf-8 -*-
"""app.py — LeadHarvest 桌面 GUI（新架构轻量版）。

基于新架构（scheduler/database/platform）的 Tkinter 界面：
- 任务创建（平台/关键词/目标数/模式）
- 任务列表与状态监控
- 平台注册表展示

运行：
  python -m app.ui.app --db data/leadharvest.db
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

# 项目根路径
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.database import Database  # noqa: E402
from app.scheduler import Scheduler  # noqa: E402
from app.platform import PLATFORM_REGISTRY  # noqa: E402
from app.ui.theme import COLORS, FONTS, PLATFORM_CN, TASK_STATUS_CN  # noqa: E402


class LeadHarvestApp:
    """主应用窗口。"""

    def __init__(self, root: tk.Tk, scheduler: Scheduler):
        self.root = root
        self.scheduler = scheduler
        self._polling = False
        self._last_task_rows = {}

        root.title("LeadHarvest — 多平台线索采集")
        root.geometry("1000x680")
        root.configure(bg=COLORS["bg"])
        self._build_ui()
        self._refresh_task_list()
        self._start_poller()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self):
        # 顶部标题栏
        header = tk.Frame(self.root, bg=COLORS["panel"], height=52)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="LeadHarvest", font=FONTS["title"],
                 bg=COLORS["panel"], fg=COLORS["accent"]).pack(side="left", padx=16)
        tk.Label(header, text="多平台线索采集工作台",
                 font=FONTS["body"], bg=COLORS["panel"], fg=COLORS["muted"]).pack(side="left", padx=8)

        # 主体：左任务创建 / 右任务列表
        main = tk.Frame(self.root, bg=COLORS["bg"])
        main.pack(fill="both", expand=True, padx=12, pady=12)

        left = tk.Frame(main, bg=COLORS["card"], width=340)
        left.pack(side="left", fill="y", padx=(0, 12))
        left.pack_propagate(False)
        self._build_create_panel(left)

        right = tk.Frame(main, bg=COLORS["bg"])
        right.pack(side="left", fill="both", expand=True)
        self._build_task_panel(right)

    def _build_create_panel(self, parent):
        tk.Label(parent, text="创建采集任务", font=FONTS["section"],
                 bg=COLORS["card"], fg=COLORS["text"]).pack(anchor="w", padx=16, pady=(16, 8))

        # 平台
        tk.Label(parent, text="平台", font=FONTS["small"],
                 bg=COLORS["card"], fg=COLORS["muted"]).pack(anchor="w", padx=16)
        self.platform_var = tk.StringVar(value="douyin")
        platform_box = ttk.Combobox(parent, textvariable=self.platform_var,
                                    values=list(PLATFORM_CN.values()), state="readonly")
        platform_box.pack(fill="x", padx=16, pady=4)

        # 关键词
        tk.Label(parent, text="关键词", font=FONTS["small"],
                 bg=COLORS["card"], fg=COLORS["muted"]).pack(anchor="w", padx=16)
        self.keyword_var = tk.StringVar()
        ttk.Entry(parent, textvariable=self.keyword_var).pack(fill="x", padx=16, pady=4)

        # 目标数
        tk.Label(parent, text="目标作品数", font=FONTS["small"],
                 bg=COLORS["card"], fg=COLORS["muted"]).pack(anchor="w", padx=16)
        self.target_var = tk.StringVar(value="20")
        ttk.Entry(parent, textvariable=self.target_var).pack(fill="x", padx=16, pady=4)

        # 模式
        tk.Label(parent, text="采集模式", font=FONTS["small"],
                 bg=COLORS["card"], fg=COLORS["muted"]).pack(anchor="w", padx=16)
        self.mode_var = tk.StringVar(value="standard")
        mode_box = ttk.Combobox(parent, textvariable=self.mode_var,
                                values=["fast", "standard", "deep"], state="readonly")
        mode_box.pack(fill="x", padx=16, pady=4)

        # 创建按钮
        tk.Button(parent, text="创建任务", command=self._on_create_task,
                  bg=COLORS["accent"], fg="white", font=FONTS["body"],
                  relief="flat", cursor="hand2").pack(fill="x", padx=16, pady=(20, 8))

        # 状态提示
        self.status_label = tk.Label(parent, text="就绪", font=FONTS["small"],
                                     bg=COLORS["card"], fg=COLORS["muted"])
        self.status_label.pack(anchor="w", padx=16)

        # 已注册平台
        tk.Label(parent, text=f"已注册平台: {len(PLATFORM_REGISTRY)} 个",
                 font=FONTS["small"], bg=COLORS["card"], fg=COLORS["muted"]).pack(anchor="w", padx=16, pady=(24, 0))

    def _build_task_panel(self, parent):
        tk.Label(parent, text="任务列表", font=FONTS["section"],
                 bg=COLORS["bg"], fg=COLORS["text"]).pack(anchor="w", pady=(0, 8))
        cols = ("id", "platform", "keyword", "status", "videos", "comments")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings", height=20)
        headings = {"id": "ID", "platform": "平台", "keyword": "关键词",
                    "status": "状态", "videos": "视频", "comments": "评论"}
        widths = {"id": 50, "platform": 70, "keyword": 200, "status": 100, "videos": 60, "comments": 60}
        for col in cols:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w")
        self.tree.pack(fill="both", expand=True)

        # 操作按钮
        btns = tk.Frame(parent, bg=COLORS["bg"])
        btns.pack(fill="x", pady=(8, 0))
        tk.Button(btns, text="暂停", command=lambda: self._on_task_action("pause"),
                  bg=COLORS["panel"], fg=COLORS["text"], relief="flat").pack(side="left", padx=4)
        tk.Button(btns, text="恢复", command=lambda: self._on_task_action("resume"),
                  bg=COLORS["panel"], fg=COLORS["text"], relief="flat").pack(side="left", padx=4)
        tk.Button(btns, text="停止", command=lambda: self._on_task_action("stop"),
                  bg=COLORS["panel"], fg=COLORS["danger"], relief="flat").pack(side="left", padx=4)
        tk.Button(btns, text="刷新", command=self._refresh_task_list,
                  bg=COLORS["panel"], fg=COLORS["text"], relief="flat").pack(side="right", padx=4)

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _on_create_task(self):
        platform_cn = self.platform_var.get()
        platform = {v: k for k, v in PLATFORM_CN.items()}.get(platform_cn, "douyin")
        keyword = self.keyword_var.get().strip()
        if not keyword:
            messagebox.showwarning("提示", "请输入关键词")
            return
        try:
            target = int(self.target_var.get())
        except ValueError:
            target = 20
        try:
            task_id = self.scheduler.create_task(
                keyword=keyword, platform=platform,
                target_count=target, collect_mode=self.mode_var.get())
            self.scheduler.start(task_id)
            self.status_label.config(text=f"任务 {task_id} 已创建并启动")
            self._refresh_task_list()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("错误", str(exc))

    def _on_task_action(self, action: str):
        sel = self.tree.selection()
        if not sel:
            return
        task_id = int(self.tree.item(sel[0], "values")[0])
        if action == "pause":
            self.scheduler.pause_task(task_id)
        elif action == "resume":
            self.scheduler.resume_task(task_id)
        elif action == "stop":
            self.scheduler.stop_task(task_id)
        self._refresh_task_list()

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------
    def _start_poller(self):
        self._polling = True
        self._poll()

    def _poll(self):
        if not self._polling:
            return
        self._refresh_task_list()
        self.root.after(3000, self._poll)

    def _refresh_task_list(self):
        try:
            report = self.scheduler.status_report()
        except Exception:
            return
        running = set(report.get("running", []))
        for row in self.tree.get_children():
            self.tree.delete(row)
        for task in report.get("tasks", []):
            tid = task["id"]
            status = task["status"]
            display = TASK_STATUS_CN.get(status, status)
            if tid in running:
                display += " ▶"
            platform_cn = PLATFORM_CN.get(task.get("platform", ""), task.get("platform", ""))
            self.tree.insert("", "end", values=(
                tid, platform_cn, task.get("keyword", ""),
                display, "", "",
            ))


def main():
    parser = argparse.ArgumentParser(description="LeadHarvest GUI")
    parser.add_argument("--db", default="")
    args = parser.parse_args()
    if not args.db:
        args.db = os.path.join(_ROOT, "data", "leadharvest.db")

    db = Database(args.db)
    db.connect()
    scheduler = Scheduler(db)

    root = tk.Tk()
    app = LeadHarvestApp(root, scheduler)
    root.mainloop()


if __name__ == "__main__":
    main()
