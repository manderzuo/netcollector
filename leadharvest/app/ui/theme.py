# -*- coding: utf-8 -*-
"""theme.py — GUI 主题常量（新架构）。"""

# 深色主题色板
COLORS = {
    "bg": "#1e1e2e",
    "panel": "#28283a",
    "card": "#2f2f45",
    "text": "#e0e0e6",
    "muted": "#9a9ab0",
    "accent": "#7c6cf0",
    "success": "#4ade80",
    "warning": "#fbbf24",
    "danger": "#f87171",
    "border": "#3a3a52",
}

FONTS = {
    "title": ("Microsoft YaHei UI", 16, "bold"),
    "section": ("Microsoft YaHei UI", 13, "bold"),
    "body": ("Microsoft YaHei UI", 10),
    "small": ("Microsoft YaHei UI", 9),
    "mono": ("Consolas", 10),
}

# 平台中文名
PLATFORM_CN = {"douyin": "抖音", "xhs": "小红书", "weibo": "微博", "bilibili": "B站"}
PLATFORM_EN = {v: k for k, v in PLATFORM_CN.items()}

# 状态中文
TASK_STATUS_CN = {
    "pending": "待开始",
    "phase_a_search": "搜索中",
    "phase_b_comments": "采评论中",
    "done": "已完成",
    "aborted": "已中止",
    "failed": "失败",
}
ACCT_STATUS_CN = {
    "idle": "空闲",
    "working": "工作中",
    "cooldown": "冷却中",
    "waiting_human": "需人工",
    "frozen": "已冻结",
    "dead": "已失效",
}
