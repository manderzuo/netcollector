# -*- coding: utf-8 -*-
"""api_verify_full.py — 严格验证是否翻到底。

验证逻辑：
1. 继续翻页到 cursor 很大(如 1000),看是否真的无数据
2. 记录每页的 has_more 和是否出现重复/空页
3. 判断"到底"是 has_more=0 还是只是没新内容
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
    max_pages = 60  # 上限保护,比上次 26 页多一倍
    consecutive_no_new = 0
    last_has_more = True

    print(f"=== 严格验证: {keyword} ===")
    while page < max_pages:
        page += 1
        data = client.search(keyword, offset=cursor, count=20)
        items = client.parse_search_results(data)
        new_count = 0
        for it in items:
            if it["vid"] not in all_items:
                all_items[it["vid"]] = it
                new_count += 1

        has_more = bool(data.get("has_more"))
        next_cursor = data.get("cursor", "")
        print(f"[第{page}页] cursor={cursor} → 下一cursor={next_cursor} "
              f"本页{len(items)} 新增{new_count} 累计{len(all_items)} "
              f"has_more={has_more}")

        if new_count == 0:
            consecutive_no_new += 1
        else:
            consecutive_no_new = 0

        # 停止条件
        if not has_more and (not items or new_count == 0):
            print(f"  [到底] has_more=False 且无新内容")
            break
        if not next_cursor or str(next_cursor) == str(cursor):
            print(f"  [到底] cursor 不再前进: {cursor}")
            break
        if consecutive_no_new >= 5:
            print(f"  [到底] 连续{consecutive_no_new}页无新内容")
            break

        cursor = str(next_cursor)
        last_has_more = has_more
        time.sleep(1.5)

    print(f"\n=== 结果 ===")
    print(f"翻页轮数: {page}")
    print(f"总提取链接: {len(all_items)}")
    print(f"最后 has_more: {last_has_more}")


if __name__ == "__main__":
    main()
