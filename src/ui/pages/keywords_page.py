# -*- coding: utf-8 -*-
"""关键词组管理页：编辑预制搜索词、逐词预览和版本化保存。"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox
from typing import Callable

import customtkinter as ctk

from ..theme import COLORS, FONTS, RADIUS, SPACE


PLATFORM_LABELS = {"": "全部平台", "douyin": "抖音", "xhs": "小红书",
                   "weibo": "微博", "bilibili": "B站"}
PLATFORM_CODES = {label: code for code, label in PLATFORM_LABELS.items()}
TERM_LABELS = (
    ("core", "核心词", "例如：京东洗衣、互联网洗衣、干洗店；每个词单独搜索"),
    ("synonym", "同义词", "可继续添加独立搜索词，例如：洗鞋店、洗衣加盟"),
    ("region", "地域词", "例如：郑州、洛阳；留空表示不限地区"),
    ("exclude", "排除词", "例如：招聘、求职、转让"),
)


def _lines(text: str) -> list[str]:
    import re
    return [item.strip() for item in re.split(r"[、，,；;\n\r]+", str(text or "")) if item.strip()]


def _preview(terms: dict[str, list[str]]) -> list[dict]:
    cores, seen = [], set()
    for value in terms.get("core", []) + terms.get("synonym", []):
        if value not in seen:
            seen.add(value)
            cores.append(value)
    regions = terms.get("region", []) or [""]
    excludes = list(dict.fromkeys(terms.get("exclude", [])))
    rows = []
    queries = set()
    for core in cores:
        for region in regions:
            query = " ".join(part for part in (core, region) if part).strip()
            if query and query not in queries:
                queries.add(query)
                rows.append({"query": query, "exclude_terms": excludes})
    return rows


class KeywordsPage(ctk.CTkFrame):
    def __init__(self, master, db_path: str, *, on_log: Callable | None = None,
                 on_changed: Callable | None = None):
        super().__init__(master, fg_color=COLORS["window"])
        self._db_path = db_path
        self._on_log = on_log
        self._on_changed = on_changed
        self._groups = []
        self._selected_id = None
        self._inflight = False
        self._name = None
        # 下拉框始终保存中文显示值；保存时再转换为数据库平台代码，
        # 避免 CTkComboBox 绑定变量后把 douyin/xhs 直接显示给用户。
        self._platform = tk.StringVar(value=PLATFORM_LABELS[""])
        self._fields = {}
        self._status = None
        self._group_list = None
        self._preview_box = None
        self._build()
        self.reload()

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=SPACE["xl"],
                    pady=(SPACE["lg"], SPACE["md"])
        )
        top = ctk.CTkFrame(header, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkLabel(top, text="关键词组", text_color=COLORS["text"],
                     font=FONTS["page_title"]).pack(side="left")
        ctk.CTkButton(top, text="新建关键词组", width=130, height=34,
                      fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                      font=FONTS["helper"], command=self._new).pack(side="right")
        ctk.CTkLabel(header, text="维护可复用的预制搜索词；任务执行时会按顺序逐个搜索，每个关键词分别达到目标数量后再进入详情采集。",
                     text_color=COLORS["muted"], font=FONTS["page_subtitle"]).pack(anchor="w", pady=(2, 0))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=SPACE["xl"], pady=(0, SPACE["lg"]))
        body.grid_columnconfigure(0, weight=1, uniform="keyword_col")
        body.grid_columnconfigure(1, weight=2, uniform="keyword_col")
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, fg_color=COLORS["surface_2"], corner_radius=RADIUS["card"],
                            border_width=1, border_color=COLORS["border"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, SPACE["md"]))
        left.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(left, text="已保存关键词组", text_color=COLORS["text"],
                     font=FONTS["section_title"]).grid(row=0, column=0, sticky="w",
                                                        padx=SPACE["lg"], pady=SPACE["lg"])
        self._group_list = ctk.CTkScrollableFrame(left, fg_color="transparent",
                                                   scrollbar_button_color=COLORS["surface_3"])
        self._group_list.grid(row=1, column=0, sticky="nsew", padx=SPACE["sm"], pady=(0, SPACE["sm"]))

        right = ctk.CTkScrollableFrame(body, fg_color=COLORS["surface_2"], corner_radius=RADIUS["card"],
                                       border_width=1, border_color=COLORS["border"],
                                       scrollbar_button_color=COLORS["surface_3"])
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(1, weight=1)
        right.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(right, text="关键词组编辑", text_color=COLORS["text"],
                     font=FONTS["section_title"]).grid(row=0, column=0, columnspan=2,
                                                        sticky="w", padx=SPACE["lg"], pady=(SPACE["lg"], SPACE["md"]))
        ctk.CTkLabel(right, text="名称", text_color=COLORS["text_2"], font=FONTS["helper"]).grid(
            row=1, column=0, sticky="w", padx=(SPACE["lg"], SPACE["sm"]), pady=SPACE["sm"])
        self._name = ctk.CTkEntry(right, height=36, fg_color=COLORS["window"],
                                  border_color=COLORS["border"], font=FONTS["body"])
        self._name.grid(row=1, column=1, sticky="ew", padx=(SPACE["sm"], SPACE["lg"]), pady=SPACE["sm"])
        ctk.CTkLabel(right, text="适用平台", text_color=COLORS["text_2"], font=FONTS["helper"]).grid(
            row=2, column=0, sticky="w", padx=(SPACE["lg"], SPACE["sm"]), pady=SPACE["sm"])
        self._platform_box = ctk.CTkComboBox(right, variable=self._platform,
                                             values=list(PLATFORM_LABELS.values()), height=36,
                                             fg_color=COLORS["window"], border_color=COLORS["border"],
                                             button_color=COLORS["surface_3"], font=FONTS["body"])
        self._platform_box.grid(row=2, column=1, sticky="ew", padx=(SPACE["sm"], SPACE["lg"]), pady=SPACE["sm"])
        self._platform_box.configure(command=self._platform_changed)

        for row, (key, label, hint) in enumerate(TERM_LABELS, start=3):
            ctk.CTkLabel(right, text=label, text_color=COLORS["text_2"],
                         font=FONTS["helper"]).grid(row=row, column=0, sticky="nw",
                                                     padx=(SPACE["lg"], SPACE["sm"]), pady=SPACE["sm"])
            box = tk.Text(right, height=3, wrap="word", bg=COLORS["window"], fg=COLORS["text"],
                          insertbackground=COLORS["text"], relief="flat", bd=0,
                          font=FONTS["body"], padx=8, pady=6)
            box.grid(row=row, column=1, sticky="ew", padx=(SPACE["sm"], SPACE["lg"]), pady=SPACE["sm"])
            ctk.CTkLabel(right, text=hint, text_color=COLORS["subtle"], font=FONTS["helper"]).grid(
                row=row, column=2, sticky="ew", padx=(0, SPACE["lg"]), pady=SPACE["sm"])
            # 右侧提示在窄弹窗内自动换行，避免被裁掉。
            right.grid_slaves(row=row, column=2)[0].configure(wraplength=240, justify="left")
            self._fields[key] = box

        self._status = ctk.CTkLabel(right, text="", text_color=COLORS["muted"],
                                    font=FONTS["helper"], anchor="w")
        self._status.grid(row=7, column=0, columnspan=3, sticky="w", padx=SPACE["lg"], pady=(SPACE["sm"], 0))
        actions = ctk.CTkFrame(right, fg_color="transparent")
        actions.grid(row=8, column=0, columnspan=3, sticky="ew", padx=SPACE["lg"], pady=SPACE["md"])
        ctk.CTkButton(actions, text="保存关键词组", width=125, height=34,
                      fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                      font=FONTS["helper"], command=self._save).pack(side="left")
        ctk.CTkButton(actions, text="删除", width=75, height=34,
                      fg_color=COLORS["danger_bg"], hover_color=COLORS["danger"],
                      text_color=COLORS["text"], font=FONTS["helper"], command=self._delete).pack(side="left", padx=SPACE["sm"])
        ctk.CTkButton(actions, text="预览组合", width=100, height=34,
                      fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                      text_color=COLORS["text"], font=FONTS["helper"], command=self._render_preview).pack(side="left")
        ctk.CTkLabel(right, text="组合预览", text_color=COLORS["text_2"],
                     font=FONTS["card_title"]).grid(row=9, column=0, columnspan=3,
                                                    sticky="w", padx=SPACE["lg"], pady=(SPACE["sm"], 0))
        self._preview_box = tk.Text(right, height=8, state="disabled", wrap="word",
                                    bg=COLORS["window"], fg=COLORS["text_2"], relief="flat", bd=0,
                                    font=FONTS["helper"], padx=8, pady=8)
        self._preview_box.grid(row=10, column=0, columnspan=3, sticky="ew", padx=SPACE["lg"], pady=(SPACE["sm"], SPACE["lg"]))

    def _platform_changed(self, label):
        self._platform.set(PLATFORM_LABELS.get(PLATFORM_CODES.get(label, ""), label))

    def _set_status(self, text, color=None):
        if self._status is not None:
            self._status.configure(text=text, text_color=color or COLORS["muted"])

    def _new(self):
        self._selected_id = None
        self._set_entry(self._name, "")
        self._platform.set(PLATFORM_LABELS[""])
        self._platform_box.set(PLATFORM_LABELS[""])
        for box in self._fields.values():
            box.delete("1.0", "end")
        self._render_preview()
        self._set_status("正在编辑新关键词组", COLORS["info"])

    @staticmethod
    def _set_entry(entry, value):
        entry.delete(0, "end")
        entry.insert(0, str(value or ""))

    def reload(self):
        if self._inflight:
            return
        self._inflight = True
        threading.Thread(target=self._load_worker, name="keywords-load", daemon=True).start()

    def _load_worker(self):
        conn = None
        error = None
        groups = []
        try:
            import db
            from operations.keywords import KeywordGroupStore
            conn = db.init_db(self._db_path, check_same_thread=False)
            store = KeywordGroupStore(conn)
            groups = [store.get(item["id"]) for item in store.list_groups()]
        except Exception as exc:  # noqa: BLE001
            error = exc
        finally:
            if conn is not None:
                conn.close()
        try:
            self.after(0, self._apply_groups, groups, error)
        except (tk.TclError, RuntimeError):
            pass

    def _apply_groups(self, groups, error=None):
        self._inflight = False
        if error:
            self._set_status(f"加载失败：{type(error).__name__}: {error}", COLORS["danger"])
            return
        self._groups = groups
        if callable(self._on_changed):
            self._on_changed()
        for child in self._group_list.winfo_children():
            child.destroy()
        for group in groups:
            row = ctk.CTkFrame(self._group_list, fg_color=COLORS["surface"], corner_radius=RADIUS["small"])
            row.pack(fill="x", padx=SPACE["xs"], pady=SPACE["xs"])
            platform = PLATFORM_LABELS.get(group.get("platform") or "", "全部平台")
            ctk.CTkLabel(row, text=group.get("name") or "未命名", text_color=COLORS["text"],
                         font=FONTS["body_bold"], anchor="w").pack(fill="x", padx=SPACE["sm"], pady=(SPACE["sm"], 0))
            ctk.CTkLabel(row, text=f"{platform} · v{group.get('version', 1)}",
                         text_color=COLORS["muted"], font=FONTS["helper"], anchor="w").pack(
                             fill="x", padx=SPACE["sm"], pady=(0, SPACE["sm"]))
            for widget in (row, *row.winfo_children()):
                widget.bind("<Button-1>", lambda _event, gid=group["id"]: self._select(gid), add="+")
        if groups and self._selected_id not in {item["id"] for item in groups}:
            self._select(groups[0]["id"])
        elif not groups:
            self._new()

    def _select(self, group_id):
        group = next((item for item in self._groups if item["id"] == group_id), None)
        if group is None:
            return
        self._selected_id = int(group_id)
        self._set_entry(self._name, group.get("name"))
        platform = group.get("platform") or ""
        self._platform.set(PLATFORM_LABELS.get(platform, PLATFORM_LABELS[""]))
        self._platform_box.set(PLATFORM_LABELS.get(platform, "全部平台"))
        terms = group.get("terms") or {key: [] for key, _, _ in TERM_LABELS}
        for key, box in self._fields.items():
            box.delete("1.0", "end")
            box.insert("1.0", "\n".join(terms.get(key, [])))
        self._render_preview()
        self._set_status(f"已选择：{group.get('name')}（v{group.get('version', 1)}）", COLORS["muted"])

    def _collect(self):
        return {key: _lines(box.get("1.0", "end")) for key, box in self._fields.items()}

    def _save(self):
        name = self._name.get().strip()
        if not name:
            self._set_status("请输入关键词组名称", COLORS["danger"])
            return
        terms = self._collect()
        if not terms["core"] and not terms["synonym"]:
            self._set_status("至少填写一个核心词或同义词", COLORS["danger"])
            return
        if self._inflight:
            return
        self._inflight = True
        group_id = self._selected_id
        platform = PLATFORM_CODES.get(self._platform.get(), "") or None
        threading.Thread(target=self._save_worker, args=(group_id, name, platform, terms),
                         name="keywords-save", daemon=True).start()

    def _save_worker(self, group_id, name, platform, terms):
        conn = None
        error = None
        saved_id = group_id
        try:
            import db
            from operations.keywords import KeywordGroupStore
            conn = db.init_db(self._db_path, check_same_thread=False)
            store = KeywordGroupStore(conn)
            if group_id is None:
                saved_id = store.create_group(name, platform)
            else:
                # 同一次保存由 set_terms 统一推进一个版本，避免名称和词条
                # 同时修改时版本号跳两次。
                store.update_group(group_id, name, platform, bump_version=False)
            store.set_terms(saved_id, terms)
        except Exception as exc:  # noqa: BLE001
            error = exc
        finally:
            if conn is not None:
                conn.close()
        try:
            self.after(0, self._save_done, saved_id, error)
        except (tk.TclError, RuntimeError):
            pass

    def _save_done(self, group_id, error):
        self._inflight = False
        if error:
            self._set_status(f"保存失败：{type(error).__name__}: {error}", COLORS["danger"])
            return
        self._selected_id = int(group_id)
        self._set_status("已保存，关键词组版本已更新", COLORS["success"])
        if callable(self._on_changed):
            self._on_changed()
        self.reload()

    def _delete(self):
        if self._selected_id is None or self._inflight:
            return
        if not messagebox.askyesno("删除关键词组", "确定删除当前关键词组及其词条吗？", parent=self):
            return
        group_id = self._selected_id
        self._inflight = True
        threading.Thread(target=self._delete_worker, args=(group_id,),
                         name="keywords-delete", daemon=True).start()

    def _delete_worker(self, group_id):
        conn = None
        error = None
        try:
            import db
            from operations.keywords import KeywordGroupStore
            conn = db.init_db(self._db_path, check_same_thread=False)
            KeywordGroupStore(conn).delete_group(group_id)
        except Exception as exc:  # noqa: BLE001
            error = exc
        finally:
            if conn is not None:
                conn.close()
        try:
            self.after(0, self._delete_done, error)
        except (tk.TclError, RuntimeError):
            pass

    def _delete_done(self, error):
        self._inflight = False
        if error:
            self._set_status(f"删除失败：{type(error).__name__}: {error}", COLORS["danger"])
            return
        self._selected_id = None
        self._set_status("已删除关键词组", COLORS["success"])
        if callable(self._on_changed):
            self._on_changed()
        self.reload()

    def _render_preview(self):
        rows = _preview(self._collect())
        lines = [f"{idx}. {row['query']}" for idx, row in enumerate(rows, 1)]
        exclude = sorted({term for row in rows for term in row["exclude_terms"]})
        if exclude:
            lines.append("\n排除词：" + "、".join(exclude))
        if not lines:
            lines = ["填写核心词或同义词后，点击预览组合。"]
        self._preview_box.configure(state="normal")
        self._preview_box.delete("1.0", "end")
        self._preview_box.insert("1.0", "\n".join(lines))
        self._preview_box.configure(state="disabled")
