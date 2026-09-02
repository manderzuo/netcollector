# -*- coding: utf-8 -*-
"""inspect_dy_page.py — 检查抖音窗口页面状态。"""

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
    info = await c.eval(
        """(() => {
      return {
        url: location.href,
        title: document.title,
        ready: document.readyState,
        bodyText: (document.body ? document.body.innerText : '').slice(0, 500),
        links: [...document.querySelectorAll('a[href*="/video/"],a[href*="/note/"]')].slice(0,5).map(a => a.href),
        hasLogin: /登录/.test(document.body ? document.body.innerText : ''),
      };
    })()""",
        sid,
    )
    print(json.dumps(info, ensure_ascii=False, indent=2))
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
