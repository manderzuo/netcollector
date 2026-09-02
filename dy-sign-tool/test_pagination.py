# -*- coding: utf-8 -*-
"""test_pagination.py — 验证 cursor 分页。"""

import sys

sys.path.insert(0, ".")
from api_client import DouyinApiClient
from douyin_collector import _load_cookie


def main():
    client = DouyinApiClient(cookie_str=_load_cookie())
    params = {
        "keyword": "快递柜", "offset": "0", "count": "20",
        "sort_type": "0", "publish_time": "0", "search_id": "",
        "device_platform": "webapp", "aid": "6383",
    }
    objs = client.request_ndjson(
        "https://www.douyin.com/aweme/v1/web/general/search/stream/", params)
    for i, obj in enumerate(objs):
        if "cursor" in obj:
            print(f"块{i}: cursor={obj.get('cursor')} "
                  f"has_more={obj.get('has_more')} "
                  f"data={len(obj.get('data') or [])}条")

    # 用 cursor 翻页
    cursor = None
    for obj in objs:
        if obj.get("data"):
            cursor = obj.get("cursor")
            break
    print(f"--- 用 cursor={cursor} 请求第2页 ---")
    params2 = dict(params)
    params2["offset"] = str(cursor)
    objs2 = client.request_ndjson(
        "https://www.douyin.com/aweme/v1/web/general/search/stream/", params2)
    total2 = sum(len(o.get("data") or []) for o in objs2 if o.get("data"))
    print(f"第2页数据条数: {total2}")
    for i, obj in enumerate(objs2):
        if "cursor" in obj:
            print(f"  块{i}: cursor={obj.get('cursor')} "
                  f"has_more={obj.get('has_more')} "
                  f"data={len(obj.get('data') or [])}条")


if __name__ == "__main__":
    main()
