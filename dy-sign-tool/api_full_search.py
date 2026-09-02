# -*- coding: utf-8 -*-
"""api_full_search.py — API 全量搜索(京东积分)。

用 cursor 翻页,一直翻到 has_more=0,统计总共能提取多少内容链接。
"""

import sys
import time

sys.path.insert(0, ".")
from api_client import DouyinApiClient
from douyin_collector import _load_cookie


def main():
    keyword = "京东积分"
    client = DouyinApiClient(cookie_str=_load_cookie())

    all_items = {}
    cursor = "0"
    page = 0
    max_pages = 200  # 上限保护
    empty_pages = 0

    print(f"=== API 全量搜索: {keyword} ===")
    while page < max_pages:
        page += 1
        data = client.search(keyword, offset=cursor, count=20)
        items = client.parse_search_results(data)
        new_count = 0
        for it in items:
            if it["vid"] not in all_items:
                all_items[it["vid"]] = it
                new_count += 1
        print(f"[第{page}页] cursor={cursor} 本页{len(items)}条 新增{new_count} "
              f"累计{len(all_items)} has_more={data.get('has_more')}")

        if not data.get("has_more") or not items:
            print("  已翻到底(has_more=0 或无数据)")
            break
        # 取下一页 cursor
        next_cursor = data.get("cursor")
        if not next_cursor or str(next_cursor) == str(cursor):
            empty_pages += 1
            if empty_pages >= 3:
                print("  cursor 不变,停止")
                break
        else:
            empty_pages = 0
            cursor = str(next_cursor)
        time.sleep(1.5)

    print(f"\n=== 全量结果 ===")
    print(f"关键词: {keyword}")
    print(f"翻页轮数: {page}")
    print(f"总提取内容链接: {len(all_items)} 个")
    print(f"\n全部链接:")
    for it in all_items.values():
        print(f"  {it['kind']:6s} {it['url']}")


if __name__ == "__main__":
    main()
