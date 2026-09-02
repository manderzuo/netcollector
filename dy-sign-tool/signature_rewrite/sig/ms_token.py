# -*- coding: utf-8 -*-
"""ms_token.py — msToken 动态获取。

通过向字节跳动安全 SDK 上报接口发送指纹载荷，换取服务端签发的 msToken：
- 优先读取响应头 x-ms-token；
- 否则从 set-cookie 中解析 msToken。

设计说明：
- 进程内缓存，TTL 600 秒，避免高频重复上报。
- 失败返回空串（不抛出），调用方按无 msToken 处理。
"""

import os
import re
import time

import requests

requests.packages.urllib3.disable_warnings()

from .env_profile import browser_profile
from .report_body import build_envelope

_REPORT_URL = "https://mssdk.bytedance.com/web/common?ms_appid=6383"

_cache = {"token": "", "ts": 0}
_TTL_SECONDS = 600


def _extract_ttwid(ttwid: str = None) -> str:
    """取 ttwid：显式传入优先，否则从环境变量 DY_COOKIES 解析。"""
    if ttwid:
        return ttwid
    match = re.search(r"ttwid=([^;]+)", os.getenv("DY_COOKIES") or "")
    return match.group(1) if match else ""


def fetch_ms_token(ttwid: str = None, proxies: dict = None,
                   use_cache: bool = True) -> str:
    """获取 msToken（带缓存）。"""
    if use_cache and _cache["token"] and (time.time() - _cache["ts"] < _TTL_SECONDS):
        return _cache["token"]

    envelope = build_envelope()
    tw = _extract_ttwid(ttwid)
    profile = browser_profile()
    headers = {
        "user-agent": profile["ua"],
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "text/plain;charset=UTF-8",
        "origin": "https://www.douyin.com",
        "referer": "https://www.douyin.com/",
        "cookie": f"ttwid={tw}" if tw else "",
    }
    try:
        resp = requests.post(_REPORT_URL, data=envelope.encode("utf-8"),
                             headers=headers, verify=False, timeout=25,
                             proxies=proxies)
        token = resp.headers.get("x-ms-token", "")
        if not token:
            match = re.search(r"msToken=([^;]+)",
                              resp.headers.get("set-cookie", ""))
            token = match.group(1) if match else ""
        if token:
            _cache["token"] = token
            _cache["ts"] = time.time()
        return token
    except Exception:  # noqa: BLE001
        return ""
