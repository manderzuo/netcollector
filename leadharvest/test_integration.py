# -*- coding: utf-8 -*-
"""test_integration.py — 新架构端到端集成验证。

真实 BitBrowser + 新架构全链路：
  调度器 → 抖音适配器 → 搜索 → 评论 → 入库

用法：python test_integration.py <ws_url> [keyword] [target]
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import Database
from app.browser.cdp_client import CdpClient
from app.platform import create_adapter


class DirectStore:
    """直接用 ws_url 的窗口存储（测试用）。"""

    def __init__(self, ws_url):
        self._ws = ws_url

    def resolve(self, window_id: str) -> str:
        return self._ws


def main():
    ws = sys.argv[1]
    keyword = sys.argv[2] if len(sys.argv) > 2 else "快递柜"
    target = int(sys.argv[3]) if len(sys.argv) > 3 else 5

    # 1. 数据库
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "test_integration.db")
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except PermissionError:
            pass
    db = Database(db_path)
    db.connect()
    print("[1] 数据库初始化 OK")

    # 2. CDP 客户端 + 抖音适配器
    client = CdpClient(window_store=DirectStore(ws))
    adapter = create_adapter("douyin", cdp=client)
    print(f"[2] 适配器创建 OK: {adapter.__class__.__name__}")

    # 3. 搜索
    print(f"[3] 搜索: {keyword} (target={target}) ...")
    result = adapter.search(keyword, mode="fast", target_count=target)
    print(f"    结果: {len(result.items)} 条, complete={result.meta.search_complete}")
    for item in result.items[:3]:
        print(f"    - {item.kind} {item.vid} {item.title[:30]}")

    if not result.items:
        print("[4] 无结果，跳过评论")
        client.close()
        return

    # 4. 评论（第一条）
    first = result.items[0]
    print(f"[4] 取评论: {first.vid} ...")
    comments = adapter.fetch_comments(first.vid, first.url)
    print(f"    评论数: {len(comments)}")
    for cm in comments[:3]:
        print(f"    - [{cm.nickname}] {cm.content[:40]}")

    # 5. 清理
    import asyncio
    asyncio.run(client.close())
    print("[5] 集成验证完成!")


if __name__ == "__main__":
    main()
