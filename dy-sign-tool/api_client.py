# -*- coding: utf-8 -*-
"""api_client.py — 抖音纯 API 客户端（dy-sign-tool 内独立运行）。

职责：
- 用签名算法本地生成 a_bogus（不依赖外部 HTTP 签名服务）
- 构造带签名的 API 请求（搜索 / 评论 / 二级评论）
- 管理真实登录 cookie（从 BitBrowser 窗口取一次，之后纯 HTTP）

依赖：仅 dy-sign-tool 内部模块，不触碰 src/ 下任何采集代码。
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

# 引入签名库（signature_rewrite 是 clean-room 重写版）
sys.path.insert(0, "signature_rewrite")
from sig.a_bogus import ABogusSigner  # noqa: E402

# 接口地址
SEARCH_API = "https://www.douyin.com/aweme/v1/web/general/search/stream/"
COMMENT_API = "https://www.douyin.com/aweme/v1/web/comment/list/"
REPLY_API = "https://www.douyin.com/aweme/v1/web/comment/list/reply/"

# 浏览器 UA（与签名算法环境一致）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")


class ApiError(Exception):
    """API 请求失败。"""

    def __init__(self, status: int, message: str = "", body: str = ""):
        self.status = status
        self.body = body
        super().__init__(message or f"HTTP {status}")


class DouyinApiClient:
    """抖音 API 客户端：本地签名 + 真实 cookie 直连。"""

    def __init__(self, cookie_str: str = "", backoff: bool = True,
                 max_retries: int = 3, backoff_base: int = 30):
        self.cookie = cookie_str
        self.backoff = backoff
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        # 本地签名器（fixed=False = 实时时间戳/随机数）
        self._signer = ABogusSigner(fixed=False)

    # ------------------------------------------------------------------
    # 核心请求
    # ------------------------------------------------------------------
    def _http_get(self, full_url: str) -> str:
        """单次 HTTP GET，返回响应体。"""
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.douyin.com/",
            "Cookie": self.cookie,
        }
        req = urllib.request.Request(full_url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ApiError(exc.code, f"HTTP {exc.code}", body[:500])
        except Exception as exc:  # noqa: BLE001
            raise ApiError(0, f"网络错误: {exc}")

    def _is_risk(self, exc: ApiError) -> bool:
        """判断是否为风控(403/验证码/风控文案)。"""
        if exc.status == 403:
            return True
        body = (exc.body or "").lower()
        return any(k in body for k in (
            "captcha", "verify", "滑块", "验证码", "risk",
            "frequently", "too many", "访问频繁", "操作频繁",
        ))

    def request_raw(self, api_url: str, params: Dict) -> str:
        """带签名请求 API，返回原始响应体（含风控退避重试）。"""
        # 1. 拼参数
        query = urllib.parse.urlencode(params)
        url = f"{api_url}?{query}"
        # 2. 本地生成 a_bogus（对完整 URL 签名）
        a_bogus = self._signer.sign(url)
        full = f"{url}&a_bogus={urllib.parse.quote(a_bogus, safe='')}"

        if not self.backoff:
            return self._http_get(full)

        # 3. 风控退避重试
        import time
        last_exc = None
        for attempt in range(self.max_retries):
            try:
                return self._http_get(full)
            except ApiError as exc:
                last_exc = exc
                if not self._is_risk(exc):
                    raise
                wait = self.backoff_base * (attempt + 1)
                print(f"  [风控退避] HTTP {exc.status} 被拦,"
                      f"等待 {wait}s 重试 ({attempt + 1}/{self.max_retries})")
                time.sleep(wait)
        raise RuntimeError(f"风控重试耗尽: {last_exc}")

    def request(self, api_url: str, params: Dict) -> dict:
        """带签名请求 API，返回 JSON（普通 JSON 接口）。"""
        body = self.request_raw(api_url, params)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ApiError(0, f"响应非 JSON: {exc}", body[:300])

    def request_ndjson(self, api_url: str, params: Dict) -> List[dict]:
        """带签名请求 API，返回 NDJSON 流解析后的对象列表。

        抖音 /general/search/stream/ 返回 SSE 分块：
          十六进制长度行\r\n + JSON 块 交替出现。
        """
        body = self.request_raw(api_url, params)
        return self._parse_ndjson(body)

    @staticmethod
    def _parse_ndjson(raw: str) -> List[dict]:
        """解析 NDJSON(SSE) 流：跳过十六进制长度行，提取 JSON 块。"""
        objects = []
        chunks = raw.split("\r\n")
        buf = ""
        for ch in chunks:
            ch = ch.strip()
            if not ch:
                continue
            try:
                int(ch, 16)  # 十六进制长度行 → 跳过
                continue
            except ValueError:
                pass
            buf += ch
            try:
                obj = json.loads(buf)
                buf = ""
            except json.JSONDecodeError:
                try:
                    obj = json.loads(ch)
                    buf = ""
                except json.JSONDecodeError:
                    continue
            if isinstance(obj, dict):
                objects.append(obj)
        return objects

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------
    def search(self, keyword: str, offset: int = 0, count: int = 20,
               sort_type: str = "0") -> dict:
        """综合搜索（图文+视频），返回聚合后的 dict。

        stream 接口返回 NDJSON 分块，这里解析后聚合为统一的
        {"data": [...], "has_more": bool} 结构。
        """
        params = {
            "keyword": keyword,
            "offset": str(offset),
            "count": str(count),
            "sort_type": sort_type,
            "publish_time": "0",
            "search_id": "",
            "device_platform": "webapp",
            "aid": "6383",
        }
        objects = self.request_ndjson(SEARCH_API, params)
        # 聚合所有块的 data / has_more / cursor
        merged_data = []
        has_more = False
        cursor = ""
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            data = obj.get("data") or []
            if isinstance(data, list):
                merged_data.extend(data)
            if obj.get("has_more"):
                has_more = True
            if obj.get("cursor") is not None and not cursor:
                cursor = str(obj.get("cursor"))
        return {"data": merged_data, "has_more": has_more, "cursor": cursor}

    # ------------------------------------------------------------------
    # 评论
    # ------------------------------------------------------------------
    def comments(self, aweme_id: str, cursor: str = "0", count: int = 20) -> dict:
        """一级评论列表。"""
        params = {
            "aweme_id": aweme_id,
            "cursor": cursor,
            "count": str(count),
            "item_type": "0",
            "device_platform": "webapp",
            "aid": "6383",
        }
        return self.request(COMMENT_API, params)

    def replies(self, aweme_id: str, comment_id: str,
                cursor: str = "0", count: int = 20) -> dict:
        """二级评论（楼中楼）。"""
        params = {
            "item_id": aweme_id,
            "comment_id": comment_id,
            "cursor": cursor,
            "count": str(count),
            "item_type": "0",
            "device_platform": "webapp",
            "aid": "6383",
        }
        return self.request(REPLY_API, params)

    # ------------------------------------------------------------------
    # 解析工具
    # ------------------------------------------------------------------
    @staticmethod
    def parse_search_results(data: dict) -> List[dict]:
        """从搜索响应提取作品列表。"""
        items = []
        for d in data.get("data") or []:
            if not isinstance(d, dict):
                continue
            ai = d.get("aweme_info") or d
            aid = ai.get("aweme_id")
            if not aid:
                continue
            author = ai.get("author") or {}
            # 判定类型
            imgs = ai.get("images") or []
            kind = "note" if (isinstance(imgs, list) and len([x for x in imgs if x]) > 0) \
                or str(ai.get("aweme_type")) == "68" else "video"
            items.append({
                "vid": str(aid),
                "url": f"https://www.douyin.com/{kind}/{aid}",
                "kind": kind,
                "title": (ai.get("desc") or "")[:300],
                "author": author.get("nickname", ""),
                "sec_uid": author.get("sec_uid", ""),
                "create_time": ai.get("create_time"),
                "comment_count": (ai.get("statistics") or {}).get("comment_count", ""),
            })
        return items

    @staticmethod
    def parse_comments(data: dict, parent_id: str = "") -> List[dict]:
        """从评论响应提取评论。"""
        items = []
        for cm in data.get("comments") or []:
            if not isinstance(cm, dict):
                continue
            user = cm.get("user") or {}
            ct = cm.get("create_time")
            try:
                ct_num = int(ct)
            except (TypeError, ValueError):
                ct_num = 0
            import time
            items.append({
                "cid": str(cm.get("cid", "")),
                "text": cm.get("text") or "",
                "region": cm.get("ip_label") or "",
                "create_time": ct,
                "create_time_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(ct_num)) if ct_num else "",
                "digg_count": cm.get("digg_count"),
                "user_id": user.get("uid", ""),
                "sec_uid": user.get("sec_uid", ""),
                "nickname": user.get("nickname", ""),
                "homepage": "https://www.douyin.com/user/" + str(user.get("sec_uid", "")),
                "reply_total": cm.get("reply_comment_total", 0),
                "parent_id": parent_id,
            })
        return items

    @staticmethod
    def has_more(data: dict) -> bool:
        """是否还有下一页。"""
        return bool(data.get("has_more"))
