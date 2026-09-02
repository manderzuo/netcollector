# -*- coding: utf-8 -*-
"""平台适配器抽象基类。

所有平台采集器（抖音/小红书/微博/B站）统一实现本接口，
调度器只依赖此抽象，不感知具体平台实现。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


@dataclass
class VideoItem:
    """统一的作品条目。"""

    vid: str
    url: str
    title: str = ""
    author: str = ""
    kind: str = "video"            # video / note
    author_id: str = ""
    create_time: Any = None
    comment_count: Any = ""
    keyword: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class CommentItem:
    """统一的评论条目。"""

    user_id: str
    nickname: str
    content: str
    comment_time: Any = None
    region: str = ""
    cid: str = ""
    homepage: str = ""
    parent_id: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class SearchMeta:
    """搜索结果元数据（区分「达到目标」与「确认无更多」）。"""

    search_complete: bool = False
    reached_target: bool = False
    no_more_results: bool = False
    rounds: int = 0
    termination_reason: str = ""


@dataclass
class SearchResult:
    """统一搜索结果：条目列表 + 元数据。"""

    items: List[VideoItem]
    meta: SearchMeta = field(default_factory=SearchMeta)


class PlatformAdapter(abc.ABC):
    """平台采集器抽象接口。"""

    platform: str = ""

    @abc.abstractmethod
    def search(
        self,
        keyword: str,
        mode: str = "standard",
        target_count: int = 100,
        window_id: str = "",
        search_sort: str = "default",
        pause_event=None,
        cancel_event=None,
    ) -> SearchResult:
        """按关键词搜索作品。"""

    @abc.abstractmethod
    def fetch_comments(
        self,
        vid: str,
        url: str = "",
        window_id: str = "",
        pause_event=None,
        cancel_event=None,
    ) -> List[CommentItem]:
        """拉取指定作品的全部评论（含二级）。"""

    # ---- 可选扩展 ----
    def fetch_detail(self, vid: str, url: str = "") -> dict:
        """获取作品详情（可选实现）。"""
        raise NotImplementedError

    def collect_with_comments(
        self,
        keyword: str,
        target_count: int = 100,
        mode: str = "standard",
        window_id: str = "",
        progress_callback: Optional[Callable[[VideoItem], None]] = None,
        pause_event=None,
        cancel_event=None,
        search_sort: str = "default",
    ) -> dict:
        """边搜边采评论（可选实现，用于微博/B站）。"""
        raise NotImplementedError


# 平台注册表：新增平台在此登记即可被调度器发现
PLATFORM_REGISTRY: dict = {}
