# -*- coding: utf-8 -*-
"""B站普通 Chrome CDP 采集器。

第一阶段只读取用户已登录 Chrome 中可见的搜索、视频详情和评论 DOM，
不读取 Cookie/本地存储，也不依赖 GUI。后续接入比特浏览器时只需替换 ws 来源。
"""
from __future__ import annotations

import asyncio
import html
import re
import time
from urllib.parse import quote

try:
    from .cdp import CdpSession
    from .dy_collect import SearchVideosResult
except ImportError:
    from cdp import CdpSession
    from dy_collect import SearchVideosResult


SEARCH_URL = "https://search.bilibili.com/all?keyword={keyword}&page={page}&order={order}"
SEARCH_LOAD_WAIT_SECONDS = 10.0


def _clean(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _bvid(url: str) -> str:
    m = re.search(r"/(BV[0-9A-Za-z]+)(?:[/?#]|$)", str(url or ""))
    return m.group(1) if m else ""


def _keyword_relevant(keyword: str, item: dict) -> bool:
    key = _clean(keyword).lower()
    extra = item.get("extra") or {}
    engagement = extra.get("engagement") or {}
    text = _clean(f"{item.get('title', '')} {engagement.get('text', '')}").lower()
    if not key or not text:
        return False
    if key in text:
        return True
    parts = [key[i:i + 2] for i in range(0, len(key), 2) if len(key[i:i + 2]) >= 2]
    return bool(parts) and any(part in text for part in parts)


def _parse_search_cards(cards):
    out, seen = [], set()
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        url = str(card.get("url") or "")
        vid = _bvid(url)
        if not vid or vid in seen:
            continue
        seen.add(vid)
        out.append({
            "vid": vid, "url": url, "title": _clean(card.get("title")),
            "author": _clean(card.get("author")), "kind": "video",
            "create_time": _clean(card.get("publish_time")),
            "extra": {"platform": "bilibili", "bvid": vid,
                      "duration": _clean(card.get("duration")),
                      "engagement": card.get("engagement") or {},
                      "cover_url": card.get("cover_url", "")},
        })
    return out


_SEARCH_JS = r'''(() => {
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
})()'''


_DETAIL_JS = r'''(() => {
  const clean = v => String(v || '').replace(/\s+/g, ' ').trim();
  const body = document.body?.innerText || '';
  const text = s => clean(document.querySelector(s)?.innerText);
  const links = Array.from(document.querySelectorAll('a[href]'));
  const up = links.find(a => /space\.bilibili\.com/.test(a.href));
  return {title: text('h1.video-title, .video-title, h1'), author: clean(up?.innerText),
    author_url: up?.href || '', body_text: body.slice(0, 12000),
    comment_hint: text('.comment h2, .reply-header, .comment-title')};
})()'''


_COMMENTS_JS = r'''(() => {
  const clean = v => String(v || '').replace(/\s+/g, ' ').trim();
    const host = document.querySelector('bili-comments');
    const root = host?.shadowRoot;
  if (!root) return [];
  const out = [];
  Array.from(root.querySelectorAll('bili-comment-thread-renderer')).forEach((thread, ri) => {
    const tr = thread.shadowRoot;
    const comment = tr?.querySelector('bili-comment-renderer')?.shadowRoot;
    if (!comment) return;
    const userInfo = comment.querySelector('bili-comment-user-info')?.shadowRoot;
    const user = userInfo?.querySelector('#user-name a');
    const rich = comment.querySelector('bili-rich-text')?.shadowRoot;
    const action = comment.querySelector('bili-comment-action-buttons-renderer')?.shadowRoot;
    const content = clean(rich?.querySelector('#contents')?.innerText);
    const info = clean(action?.querySelector('#pubdate')?.innerText);
    const uid = (user?.href || '').match(/space\.bilibili\.com\/(\d+)/)?.[1] || '';
    if (content) out.push({user_id: uid, nickname: clean(user?.innerText), content,
      comment_time: info, region: '', reply_to: '',
      extra: {root_index: ri, is_reply: false,
              digg_count: clean(action?.querySelector('#like #count')?.innerText)}});
    // 当前版本的二级回复同样位于独立 Shadow DOM，按相同结构提取。
    const replies = tr?.querySelector('bili-comment-replies-renderer')?.shadowRoot;
    Array.from(replies?.querySelectorAll('bili-comment-reply-renderer') || []).forEach((reply, ji) => {
      const rr = reply.shadowRoot;
      const ru = rr?.querySelector('bili-comment-user-info')?.shadowRoot?.querySelector('#user-name a');
      const rc = rr?.querySelector('bili-rich-text')?.shadowRoot?.querySelector('#contents');
      const ra = rr?.querySelector('bili-comment-action-buttons-renderer')?.shadowRoot;
      const text = clean(rc?.innerText);
      if (text) out.push({user_id: (ru?.href || '').match(/space\.bilibili\.com\/(\d+)/)?.[1] || '',
        nickname: clean(ru?.innerText), content: text,
        comment_time: clean(ra?.querySelector('#pubdate')?.innerText), region: '',
        reply_to: clean(user?.innerText), extra: {root_index: ri, reply_index: ji, is_reply: true}});
    });
  });
  return out;
})()'''


_EXPAND_REPLIES_JS = r'''(() => {
  const clean = v => String(v || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none'
      && s.visibility !== 'hidden';
  };
  const isExpandLabel = text => {
    const value = clean(text);
    if (!value || value.length > 60 || /收起|隐藏|没有更多|暂无更多|暂时没有更多|已加载全部/.test(value)) return false;
    return /(?:展开|查看|显示|更多)\s*(?:更多\s*)?\d*\s*(?:条)?\s*(?:回复|评论)/.test(value)
      || /^\d+\s*条\s*(?:回复|评论)$/.test(value);
  };
  const host = document.querySelector('bili-comments');
  const root = host?.shadowRoot;
  if (!root) return {expandedCount: 0, labels: [], shadowDom: false};
  const controls = new Set();
  const walk = currentRoot => {
    for (const el of currentRoot.querySelectorAll('*')) {
      const label = clean(el.getAttribute?.('aria-label') || el.getAttribute?.('title')
        || el.innerText || el.textContent);
      if (isExpandLabel(label) && visible(el)) controls.add(el);
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(root);
  const clicked = [];
  for (const control of controls) {
    const label = clean(control.innerText || control.textContent).slice(0, 80);
    control.focus?.();
    control.click();
    clicked.push(label);
  }
  return {expandedCount: clicked.length, labels: clicked.slice(0, 20), shadowDom: true};
})()'''


class BilibiliChromeCollector:
    platform = "bilibili"

    def __init__(self, ws_url: str, page_delay: float = 2.5,
                 detail_interval: float = 4.0, cooldown_every: int = 20,
                 cooldown_seconds: int = 30):
        self.ws_url = ws_url
        self.page_delay = max(.5, float(page_delay))
        self.detail_interval = max(1.0, float(detail_interval))
        self.cooldown_every = max(1, int(cooldown_every))
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        self._last_detail = 0.0
        self._details_opened = 0
        self._comment_cache = {}

    def _wait_interval(self):
        if self._details_opened and self._details_opened % self.cooldown_every == 0:
            time.sleep(self.cooldown_seconds)
        remain = self.detail_interval - (time.monotonic() - self._last_detail)
        if remain > 0:
            time.sleep(remain)
        self._last_detail = time.monotonic()
        self._details_opened += 1

    async def _run(self, operation):
        c = CdpSession(self.ws_url)
        await c.connect()
        sid = await c.attach_page()
        try:
            return await operation(c, sid)
        finally:
            await c.close()

    async def _search(self, keyword, mode, target_count,
                      pause_event=None, cancel_event=None,
                      search_sort="totalrank"):
        target = max(1, int(target_count or (50 if mode == "fast" else 500 if mode == "standard" else 10000)))
        result = []
        async def work(c, sid):
            page = 1
            no_progress_rounds = 0
            reached_target = False
            no_more_results = False
            termination_reason = ""
            while len(result) < target and page <= 500:
                if not await _wait_control(pause_event, cancel_event):
                    break
                order = str(search_sort or "totalrank")
                if order not in {"totalrank", "click", "pubdate", "dm", "stow"}:
                    order = "totalrank"
                await c.navigate(SEARCH_URL.format(keyword=quote(str(keyword)), page=page, order=order), sid, wait_load=False)
                # B站搜索页是 SPA：导航后最多等待 10 秒；中途无卡片时
                # 刷新一次，但总等待不超过上限，避免空骨架被当成无更多。
                deadline = asyncio.get_running_loop().time() + SEARCH_LOAD_WAIT_SECONDS
                has_cards = False
                reloaded = False
                while True:
                    count = await c.eval("document.querySelectorAll('.bili-video-card, .video-list-item').length", sid) or 0
                    if int(count) >= 1:
                        has_cards = True
                        break
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    if not reloaded and remaining <= SEARCH_LOAD_WAIT_SECONDS / 2:
                        await c.cmd("Page.reload", {}, sid)
                        reloaded = True
                    await asyncio.sleep(min(.5, remaining))
                batch = [x for x in _parse_search_cards(await c.eval(_SEARCH_JS, sid) or [])
                         if _keyword_relevant(keyword, x)]
                known = {x["vid"] for x in result}
                before = len(result)
                result.extend(x for x in batch if x["vid"] not in known)
                if len(result) >= target:
                    reached_target = True
                    termination_reason = "达到目标数量"
                    break
                if not batch or len(result) == before:
                    no_progress_rounds += 1
                else:
                    no_progress_rounds = 0
                if no_progress_rounds >= 3:
                    no_more_results = True
                    termination_reason = "连续 3 页无新增结果"
                    break
                if page % self.cooldown_every == 0 and len(result) < target:
                    await asyncio.sleep(self.cooldown_seconds)
                page += 1
            return SearchVideosResult(
                result[:target],
                search_complete=(reached_target or no_more_results),
                reached_target=reached_target,
                no_more_results=no_more_results,
                rounds=page - 1,
                termination_reason=termination_reason,
            )
        return await self._run(work)

    def search(self, keyword, platform="bilibili", mode="standard", target_count=100, window_id=None,
               pause_event=None, cancel_event=None, search_sort="totalrank"):
        if platform not in ("bilibili", "bili"):
            raise ValueError(f"BilibiliChromeCollector 不支持平台: {platform}")
        return asyncio.run(self._search(keyword, mode, target_count, pause_event, cancel_event, search_sort))

    async def _fetch_comments(self, url, pause_event=None, cancel_event=None):
        async def work(c, sid):
            await c.navigate(url, sid, wait_load=False)
            await asyncio.sleep(self.page_delay)
            # B站评论组件使用 Shadow DOM，必须滚动到挂载点才会加载。
            await c.eval("document.querySelector('#commentapp')?.scrollIntoView({block:'center'}); true", sid)
            await asyncio.sleep(1.5)
            found, seen = [], set()
            idle_rounds = 0
            # 300 轮仅作异常页面的安全兜底；正常结束必须依靠“没有更多评论”
            # 或连续无新增，而不是固定轮数截断已加载内容。
            for _ in range(300):
                if not await _wait_control(pause_event, cancel_event):
                    break
                await c.eval(_EXPAND_REPLIES_JS, sid)
                before = len(found)
                for row in await c.eval(_COMMENTS_JS, sid) or []:
                    key = (row.get("user_id", ""), row.get("comment_time", ""), row.get("content", ""))
                    if key not in seen:
                        seen.add(key); found.append(row)
                idle_rounds = idle_rounds + 1 if len(found) == before else 0
                body = await c.eval("document.body?.innerText || ''", sid) or ""
                if "没有更多评论" in body or "已加载全部评论" in body:
                    break
                if idle_rounds >= 4:
                    break
                await c.eval("""new Promise(r=>{
                  const e=document.querySelector('#commentapp');
                  if(!e){r();return;}
                  const rect=e.getBoundingClientRect();
                  if(rect.bottom > window.innerHeight + 120) window.scrollBy(0, Math.min(450, rect.bottom-window.innerHeight+120));
                  setTimeout(r,900);
                })""", sid)
                await c.eval(_EXPAND_REPLIES_JS, sid)
                await asyncio.sleep(.4)
            # 不按时间筛掉评论；返回当前页面在结束信号前加载到的全部评论。
            return found
        return await self._run(work)

    def fetch_comments(self, vid, account="", url="", platform="bilibili", window_id=None,
                       pause_event=None, cancel_event=None):
        key = str(vid)
        if key in self._comment_cache:
            return [dict(x) for x in self._comment_cache[key]]
        self._wait_interval()
        rows = asyncio.run(self._fetch_comments(
            url or f"https://www.bilibili.com/video/{key}", pause_event, cancel_event))
        self._comment_cache[key] = [dict(x) for x in rows]
        return rows

    def fetch_detail(self, vid, url=""):
        self._wait_interval()
        return asyncio.run(self._run(lambda c, sid: self._detail_work(c, sid, url or f"https://www.bilibili.com/video/{vid}")))

    async def _detail_work(self, c, sid, url):
        await c.navigate(url, sid, wait_load=False)
        await asyncio.sleep(self.page_delay)
        return await c.eval(_DETAIL_JS, sid) or {}

    def collect_with_comments(self, keyword, target_count=100, mode="standard", max_candidates=None,
                              progress_callback=None, pause_event=None, cancel_event=None,
                              search_sort="totalrank"):
        target_count = max(1, int(target_count))
        max_candidates = min(10000, max(target_count, int(max_candidates or target_count * 3)))
        candidates = self.search(keyword, mode=mode, target_count=max_candidates,
                                 pause_event=pause_event, cancel_event=cancel_event,
                                 search_sort=search_sort)
        accepted, zero, failed = [], 0, 0
        for candidate in candidates:
            while pause_event is not None and not pause_event.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    break
                time.sleep(.2)
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                comments = self.fetch_comments(
                    candidate["vid"], "chrome", candidate.get("url", ""),
                    pause_event=pause_event, cancel_event=cancel_event)
            except Exception:
                failed += 1; continue
            if not comments:
                zero += 1; continue
            item = dict(candidate); item["comments"] = comments; item["comment_count"] = len(comments)
            accepted.append(item)
            if progress_callback:
                progress_callback(dict(item))
            if len(accepted) >= target_count:
                break
        return {"items": accepted[:target_count], "target_count": target_count,
                "candidate_count": len(candidates), "scanned_count": zero + len(accepted) + failed,
                "zero_comment_count": zero, "failed_count": failed,
                "filled": len(accepted) >= target_count,
                "search_complete": bool(getattr(candidates, "search_complete", True)),
                "reached_target": bool(getattr(candidates, "reached_target", False)),
                "no_more_results": bool(getattr(candidates, "no_more_results", False)),
                "rounds": int(getattr(candidates, "rounds", 0) or 0)}


__all__ = ["BilibiliChromeCollector", "_parse_search_cards"]


async def _wait_control(pause_event=None, cancel_event=None):
    while pause_event is not None and not pause_event.is_set():
        if cancel_event is not None and cancel_event.is_set():
            return False
        await asyncio.sleep(.2)
    return not (cancel_event is not None and cancel_event.is_set())
