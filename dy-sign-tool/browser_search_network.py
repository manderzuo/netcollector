# -*- coding: utf-8 -*-
"""browser_search_network.py — 浏览器搜索 + 拦截接口拿 ID。

这是"浏览器驱动搜索"最可靠的方式：
页面渲染的同时，旁观拦截 /general/search/stream/ 响应，直接拿结构化 ID。
"""

import asyncio
import json
import sys

sys.path.insert(0, ".")
from cdp import CdpSession


async def main():
    c = CdpSession(sys.argv[1])
    await c.connect()
    sid = await c.attach_page()

    # 注册响应拦截
    responses = []
    c.on("Network.responseReceived", lambda p: responses.append(
        (p.get("response", {}).get("url", ""), p.get("requestId", ""))))
    await c.cmd("Network.enable", {}, session_id=sid)

    await c.navigate("https://www.douyin.com/search/%E4%BA%AC%E4%B8%9C%E7%A7%AF%E5%88%86?type=general", sid)
    print("等待搜索 + 拦截响应(25s)...")
    await asyncio.sleep(25)

    # 过滤搜索接口响应
    search_resps = [r for r in responses if "general/search/stream" in r[0]]
    print(f"拦截到搜索接口响应: {len(search_resps)} 个")

    # 解析响应体
    all_vids = []
    for url, rid in search_resps:
        try:
            body = await c.get_body(rid, sid)
        except Exception:
            continue
        # 解析 NDJSON
        import re
        for chunk in re.split(r"\r?\n", body):
            chunk = chunk.strip()
            if not chunk or not chunk.startswith("{"):
                continue
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            for d in obj.get("data") or []:
                if not isinstance(d, dict):
                    continue
                ai = d.get("aweme_info") or d
                aid = ai.get("aweme_id")
                if aid:
                    imgs = ai.get("images") or []
                    kind = "note" if (isinstance(imgs, list) and len(imgs) > 0) else "video"
                    all_vids.append({
                        "vid": str(aid),
                        "kind": kind,
                        "title": (ai.get("desc") or "")[:40],
                        "author": (ai.get("author") or {}).get("nickname", ""),
                    })

    # 去重
    seen = set()
    unique = []
    for v in all_vids:
        if v["vid"] not in seen:
            seen.add(v["vid"])
            unique.append(v)

    print(f"\n=== 浏览器搜索 + 接口拦截 结果 ===")
    print(f"关键词: 京东积分")
    print(f"拦截到作品: {len(unique)} 个")
    for v in unique[:10]:
        print(f"  {v['kind']:6s} {v['vid']}  {v['title'][:35]}  @{v['author']}")
    if len(unique) > 10:
        print(f"  ... 共 {len(unique)} 个")

    # 验证翻页: 滚动触发更多
    print(f"\n=== 滚动翻页测试 ===")
    before = len(unique)
    for i in range(10):
        await c.eval("window.scrollBy(0, 1200)", sid)
        await asyncio.sleep(3)
        for url, rid in list(responses):
            if "general/search/stream" not in url:
                continue
            try:
                body = await c.get_body(rid, sid)
            except Exception:
                continue
            import re
            for chunk in re.split(r"\r?\n", body):
                chunk = chunk.strip()
                if not chunk.startswith("{"):
                    continue
                try:
                    obj = json.loads(chunk)
                except Exception:
                    continue
                for d in obj.get("data") or []:
                    ai = d.get("aweme_info") or d
                    aid = ai.get("aweme_id")
                    if aid and str(aid) not in seen:
                        seen.add(str(aid))
                        imgs = ai.get("images") or []
                        kind = "note" if (isinstance(imgs, list) and len(imgs) > 0) else "video"
                        unique.append({"vid": str(aid), "kind": kind,
                                       "title": (ai.get("desc") or "")[:40],
                                       "author": (ai.get("author") or {}).get("nickname", "")})
        if len(unique) > before:
            print(f"  滚动 {i + 1} 次后: 累计 {len(unique)} 个")
            before = len(unique)
        if i >= 4 and len(unique) == before:
            print(f"  连续无新增,停止")
            break

    print(f"\n[完成] 浏览器搜索最终结果: {len(unique)} 个作品")
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
