# -*- coding: utf-8 -*-
"""平台适配器边界。

每个平台只需要实现本模块定义的最小接口，就可以被调度器或独立项目复用。
平台适配器不应直接依赖 GUI、SQLite 或其它平台适配器。
"""

from typing import Protocol


class PlatformAdapter(Protocol):
    platform: str

    def search(self, keyword: str, platform: str, mode: str = "standard",
               target_count: int = 100, window_id: str = None,
               search_sort: str = "default") -> list[dict]:
        ...

    def fetch_comments(self, vid: str, account: str, url: str = "",
                       platform: str = None, window_id: str = None) -> list[dict]:
        ...


PLATFORM_MODULES = {
    "douyin": "src.dy_collect",
    "xhs": "src.xhs_collect3",
    "weibo": "src.weibo_chrome",
    # 后续平台只需在这里登记，不需要修改调度器核心。
    "bilibili": "src.bilibili_adapter",
    "kuaishou": "src.kuaishou_collect",
    "tieba": "src.tieba_adapter",
}


__all__ = ["PlatformAdapter", "PLATFORM_MODULES"]
