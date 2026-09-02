# -*- coding: utf-8 -*-
"""test_douyin_adapter.py — 新架构抖音适配器端到端验证。

用法：
  python test_douyin_adapter.py <ws_url> [keyword] [limit]

流程：用真实 BitBrowser ws 连接 → 新适配器搜索 → 打印结果元数据。
"""

import asyncio
import sys

sys.path.insert(0, "leadharvest")
from app.platform.douyin import DouyinAdapter
from app.browser.cdp_client import CdpClient, WindowStore


class DirectStore:
    """直接用 ws_url 的窗口存储（测试用）。"""

    def __init__(self, ws_url):
        self._ws = ws_url

    def resolve(self, window_id: str) -> str:
        return self._ws


async def main():
    ws = sys.argv[1]
    keyword = sys.argv[2] if len(sys.argv) > 2 else "快递柜"
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 10

    client = CdpClient(window_store=DirectStore(ws))
    adapter = DouyinAdapter(cdp=client)

    print(f"[1] 搜索关键词: {keyword} (limit={limit})")
    result = await adapter._search(keyword, mode="fast", target_count=limit)
    print(f"[2] 结果: {len(result.items)} 条")
    print(f"    meta: complete={result.meta.search_complete} "
          f"reached={result.meta.reached_target} rounds={result.meta.rounds}")
    for item in result.items[:5]:
        print(f"    - {item.kind:6s} {item.vid}  {item.title[:40]}")

    print(f"[3] 取第一条评论 (如果存在)...")
    if result.items:
        first = result.items[0]
        comments = await adapter._fetch_comments(
            first.vid, first.url, quiet=4, max_work=60)
        print(f"    评论数: {len(comments)}")
        for cm in comments[:3]:
            print(f"    - [{cm.nickname}] {cm.content[:40]}")

    await client.close()
    print("[4] 完成")


if __name__ == "__main__":
    asyncio.run(main())
