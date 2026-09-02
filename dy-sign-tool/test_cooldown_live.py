# -*- coding: utf-8 -*-
"""test_cooldown_live.py — 实测冷却是否在真实请求中生效。

模拟 batch_keywords 的调用链（搜索翻页 + 作品切换 + 随机批次冷却），
打真实接口（当前 IP 可能限流，重点看冷却节奏是否触发）。
"""

import sys
import time

sys.path.insert(0, ".")
from api_client import DouyinApiClient
from douyin_collector import _load_cookie
from cooldown import CooldownPolicy


def main():
    client = DouyinApiClient(cookie_str=_load_cookie())
    cooldown = CooldownPolicy(
        page_interval=(2.0, 4.0),
        comment_interval=(1.5, 3.0),
        item_interval=(3.0, 6.0),
        batch_cooldown=(20, 40),
        extra_jitter=(0.0, 5.0),
    )
    print("=== 冷却实测（真实请求） ===")
    t0 = time.time()

    # 模拟 3 次搜索翻页（每次翻页触发 wait_page + wait_extra）
    for i in range(1, 4):
        print(f"\n--- 搜索翻页 {i} ---")
        d = client.search("快递柜", offset=str((i - 1) * 10), count=20)
        print(f"  API: items={len(client.parse_search_results(d))} "
              f"has_more={d.get('has_more')}")
        if i < 3:
            cooldown.wait_page()  # 翻页冷却

    # 模拟 3 个作品切换（wait_item + 随机批次冷却）
    for i in range(1, 4):
        print(f"\n--- 作品 {i} ---")
        cooldown.wait_item()  # 作品间冷却
        hit = cooldown.maybe_batch_cooldown()  # 每1-10随机冷却
        print(f"  随机批次冷却触发: {hit} (阈值={cooldown._cooldown_threshold})")

    print(f"\n=== 总耗时: {time.time() - t0:.1f}s ===")


if __name__ == "__main__":
    main()
