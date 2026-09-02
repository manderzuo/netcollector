# -*- coding: utf-8 -*-
"""extract_card_ids.py — 从搜索结果卡片提取作品 ID。"""

import asyncio
import json
import sys

sys.path.insert(0, ".")
from cdp import CdpSession


async def main():
    c = CdpSession(sys.argv[1])
    await c.connect()
    sid = await c.attach_page()
    await asyncio.sleep(20)

    info = await c.eval(
        """(() => {
          const cards = document.querySelectorAll('.search-result-card');
          const out = [];
          for (const card of Array.from(cards)) {
            // 找卡片上所有可能的 ID 来源
            const allAttrs = {};
            // 检查 card 本身和父级
            let node = card;
            for (let i = 0; i < 3 && node; i++) {
              const attrs = node.getAttributeNames ? node.getAttributeNames() : [];
              for (const a of attrs) {
                const v = node.getAttribute(a);
                if (v && /(id|url|href|link|route|data)/i.test(a) && String(v).length > 8) {
                  allAttrs[a] = String(v).slice(0, 150);
                }
              }
              node = node.parentElement;
            }
            // 找所有带 href 或 data 的元素
            const clickables = [...card.querySelectorAll('[href],[data-e2e],[data-id]')].slice(0, 3)
              .map(el => ({
                tag: el.tagName,
                href: el.getAttribute('href') || null,
                dataE2e: el.getAttribute('data-e2e') || null,
                dataId: el.getAttribute('data-id') || null,
              }));
            // 文本
            const text = (card.innerText || '').trim().slice(0, 100);
            out.push({ text, allAttrs, clickables });
          }
          return out;
        })()""", sid)
    print(json.dumps(info, ensure_ascii=False, indent=2)[:3000])
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
