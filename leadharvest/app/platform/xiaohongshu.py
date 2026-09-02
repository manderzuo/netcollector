# -*- coding: utf-8 -*-
"""xiaohongshu.py — 小红书平台采集器（新架构）。

迁移自旧 xhs_collect3 的核心采集逻辑：
- 单关键词首屏约 22 条 feeds，用多关键词变体凑目标量
- 通过页面 __INITIAL_STATE__ 提取 feeds / 笔记详情 / 评论
- 风控探测 + 登录/验证码 → HumanBlock
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from typing import List, Optional

from .base import (
    PlatformAdapter,
    VideoItem,
    CommentItem,
    SearchMeta,
    SearchResult,
)
from ..browser.cdp_client import CdpClient
from ..errors import HumanBlock

# 搜索页探针：检查 __INITIAL_STATE__.search.feeds 是否就绪
_PROBE_READY_JS = """(function(){
  const s = window.__INITIAL_STATE__ || {};
  const f = s.search && s.search.feeds;
  const d = f ? (f.value !== undefined ? f.value : f._value) : [];
  return location.pathname.includes('search_result') && !!s.search && Array.isArray(d) && d.length > 0;
})()"""

_READ_FEEDS_JS = """(function(){
  const s = window.__INITIAL_STATE__ || {};
  const f = s.search && s.search.feeds;
  const d = f ? (f.value !== undefined ? f.value : f._value) : [];
  return JSON.stringify(d || []);
})()"""

_READ_NOTE_JS = """(function(){
  const s = window.__INITIAL_STATE__ || {};
  const m = s.note && s.note.noteDetailMap;
  if (!m) return null;
  const keys = Object.keys(m);
  return keys.length ? JSON.stringify(m[keys[0]]) : null;
})()"""

# 风控探针
_PROBE_BLOCK_JS = """(function(){
  const bodyText = document.body ? document.body.innerText : '';
  const isVisible = (el) => {
    const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0;
  };
  const candidates = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"],[class*="captcha"],[class*="Captcha"],[class*="verify"],[class*="Verify"],[class*="security"],[class*="Security"],iframe')].filter(isVisible);
  const t = candidates.map(e => (e.innerText || '').trim()).filter(Boolean).join('\\n');
  if (location.href.startsWith('chrome-extension:')) return 'bitbrowser_block';
  if (/请求太频繁|操作频繁|一分钟后再试|访问频繁/.test(bodyText)) return 'rate_limited';
  if (/登录后查看|扫码登录/.test(t)) return 'login_popup';
  if (/滑块|拖动验证|滑动验证|安全验证|请完成验证|验证码|人机验证/.test(t)) return 'captcha';
  return null;
})()"""

# 评论滚动探针：找最大可滚动评论容器
_FIND_SCROLLER_JS = """(function(){
  const sels = ['.comment-scroller','[class*="comment"]','[class*="scroll"]','[data-e2e*="comment"]'];
  const visible = (el) => {
    if (!el) return null;
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    if (s.display === 'none' || s.visibility === 'hidden') return null;
    if (Number(s.opacity || 1) <= 0 || r.width <= 0 || r.height <= 0) return null;
    return {el, r};
  };
  let best = null;
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      const v = visible(el);
      if (!v) continue;
      const scrollable = Number(el.scrollHeight || 0) - Number(el.clientHeight || 0);
      if (scrollable > 40 && (!best || scrollable > best.scrollable)) {
        best = {el, scrollable};
      }
    }
  }
  if (!best) return null;
  const r = best.el.getBoundingClientRect();
  return {scrollable: best.scrollable,
    x: Math.round(Math.max(r.left + 10, Math.min(r.right - 10, r.left + r.width / 2))),
    y: Math.round(Math.max(r.top + 10, Math.min(r.bottom - 10, r.top + r.height / 2)))};
})()"""

# 评论解析
_READ_COMMENTS_JS = """(function(){
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('.comment-item,[class*="comment-item"],[data-e2e*="comment"]')) {
    const textEl = el.querySelector('.content,[class*="content"],[class*="text"]') || el;
    const userEl = el.querySelector('.user,[class*="user"],[class*="author"]');
    const timeEl = el.querySelector('.date,[class*="date"],[class*="time"]');
    const text = (textEl.innerText || '').trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    out.push({
      text,
      user: (userEl ? userEl.innerText : '').trim(),
      time: (timeEl ? timeEl.innerText : '').trim(),
    });
  }
  return JSON.stringify(out);
})()"""


class XiaohongshuAdapter(PlatformAdapter):
    """小红书平台采集器。"""

    platform = "xhs"

    def __init__(self, cdp: CdpClient = None):
        self._cdp = cdp

    # ---- 阶段 A：搜索（多关键词变体） ----
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
        kw = urllib.parse.quote(keyword)
        url = (f"https://www.xiaohongshu.com/search_result"
               f"?keyword={kw}&source=web_explore_feed")

        # 导航重试：SPA 首次可能空 feeds
        ready = False
        for attempt in range(3):
            await c.navigate(url, sid)
            await asyncio.sleep(3 + attempt * 2)
            blk = await self._probe_blocked(c, sid)
            if blk:
                raise HumanBlock(blk)
            try:
                await c.eval("location.reload()", sid)
            except Exception:
                pass
            await asyncio.sleep(5 + attempt * 2)
            blk = await self._probe_blocked(c, sid)
            if blk:
                raise HumanBlock(blk)
            for _ in range(12):
                try:
                    ok = await c.eval(_PROBE_READY_JS, sid)
                    if ok:
                        ready = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(1.0)
            if ready:
                break
        if not ready:
            raise RuntimeError("小红书搜索结果加载失败")

        # 排序（可选）
        if search_sort in ("latest", "hot"):
            sort_labels = {"latest": ["最新"], "hot": ["最热", "热门"]}[search_sort]
            await self._click_sort(c, sid, sort_labels)
            await asyncio.sleep(1.0)

        # 滚动采集 feeds
        all_items = {}
        max_rounds = max(80, min(600, target_count // 8 + 80))
        max_load_wait = 10.0
        bottom_stale = 0
        reached = False
        no_more = False
        reason = ""
        rounds = 0

        async def absorb_feeds():
            raw = await c.eval(_READ_FEEDS_JS, sid)
            feeds = json.loads(raw or "[]")
            for feed in feeds:
                nid = str(feed.get("id") or "")
                if not nid:
                    continue
                if nid in all_items:
                    continue
                user = feed.get("user") or {}
                all_items[nid] = VideoItem(
                    vid=nid,
                    url=f"https://www.xiaohongshu.com/explore/{nid}?xsec_token={feed.get('xsec_token') or ''}",
                    title=feed.get("title") or "",
                    author=user.get("nickname", ""),
                    author_id=user.get("user_id", "") or user.get("nickname", ""),
                    create_time=feed.get("time"),
                    comment_count=feed.get("comments_count", ""),
                    keyword=keyword,
                    extra={"liked_count": feed.get("liked_count"),
                           "xsec_token": feed.get("xsec_token")},
                )

        for _ in range(max_rounds):
            rounds += 1
            if pause_event is not None and not pause_event.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    break
                await asyncio.sleep(0.2)
            if cancel_event is not None and cancel_event.is_set():
                break
            before = len(all_items)
            await c.eval("window.scrollBy(0, 900)", sid)
            wait_start = asyncio.get_running_loop().time()
            while True:
                await asyncio.sleep(0.5)
                await absorb_feeds()
                if len(all_items) > before:
                    break
                if asyncio.get_running_loop().time() - wait_start >= max_load_wait:
                    break
            blk = await self._probe_blocked(c, sid)
            if blk:
                raise HumanBlock(blk)
            if len(all_items) >= target_count:
                reached = True
                reason = "reached_target"
                break
            # 底部终止判断
            try:
                state = await c.eval(
                    """(function(){
                      const sc = document.scrollingElement || document.documentElement;
                      const text = (document.body ? document.body.innerText : '') || '';
                      const top = Number(sc.scrollTop || window.scrollY || 0);
                      const height = Number(sc.scrollHeight || 0);
                      const viewport = Number(sc.clientHeight || 0);
                      return {
                        atBottom: top + viewport >= height - 24,
                        endText: /暂时没有更多了|到底了|已显示全部/.test(text),
                      };
                    })()""", sid)
            except Exception:
                state = {}
            if state.get("atBottom"):
                bottom_stale += 1
                if bottom_stale >= 12:
                    no_more = True
                    reason = "bottom_stale"
                    break
            else:
                bottom_stale = 0
            if state.get("endText"):
                no_more = True
                reason = "page_end"
                break

        await absorb_feeds()
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

    # ---- 阶段 B：笔记详情 + 评论 ----
    async def _fetch_comments(
        self,
        vid: str,
        url: str = "",
        window_id: str = "",
        pause_event=None,
        cancel_event=None,
    ) -> List[CommentItem]:
        c, sid = await self._cdp.connect_window(window_id)
        note_url = url or f"https://www.xiaohongshu.com/explore/{vid}"
        await c.navigate(note_url, sid)
        await asyncio.sleep(3)
        blk = await self._probe_blocked(c, sid)
        if blk:
            raise HumanBlock(blk)

        # 等待详情加载
        for _ in range(10):
            raw = await c.eval(_READ_NOTE_JS, sid)
            if raw:
                break
            await asyncio.sleep(1.0)

        # 评论滚动采集
        all_comments = {}
        max_rounds = 80
        no_more = False
        idle = 0

        for _ in range(max_rounds):
            if pause_event is not None and not pause_event.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    break
                await asyncio.sleep(0.2)
            if cancel_event is not None and cancel_event.is_set():
                break
            # 滚动评论容器
            scroller = await c.eval(_FIND_SCROLLER_JS, sid)
            if scroller and isinstance(scroller, dict):
                x = int(scroller.get("x", 0))
                y = int(scroller.get("y", 0))
                await c.cmd("Input.dispatchMouseEvent", {
                    "type": "mouseWheel", "x": x, "y": y,
                    "deltaX": 0, "deltaY": 900,
                }, session_id=sid)
            else:
                await c.eval("window.scrollBy(0, 900)", sid)
            await asyncio.sleep(1.0)
            blk = await self._probe_blocked(c, sid)
            if blk:
                raise HumanBlock(blk)
            # 解析评论
            raw = await c.eval(_READ_COMMENTS_JS, sid)
            new_found = False
            for cm in json.loads(raw or "[]"):
                key = cm.get("text", "")[:80]
                if key in all_comments:
                    continue
                all_comments[key] = CommentItem(
                    user_id=cm.get("user", ""),
                    nickname=cm.get("user", ""),
                    content=cm.get("text", ""),
                    comment_time=cm.get("time", ""),
                    extra={"platform": "xhs"},
                )
                new_found = True
            if not new_found:
                idle += 1
                if idle >= 3:
                    no_more = True
                    break
            else:
                idle = 0

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

    # ---- 内部工具 ----
    @staticmethod
    async def _probe_blocked(c, sid):
        return await c.eval(_PROBE_BLOCK_JS, sid)

    @staticmethod
    async def _click_sort(c, sid, labels):
        """点击排序选项（多文案兜底）。"""
        js = """(labels) => {
          const visible = (el) => {
            const s = getComputedStyle(el); const r = el.getBoundingClientRect();
            return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity||1) > 0 && r.width > 0 && r.height > 0;
          };
          const nodes = [...document.querySelectorAll('span,div,button,li')];
          for (const label of labels) {
            const hit = nodes.find(el => visible(el) && (el.textContent||'').trim() === label && el.children.length <= 2);
            if (hit) { hit.click(); return label; }
          }
          return null;
        }"""
        return await c.eval(f"({js})({json.dumps(labels)})", sid)
