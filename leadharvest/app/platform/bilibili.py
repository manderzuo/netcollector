# -*- coding: utf-8 -*-
"""bilibili.py — B站平台采集器（新架构）。

迁移自旧 bilibili_adapter 的核心采集逻辑：
- 搜索：search.bilibili.com 分页 + DOM 卡片解析
- 评论：视频页滚动 + DOM 评论解析
- 依赖 CdpClient 注入
"""

from __future__ import annotations

import asyncio
import html
import re
from typing import List, Optional
from urllib.parse import quote

from .base import (
    PlatformAdapter,
    VideoItem,
    CommentItem,
    SearchMeta,
    SearchResult,
)
from ..browser.cdp_client import CdpClient
from ..errors import HumanBlock

_SEARCH_URL = "https://search.bilibili.com/all?keyword={keyword}&page={page}&order={order}"
_LOAD_WAIT = 10.0

# 搜索卡片 DOM 快照
_SEARCH_JS = r"""(() => {
  const abs = u => { try { return new URL(u, location.href).href } catch(e) { return '' } };
  const clean = v => String(v || '').replace(/\s+/g, ' ').trim();
  return Array.from(document.querySelectorAll('.bili-video-card a[href*="/video/BV"], .video-list-item a[href*="/video/BV"]')).map(a => {
    const box = a.closest('.bili-video-card, .video-list-item, .video-card');
    if (!box) return null;
    const text = clean(box?.innerText || a.innerText);
    const lines = text.split(' ').filter(Boolean);
    const img = box?.querySelector('img');
    return {url: abs(a.href), title: clean(box?.querySelector('.bili-video-card__info--tit, [class*="info--tit"]')?.innerText || a.getAttribute('title') || a.innerText),
      author: clean(box?.querySelector('.bili-video-card__info--author, .up-name, a[href*="space.bilibili.com"]')?.innerText),
      publish_time: clean(box?.querySelector('.bili-video-card__info--date, .time')?.innerText),
      duration: clean(box?.querySelector('.bili-video-card__stats__duration, .duration')?.innerText),
      cover_url: img?.src || '', engagement: {text: text.slice(-180)}};
  });
})()"""

# 评论 DOM 解析
_COMMENT_JS = r"""(() => {
  const clean = v => String(v || '').replace(/\s+/g, ' ').trim();
  return Array.from(document.querySelectorAll('.reply-item, [class*="reply-item"]')).map(el => {
    const user = el.querySelector('.user-name, [class*="user-name"], .bili-avatar a');
    const text = el.querySelector('.reply-content, [class*="reply-content"], .text-con');
    const time = el.querySelector('.reply-time, [class*="reply-time"], .pubdate');
    return {
      user_id: user?.getAttribute('href')?.match(/uid=(\d+)/)?.[1] || '',
      nickname: clean(user?.innerText),
      content: clean(text?.innerText || el.innerText),
      comment_time: clean(time?.innerText),
    };
  });
})()"""

# 等待卡片
_WAIT_CARD_JS = """() => document.querySelectorAll('.bili-video-card, .video-list-item').length > 0"""

# 评论结束提示
_END_JS = """() => {
  const t = document.body ? document.body.innerText : '';
  return /没有更多评论|暂无评论/.test(t);
}"""


def _clean(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _bvid(url: str) -> str:
    m = re.search(r"/(BV[0-9A-Za-z]+)(?:[/?#]|$)", str(url or ""))
    return m.group(1) if m else ""


class BilibiliAdapter(PlatformAdapter):
    """B站平台采集器。"""

    platform = "bilibili"

    def __init__(self, cdp: CdpClient = None):
        self._cdp = cdp

    # ---- 阶段 A：搜索 ----
    async def _search(
        self,
        keyword: str,
        mode: str = "standard",
        target_count: int = 100,
        window_id: str = "",
        search_sort: str = "default",
        pause_event=None,
        cancel_event=None,
    ) -> SearchResult:
        c, sid = await self._cdp.connect_window(window_id)
        order = {"most_like": "click", "latest": "pubdate"}.get(search_sort, "totalrank")
        all_items = {}
        rounds = 0
        reached = False
        no_more = False
        reason = ""

        while not reached and not no_more and rounds < 30:
            rounds += 1
            page_url = _SEARCH_URL.format(
                keyword=quote(keyword), page=rounds, order=order)
            await c.navigate(page_url, sid)
            await asyncio.sleep(3)

            for _ in range(10):
                try:
                    has = await c.eval(_WAIT_CARD_JS, sid)
                    if has:
                        break
                except Exception:
                    pass
                await asyncio.sleep(1.0)

            raw = await c.eval(_SEARCH_JS, sid)
            new_count = 0
            for card in raw or []:
                if not isinstance(card, dict):
                    continue
                url = str(card.get("url") or "")
                vid = _bvid(url)
                if not vid or vid in all_items:
                    continue
                all_items[vid] = VideoItem(
                    vid=vid,
                    url=url,
                    title=_clean(card.get("title")),
                    author=_clean(card.get("author")),
                    kind="video",
                    create_time=_clean(card.get("publish_time")),
                    keyword=keyword,
                    extra={"bvid": vid,
                           "duration": _clean(card.get("duration")),
                           "engagement": card.get("engagement") or {},
                           "cover_url": card.get("cover_url", "")},
                )
                new_count += 1
            if len(all_items) >= target_count:
                reached = True
                reason = "reached_target"
                break
            if new_count == 0:
                no_more = True
                reason = "no_new_pages"
                break
            if rounds % 10 == 0:
                await asyncio.sleep(2)

        items = list(all_items.values())[:target_count]
        return SearchResult(
            items=items,
            meta=SearchMeta(
                search_complete=reached or no_more,
                reached_target=reached,
                no_more_results=no_more,
                rounds=rounds,
                termination_reason=reason,
            ),
        )

    # ---- 阶段 B：评论 ----
    async def _fetch_comments(
        self,
        vid: str,
        url: str = "",
        window_id: str = "",
        pause_event=None,
        cancel_event=None,
    ) -> List[CommentItem]:
        c, sid = await self._cdp.connect_window(window_id)
        if not url:
            url = f"https://www.bilibili.com/video/{vid}"
        await c.navigate(url, sid)
        await asyncio.sleep(4)

        all_comments = {}
        idle = 0
        for _ in range(50):
            if pause_event is not None and not pause_event.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    break
                await asyncio.sleep(0.2)
            if cancel_event is not None and cancel_event.is_set():
                break
            await c.eval("window.scrollBy(0, 900)", sid)
            await asyncio.sleep(1.5)
            raw = await c.eval(_COMMENT_JS, sid)
            new_found = False
            for cm in raw or []:
                key = cm.get("content", "")[:80]
                if key in all_comments:
                    continue
                all_comments[key] = CommentItem(
                    user_id=cm.get("user_id", ""),
                    nickname=cm.get("nickname", ""),
                    content=cm.get("content", ""),
                    comment_time=cm.get("comment_time", ""),
                )
                new_found = True
            if not new_found:
                idle += 1
                if idle >= 5:
                    break
            else:
                idle = 0
            try:
                ended = await c.eval(_END_JS, sid)
                if ended:
                    break
            except Exception:
                pass

        return list(all_comments.values())

    # ---- 同步包装 ----
    def search(self, keyword, mode="standard", target_count=100, window_id="",
               search_sort="default", pause_event=None, cancel_event=None) -> SearchResult:
        return asyncio.run(self._search(
            keyword, mode, target_count, window_id, search_sort,
            pause_event, cancel_event))

    def fetch_comments(self, vid, url="", window_id="",
                       pause_event=None, cancel_event=None) -> List[CommentItem]:
        return asyncio.run(self._fetch_comments(
            vid, url, window_id, pause_event, cancel_event))
