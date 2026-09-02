# -*- coding: utf-8 -*-
"""verify_new_service.py — 验证新版签名服务的签名被抖音接受。

流程：BitBrowser 取真实 cookie → 新版签名服务生成 a_bogus → 请求评论接口。
"""

import asyncio
import json
import os
import sys
import urllib.parse
import urllib.request

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PARENT)
from cdp import CdpSession

COMMENT_API = "https://www.douyin.com/aweme/v1/web/comment/list/"


async def get_cookie_str(ws_url):
    c = CdpSession(ws_url)
    await c.connect()
    sid = await c.attach_page()
    r = await c.cmd("Network.getAllCookies", {}, session_id=sid)
    cookies = r.get("cookies", [])
    dy = [ck for ck in cookies if "douyin.com" in str(ck.get("domain", ""))]
    ck = "; ".join(f"{c['name']}={c['value']}" for c in dy)
    await c.close()
    return ck


def main():
    ws = sys.argv[1]
    aweme_id = sys.argv[2]
    cookie = asyncio.run(get_cookie_str(ws))
    print(f"[1] cookie len={len(cookie)}")

    params = urllib.parse.urlencode({
        "aweme_id": aweme_id, "cursor": "0", "count": "20",
        "item_type": "0", "device_platform": "webapp", "aid": "6383",
    })
    url = f"{COMMENT_API}?{params}"

    # 从新版服务取签名
    req = urllib.request.Request(
        "http://127.0.0.1:8766/sign?url=" + urllib.parse.quote(url, safe=""),
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        a_bogus = json.loads(resp.read().decode("utf-8"))["a_bogus"]
    print(f"[2] 新服务 a_bogus len={len(a_bogus)} prefix={a_bogus[:40]}")

    full = f"{url}&a_bogus={urllib.parse.quote(a_bogus, safe='')}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.douyin.com/",
        "Cookie": cookie,
    }
    r = urllib.request.Request(full, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8", errors="replace")

    print(f"[3] HTTP {status}")
    if status == 200:
        try:
            data = json.loads(body)
            n = len(data.get("comments") or [])
            print(f"[4] 新版服务签名被接受! 评论数={n}, has_more={data.get('has_more')}")
            print("    VERDICT: PASS")
        except json.JSONDecodeError:
            print("[4] 200 但非 JSON（cookie 可能无效）")
            print("    VERDICT: SUSPECT")
    else:
        print(f"[4] 失败: {body[:200]}")
        print("    VERDICT: FAIL")


if __name__ == "__main__":
    main()
