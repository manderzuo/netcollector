# -*- coding: utf-8 -*-
"""inspect_search_page.py — 检查搜索页实际内容。"""

import asyncio
import json
import sys

sys.path.insert(0, ".")
from cdp import CdpSession


async def main():
    c = CdpSession(sys.argv[1])
    await c.connect()
    sid = await c.attach_page()
    await asyncio.sleep(6)
    info = await c.eval(
        """(() => {
          const text = document.body ? document.body.innerText : '';
          return {
            url: location.href,
            ready: document.readyState,
            bodyText: text.slice(0, 600),
            linkCount: document.querySelectorAll('a').length,
            videoLinks: document.querySelectorAll('a[href*="/video/"]').length,
            noteLinks: document.querySelectorAll('a[href*="/note/"]').length,
            bodyLen: text.length,
          };
        })()""", sid)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
