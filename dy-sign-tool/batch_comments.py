# -*- coding: utf-8 -*-
"""batch_comments.py — 批量评论采集（从链接文件读取作品 → 逐个拉评论）。

用法：
  python batch_comments.py --links out/京东积分_links.json [--full] [--max-items 10]

功能：
- 读取 api_save_links.py 保存的链接文件
- 逐作品用 API 拉评论（含冷却：随机间隔 + 批次冷却 + 风控退避）
- 结果保存到 out/京东积分_comments.json
- 支持断点续采：已采集的作品跳过
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api_client import DouyinApiClient  # noqa: E402
from douyin_collector import DouyinCollector, _load_cookie  # noqa: E402
from cooldown import CooldownPolicy  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def load_links(path: str) -> list:
    """读取链接文件。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("items", [])


def load_done(output_path: str) -> dict:
    """读取已采集结果（断点续采）。"""
    if os.path.exists(output_path):
        with open(output_path, encoding="utf-8") as f:
            return json.load(f)
    return {"keyword": "", "items": []}


def main():
    parser = argparse.ArgumentParser(description="批量评论采集")
    parser.add_argument("--links", required=True, help="链接文件路径")
    parser.add_argument("--full", action="store_true", help="采集二级评论")
    parser.add_argument("--max-items", type=int, default=0,
                        help="最多采多少作品(0=全部)")
    parser.add_argument("--out", default="", help="输出文件")
    args = parser.parse_args()

    links_path = args.links
    keyword = os.path.basename(links_path).replace("_links.json", "")
    out_path = args.out or os.path.join(OUT_DIR, f"{keyword}_comments.json")

    # 读取链接 + 已采结果
    items = load_links(links_path)
    done_data = load_done(out_path)
    done_vids = {it.get("vid") for it in done_data.get("items", [])}
    print(f"链接总数: {len(items)}, 已采: {len(done_vids)}")

    # 过滤待采
    pending = [it for it in items if it.get("vid") not in done_vids]
    if args.max_items > 0:
        pending = pending[:args.max_items]
    print(f"本次待采: {len(pending)} 个作品")

    if not pending:
        print("无可采作品（已全部完成或全部跳过）")
        return

    # 采集器（带冷却）
    client = DouyinApiClient(cookie_str=_load_cookie())
    collector = DouyinCollector(client)
    cooldown = CooldownPolicy()

    # 逐作品采集
    all_items = list(done_data.get("items", []))
    success = 0
    failed = 0
    empty = 0

    for i, item in enumerate(pending):
        vid = item.get("vid", "")
        kind = item.get("kind", "video")
        title = item.get("title", "")[:30]
        print(f"\n[{i + 1}/{len(pending)}] {kind} {vid} ({title})")

        try:
            comments = collector.comments(vid, full=args.full)
            if comments:
                item["comments"] = comments
                item["comment_count"] = len(comments)
                success += 1
                print(f"  评论 {len(comments)} 条")
            else:
                item["comments"] = []
                item["comment_count"] = 0
                empty += 1
                print(f"  无评论")
            all_items.append(item)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            item["comments"] = []
            item["comment_count"] = 0
            item["error"] = str(exc)
            all_items.append(item)
            print(f"  失败: {exc}")

        # 批次冷却（每 10 个作品长停）
        cooldown.maybe_batch_cooldown(i + 1)
        # 作品间随机间隔
        cooldown.wait_item()

        # 每采 5 个落盘一次（防中断丢数据）
        if (i + 1) % 5 == 0:
            _save(out_path, keyword, all_items)
            print(f"  [自动保存] {len(all_items)} 个作品已落盘")

    # 最终落盘
    _save(out_path, keyword, all_items)
    print(f"\n=== 完成 ===")
    print(f"输出: {out_path}")
    print(f"成功: {success} 个, 无评论: {empty} 个, 失败: {failed} 个")
    total_comments = sum(len(it.get("comments", [])) for it in all_items)
    print(f"总评论数: {total_comments}")


def _save(path: str, keyword: str, items: list):
    """落盘。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "keyword": keyword,
        "total_items": len(items),
        "total_comments": sum(len(it.get("comments", [])) for it in items),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
