# -*- coding: utf-8 -*-
"""平台层包：各平台采集器的统一入口与注册表。"""

from .base import (
    PlatformAdapter,
    VideoItem,
    CommentItem,
    SearchMeta,
    SearchResult,
    PLATFORM_REGISTRY,
)
from . import douyin, xiaohongshu, weibo, bilibili
from .douyin import DouyinAdapter
from .xiaohongshu import XiaohongshuAdapter
from .weibo import WeiboAdapter
from .bilibili import BilibiliAdapter


def register_platform(adapter_cls):
    """类装饰器：注册平台适配器到全局注册表。"""
    PLATFORM_REGISTRY[adapter_cls.platform] = adapter_cls
    return adapter_cls


# 注册全部平台
register_platform(DouyinAdapter)
register_platform(XiaohongshuAdapter)
register_platform(WeiboAdapter)
register_platform(BilibiliAdapter)


def create_adapter(platform: str, **kwargs) -> PlatformAdapter:
    """按平台名实例化适配器。"""
    cls = PLATFORM_REGISTRY.get(platform)
    if cls is None:
        raise KeyError(f"未知平台: {platform}，已注册: {list(PLATFORM_REGISTRY)}")
    return cls(**kwargs)


__all__ = [
    "PlatformAdapter",
    "VideoItem",
    "CommentItem",
    "SearchMeta",
    "SearchResult",
    "PLATFORM_REGISTRY",
    "register_platform",
    "create_adapter",
    "DouyinAdapter",
    "XiaohongshuAdapter",
    "WeiboAdapter",
    "BilibiliAdapter",
]
