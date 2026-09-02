# -*- coding: utf-8 -*-
"""平台专属搜索排序选项。

排序键是内部稳定值，界面只显示中文。平台页面的文案可能变化，采集器
会把内部键映射为当前页面可见的排序按钮；旧任务缺省使用 default。
"""

SEARCH_SORT_OPTIONS = {
    "douyin": [
        ("综合排序", "default"),
        ("最多点赞", "most_like"),
        ("最新发布", "latest"),
    ],
    "xhs": [
        ("综合排序", "default"),
        ("最新", "latest"),
        ("最热", "hot"),
    ],
    "weibo": [
        ("综合", "default"),
        ("实时", "realtime"),
        ("热门", "hot"),
    ],
    "bilibili": [
        ("综合排序", "totalrank"),
        ("最多播放", "click"),
        ("最新发布", "pubdate"),
        ("最多弹幕", "dm"),
        ("最多收藏", "stow"),
    ],
    "kuaishou": [
        ("综合排序", "default"),
    ],
}


def sort_options(platform: str):
    return list(SEARCH_SORT_OPTIONS.get(platform, SEARCH_SORT_OPTIONS["douyin"]))


def sort_labels(platform: str):
    return [label for label, _key in sort_options(platform)]


def sort_key(platform: str, label: str, default: str = "default") -> str:
    for option_label, key in sort_options(platform):
        if option_label == label:
            return key
    return default


def sort_label(platform: str, key: str) -> str:
    for label, option_key in sort_options(platform):
        if option_key == key:
            return label
    return sort_options(platform)[0][0]


def latest_sort_key(platform: str) -> str:
    """返回平台对应的“最新”排序内部值。"""
    return {
        "douyin": "latest",
        "xhs": "latest",
        "weibo": "realtime",
        "bilibili": "pubdate",
        "kuaishou": "latest",
    }.get(platform, "latest")
