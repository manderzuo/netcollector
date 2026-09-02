# -*- coding: utf-8 -*-
"""api_save_links.py — API 全量搜索并保存链接到文件。

用法：python api_save_links.py --keyword 京东积分 --out out/京东积分_links.json
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_client import DouyinApiClient
from douyin_collector import _load_cookie

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def full_search(client, keyword, max_pages=60):
    """全量搜索,返回 (链接列表, 翻页轮数)。"""
    all_items = {}
    cursor = "0"
    page = 0
    while page < max_pages:
        page += 1
        data = client.search(keyword, offset=cursor, count=20)
        items = client.parse_search_results(data)
        for it in items:
            all_items[it["vid"]] = it
        has_more = bool(data.get("has_more"))
        next_cursor = data.get("cursor", "")
        print(f"  [第{page}页] 本页{len(items)} 累计{len(all_items)} "
              f"has_more={has_more}")
        if not has_more or not items:
            break
        if not next_cursor or str(next_cursor) == str(cursor):
            break
        cursor = str(next_cursor)
        time.sleep(1.5)
    return list(all_items.values()), page


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = args.out or os.path.join(OUT_DIR, f"{args.keyword}_links.json")

    client = DouyinApiClient(cookie_str=_load_cookie())
    print(f"=== 全量搜索: {args.keyword} ===")
    items, pages = full_search(client, args.keyword)

    # 保存
    payload = {
        "keyword": args.keyword,
        "pages": pages,
        "total": len(items),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n[保存] {len(items)} 个链接 → {out_path}")

    # 统计
    kinds = {}
    for it in items:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    print(f"类型分布: {kinds}")


if __name__ == "__main__":
    main()
