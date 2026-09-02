# -*- coding: utf-8 -*-
"""inspect_result_cards.py — 检查 search-result-card 内部结构。"""

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
          const sample = [];
          for (const card of Array.from(cards).slice(0, 3)) {
            const links = [...card.querySelectorAll('a')].map(a => ({
              href: (a.href || '').slice(0, 120),
              text: (a.innerText || '').trim().slice(0, 40),
            })).filter(x => x.href);
            const img = card.querySelector('img');
            const title = card.querySelector('[class*="title"],h3,h4,[class*="content"]');
            sample.push({
              linkCount: links.length,
              links: links.slice(0, 3),
              imgSrc: img ? img.src.slice(0, 60) : null,
              titleText: title ? (title.innerText || '').trim().slice(0, 50) : null,
              text: (card.innerText || '').trim().slice(0, 80),
            });
          }
          return {
            cardCount: cards.length,
            sample,
          };
        })()""", sid)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
