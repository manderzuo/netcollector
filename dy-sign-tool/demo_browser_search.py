# -*- coding: utf-8 -*-
"""demo_browser_search.py — 浏览器驱动搜索演示。

流程：
1. CDP 连接 BitBrowser 抖音窗口
2. 导航到搜索页: 京东积分
3. 滚动页面触发懒加载,统计可见结果数
4. 提取全部作品 URL(video/note)
5. 验证能否翻页、全量采集

用法：python demo_browser_search.py <ws_url> [keyword] [max_rounds]
"""

import asyncio
import json
import sys
import urllib.parse

sys.path.insert(0, ".")
from cdp import CdpSession


async def main():
    ws = sys.argv[1]
    keyword = sys.argv[2] if len(sys.argv) > 2 else "京东积分"
    max_rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 40

    c = CdpSession(ws)
    await c.connect()
    sid = await c.attach_page()

    # 1. 导航到搜索页
    kw = urllib.parse.quote(keyword)
    url = f"https://www.douyin.com/search/{kw}?type=general"
    print(f"[1] 导航: {url}")
    await c.navigate(url, sid)
    await asyncio.sleep(8)

    # 2. 检查页面状态(是否登录/风控)
    page_info = await c.eval(
        """(() => {
          return {
            url: location.href,
            title: document.title,
            bodyLen: (document.body ? document.body.innerText : '').length,
            hasLogin: /登录/.test(document.body ? document.body.innerText : ''),
            hasCaptcha: /滑块|安全验证|验证码|拖动/.test(document.body ? document.body.innerText : ''),
          };
        })()""", sid)
    print(f"[2] 页面: url={page_info['url'][:60]}")
    print(f"    标题={page_info['title'][:30]} 正文长度={page_info['bodyLen']} "
          f"登录={page_info['hasLogin']} 验证码={page_info['hasCaptcha']}")

    if page_info["hasCaptcha"]:
        print("[!] 检测到验证码!无法继续,请人工处理")
        await c.close()
        return

    # 3. 滚动翻页 + 统计
    seen_ids = set()
    rounds = 0
    last_count = 0
    stale = 0
    no_more = False

    while rounds < max_rounds:
        rounds += 1
        # 滚动
        await c.eval("window.scrollBy(0, 900)", sid)
        await asyncio.sleep(2.0)

        # 统计当前可见结果
        info = await c.eval(
            """(() => {
              const ids = new Set();
              const items = [];
              for (const a of document.querySelectorAll('a[href*="/video/"],a[href*="/note/"]')) {
                const m = (a.href || '').match(/\\/(?:video|note)\\/([A-Za-z0-9_-]+)/);
                if (m) {
                  ids.add(m[1]);
                  items.push({kind: m[1] === 'note' ? 'note' : 'video', vid: m[2], href: a.href});
                }
              }
              const sc = document.scrollingElement || document.documentElement;
              const text = (document.body ? document.body.innerText : '') || '';
              return {
                ids: [...ids],
                count: ids.size,
                items: items.slice(0, 3),
                atBottom: sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 24,
                height: sc.scrollHeight,
                scrollTop: sc.scrollTop,
                endText: /暂时没有更多了|没有更多|暂无更多|到底了|已显示全部|没有找到更多/.test(text),
              };
            })()""", sid)

        count = info.get("count", 0)
        new_ids = [x for x in info.get("ids", []) if x not in seen_ids]
        seen_ids.update(info.get("ids", []))
        if new_ids:
            stale = 0
            print(f"[第{rounds}轮] 新增{len(new_ids)} 累计去重{len(seen_ids)} "
                  f"底部={info.get('atBottom')} 结束文案={info.get('endText')}")
        else:
            stale += 1
            print(f"[第{rounds}轮] 无新增(连续{stale}轮)")

        # 结束条件
        if info.get("endText"):
            print(f"[!] 页面提示: 没有更多了")
            no_more = True
            break
        if info.get("atBottom") and stale >= 5:
            print(f"[!] 已到底部且连续{stale}轮无新增")
            no_more = True
            break
        if info.get("atBottom") and rounds >= max_rounds * 0.6 and stale >= 2:
            print(f"[!] 接近底部且无新增,提前结束")
            no_more = True
            break

    # 4. 结果汇总
    print(f"\n=== 汇总 ===")
    print(f"搜索关键词: {keyword}")
    print(f"滚动轮数: {rounds}")
    print(f"累计去重作品: {len(seen_ids)}")
    print(f"是否到底: {no_more}")

    # 5. 提取全部作品信息
    all_items = await c.eval(
        """(() => {
          const out = []; const seen = new Set();
          for (const a of document.querySelectorAll('a[href*="/video/"],a[href*="/note/"]')) {
            const href = a.href || '';
            const m = href.match(/\\/(?:video|note)\\/([A-Za-z0-9_-]+)/);
            if (!m || seen.has(m[2])) continue;
            seen.add(m[2]);
            const kind = m[1];
            out.push({
              vid: m[2],
              kind: kind,
              url: href,
              title: (a.innerText || a.textContent || '').trim().slice(0, 60),
            });
          }
          return out;
        })()""", sid)
    print(f"DOM 提取作品数: {len(all_items)}")
    print(f"\n前 10 个作品:")
    for item in all_items[:10]:
        print(f"  {item['kind']:6s} {item['vid']}  {item['title'][:40]}")
    if len(all_items) > 10:
        print(f"  ... 共 {len(all_items)} 个")

    await c.close()
    print(f"\n[完成] 浏览器驱动搜索演示结束")


if __name__ == "__main__":
    asyncio.run(main())
