# -*- coding: utf-8 -*-
"""inspect_search_raw.py — 查看搜索接口原始返回。"""

import sys

sys.path.insert(0, ".")
from api_client import DouyinApiClient, SEARCH_API
from douyin_collector import _load_cookie
import urllib.parse
import urllib.request


def main():
    cookie = _load_cookie()
    client = DouyinApiClient(cookie_str=cookie)
    params = {
        "keyword": "快递柜", "offset": "0", "count": "20",
        "sort_type": "0", "publish_time": "0", "search_id": "",
        "device_platform": "webapp", "aid": "6383",
    }
    query = urllib.parse.urlencode(params)
    url = f"{SEARCH_API}?{query}"
    sig = client._signer.sign(url)
    full = f"{url}&a_bogus={urllib.parse.quote(sig, safe='')}"
    print("[url]", full[:150])

    req = urllib.request.Request(full, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.douyin.com/",
        "Cookie": cookie,
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    print("[status]", resp.status)
    print("[content-type]", resp.headers.get("Content-Type"))
    print("[body 前 500 字符]")
    print(repr(body[:500]))


if __name__ == "__main__":
    main()
