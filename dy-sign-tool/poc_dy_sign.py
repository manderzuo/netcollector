# -*- coding: utf-8 -*-
"""poc_dy_sign.py — 抖音签名服务 POC 验证。

流程：
1. 通过 BitBrowser API 打开抖音窗口（或复用已打开窗口）
2. CDP 连接窗口 → 取当前页面 URL / cookie（真实登录态）
3. 用签名服务生成 a_bogus
4. 用真实 cookie + a_bogus 请求 /aweme/v1/web/comment/list/
5. 判断：200 = 签名有效；403/参数错误 = 签名失效

用法：
  python poc_dy_sign.py --window 65b60ce115d24f56a85cbcc5b48ac9ca \
      --sign http://127.0.0.1:8765
"""

import argparse
import asyncio
import json
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, ".")
from cdp import CdpSession
from bitbrowser import BitBrowserClient

COMMENT_API = "https://www.douyin.com/aweme/v1/web/comment/list/"


async def get_cookies(c, sid):
    """用 CDP Network.getAllCookies 拿当前窗口全部 cookie（含登录态）。"""
    r = await c.cmd("Network.getAllCookies", {}, session_id=sid)
    cookies = r.get("cookies", [])
    # 过滤抖音域
    dy = [ck for ck in cookies if "douyin.com" in str(ck.get("domain", ""))]
    return dy


def cookie_header(cookies):
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def fetch_with_sign(url, cookie_str, sign_service):
    """生成 a_bogus 并请求（带 cookie + 真实 UA）。"""
    # 1. 生成签名（签名服务需要完整 URL）
    q = urllib.parse.urlsplit(url).query
    sig_url = url
    req = urllib.request.Request(
        f"{sign_service}/sign?url=" + urllib.parse.quote(sig_url, safe=""),
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        sig = json.loads(resp.read().decode("utf-8"))["a_bogus"]
    # 2. 拼回 URL
    sep = "&" if q else "?"
    full = f"{url}{sep}a_bogus={urllib.parse.quote(sig, safe='')}"
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/148.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.douyin.com/",
        "Cookie": cookie_str,
    }
    r = urllib.request.Request(full, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="65b60ce115d24f56a85cbcc5b48ac9ca")
    ap.add_argument("--sign", default="http://127.0.0.1:8765")
    ap.add_argument("--aweme-id", default="")
    args = ap.parse_args()

    bb = BitBrowserClient()
    # 1. 打开窗口拿 ws
    opened = bb.open_browser(args.window)
    ws_url = opened.get("ws")
    print("[1] 窗口已打开 ws =", ws_url)

    c = CdpSession(ws_url)
    await c.connect()
    sid = await c.attach_page()
    print("[2] CDP 已连接 sessionId =", sid)

    # 取页面 URL（确认在抖音页）
    page_url = await c.eval("location.href", sid)
    print("[3] 当前页面 =", page_url)

    # 3. 取 cookie
    cookies = await get_cookies(c, sid)
    ck_str = cookie_header(cookies)
    print(f"[4] 拿到 {len(cookies)} 个抖音 cookie, 长度={len(ck_str)}")
    names = [x["name"] for x in cookies]
    print("    cookie 名称:", names[:20])

    # 4. 挑一个真实 aweme_id（从页面或默认）
    aweme_id = args.aweme_id
    if not aweme_id:
        # 从当前页面 URL 提取
        import re
        m = re.search(r"/(?:video|note)/(\d+)", page_url or "")
        if m:
            aweme_id = m.group(1)
    if not aweme_id:
        aweme_id = "7300000000000000000"  # 测试占位
    print(f"[5] 测试 aweme_id = {aweme_id}")

    # 5. 构造评论接口 URL 并签名请求
    params = urllib.parse.urlencode({
        "aweme_id": aweme_id,
        "cursor": "0",
        "count": "20",
        "item_type": "0",
        "device_platform": "webapp",
        "aid": "6383",
    })
    url = f"{COMMENT_API}?{params}"
    print("[6] 请求评论接口（带 a_bogus + cookie）...")
    status, body = fetch_with_sign(url, ck_str, args.sign)
    print(f"[7] HTTP {status}")
    if status == 200:
        try:
            data = json.loads(body)
            n = len(data.get("comments") or [])
            has_more = data.get("has_more")
            print(f"    [OK] 签名有效! 返回 {n} 条评论, has_more={has_more}")
            if n > 0:
                c0 = data["comments"][0]
                print("    示例:", json.dumps({
                    "cid": c0.get("cid"),
                    "text": (c0.get("text") or "")[:60],
                    "user": (c0.get("user") or {}).get("nickname", ""),
                }, ensure_ascii=False))
        except Exception as exc:
            print("    [WARN] 200 但解析失败:", exc, body[:300])
    else:
        print("    [FAIL] 签名可能失效或被风控")
        print("    响应体:", body[:400])
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
