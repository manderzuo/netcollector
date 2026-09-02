# -*- coding: utf-8 -*-
"""retry_browser_search.py — 重试浏览器搜索(更长等待+交互检查)。"""

import asyncio
import json
import sys

sys.path.insert(0, ".")
from cdp import CdpSession


async def main():
    c = CdpSession(sys.argv[1])
    await c.connect()
    sid = await c.attach_page()

    await c.navigate("https://www.douyin.com/search/%E4%BA%AC%E4%B8%9C%E7%A7%AF%E5%88%86?type=general", sid)
    print("等待 20s 让 SPA 加载...")
    await asyncio.sleep(20)

    # 检查是否有"综合"标签可选
    tabs = await c.eval(
        """(() => {
          const spans = [...document.querySelectorAll('span,div,li')];
          const hits = spans.filter(el => {
            const t = (el.innerText || '').trim();
            return (t === '综合' || t === '视频' || t === '用户' || t === '直播') && el.children.length <= 2;
          }).map(el => ({text: (el.innerText||'').trim(), tag: el.tagName}));
          return hits.slice(0, 10);
        })()""", sid)
    print(f"可点击标签: {json.dumps(tabs, ensure_ascii=False)}")

    # 检查结果区
    info = await c.eval(
        """(() => {
          const text = document.body ? document.body.innerText : '';
          // 找"为你找到 X 个结果"这类文案
          const m = text.match(/为你找到[^\\n]*/);
          const videoLinks = document.querySelectorAll('a[href*="/video/"]').length;
          const noteLinks = document.querySelectorAll('a[href*="/note/"]').length;
          // 找包含 swiper/feed/result 的容器
          const containers = [...document.querySelectorAll('[class*="feed"],[class*="result"],[class*="search"],[class*="swiper"]')]
            .filter(el => el.children.length > 0).slice(0, 8)
            .map(el => ({cls: (typeof el.className === 'string' ? el.className : '').slice(0, 60), kids: el.children.length}));
          return {
            foundText: m ? m[0] : null,
            videoLinks, noteLinks,
            bodyLen: text.length,
            containers,
          };
        })()""", sid)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
