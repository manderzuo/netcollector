# -*- coding: utf-8 -*-
"""wait_and_resume.py — 限流窗口等待 + 自动续采守护（dy-sign-tool 内）。

逻辑：
1. 等待指定分钟数（默认 40），期间不发出任何抖音请求（避免给 IP 加标记）
2. 等待结束后，先探针验证是否解限（用确定有结果的词"干洗店"）
3. 解限 → 自动调用 batch_keywords.py --resume 续采空结果的后17词
4. 未解限 → 再等 15 分钟重试，最多 3 轮

用法：python -u wait_and_resume.py --wait-min 40
"""

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PROBE_KEYWORD = "干洗店"  # 确定有大量结果的词，用于探针


def probe_unblocked() -> bool:
    """探针：干洗店搜索是否恢复。"""
    try:
        from api_client import DouyinApiClient
        from douyin_collector import _load_cookie
        c = DouyinApiClient(cookie_str=_load_cookie())
        data = c.search(PROBE_KEYWORD, offset="0", count=20)
        items = c.parse_search_results(data)
        print(f"[探针] {PROBE_KEYWORD}: items={len(items)} "
              f"has_more={data.get('has_more')}")
        return len(items) > 0
    except Exception as exc:  # noqa: BLE001
        print(f"[探针] 异常: {exc}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-min", type=int, default=40,
                        help="等待分钟数（窗口自愈）")
    args = parser.parse_args()

    print(f"[守护] 限流窗口等待开始: {args.wait_min} 分钟 "
          f"({time.strftime('%H:%M:%S')})")
    print("       期间不发出任何抖音请求，等待 IP 限流窗口自愈...")
    time.sleep(args.wait_min * 60)

    # 探针验证（最多 3 轮，每轮间隔 15 分钟）
    for attempt in range(1, 4):
        print(f"\n[守护] 第 {attempt} 轮探针 ({time.strftime('%H:%M:%S')})")
        if probe_unblocked():
            print("[守护] ✅ 解限！开始自动续采...")
            subprocess.run(
                [sys.executable, "-u", "batch_keywords.py", "--resume"],
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
            print("[守护] 续采完成")
            return
        if attempt < 3:
            print(f"[守护] 仍未解限，再等 15 分钟重试...")
            time.sleep(15 * 60)

    print("[守护] 3 轮探针均未解限，请手动检查 IP/账号状态。")


if __name__ == "__main__":
    main()
