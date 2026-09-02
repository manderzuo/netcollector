# -*- coding: utf-8 -*-
"""test_comments.py — 纯 API 评论采集验证。"""

import sys

sys.path.insert(0, ".")
from api_client import DouyinApiClient
from douyin_collector import DouyinCollector, _load_cookie


def main():
    cookie = _load_cookie()
    client = DouyinApiClient(cookie_str=cookie)
    collector = DouyinCollector(client)

    # 用真实视频 ID 测试评论
    vid = "7660380790026619897"
    print(f"=== 测试评论: {vid} ===")
    comments = collector.comments(vid, full=False, max_pages=5)
    print(f"一级评论: {len(comments)} 条")
    for cm in comments[:5]:
        print(f"  - [{cm['nickname']}] {cm['text'][:40]} ({cm['region']})")


if __name__ == "__main__":
    main()
