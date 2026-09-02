# -*- coding: utf-8 -*-
"""平台基础元数据。

平台名称、主页和 BitBrowser 缓存文件名集中在这里，避免新增平台时只改了
某一个下拉框，却漏掉账号绑定或浏览器连接路径。采集器本身仍保持独立文件。
"""

from __future__ import annotations

PLATFORM_LABELS = {
    "douyin": "抖音",
    "xhs": "小红书",
    "bilibili": "B站",
    "weibo": "微博",
    "kuaishou": "快手",
}

PLATFORM_ALIASES = {
    "dy": "douyin",
    "douyin": "douyin",
    "xhs": "xhs",
    "xiaohongshu": "xhs",
    "小红书": "xhs",
    "bili": "bilibili",
    "bilibili": "bilibili",
    "b站": "bilibili",
    "weibo": "weibo",
    "wb": "weibo",
    "微博": "weibo",
    "kuaishou": "kuaishou",
    "ks": "kuaishou",
    "快手": "kuaishou",
}

PLATFORM_ORDER = ("douyin", "xhs", "bilibili", "weibo", "kuaishou")

PLATFORM_HOME_URLS = {
    "douyin": "https://www.douyin.com/",
    "xhs": "https://www.xiaohongshu.com/explore",
    "bilibili": "https://www.bilibili.com/",
    "weibo": "https://weibo.com/",
    "kuaishou": "https://www.kuaishou.com/new-reco",
}

PLATFORM_WINDOW_FILES = {
    "douyin": "dy_window.txt",
    "xhs": "xhs_window.txt",
    "bilibili": "bilibili_chrome_window.txt",
    "weibo": "weibo_chrome_window.txt",
    "kuaishou": "kuaishou_window.txt",
}

PLATFORM_HOSTS = {
    "douyin": ("douyin.com",),
    "xhs": ("xiaohongshu.com", "xhslink.com"),
    "bilibili": ("bilibili.com", "b23.tv"),
    "weibo": ("weibo.com", "weibo.cn"),
    "kuaishou": ("kuaishou.com", "gifshow.com"),
}


def normalize_platform(value: str, default: str = "") -> str:
    key = str(value or "").strip().lower()
    return PLATFORM_ALIASES.get(key, key or default)


__all__ = [
    "PLATFORM_LABELS", "PLATFORM_ALIASES", "PLATFORM_ORDER",
    "PLATFORM_HOME_URLS", "PLATFORM_WINDOW_FILES", "PLATFORM_HOSTS",
    "normalize_platform",
]
