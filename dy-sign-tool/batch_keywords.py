# -*- coding: utf-8 -*-
"""batch_keywords.py — 21关键词不间断全量采集（dy-sign-tool 内独立）。

流程：对每个关键词
  1. 全量搜索（cursor翻页直到 has_more=0）
  2. 批量一级评论（_collect_comments + CooldownPolicy 0-5s额外随机）
  3. 落盘 JSON（out/<keyword>_links.json + _comments.json）

用法：
  python batch_keywords.py
  python batch_keywords.py --resume            # 断点续采（跳过已完成的关键词）
  python batch_keywords.py --report 20         # 每20分钟汇报（后台线程）

约束：所有操作仅在 dy-sign-tool 内，不触碰 src/ 老代码。
"""

import argparse
import json
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api_client import DouyinApiClient  # noqa: E402
from cooldown import CooldownPolicy  # noqa: E402

# 21 关键词（按用户给定顺序，不中断）
KEYWORDS = [
    "干洗店", "快递柜", "AI漫剧", "穿越剧", "短剧创作", "短剧道具制作",
    "快递加盟", "京东家政", "家政门店", "无人快递柜加盟", "幼儿园招生",
    "美食推荐", "云台", "稳定器", "私人定制", "新疆旅游", "机器人运动会",
    "自动驾驶", "科普好物", "三折叠", "惠济区幼儿园",
]

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
PROGRESS_FILE = os.path.join(OUT_DIR, "batch_keywords_progress.json")


def _load_cookie() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie.txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def _is_done(keyword: str) -> bool:
    """已完成则跳过（links + comments 都存在且非空）。"""
    links = os.path.join(OUT_DIR, f"{keyword}_links.json")
    comments = os.path.join(OUT_DIR, f"{keyword}_comments.json")
    if not (os.path.exists(links) and os.path.exists(comments)):
        return False
    try:
        with open(comments, encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("items"))
    except Exception:
        return False


def _report_progress(keywords, current_idx, cooldown_policy):
    """20分钟报表线程。"""
    while True:
        time.sleep(20 * 60)
        done = current_idx[0]
        total = len(keywords)
        cur_kw = keywords[done] if done < total else keywords[-1]
        print(f"\n[汇报 {time.strftime('%H:%M:%S')}] 进度: {done}/{total} "
              f"当前关键词: {cur_kw} | 下一汇报 20分钟后")
        # 快速汇总已完成数
        ok = sum(1 for kw in keywords[:done] if _is_done(kw))
        print(f"  已完成且落盘: {ok}/{done} 关键词")


def full_search(client, keyword, cooldown):
    """全量搜索：cursor翻页直到到底。"""
    all_items = {}
    cursor = "0"
    page = 0
    while page < 60:
        page += 1
        data = client.search(keyword, offset=cursor, count=20)
        items = client.parse_search_results(data)
        for it in items:
            all_items[it["vid"]] = it
        has_more = bool(data.get("has_more"))
        next_cursor = str(data.get("cursor") or "")
        print(f"  [搜索第{page}页] cursor={cursor} 本页{len(items)} "
              f"累计{len(all_items)} has_more={has_more}")
        if not has_more or not items:
            break
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        cooldown.wait_page()
    return list(all_items.values())


def fetch_comments(client, aweme_id, cooldown, max_pages=30):
    """一级评论 + cursor翻页。"""
    all_c = {}
    cursor = "0"
    for _ in range(max_pages):
        data = client.comments(aweme_id, cursor=cursor, count=20)
        for cm in client.parse_comments(data, parent_id=""):
            all_c[cm["cid"]] = cm
        if not data.get("has_more"):
            break
        nc = str(data.get("cursor") or "0")
        if nc == cursor:
            break
        cursor = nc
        cooldown.wait_comment()
    return list(all_c.values())


def run_keyword(keyword, client, idx, total):
    print(f"\n{'='*60}")
    print(f"[{idx}/{total}] 关键词: {keyword}")
    print(f"{'='*60}")
    cooldown = CooldownPolicy(batch_size=10, batch_cooldown=(60, 120))
    # 1. 全量搜索
    print(f"[1/2] 全量搜索: {keyword}")
    items = full_search(client, keyword, cooldown)
    print(f"  搜索完成: {len(items)} 个作品")
    # 2. 保存链接
    links_path = os.path.join(OUT_DIR, f"{keyword}_links.json")
    kinds = {}
    for it in items:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    with open(links_path, "w", encoding="utf-8") as f:
        json.dump({
            "keyword": keyword, "total": len(items),
            "kinds": kinds, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "items": items,
        }, f, ensure_ascii=False, indent=2)
    print(f"  链接已保存: {links_path} {kinds}")
    # 3. 批量评论
    print(f"[2/2] 批量评论: {len(items)} 个作品")
    all_items = []
    for i, it in enumerate(items):
        vid = it["vid"]
        try:
            cms = fetch_comments(client, vid, cooldown)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i+1}/{len(items)}] {vid} 失败: {exc}")
            cms = []
        it["comments"] = cms
        all_items.append(it)
        print(f"  [{i+1}/{len(items)}] {vid} 评论{len(cms)}")
        cooldown.maybe_batch_cooldown(i + 1)
        cooldown.wait_item()
        if (i + 1) % 5 == 0:
            # 增量落盘
            _save_comments(keyword, all_items)
    _save_comments(keyword, all_items)
    total_c = sum(len(it.get("comments", [])) for it in all_items)
    print(f"  评论完成: {keyword} 评论{total_c}条")
    return len(items), total_c


def _save_comments(keyword, items):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{keyword}_comments.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "keyword": keyword, "total_items": len(items),
            "total_comments": sum(len(it.get("comments", [])) for it in items),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "items": items,
        }, f, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true", help="断点续采")
    parser.add_argument("--report-sec", type=int, default=20*60, help="汇报间隔秒")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    cookie = _load_cookie()
    if not cookie:
        print("未找到 dy-sign-tool/cookie.txt，请先 python save_cookie.py <ws_url>")
        sys.exit(1)
    client = DouyinApiClient(cookie_str=cookie)

    print(f"批次关键词: {len(KEYWORDS)} 个 | resume={args.resume} | 汇报间隔={args.report_sec}s")
    # 汇报线程
    current_idx = [0]
    reporter = threading.Thread(
        target=_report_progress,
        args=(KEYWORDS, current_idx, None), daemon=True)
    # 用自定义间隔
    def reporter_loop():
        while True:
            time.sleep(args.report_sec)
            done = current_idx[0]
            cur_kw = KEYWORDS[done] if done < len(KEYWORDS) else KEYWORDS[-1]
            print(f"\n[汇报 {time.strftime('%H:%M:%S')}] 进度: {done}/{len(KEYWORDS)} "
                  f"当前关键词: {cur_kw}")
    t = threading.Thread(target=reporter_loop, daemon=True)
    t.start()

    done_count = 0
    failed = []
    start_ts = time.time()
    for idx, kw in enumerate(KEYWORDS, 1):
        if args.resume and _is_done(kw):
            print(f"[{idx}/{len(KEYWORDS)}] {kw} 已完成，跳过")
            current_idx[0] = idx
            done_count += 1
            continue
        current_idx[0] = idx - 1
        try:
            n_links, n_comments = run_keyword(kw, client, idx, len(KEYWORDS))
            done_count += 1
            current_idx[0] = idx
        except Exception as exc:  # noqa: BLE001
            print(f"[{idx}/{len(KEYWORDS)}] {kw} 失败: {exc}")
            failed.append(kw)
            current_idx[0] = idx

    elapsed = time.time() - start_ts
    print(f"\n{'='*60}")
    print(f"批次完成: {done_count}/{len(KEYWORDS)} 耗时 {elapsed/60:.1f}分")
    if failed:
        print(f"失败: {failed}")
    else:
        print("全部成功")


if __name__ == "__main__":
    main()
