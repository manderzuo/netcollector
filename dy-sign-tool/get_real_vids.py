# -*- coding: utf-8 -*-
"""get_real_vids.py — 从抖音页面提取真实视频/笔记 ID。"""

import asyncio
import json
import sys

sys.path.insert(0, ".")
from cdp import CdpSession


async def main():
    ws = sys.argv[1]
    c = CdpSession(ws)
    await c.connect()
    sid = await c.attach_page()
    links = await c.eval(
        """(() => {
      const ids = new Set();
      for (const a of document.querySelectorAll('a[href*="/video/"],a[href*="/note/"]')) {
        const m = (a.href || '').match(/\\/(?:video|note)\\/([0-9]+)/);
        if (m) ids.add(m[1]);
      }
      return [...ids].slice(0, 10);
    })()""",
        sid,
    )
    print("REAL_VIDS:", json.dumps(links, ensure_ascii=False))
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
