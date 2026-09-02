# -*- coding: utf-8 -*-
"""test_search.py — 纯 API 搜索验证。"""

import sys

sys.path.insert(0, ".")
from api_client import DouyinApiClient
from douyin_collector import DouyinCollector, _load_cookie


def main():
    cookie = _load_cookie()
    print(f"cookie 长度: {len(cookie)}")
    client = DouyinApiClient(cookie_str=cookie)
    collector = DouyinCollector(client)

    print("=== 测试搜索: 快递柜 (limit=5) ===")
    videos = collector.search("快递柜", limit=5, max_pages=2)
    print(f"搜索结果: {len(videos)} 条")
    for v in videos[:5]:
        print(f"  - {v['kind']} {v['vid']} {v['title'][:35]}")


if __name__ == "__main__":
    main()
