"""Visual tokens shared by every workbench page.

Keep colors, typography and spacing here so individual pages cannot drift away
from the approved UI mockups in ``docs/ui-mockups``.
"""

COLORS = {
    "window": "#0d131d",
    "sidebar": "#101722",
    "surface": "#131b27",
    "surface_2": "#182230",
    "surface_3": "#202b3b",
    "surface_hover": "#263348",
    "border": "#2b384a",
    "border_soft": "#202c3b",
    "primary": "#635bff",
    "primary_hover": "#756fff",
    "primary_soft": "#282f68",
    "text": "#eef2f8",
    "text_2": "#c2cbd8",
    "muted": "#9aa6b8",
    "subtle": "#718095",
    "success": "#35c98b",
    "success_bg": "#123728",
    "warning": "#f5a524",
    "warning_bg": "#2f281c",
    "danger": "#f05d68",
    "danger_bg": "#3d2027",
    "info": "#7898ff",
    "info_bg": "#1d2949",
    "log_bg": "#070d14",
    "log_text": "#8ca9d8",
}

FONT_FAMILY = "Microsoft YaHei UI"
MONO_FAMILY = "Consolas"

FONTS = {
    "app": (FONT_FAMILY, 14),
    "page_title": (FONT_FAMILY, 24, "bold"),
    "page_subtitle": (FONT_FAMILY, 13),
    "section_title": (FONT_FAMILY, 18, "bold"),
    "card_title": (FONT_FAMILY, 14, "bold"),
    "body": (FONT_FAMILY, 14),
    "body_bold": (FONT_FAMILY, 14, "bold"),
    "table": (FONT_FAMILY, 13),
    "table_bold": (FONT_FAMILY, 13, "bold"),
    "helper": (FONT_FAMILY, 12),
    "stat_value": (FONT_FAMILY, 28, "bold"),
    "log": (MONO_FAMILY, 12),
    "log_title": (FONT_FAMILY, 14, "bold"),
}

RADIUS = {"small": 8, "control": 10, "card": 15, "dialog": 18}
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 18, "xl": 24}


def status_palette(status):
    """Return foreground/background colors for account and task status text."""
    if status in {"done", "ready", "active", "idle", "空闲", "完成", "已激活", "已绑定", "打开"}:
        return COLORS["success"], COLORS["success_bg"]
    if status in {"waiting_human", "failed", "dead", "需人工", "失效", "未绑定"}:
        return COLORS["danger"], COLORS["danger_bg"]
    if status in {"paused", "incomplete", "cooldown", "waiting_account", "no_account",
                  "暂停", "冷却", "待开始", "等待账号", "无可用账号"}:
        return COLORS["warning"], COLORS["warning_bg"]
    if status in {"running", "phase_a_search", "phase_b_comments", "working", "采集中", "工作中"}:
        return COLORS["info"], COLORS["info_bg"]
    return COLORS["text_2"], COLORS["surface_3"]
