# -*- coding: utf-8 -*-
"""百度贴吧 API 采集适配器。

贴吧 skill 提供的是受控 API，而不是 BitBrowser 页面采集接口，因此这里把
贴吧作为 ``token + HTTPS API`` 平台接入。适配器只访问 ``tieba.baidu.com``，
不读取浏览器 Cookie，也不把 ``TB_TOKEN`` 写入日志或异常信息。

接口边界：
* 阶段 A 使用贴吧官方 ``/mo/q/search/thread`` 关键词接口搜索帖子；广场
  ``/c/f/frs/page_claw`` 仅作为无关键词时的兼容列表接口。
* 阶段 B 使用 ``/c/f/pb/page_claw`` 分页读取楼层，并保留接口返回的楼中楼。
* 只做读取；发帖/评论由后续互动中心显式调用 ``TiebaApiClient.add_post``。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

try:
    from .dy_collect import SearchVideosResult
except ImportError:  # pragma: no cover - 直接运行 src 下脚本时兼容
    from dy_collect import SearchVideosResult  # type: ignore


BASE_URL = "https://tieba.baidu.com"
PLATFORM = "tieba"
PLATFORM_LABEL = "百度贴吧"
MAX_CONTENT_LENGTH = 1000
MAX_THREAD_PAGES = 500
MAX_SEARCH_PAGES = 20


class TiebaApiError(RuntimeError):
    """贴吧 API 返回错误或连接失败。"""


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _keyword_matches(searchable: str, keyword: str) -> bool:
    """判断标题/正文是否命中关键词，兼容中英文混合词。

    贴吧搜索会把 ``PS资源`` 视为 ``PS`` 与 ``资源`` 两个词，结果标题
    往往不是连续出现 ``PS资源``。中文连续短语仍按整体匹配，英文/数字
    与中文相邻时按词段匹配，避免把结果误筛成 0 条。
    """
    needle = _clean(keyword).casefold()
    haystack = _clean(searchable).casefold()
    if not needle:
        return True
    if needle in haystack:
        return True
    parts = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", needle)
    return len(parts) > 1 and all(part in haystack for part in parts)


def _content_text(value: Any) -> str:
    """把贴吧 content 数组/字符串安全转换为纯文本。"""
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, Mapping):
        for key in ("text", "content", "desc"):
            if value.get(key):
                return _clean(value.get(key))
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            text = _content_text(item)
            if text:
                parts.append(text)
        return _clean(" ".join(parts))
    return ""


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return ""


def _nested_list(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        for key in ("sub_post_list", "post_list", "list", "replies", "items"):
            if key in value:
                return _nested_list(value.get(key))
    return []


def _author_info(item: Mapping[str, Any]) -> tuple[str, str]:
    raw = _first(item, "author", "user", "user_info", "author_info")
    if isinstance(raw, Mapping):
        uid = _clean(_first(raw, "id", "user_id", "uid", "portrait"))
        name = _clean(_first(raw, "name", "user_name", "nickname", "show_name"))
        return uid, name
    return _clean(_first(item, "user_id", "uid", "portrait")), _clean(raw)


def parse_thread_list(payload: Mapping[str, Any] | None,
                      keyword: str = "") -> list[dict[str, Any]]:
    """解析帖子列表，按 thread_id 去重并在本地过滤关键词。"""
    payload = payload if isinstance(payload, Mapping) else {}
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    # ``/c/f/frs/page_claw`` uses ``thread_list`` while the official keyword
    # search endpoint ``/mo/q/search/thread`` returns the same thread-shaped
    # records in ``post_list``.  Normalize both responses here so the rest of
    # the scheduler receives one stable item format.
    raw_items = []
    if isinstance(data, Mapping):
        raw_items = data.get("thread_list") or data.get("post_list") or []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    needle = _clean(keyword).casefold()
    for raw in raw_items or []:
        if not isinstance(raw, Mapping):
            continue
        thread_id = _clean(_first(raw, "thread_id", "tid", "id", "kz"))
        if not thread_id or thread_id in seen:
            continue
        abstract = _content_text(_first(raw, "abstract", "content", "summary"))
        title = _clean(_first(raw, "title", "name"))
        author_id, author = _author_info(raw)
        searchable = f"{title} {abstract} {author}".casefold()
        if needle and not _keyword_matches(searchable, needle):
            continue
        seen.add(thread_id)
        url = _clean(_first(raw, "url", "thread_url")) or f"{BASE_URL}/p/{thread_id}"
        out.append({
            "vid": thread_id,
            "thread_id": thread_id,
            "url": url,
            "title": title or "未命名帖子",
            "content": abstract,
            "author": author or "匿名吧友",
            "kind": "thread",
            "create_time": _clean(_first(raw, "create_time", "time", "date")),
            "extra": {
                "platform": PLATFORM,
                "platform_user_id": author_id,
                "reply_count": _first(raw, "reply_num", "reply_count"),
                "view_count": _first(raw, "view_num", "view_count"),
                "agree_count": _first(raw, "agree_num", "agree_count"),
                "forum_name": _clean(_first(raw, "forum_name")),
                "abstract": abstract,
                "source": "tieba_api_search" if data.get("post_list") else "tieba_api_thread_list",
            },
        })
    return out


def parse_post_list(payload: Mapping[str, Any] | None,
                    thread_id: str) -> list[dict[str, Any]]:
    """解析帖子楼层和接口内嵌的楼中楼，保留父子关系。"""
    payload = payload if isinstance(payload, Mapping) else {}
    raw_items = payload.get("post_list")
    if not isinstance(raw_items, list):
        page = payload.get("data")
        raw_items = page.get("post_list") if isinstance(page, Mapping) else []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_item(raw: Mapping[str, Any], parent_id: str = "") -> None:
        cid = _clean(_first(raw, "post_id", "id", "pid"))
        text = _content_text(_first(raw, "content", "text", "comment"))
        if not text or not cid or cid in seen:
            return
        seen.add(cid)
        user_id, nickname = _author_info(raw)
        out.append({
            "cid": cid,
            "post_id": cid,
            "thread_id": str(thread_id),
            "user_id": user_id,
            "nickname": nickname or "匿名吧友",
            "text": text,
            "content": text,
            "comment_time": _clean(_first(raw, "comment_time", "create_time", "time", "date")),
            "region": "",
            "homepage": f"{BASE_URL}/home/main?id={urllib.parse.quote(user_id)}" if user_id else "",
            "parent_id": parent_id,
            "reply_to": _clean(_first(raw, "reply_to", "quote_user", "to_user")),
            "extra": {
                "platform": PLATFORM,
                "is_reply": bool(parent_id),
                "parent_id": parent_id,
                "source": "tieba_api_post",
            },
        })
        for nested in _nested_list(_first(raw, "sub_post_list", "sub_posts", "replies")):
            add_item(nested, parent_id=cid)

    for raw in raw_items or []:
        if isinstance(raw, Mapping):
            add_item(raw)
    return out


class TiebaApiClient:
    """严格限定在百度贴吧官方域名上的轻量 API 客户端。"""

    def __init__(self, token: str, *, timeout: float = 30.0,
                 base_url: str = BASE_URL):
        self.token = str(token or "").strip()
        self.timeout = max(1.0, min(300.0, float(timeout or 30)))
        parsed = urllib.parse.urlsplit(str(base_url or BASE_URL).rstrip("/"))
        if parsed.scheme != "https" or parsed.hostname != "tieba.baidu.com":
            raise ValueError("贴吧 API 地址必须是 https://tieba.baidu.com")
        self.base_url = BASE_URL

    def _request(self, path: str, *, params: Mapping[str, Any] | None = None,
                 payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not self.token:
            raise TiebaApiError("未配置 TB_TOKEN，请在设置中填写贴吧认证令牌")
        if not str(path).startswith("/"):
            raise ValueError("贴吧 API 路径必须以 / 开头")
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items()
                                        if v is not None})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        headers = {
            "Authorization": self.token,
            "User-Agent": "LeadHarvest-Tieba/1.0",
        }
        if payload is None:
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
            data = None
            method = "GET"
        else:
            headers["Content-Type"] = "application/json"
            data = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
            method = "POST"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # 不回显 Authorization，错误只保留 HTTP 状态和接口路径。
            if exc.code == 429:
                raise TiebaApiError("贴吧接口触发频率限制，请稍后重试") from exc
            raise TiebaApiError(f"贴吧接口请求失败：HTTP {exc.code} {path}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TiebaApiError(f"贴吧接口连接失败：{type(exc).__name__}") from exc
        try:
            result = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise TiebaApiError(f"贴吧接口返回不是有效 JSON：{path}") from exc
        if not isinstance(result, dict):
            raise TiebaApiError(f"贴吧接口返回格式错误：{path}")
        error_code = result.get(
            "error_code", result.get("errno", result.get("no", 0))
        )
        if str(error_code) not in ("", "0", "None"):
            message = _clean(
                result.get("error_msg") or result.get("errmsg")
                or result.get("error") or "未知错误"
            )
            raise TiebaApiError(f"贴吧接口业务错误：{message}")
        return result

    def list_threads(self, *, sort_type: int = 0) -> dict[str, Any]:
        return self._request("/c/f/frs/page_claw", params={"sort_type": int(sort_type)})

    def search_threads(self, word: str, *, page: int = 1,
                       sort_type: int = 0, page_size: int = 50) -> dict[str, Any]:
        """按关键词搜索帖子。

        贴吧官方 claw 接口的广场列表没有关键词参数；关键词搜索使用
        ``/mo/q/search/thread`` 的 ``word`` 参数，返回结果位于
        ``data.post_list``。所有请求仍严格限定在 tieba.baidu.com。
        """
        text = str(word or "").strip()
        if not text:
            return {"data": {"post_list": [], "has_more": 0}}
        return self._request(
            "/mo/q/search/thread",
            params={
                "word": text,
                "pn": max(1, int(page)),
                "sort_type": int(sort_type),
                "rn": max(1, min(50, int(page_size))),
            },
        )

    def thread_page(self, thread_id: str, *, page: int = 1,
                    order: int = 0) -> dict[str, Any]:
        return self._request(
            "/c/f/pb/page_claw",
            params={"pn": max(1, int(page)), "kz": str(thread_id), "r": int(order)},
        )

    def nested_floor(self, post_id: str, thread_id: str) -> dict[str, Any]:
        return self._request(
            "/c/f/pb/nestedFloor_claw",
            params={"post_id": str(post_id), "thread_id": str(thread_id)},
        )

    def replyme(self, *, page: int = 1) -> dict[str, Any]:
        return self._request("/mo/q/claw/replyme", params={"pn": max(1, int(page))})

    def add_post(self, content: str, *, thread_id: str | None = None,
                 post_id: str | None = None) -> dict[str, Any]:
        text = str(content or "").strip()
        if not text:
            raise ValueError("贴吧回复内容不能为空")
        if len(text) > MAX_CONTENT_LENGTH:
            raise ValueError("贴吧回复内容不能超过1000个字符")
        body: dict[str, Any] = {"content": text}
        if thread_id:
            body["thread_id"] = str(thread_id)
        if post_id:
            body["post_id"] = str(post_id)
        return self._request("/c/c/claw/addPost", payload=body)

    def comment_thread(self, thread_id: str, content: str) -> dict[str, Any]:
        """在帖子下发表评论。"""
        return self.add_post(content, thread_id=str(thread_id))

    def reply_to_post(self, thread_id: str, post_id: str, content: str) -> dict[str, Any]:
        """回复帖子中的指定楼层。"""
        if not str(post_id or "").strip():
            raise ValueError("贴吧楼层 ID 不能为空")
        return self.add_post(
            content, thread_id=str(thread_id), post_id=str(post_id)
        )


class TiebaApiCollector:
    """实现 scheduler Collector 契约的贴吧 API 版本。"""

    platform = PLATFORM

    def __init__(self, token: str = "", *, timeout: float = 30.0):
        self.token = str(token or "").strip()
        self.timeout = timeout

    def set_token(self, token: str) -> None:
        self.token = str(token or "").strip()

    def _client(self) -> TiebaApiClient:
        return TiebaApiClient(self.token, timeout=self.timeout)

    @staticmethod
    def _wait_control(pause_event=None, cancel_event=None) -> bool:
        while pause_event is not None and not pause_event.is_set():
            if cancel_event is not None and cancel_event.is_set():
                return False
            time.sleep(.2)
        return not (cancel_event is not None and cancel_event.is_set())

    def search(self, keyword: str, platform: str = PLATFORM, mode: str = "standard",
               target_count: int = 100, window_id: str = None,
               search_sort: str = "default", pause_event=None,
               cancel_event=None) -> SearchVideosResult:
        del mode, window_id
        if not self._wait_control(pause_event, cancel_event):
            return SearchVideosResult([], search_complete=False, rounds=0,
                                      termination_reason="用户停止")
        sort_type = 3 if str(search_sort or "default") in {"hot", "popular"} else 0
        target = max(1, int(target_count or 100))
        client = self._client()
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        exhausted = False
        rounds = 0
        for page in range(1, MAX_SEARCH_PAGES + 1):
            if not self._wait_control(pause_event, cancel_event):
                return SearchVideosResult(
                    items, search_complete=False, rounds=rounds,
                    termination_reason="用户停止",
                )
            rounds = page
            payload = client.search_threads(
                keyword, page=page, sort_type=sort_type, page_size=50,
            )
            page_items = parse_thread_list(payload, keyword)
            for item in page_items:
                thread_id = str(item.get("thread_id") or item.get("vid") or "")
                if thread_id and thread_id not in seen:
                    seen.add(thread_id)
                    items.append(item)
                    if len(items) >= target:
                        break
            if len(items) >= target:
                break
            data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
            has_more = data.get("has_more")
            exhausted = str(has_more) in {"", "0", "False", "false", "None"}
            if exhausted or not page_items:
                break
        reached = len(items) >= target
        return SearchVideosResult(
            items[:target], search_complete=True, reached_target=reached,
            no_more_results=not reached and exhausted, rounds=rounds,
            termination_reason=(
                "达到目标数量" if reached
                else "贴吧关键词搜索接口已返回完毕"
            ),
        )

    def fetch_comments(self, vid: str, account: str = "", url: str = "",
                       platform: str = PLATFORM, window_id: str = None,
                       pause_event=None, cancel_event=None) -> list[dict[str, Any]]:
        del account, url, platform, window_id
        thread_id = str(vid or "").strip()
        if not thread_id:
            raise ValueError("贴吧帖子 ID 不能为空")
        client = self._client()
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, MAX_THREAD_PAGES + 1):
            if not self._wait_control(pause_event, cancel_event):
                break
            payload = client.thread_page(thread_id, page=page, order=0)
            rows = parse_post_list(payload, thread_id)
            for row in rows:
                cid = str(row.get("cid") or "")
                if cid and cid not in seen:
                    seen.add(cid)
                    output.append(row)
            # page_claw 有时只返回楼层摘要，楼中楼需要单独调用
            # nestedFloor_claw。按接口返回的 sub_post_num 判断，避免对没有
            # 子回复的楼层增加请求；已内嵌完整回复的响应无需重复请求。
            raw_items = payload.get("post_list")
            if not isinstance(raw_items, list):
                page_data = payload.get("data")
                raw_items = page_data.get("post_list") if isinstance(page_data, Mapping) else []
            for raw in raw_items or []:
                if not isinstance(raw, Mapping):
                    continue
                root_id = str(_first(raw, "post_id", "id", "pid") or "").strip()
                if not root_id:
                    continue
                nested_value = _first(raw, "sub_post_list", "sub_posts", "replies")
                nested_rows = _nested_list(nested_value)
                try:
                    nested_count = int(_first(raw, "sub_post_num", "sub_reply_num", "reply_num") or 0)
                except (TypeError, ValueError):
                    nested_count = 0
                if nested_rows or nested_count <= 0:
                    continue
                nested_payload = client.nested_floor(root_id, thread_id)
                for nested in parse_post_list(nested_payload, thread_id):
                    cid = str(nested.get("cid") or "")
                    if not cid or cid in seen:
                        continue
                    nested["parent_id"] = root_id
                    nested["extra"] = dict(nested.get("extra") or {})
                    nested["extra"]["is_reply"] = True
                    nested["extra"]["parent_id"] = root_id
                    seen.add(cid)
                    output.append(nested)
            page_info = payload.get("page") if isinstance(payload.get("page"), Mapping) else {}
            if not page_info:
                page_data = payload.get("data")
                page_info = page_data.get("page") if isinstance(page_data, Mapping) else {}
            has_more = page_info.get("has_more")
            if not rows or str(has_more) in {"", "0", "False", "false", "None"}:
                break
        return output

    def collect_with_comments(self, keyword: str, target_count: int = 100,
                              mode: str = "standard", platform: str = PLATFORM,
                              window_id: str = None, progress_callback: Callable | None = None,
                              pause_event=None, cancel_event=None,
                              search_sort: str = "default") -> dict[str, Any]:
        items = self.search(keyword, platform=platform, mode=mode,
                            target_count=target_count, window_id=window_id,
                            search_sort=search_sort, pause_event=pause_event,
                            cancel_event=cancel_event)
        for item in items:
            if not self._wait_control(pause_event, cancel_event):
                break
            item["comments"] = self.fetch_comments(
                item["vid"], url=item.get("url", ""), platform=platform,
                pause_event=pause_event, cancel_event=cancel_event,
            )
            if callable(progress_callback):
                progress_callback(item)
        return {
            "items": items,
            "search_complete": getattr(items, "search_complete", True),
            "reached_target": getattr(items, "reached_target", False),
            "no_more_results": getattr(items, "no_more_results", False),
            "termination_reason": getattr(items, "termination_reason", ""),
        }


__all__ = [
    "BASE_URL", "PLATFORM", "PLATFORM_LABEL", "TiebaApiError",
    "TiebaApiClient", "TiebaApiCollector", "parse_thread_list", "parse_post_list",
]
