# -*- coding: utf-8 -*-
"""右侧详情抽屉（技术方案 Agent E 组件 / 8.3）。

用 ``place(relx=1, rely=0, anchor="ne")`` 相对**父容器**停靠，绝不使用固定
屏幕坐标（验收红线 9.6）。内容按区块展示线索详情：公开资料、地域判定与证据、
评论时间线、意向原因、分配历史、审计记录。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import customtkinter as ctk

from ..theme import COLORS, FONTS, RADIUS, SPACE


class DetailDrawer(ctk.CTkFrame):
    def __init__(self, master, *, width: int = 380):
        super().__init__(master, width=width, fg_color=COLORS["surface_2"],
                         corner_radius=0, border_width=1, border_color=COLORS["border"])
        self._width = width
        self._body = None
        self.grid_propagate(False)

    # ------------------------------------------------------------------
    def open(self, detail: Dict[str, Any]):
        """在父容器右侧停靠并填充内容。detail 为 LeadDetail 字典。"""
        self.place(relx=1.0, rely=0.0, anchor="ne", relheight=1.0,
                   width=self._width)
        self.lift()
        self._render(detail)

    def close(self):
        self.place_forget()

    def is_open(self) -> bool:
        try:
            return bool(self.winfo_manager())
        except Exception:
            return False

    # ------------------------------------------------------------------
    def _render(self, detail: Dict[str, Any]):
        if self._body is not None:
            self._body.destroy()
        lead = detail.get("lead") or {}
        body = ctk.CTkScrollableFrame(self, fg_color="transparent",
                                      scrollbar_button_color=COLORS["surface_3"])
        body.pack(fill="both", expand=True)
        self._body = body

        # 头部
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x", padx=SPACE["lg"], pady=(SPACE["lg"], SPACE["sm"]))
        ctk.CTkLabel(head, text=lead.get("nickname") or "未知用户",
                     text_color=COLORS["text"], font=FONTS["card_title"],
                     anchor="w").pack(fill="x")
        ctk.CTkLabel(head, text=f"{lead.get('platform') or ''} · {lead.get('id')}",
                     text_color=COLORS["muted"], font=FONTS["helper"],
                     anchor="w").pack(fill="x", pady=(2, 0))
        ctk.CTkButton(head, text="关闭", width=56, height=28,
                      fg_color=COLORS["surface_3"], hover_color=COLORS["surface_hover"],
                      text_color=COLORS["text_2"], corner_radius=7,
                      command=self.close).pack(anchor="e", pady=(SPACE["sm"], 0))

        # 公开资料
        self._section(body, "用户公开资料")
        self._kv(body, "平台用户 ID", lead.get("platform_user_id"))
        self._kv(body, "主页", lead.get("profile_url"))
        self._kv(body, "首次出现", lead.get("first_seen_at"))
        self._kv(body, "最近互动", lead.get("last_interaction_at"))

        # 地域判定
        self._section(body, "地域判定")
        self._kv(body, "省份", lead.get("region_province"))
        self._kv(body, "城市", lead.get("region_city"))
        self._kv(body, "来源", lead.get("region_source"))
        self._kv(body, "可信度", lead.get("region_confidence"))
        self._kv(body, "人工修正", lead.get("region_manual_province"))

        # 意向
        self._section(body, "意向判定")
        self._kv(body, "等级", lead.get("intent_level"))
        self._kv(body, "分数", lead.get("intent_score"))
        reasons = lead.get("intent_reasons") or []
        if reasons:
            if isinstance(reasons, str):
                import json

                try:
                    reasons = json.loads(reasons)
                except Exception:
                    reasons = [reasons]
            for r in reasons:
                ctk.CTkLabel(body, text=f"• {r}", text_color=COLORS["text_2"],
                             font=FONTS["helper"], anchor="w", wraplength=self._width - 60
                             ).pack(fill="x", padx=(SPACE["lg"] + 4, SPACE["lg"]))

        # 证据
        evidence = detail.get("evidence") or []
        self._section(body, f"证据（{len(evidence)}）")
        for e in evidence:
            txt = (e.get("evidence_text") or "")[:120]
            ctk.CTkLabel(body, text=f"{e.get('evidence_type')} · {e.get('occurred_at') or ''}",
                         text_color=COLORS["muted"], font=FONTS["helper"],
                         anchor="w").pack(fill="x", padx=(SPACE["lg"] + 4, SPACE["lg"]))
            ctk.CTkLabel(body, text=txt, text_color=COLORS["text_2"],
                         font=FONTS["body"], anchor="w", wraplength=self._width - 60
                         ).pack(fill="x", padx=(SPACE["lg"] + 4, SPACE["lg"]), pady=(1, SPACE["sm"]))

        # 分配历史
        assignments = detail.get("assignments") or []
        self._section(body, f"分配历史（{len(assignments)}）")
        for a in assignments:
            ctk.CTkLabel(body,
                         text=f"{a.get('action')} · {a.get('created_at') or ''}",
                         text_color=COLORS["text_2"], font=FONTS["helper"],
                         anchor="w").pack(fill="x", padx=(SPACE["lg"] + 4, SPACE["lg"]))
            if a.get("reason"):
                ctk.CTkLabel(body, text=f"  理由: {a['reason']}",
                             text_color=COLORS["muted"], font=FONTS["helper"],
                             anchor="w").pack(fill="x", padx=(SPACE["lg"] + 4, SPACE["lg"]),
                             pady=(0, SPACE["xs"]))

        # 审计
        audit = detail.get("audit") or []
        self._section(body, f"审计记录（{len(audit)}）")
        for a in audit[-10:]:
            ctk.CTkLabel(body,
                         text=f"{a.get('created_at') or ''} {a.get('actor')} → {a.get('action')}",
                         text_color=COLORS["muted"], font=FONTS["helper"],
                         anchor="w").pack(fill="x", padx=(SPACE["lg"] + 4, SPACE["lg"]),
                         pady=(0, 2))

        pad = ctk.CTkFrame(body, height=20, fg_color="transparent")
        pad.pack(fill="x")

    # ------------------------------------------------------------------
    def _section(self, body, title: str):
        ctk.CTkLabel(body, text=title, text_color=COLORS["primary"],
                     font=FONTS["section_title"]).pack(
            fill="x", padx=SPACE["lg"], pady=(SPACE["lg"], SPACE["xs"]))

    def _kv(self, body, key: str, value: Any):
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", padx=SPACE["lg"], pady=1)
        ctk.CTkLabel(row, text=key, width=86, text_color=COLORS["muted"],
                     font=FONTS["helper"], anchor="w").pack(side="left")
        ctk.CTkLabel(row, text=str(value) if value not in (None, "") else "—",
                     text_color=COLORS["text_2"], font=FONTS["helper"],
                     anchor="w", wraplength=self._width - 140).pack(side="left", fill="x", expand=True)
