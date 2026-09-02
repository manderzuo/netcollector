# -*- coding: utf-8 -*-
"""线索/互动专用状态徽章（技术方案 Agent E 组件）。

复用 ``src.ui.widgets.StatusBadge`` 的基础控件，为线索池、意向和草稿状态
提供专用配色映射（避免重复实现控件，只做状态语义层）。评论时间直接展示，
不再转换为热度级别。
"""

from __future__ import annotations

import customtkinter as ctk

from ..theme import COLORS, FONTS
from ..widgets import StatusBadge as BaseBadge

# 线索池配色
POOL_PALETTE = {
    "henan": (COLORS["success"], COLORS["success_bg"]),
    "other_province": (COLORS["info"], COLORS["info_bg"]),
    "region_review": (COLORS["warning"], COLORS["warning_bg"]),
    "unknown_region": (COLORS["text_2"], COLORS["surface_3"]),
    "archived": (COLORS["muted"], COLORS["surface_2"]),
    "unclassified": (COLORS["text_2"], COLORS["surface_3"]),
}

# 意向配色
INTENT_PALETTE = {
    "high": (COLORS["success"], COLORS["success_bg"]),
    "medium": (COLORS["warning"], COLORS["warning_bg"]),
    "low": (COLORS["text_2"], COLORS["surface_3"]),
    "unknown": (COLORS["muted"], COLORS["surface_2"]),
}

# 草稿状态配色
DRAFT_PALETTE = {
    "draft": (COLORS["text_2"], COLORS["surface_3"]),
    "pending_review": (COLORS["warning"], COLORS["warning_bg"]),
    "approved": (COLORS["success"], COLORS["success_bg"]),
    "queued": (COLORS["info"], COLORS["info_bg"]),
    "sending": (COLORS["info"], COLORS["info_bg"]),
    "sent": (COLORS["success"], COLORS["success_bg"]),
    "rejected": (COLORS["danger"], COLORS["danger_bg"]),
    "failed": (COLORS["danger"], COLORS["danger_bg"]),
    "cancelled": (COLORS["muted"], COLORS["surface_2"]),
}

# 线索状态配色
LEAD_STATUS_PALETTE = {
    "new": (COLORS["info"], COLORS["info_bg"]),
    "qualified": (COLORS["success"], COLORS["success_bg"]),
    "assigned": (COLORS["warning"], COLORS["warning_bg"]),
    "draft_ready": (COLORS["info"], COLORS["info_bg"]),
    "awaiting_review": (COLORS["warning"], COLORS["warning_bg"]),
    "approved": (COLORS["success"], COLORS["success_bg"]),
    "contacted": (COLORS["info"], COLORS["info_bg"]),
    "replied": (COLORS["success"], COLORS["success_bg"]),
    "converted": (COLORS["success"], COLORS["success_bg"]),
    "closed": (COLORS["muted"], COLORS["surface_2"]),
    "suppressed": (COLORS["danger"], COLORS["danger_bg"]),
    "archived": (COLORS["muted"], COLORS["surface_2"]),
}

# 中文显示名
POOL_LABELS = {
    "henan": "河南", "other_province": "外省", "region_review": "待复核",
    "unknown_region": "未知地区", "archived": "已归档", "unclassified": "未分类",
}
INTENT_LABELS = {"high": "高", "medium": "中", "low": "低", "unknown": "未知"}
DRAFT_LABELS = {
    "draft": "草稿", "pending_review": "待审核", "approved": "已批准",
    "queued": "待发送", "sending": "发送中", "sent": "已回复",
    "rejected": "已拒绝", "failed": "失败", "cancelled": "已取消",
}
LEAD_STATUS_LABELS = {
    "new": "新线索", "qualified": "已合格", "assigned": "已分配",
    "draft_ready": "可生成草稿", "awaiting_review": "待审核",
    "approved": "已批准", "contacted": "已联系", "replied": "已回复",
    "converted": "已转化", "closed": "已关闭", "suppressed": "勿扰",
    "archived": "已归档",
}


class LeadStatusBadge(BaseBadge):
    """线索状态徽章（显示中文 + 专用配色）。"""

    def __init__(self, master, status: str, text: str = None):
        fg, bg = LEAD_STATUS_PALETTE.get(status, (COLORS["text_2"], COLORS["surface_3"]))
        super().__init__(master, text=text or LEAD_STATUS_LABELS.get(status, status),
                         status="", **{})
        self.configure(fg_color=bg, text_color=fg)


class PoolBadge(BaseBadge):
    def __init__(self, master, pool: str, text: str = None):
        fg, bg = POOL_PALETTE.get(pool, (COLORS["text_2"], COLORS["surface_3"]))
        super().__init__(master, text=text or POOL_LABELS.get(pool, pool),
                         status="", **{})
        self.configure(fg_color=bg, text_color=fg)


class IntentBadge(BaseBadge):
    def __init__(self, master, level: str, text: str = None):
        fg, bg = INTENT_PALETTE.get(level, (COLORS["text_2"], COLORS["surface_3"]))
        super().__init__(master, text=text or INTENT_LABELS.get(level, level),
                         status="", **{})
        self.configure(fg_color=bg, text_color=fg)


class DraftStatusBadge(BaseBadge):
    def __init__(self, master, status: str, text: str = None):
        fg, bg = DRAFT_PALETTE.get(status, (COLORS["text_2"], COLORS["surface_3"]))
        super().__init__(master, text=text or DRAFT_LABELS.get(status, status),
                         status="", **{})
        self.configure(fg_color=bg, text_color=fg)
