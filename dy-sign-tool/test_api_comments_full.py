# -*- coding: utf-8 -*-
"""test_api_comments_full.py — API 拉评论完整测试。

用保存的链接文件,测试：
1. 单作品评论(一级) + cursor 翻页
2. 二级评论(楼中楼)
3. 统计评论总量
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_client import DouyinApiClient
from douyin_collector import _load_cookie

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def fetch_all_comments(client, aweme_id, full=False, max_pages=30):
    """拉取作品全部一级评论(可选二级)。返回 (评论列表, 页数)。"""
    all_comments = {}
    cursor = "0"
    page = 0
    while page < max_pages:
        page += 1
        data = client.comments(aweme_id, cursor=cursor, count=20)
        items = client.parse_comments(data, parent_id="")
        for cm in items:
            all_comments[cm["cid"]] = cm
        has_more = bool(data.get("has_more"))
        next_cursor = str(data.get("cursor", "0"))
        if not has_more or not items:
            break
        if next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(1.2)
    # 二级
    if full:
        for cm in list(all_comments.values()):
            if cm.get("reply_total"):
                rcursor = "0"
                for _ in range(5):
                    rdata = client.replies(aweme_id, cm["cid"], cursor=rcursor)
                    ritems = client.parse_comments(rdata, parent_id=cm["cid"])
                    for r in ritems:
                        all_comments[r["cid"]] = r
                    if not rdata.get("has_more") or not ritems:
                        break
                    rcursor = str(rdata.get("cursor", "0"))
                    time.sleep(1.0)
    return list(all_comments.values()), page


def main():
    # 读取保存的链接
    links_path = os.path.join(OUT_DIR, "京东积分_links.json")
    with open(links_path, encoding="utf-8") as f:
        data = json.load(f)
    items = data["items"]
    print(f"读取 {len(items)} 个链接")

    client = DouyinApiClient(cookie_str=_load_cookie())

    # 测 3 个作品(不同 kind)
    test_items = items[:3]
    for it in test_items:
        print(f"\n=== 评论: {it['kind']} {it['vid']} ({it['title'][:30]}) ===")
        try:
            comments, pages = fetch_all_comments(client, it["vid"], full=True)
            print(f"  一级+二级评论: {len(comments)} 条, 翻页 {pages} 轮")
            for cm in comments[:3]:
                print(f"    [{cm['nickname']}] {cm['text'][:40]} ({cm['region']})")
        except Exception as exc:  # noqa: BLE001
            print(f"  失败: {exc}")
        time.sleep(1.5)


if __name__ == "__main__":
    main()
