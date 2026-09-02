# -*- coding: utf-8 -*-
"""douyin_collector.py — 抖音纯 API 采集器（dy-sign-tool 内独立运行）。

功能：
- 关键词搜索 → 作品列表（图文+视频）
- 作品 → 一级评论（翻页拉全）+ 二级评论
- 结果 JSON 落盘到 out/

用法：
  python douyin_collector.py --keyword 快递柜 --limit 20 --comments 1
  python douyin_collector.py --keyword 快递柜 --limit 20 --full-comments

说明：
- 完全独立运行，不触碰 src/ 下 CDP 采集代码。
- Cookie 需从 BitBrowser 窗口获取（首次运行提示），之后纯 HTTP。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api_client import DouyinApiClient  # noqa: E402
from cooldown import CooldownPolicy  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


class DouyinCollector:
    """抖音采集主流程。"""

    def __init__(self, client: DouyinApiClient,
                 cooldown: CooldownPolicy = None):
        self.client = client
        self.cooldown = cooldown or CooldownPolicy()

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------
    def search(self, keyword: str, limit: int = 20,
               sort_type: str = "0", max_pages: int = 10) -> List[dict]:
        """搜索作品，翻页直到达到 limit 或没有更多。"""
        all_items: Dict[str, dict] = {}
        offset = 0
        page = 0
        while page < max_pages:
            page += 1
            print(f"  [搜索] 第 {page} 页 offset={offset} ...")
            data = self.client.search(keyword, offset=offset, count=20,
                                      sort_type=sort_type)
            items = self.client.parse_search_results(data)
            for it in items:
                all_items[it["vid"]] = it
            print(f"    本页 {len(items)} 条, 累计去重 {len(all_items)} 条")
            if len(all_items) >= limit:
                break
            if not self.client.has_more(data) or not items:
                print("    已无更多结果")
                break
            # 下一页 offset
            offset += len(items)
            self.cooldown.wait_page()  # 随机间隔翻页
        return list(all_items.values())[:limit]

    # ------------------------------------------------------------------
    # 评论
    # ------------------------------------------------------------------
    def comments(self, aweme_id: str, full: bool = False,
                 max_pages: int = 30) -> List[dict]:
        """拉取作品一级评论（可选二级）。"""
        all_comments: Dict[str, dict] = {}
        cursor = "0"
        page = 0
        while page < max_pages:
            page += 1
            data = self.client.comments(aweme_id, cursor=cursor, count=20)
            items = self.client.parse_comments(data, parent_id="")
            for cm in items:
                all_comments[cm["cid"]] = cm
            if not self.client.has_more(data) or not items:
                break
            cursor = str(data.get("cursor", "0"))
            self.cooldown.wait_comment()  # 随机间隔评论翻页
        # 二级评论（可选）
        if full:
            for cm in list(all_comments.values()):
                if cm.get("reply_total"):
                    replies = self._replies(aweme_id, cm["cid"])
                    for r in replies:
                        all_comments[r["cid"]] = r
                    self.cooldown.wait_comment()
        return list(all_comments.values())

    def _replies(self, aweme_id: str, comment_id: str,
                 max_pages: int = 5) -> List[dict]:
        """二级评论。"""
        out: Dict[str, dict] = {}
        cursor = "0"
        for _ in range(max_pages):
            data = self.client.replies(aweme_id, comment_id, cursor=cursor)
            items = self.client.parse_comments(data, parent_id=comment_id)
            for r in items:
                out[r["cid"]] = r
            if not self.client.has_more(data) or not items:
                break
            cursor = str(data.get("cursor", "0"))
            time.sleep(1.0)
        return list(out.values())

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def collect(self, keyword: str, limit: int = 20,
                full_comments: bool = False, max_videos: int = 5) -> dict:
        """完整采集：搜索 → 逐作品评论 → 落盘。"""
        print(f"\n=== 采集关键词: {keyword} ===")
        videos = self.search(keyword, limit=limit)
        print(f"\n[搜索完成] {len(videos)} 个作品")

        result = {"keyword": keyword, "videos": []}
        for i, v in enumerate(videos[:max_videos] if max_videos else videos):
            print(f"\n[{i + 1}/{len(videos[:max_videos] if max_videos else videos)}] "
                  f"采评论: {v['kind']} {v['vid']} ({v['title'][:30]})")
            try:
                comments = self.comments(v["vid"], full=full_comments)
                v["comments"] = comments
                print(f"    评论 {len(comments)} 条")
            except Exception as exc:  # noqa: BLE001
                print(f"    评论失败: {exc}")
                v["comments"] = []
            result["videos"].append(v)
            # 批次冷却：每 batch_size 个作品后长停
            self.cooldown.maybe_batch_cooldown(i + 1)
            self.cooldown.wait_item()  # 作品间随机间隔

        # 落盘
        os.makedirs(OUT_DIR, exist_ok=True)
        out_path = os.path.join(OUT_DIR, f"{keyword}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n[完成] 结果已保存: {out_path}")
        return result


def _load_cookie() -> str:
    """从 cookie 缓存文件读取（若存在）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie.txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def main():
    parser = argparse.ArgumentParser(description="抖音纯 API 采集器")
    parser.add_argument("--keyword", required=True, help="搜索关键词")
    parser.add_argument("--limit", type=int, default=20, help="搜索作品数")
    parser.add_argument("--max-videos", type=int, default=5, help="采评论的作品数上限")
    parser.add_argument("--full-comments", action="store_true",
                        help="同时采集二级评论（默认只采一级）")
    parser.add_argument("--cookie", default="", help="抖音 cookie（可选，否则读 cookie.txt）")
    args = parser.parse_args()

    cookie = args.cookie or _load_cookie()
    if not cookie:
        print("[提示] 未提供 cookie。")
        print("  方式1: 用 run_probe_real.py 从 BitBrowser 窗口取 cookie 存入 cookie.txt")
        print("  方式2: 手动从浏览器 F12 复制 cookie 存到 dy-sign-tool/cookie.txt")
        sys.exit(1)

    client = DouyinApiClient(cookie_str=cookie)
    collector = DouyinCollector(client)
    collector.collect(
        keyword=args.keyword,
        limit=args.limit,
        full_comments=args.full_comments,
        max_videos=args.max_videos,
    )


if __name__ == "__main__":
    main()
