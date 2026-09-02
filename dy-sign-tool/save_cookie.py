# -*- coding: utf-8 -*-
"""save_cookie.py — 从 BitBrowser 抖音窗口取 cookie 存入 cookie.txt。

用法：
  python save_cookie.py <ws_url>

说明：
- 仅用 cdp.py 取一次 cookie（本工具内独立），不触碰 src/ 采集代码。
- 取到的 cookie 写入 dy-sign-tool/cookie.txt，供采集器使用。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CdpSession


async def get_cookie_str(ws_url: str) -> str:
    c = CdpSession(ws_url)
    await c.connect()
    sid = await c.attach_page()
    r = await c.cmd("Network.getAllCookies", {}, session_id=sid)
    cookies = r.get("cookies", [])
    dy = [ck for ck in cookies if "douyin.com" in str(ck.get("domain", ""))]
    ck_str = "; ".join(f"{c['name']}={c['value']}" for c in dy)
    await c.close()
    return ck_str


def main():
    if len(sys.argv) < 2:
        print("用法: python save_cookie.py <ws_url>")
        return
    ws = sys.argv[1]
    print("[1] 从 BitBrowser 窗口取 cookie ...")
    ck = asyncio.run(get_cookie_str(ws))
    if not ck:
        print("[FAIL] 未取到 cookie")
        return
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(ck)
    print(f"[OK] cookie 已保存 ({len(ck)} 字符): {path}")


if __name__ == "__main__":
    main()
