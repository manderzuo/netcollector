# -*- coding: utf-8 -*-
"""inspect_links.py — 检查搜索页所有链接模式。"""

import asyncio
import json
import sys

sys.path.insert(0, ".")
from cdp import CdpSession


async def main():
    c = CdpSession(sys.argv[1])
    await c.connect()
    sid = await c.attach_page()

    # 重新导航并等待更久
    await c.navigate("https://www.douyin.com/search/%E4%BA%AC%E4%B8%9C%E7%A7%AF%E5%88%86?type=general", sid)
    print("等待搜索结果加载(15s)...")
    await asyncio.sleep(15)

    info = await c.eval(
        """(() => {
          const links = [...document.querySelectorAll('a')];
          const hrefs = links.map(a => a.href).filter(h => h && !h.startsWith('javascript'));
          // 统计 href 模式
          const patterns = {};
          for (const h of hrefs) {
            const m = h.match(/\\/\\/([^/]+)\\//);
            const domain = m ? m[1] : 'other';
            patterns[domain] = (patterns[domain] || 0) + 1;
          }
          // 找包含 /video/ 或 /note/ 或数字 ID 的
          const videoLike = hrefs.filter(h => /\\/video\\/|\\/note\\//.test(h));
          const modalLike = hrefs.filter(h => /modal_id/.test(h));
          const text = document.body ? document.body.innerText : '';
          return {
            totalLinks: links.length,
            domainPatterns: patterns,
            videoLike: videoLike.slice(0, 10),
            modalLike: modalLike.slice(0, 10),
            bodyText: text.slice(0, 300),
          };
        })()""", sid)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
