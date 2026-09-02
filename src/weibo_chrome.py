# -*- coding: utf-8 -*-
"""微博 Chrome 采集器（首个可运行适配器）。

通过普通 Chrome 的 CDP 浏览器级 WebSocket 工作，不读取 Cookie 或本地存储。
搜索使用微博公开搜索页的服务端分页；详情页只读取页面已经渲染到 DOM 的内容。
接口与 scheduler.Collector 对齐，后续可由 BitBrowser 复用同一套页面解析逻辑。
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from urllib.parse import quote, urlparse

try:
    from .cdp import CdpSession
    from .dy_collect import SearchVideosResult
    from .search_sort_dom import click_sort_option
except ImportError:
    from cdp import CdpSession
    from dy_collect import SearchVideosResult
    from search_sort_dom import click_sort_option


SEARCH_URL = "https://s.weibo.com/weibo?q={keyword}&page={page}"
SEARCH_LOAD_WAIT_SECONDS = 10.0


def _clean(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _first(values):
    return next((v for v in values if v), "")


def _status_id(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[-1] if len(parts) >= 2 else ""


def _parse_search_cards(raw_cards: list[dict]) -> list[dict]:
    """把页面 DOM 快照归一化为 scheduler 可写入 videos 的结构。"""
    out = []
    seen = set()
    for card in raw_cards:
        url = _first(card.get("status_urls", []))
        mid = _status_id(url)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        author_url = _first(card.get("author_urls", []))
        uid = urlparse(author_url).path.rstrip("/").split("/")[-1]
        out.append({
            "vid": mid,
            "url": url,
            "title": _clean(card.get("text")),
            "author": _clean(card.get("author")),
            "kind": "image" if card.get("image_urls") else "text",
            "content_type": "image_text" if card.get("image_urls") else "text",
            "create_time": _clean(card.get("publish_time")),
            "author_id": uid,
            "image_urls": list(dict.fromkeys(card.get("image_urls", []))),
            "engagement": card.get("engagement", {}),
            "extra": {"platform": "weibo", "source": "chrome_search"},
        })
    return out


_CARD_JS = r'''Array.from(document.querySelectorAll('.card-wrap .card-feed')).map(card => {
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
})'''


_COMMENT_JS = r'''(() => {
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
})()''';


_EXPAND_REPLIES_JS = r'''(() => {
  const clean = s => String(s || '').replace(/\s+/g, ' ').trim();
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
  const controls = new Set();
  for (const row of document.querySelectorAll('#scroller .wbpro-scroller-item')) {
    for (const el of row.querySelectorAll('button,[role="button"],a,span,div')) {
      const label = clean(el.getAttribute('aria-label') || el.getAttribute('title')
        || el.innerText || el.textContent);
      if (!isExpandLabel(label)) continue;
      const control = el.closest('button,[role="button"],a') || el;
      if (control !== row && visible(control)) controls.add(control);
    }
  }
  const clicked = [];
  for (const control of controls) {
    const label = clean(control.innerText || control.textContent).slice(0, 80);
    control.focus?.();
    control.click();
    clicked.push(label);
  }
  return {expandedCount: clicked.length, labels: clicked.slice(0, 20)};
})()''';


class WeiboChromeCollector:
    """普通 Chrome 的微博采集器。"""

    platform = "weibo"

    def __init__(self, ws_url: str, page_delay: float = 2.0,
                 cooldown_every: int = 20, cooldown_seconds: int = 30):
        self.ws_url = ws_url
        self.page_delay = max(0.0, float(page_delay))
        self.cooldown_every = max(1, int(cooldown_every))
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        # 两条微博详情之间的最小间隔，降低连续跳转触发风控的概率。
        self.detail_interval = 3.0
        self._last_detail_started = 0.0
        self._comment_cache = {}

    def _wait_detail_interval(self):
        now = time.monotonic()
        remaining = self.detail_interval - (now - self._last_detail_started)
        if remaining > 0:
            time.sleep(remaining)
        self._last_detail_started = time.monotonic()

    async def _run(self, operation):
        c = CdpSession(self.ws_url)
        await c.connect()
        sid = await c.attach_page()
        try:
            return await operation(c, sid)
        finally:
            await c.close()

    async def _wait_dom(self, seconds=1.5):
        await asyncio.sleep(max(0.1, seconds))

    async def _wait_search_cards(self, c, sid):
        """微博翻页后最多等待 10 秒，避免一次空 DOM 误判为没有更多。"""
        deadline = asyncio.get_running_loop().time() + SEARCH_LOAD_WAIT_SECONDS
        raw = []
        while True:
            raw = await c.eval(_CARD_JS, sid) or []
            if raw:
                return raw
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return raw
            await asyncio.sleep(min(0.5, remaining))

    async def _search(self, keyword, mode="standard", target_count=100,
                      pause_event=None, cancel_event=None,
                      search_sort="default"):
        target = int(target_count or (50 if mode == "fast" else 500 if mode == "standard" else 10000))
        target = max(1, target)
        results = []

        async def work(c, sid):
            page = 1
            no_progress_rounds = 0
            reached_target = False
            no_more_results = False
            termination_reason = ""
            while len(results) < target and page <= 500:
                if not await _wait_control(pause_event, cancel_event):
                    break
                url = SEARCH_URL.format(keyword=quote(str(keyword)), page=page)
                await c.navigate(url, sid, wait_load=False)
                raw = await self._wait_search_cards(c, sid)
                sort_labels = {"realtime": ["实时"], "hot": ["热门"]}.get(str(search_sort), [])
                if sort_labels:
                    clicked = await click_sort_option(c, sid, sort_labels)
                    if clicked:
                        await asyncio.sleep(0.8)
                        raw = await self._wait_search_cards(c, sid)
                batch = _parse_search_cards(raw or [])
                before = len(results)
                known = {x["vid"] for x in results}
                results.extend(x for x in batch if x["vid"] not in known)
                if len(results) >= target:
                    reached_target = True
                    termination_reason = "达到目标数量"
                    break
                if not batch or len(results) == before:
                    no_progress_rounds += 1
                else:
                    no_progress_rounds = 0
                # 微博偶发空页/重复页，不能一次就认定没有更多；连续多页
                # 都没有新增结果才结束搜索。
                if no_progress_rounds >= 3:
                    no_more_results = True
                    termination_reason = "连续 3 页无新增结果"
                    break
                if page % self.cooldown_every == 0 and len(results) < target:
                    await asyncio.sleep(self.cooldown_seconds)
                page += 1
            return SearchVideosResult(
                results[:target],
                search_complete=(reached_target or no_more_results),
                reached_target=reached_target,
                no_more_results=no_more_results,
                rounds=page - 1,
                termination_reason=termination_reason,
            )

        return await self._run(work)

    def search(self, keyword: str, platform: str = "weibo", mode: str = "standard",
               target_count: int = 100, window_id: str = None,
               pause_event=None, cancel_event=None, search_sort="default"):
        if platform not in ("weibo", "wb"):
            raise ValueError(f"WeiboChromeCollector 不支持平台: {platform}")
        return asyncio.run(self._search(keyword, mode, target_count, pause_event, cancel_event, search_sort))

    def collect_with_comments(self, keyword: str, target_count: int = 100,
                              mode: str = "standard", max_candidates: int = None,
                              progress_callback=None, pause_event=None, cancel_event=None,
                              search_sort="default"):
        """只返回有评论的微博，并按有效微博数量补足目标。

        该方法保留候选上限，避免关键词结果中大量 0 评论内容导致无限翻页。
        返回结果中的每项都会带 comments；0 评论候选不会进入 items。
        """
        target_count = max(1, int(target_count))
        if max_candidates is None:
            max_candidates = max(target_count + 100, target_count * 3)
        max_candidates = min(10000, max(target_count, int(max_candidates)))
        candidates = self.search(keyword, mode=mode, target_count=max_candidates,
                                 pause_event=pause_event, cancel_event=cancel_event,
                                 search_sort=search_sort)
        accepted = []
        zero_comments = 0
        failed = 0
        for candidate in candidates:
            while pause_event is not None and not pause_event.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    break
                time.sleep(.2)
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                comments = self.fetch_comments(
                    candidate["vid"], "chrome", candidate.get("url", ""))
            except Exception:
                failed += 1
                continue
            if not comments:
                zero_comments += 1
                continue
            item = dict(candidate)
            item["comments"] = comments
            item["comment_count"] = len(comments)
            accepted.append(item)
            if progress_callback:
                progress_callback(dict(item))
            if len(accepted) >= target_count:
                break
        return {
            "items": accepted[:target_count],
            "target_count": target_count,
            "candidate_count": len(candidates),
            "scanned_count": zero_comments + len(accepted) + failed,
            "zero_comment_count": zero_comments,
            "failed_count": failed,
            "filled": len(accepted) >= target_count,
            "search_complete": bool(getattr(candidates, "search_complete", True)),
            "reached_target": bool(getattr(candidates, "reached_target", False)),
            "no_more_results": bool(getattr(candidates, "no_more_results", False)),
            "rounds": int(getattr(candidates, "rounds", 0) or 0),
        }

    async def _fetch_detail(self, vid: str, url: str):
        async def work(c, sid):
            await c.navigate(url, sid, wait_load=False)
            await self._wait_dom(self.page_delay)
            return await c.eval(r'''(() => {
              const txt = s => document.querySelector(s)?.innerText || '';
              const imgs = Array.from(document.querySelectorAll('img')).map(x=>x.src)
                .filter(x=>x && !x.includes('avatar'));
              return {title: txt('[node-type="feed_list_content"]') || document.body.innerText,
                author: txt('.head-info .name, a.name'), image_urls: imgs,
                body_text: document.body.innerText};
            })()''', sid)
        return await self._run(work)

    def fetch_detail(self, vid: str, url: str):
        self._wait_detail_interval()
        return asyncio.run(self._fetch_detail(str(vid), url))

    def fetch_comments(self, vid: str, account: str = "", url: str = "",
                       platform: str = "weibo", window_id: str = None,
                       pause_event=None, cancel_event=None):
        if str(vid) in self._comment_cache:
            return [dict(x) for x in self._comment_cache[str(vid)]]
        self._wait_detail_interval()
        result = asyncio.run(self._fetch_comments(str(vid), url, pause_event, cancel_event))
        self._comment_cache[str(vid)] = [dict(x) for x in result]
        return result

    async def _fetch_comments(self, vid: str, url: str, pause_event=None, cancel_event=None):
        async def work(c, sid):
            await c.navigate(url, sid, wait_load=False)
            await self._wait_dom(self.page_delay)
            found = []
            seen = set()
            idle_rounds = 0
            # 300 轮仅作异常页面的安全兜底；正常结束依靠页面结束提示或连续无新增。
            for _ in range(300):
                if not await _wait_control(pause_event, cancel_event):
                    break
                await c.eval(_EXPAND_REPLIES_JS, sid)
                batch = await c.eval(_COMMENT_JS, sid) or []
                before = len(found)
                for row in batch:
                    key = (row.get("user_id", ""), row.get("comment_time", ""),
                           row.get("content", ""))
                    if key not in seen:
                        seen.add(key)
                        found.append(row)
                idle_rounds = idle_rounds + 1 if len(found) == before else 0
                done = await c.eval(
                    "document.body && document.body.innerText.includes('已加载全部评论')", sid
                )
                if done and idle_rounds >= 1:
                    break
                await c.eval(
                    "new Promise(r=>{window.scrollBy(0,900);setTimeout(r,700)})", sid
                )
                await c.eval(_EXPAND_REPLIES_JS, sid)
                if idle_rounds >= 5:
                    break
            return found
        return await self._run(work)


__all__ = ["WeiboChromeCollector", "_parse_search_cards"]
async def _wait_control(pause_event=None, cancel_event=None):
    while pause_event is not None and not pause_event.is_set():
        if cancel_event is not None and cancel_event.is_set():
            return False
        await asyncio.sleep(.2)
    return not (cancel_event is not None and cancel_event.is_set())
