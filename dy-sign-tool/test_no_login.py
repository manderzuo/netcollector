# -*- coding: utf-8 -*-
"""test_no_login.py — 实测不登录(空 cookie)能否搜索+评论。

关键：DouyinApiClient(cookie_str="") = 完全不携带 cookie。
对比：登录态(cookie.txt) vs 空 cookie。
"""

import sys

sys.path.insert(0, ".")
from api_client import DouyinApiClient
from douyin_collector import _load_cookie


def main():
    keyword = "京东积分"
    vid = "7570924435265623411"  # 前面测试过有评论的作品

    print("=" * 60)
    print("实测: 不登录(空 cookie) 能否采集")
    print("=" * 60)

    # 1. 空 cookie 客户端(不登录)
    anon = DouyinApiClient(cookie_str="")
    print(f"\n[空 cookie 客户端] cookie 长度: {len(anon.cookie)}")

    # 2. 搜索测试
    print(f"\n--- 搜索: {keyword} (不登录) ---")
    try:
        data = anon.search(keyword, offset="0", count=20)
        items = anon.parse_search_results(data)
        print(f"结果: {len(items)} 条, has_more={data.get('has_more')}")
        for it in items[:5]:
            print(f"  {it['kind']} {it['vid']} {it['title'][:30]}")
    except Exception as exc:
        print(f"搜索失败: {type(exc).__name__}: {exc}")

    # 3. 评论测试
    print(f"\n--- 评论: {vid} (不登录) ---")
    try:
        data = anon.comments(vid, cursor="0", count=20)
        items = anon.parse_comments(data, parent_id="")
        print(f"结果: {len(items)} 条, has_more={data.get('has_more')}")
        for cm in items[:5]:
            print(f"  [{cm['nickname']}] {cm['text'][:40]}")
    except Exception as exc:
        print(f"评论失败: {type(exc).__name__}: {exc}")

    # 4. 对比: 登录态
    print(f"\n--- 对比: 登录态(cookie.txt) ---")
    logged = DouyinApiClient(cookie_str=_load_cookie())
    print(f"登录 cookie 长度: {len(logged.cookie)}")
    try:
        data = logged.comments(vid, cursor="0", count=20)
        items = logged.parse_comments(data, parent_id="")
        print(f"评论结果: {len(items)} 条")
    except Exception as exc:
        print(f"评论失败: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 60)
    print("实测完成")


if __name__ == "__main__":
    main()
