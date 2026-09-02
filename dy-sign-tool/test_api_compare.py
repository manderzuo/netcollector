# -*- coding: utf-8 -*-
"""test_api_compare.py — API 搜索对比(京东积分)。"""

import sys

sys.path.insert(0, ".")
from api_client import DouyinApiClient
from douyin_collector import _load_cookie


def main():
    client = DouyinApiClient(cookie_str=_load_cookie())
    print("=== API 搜索: 京东积分 ===")
    data = client.search("京东积分", offset=0, count=20)
    items = client.parse_search_results(data)
    has_more = data.get("has_more")
    print(f"API 结果: {len(items)} 条, has_more={has_more}")
    for it in items[:5]:
        print(f"  {it['kind']} {it['vid']} {it['title'][:35]}")


if __name__ == "__main__":
    main()
