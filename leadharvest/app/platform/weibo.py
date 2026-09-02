# -*- coding: utf-8 -*-
"""weibo.py — 微博平台采集器（新架构）。

迁移自旧 weibo_chrome 的核心采集逻辑：
- 搜索：s.weibo.com 分页滚动 + DOM 卡片解析
- 评论：详情页滚动 + DOM 评论解析
- 依赖 CdpClient 注入，与浏览器层解耦
"""

from __future__ import annotations

import asyncio
import html
import re
import urllib.parse
from typing import List, Optional
from urllib.parse import urlparse

from .base import (
    PlatformAdapter,
    VideoItem,
    CommentItem,
    SearchMeta,
    SearchResult,
)
from ..browser.cdp_client import CdpClient
from ..errors import HumanBlock

_SEARCH_URL = "https://s.weibo.com/weibo?q={keyword}&page={page}"

# 搜索卡片 DOM 快照
_CARD_JS = r"""Array.from(document.querySelectorAll('.card-wrap .card-feed')).map(card => {
  const abs = u => { try { return new URL(u, location.href).href } catch(e) { return '' } };
  const links = Array.from(card.querySelectorAll('a'));
  const authors = links.filter(a => a.matches('a.name, a[nick-name]') || /weibo\.com\/\d+/.test(a.href));
  const status = links.filter(a => /weibo\.com\/\d+\/[^/?]+/.test(abs(a.href)));
  const from = card.querySelector('.from');
  const text = card.querySelector('[node-type="feed_list_content"]');
  const imgs = Array.from(card.querySelectorAll('.media-piclist img, [node-type="feed_list_media_prev"] img'))
    .map(x => x.src).filter(Boolean);
  return {
    author: (card.querySelector('a.name') || card.querySelector('[nick-name]'))?.innerText || '',
    author_urls: authors.map(a => abs(a.href)),
    status_urls: status.map(a => abs(a.href)),
    publish_time: from?.innerText || '',
    text: text?.innerText || '',
    image_urls: imgs,
    engagement: { action_text: card.querySelector('.card-act')?.innerText || '' }
  };
})"""

# 评论 DOM 解析
_COMMENT_JS = r"""(() => {
  const clean = v => String(v || '').replace(/\s+/g, ' ').trim();
  const parse = box => {
    if (!box) return null;
    const text = box.querySelector('.text');
    const user = text?.querySelector('a[usercard], a[href*="/u/"]');
    const name = clean(user?.innerText);
    let content = clean(text?.innerText);
    if (name && content.startsWith(name)) {
      content = content.slice(name.length).replace(/^\s*[:：]?\s*/, '').trim();
    }
    const infoLines = (box.querySelector('.info')?.innerText || '')
      .split('\n').map(clean).filter(Boolean);
    const info = infoLines.find(x => /\d{1,2}-\d{1,2}-\d{1,2}/.test(x)) || infoLines[0] || '';
    const p = info.split(/\s+来自\s+/);
    return {
      user_id: user?.getAttribute('usercard') || ((user?.getAttribute('href') || '').match(/\/u\/(\d+)/) || [,''])[1],
      nickname: name,
      content,
      comment_time: clean(p[0]),
      region: clean(p[1] || ''),
      reply_to: content.match(/^回复@([^:：\s]+)[:：]/)?.[1] || ''
    };
  };
  return Array.from(document.querySelectorAll('#scroller .wbpro-scroller-item')).flatMap(item => {
    const roots = [item.querySelector('.con1'), ...Array.from(item.querySelectorAll('.con2 .con1'))];
    return roots.map(parse).filter(x => x && x.content);
  });
})()"""

# 等待搜索卡片出现
_WAIT_CARD_JS = """() => document.querySelectorAll('.card-wrap .card-feed').length > 0"""

# 检测"已加载全部评论"
_END_TEXT_JS = """() => {
  const t = document.body ? document.body.innerText : '';
  return /已加载全部评论|没有更多评论|暂时没有更多/.test(t);
}"""


def _clean(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _first(values):
    return next((v for v in values if v), "")


def _status_id(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[-1] if len(parts) >= 2 else ""


class WeiboAdapter(PlatformAdapter):
    """微博平台采集器。"""

    platform = "weibo"

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
        all_items = {}
        rounds = 0
        reached = False
        no_more = False
        reason = ""

        while not reached and not no_more and rounds < 50:
            rounds += 1
            page_url = _SEARCH_URL.format(
                keyword=urllib.parse.quote(keyword), page=rounds)
            await c.navigate(page_url, sid)
            await asyncio.sleep(3)

            # 等待卡片加载（最多 10s）
            for _ in range(10):
                try:
                    has = await c.eval(_WAIT_CARD_JS, sid)
                    if has:
                        break
                except Exception:
                    pass
                await asyncio.sleep(1.0)

            raw = await c.eval(_CARD_JS, sid)
            cards = raw or []
            new_count = 0
            for card in cards:
                url = _first(card.get("status_urls", []))
                mid = _status_id(url)
                if not mid or mid in all_items:
                    continue
                author_url = _first(card.get("author_urls", []))
                uid = urlparse(author_url).path.rstrip("/").split("/")[-1]
                all_items[mid] = VideoItem(
                    vid=mid,
                    url=url,
                    title=_clean(card.get("text")),
                    author=_clean(card.get("author")),
                    kind="image" if card.get("image_urls") else "text",
                    author_id=uid,
                    create_time=_clean(card.get("publish_time")),
                    keyword=keyword,
                    extra={"image_urls": list(dict.fromkeys(card.get("image_urls", []))),
                           "engagement": card.get("engagement", {})},
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
            # 冷却
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
            url = f"https://weibo.com/{vid}"
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
                    region=cm.get("region", ""),
                    extra={"reply_to": cm.get("reply_to", "")},
                )
                new_found = True
            if not new_found:
                idle += 1
                if idle >= 5:
                    break
            else:
                idle = 0
            try:
                ended = await c.eval(_END_TEXT_JS, sid)
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
