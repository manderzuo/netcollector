# -*- coding: utf-8 -*-
"""run_probe_real.py — 用 BitBrowser 真实 cookie 跑签名失效探测。

用法：python run_probe_real.py <ws_url> <aweme_id>
"""

import asyncio
import json
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, ".")
from cdp import CdpSession


async def get_cookie_str(ws_url):
    c = CdpSession(ws_url)
    await c.connect()
    sid = await c.attach_page()
    r = await c.cmd("Network.getAllCookies", {}, session_id=sid)
    cookies = r.get("cookies", [])
    dy = [ck for ck in cookies if "douyin.com" in str(ck.get("domain", ""))]
    ck_str = "; ".join(f"{c['name']}={c['value']}" for c in dy)
    await c.close()
    return ck_str


def probe(aweme_id, cookie):
    from dy_sign.ab_pure import ABogusPureSigner
    signer = ABogusPureSigner(fixed=False)
    params = urllib.parse.urlencode({
        "aweme_id": aweme_id, "cursor": "0", "count": "20",
        "item_type": "0", "device_platform": "webapp", "aid": "6383",
    })
    url = f"https://www.douyin.com/aweme/v1/web/comment/list/?{params}"
    a_bogus = signer.sign(url)
    full = f"{url}&a_bogus={urllib.parse.quote(a_bogus, safe='')}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.douyin.com/",
        "Cookie": cookie,
    }
    req = urllib.request.Request(full, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8", errors="replace")
    print(f"[probe] HTTP {status}")
    try:
        data = json.loads(body)
        n = len(data.get("comments") or [])
        print(f"[probe] 评论数={n}, has_more={data.get('has_more')}")
        print("[probe] VERDICT: PASS" if status == 200 else "[probe] VERDICT: FAIL")
    except json.JSONDecodeError:
        print("[probe] 200 但非 JSON（cookie 可能无效）")
        print("[probe] body:", body[:200])


async def main():
    ws = sys.argv[1]
    aweme_id = sys.argv[2]
    print("[1] 从 BitBrowser 取真实 cookie ...")
    ck = await get_cookie_str(ws)
    print(f"[2] cookie 长度={len(ck)}")
    probe(aweme_id, ck)


if __name__ == "__main__":
    asyncio.run(main())
