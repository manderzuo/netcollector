#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""xhs_collect3.py — 多关键词批量采集 + 去重，凑目标量。

策略：小红书单关键词只返回约 22 条首屏 feeds。要凑 50 篇 / 500+ 评论，
用多个关键词变体分别采集，跨关键词按 note_id 去重后合并。

用法:
  python xhs_collect3.py --keywords "洗衣洗鞋店,洗衣店,洗鞋店,干洗店" --target-notes 50
  --target-comments 500 --cooldown 3

输出: out/洗衣洗鞋店/notes3.json（合并去重后）
"""

import argparse
import asyncio
import json
import os
import random
import sys
import urllib.parse

try:
    from .search_sort_dom import click_sort_option
except ImportError:
    from search_sort_dom import click_sort_option
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    # 与 dy_collect 保持同一套源码/冻结包兼容导入策略。
    from .cdp import CdpSession
except ImportError:
    from cdp import CdpSession
try:
    from .dy_collect import SearchVideosResult
except ImportError:
    from dy_collect import SearchVideosResult

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")


class HumanBlock(Exception):
    """需要人工处理的页面状态（登录、验证码）。"""


class RateLimited(Exception):
    """平台限流（请求太频繁 / 操作过于频繁）。

    限流是暂时性的平台反压信号，不是需要人工介入的状态：正确做法是停止
    当前请求、按退避时长等待后重试。旧实现把限流和验证码混成同一类
    ``HumanBlock`` 抛出，导致一次限流就冻结账号并要求人工点继续。
    """

    def __init__(self, reason: str = "rate_limited", message: str | None = None,
                 retry_after: float | None = None):
        self.reason = str(reason or "rate_limited")
        self.retry_after = retry_after
        super().__init__(str(message or "小红书触发限流，需要退避重试"))


# ---------------------------------------------------------------------------
# 节奏控制：小红书对同一窗口的连续请求非常敏感，固定间隔（固定 4 秒开详情、
# 固定 0.7 秒滚一屏、固定 30 秒冷却）本身就是最容易被风控建模的特征。
# 这里统一使用随机区间，并按“篇/分钟”限制整体速率。
# ---------------------------------------------------------------------------
NOTE_MIN_INTERVAL = (55.0, 75.0)        # 两篇笔记之间的随机最小间隔（约 1 篇/分钟）
NOTE_SETTLE = (5.0, 9.0)               # 打开笔记页后的随机稳定等待
COMMENT_SCROLL_INTERVAL = (2.2, 4.5)   # 评论每轮滚动的随机间隔
COMMENT_BREAK_EVERY = (5, 9)           # 每滚 N 轮进入一次长休（N 随机）
COMMENT_BREAK = (12.0, 28.0)           # 评论长休时长
SEARCH_SCROLL_INTERVAL = (2.5, 5.5)    # 搜索每轮滚动的随机间隔
SEARCH_BREAK_EVERY = (4, 7)            # 每滚 N 轮进入一次长休
SEARCH_BREAK = (15.0, 35.0)            # 搜索长休时长
NAV_SETTLE = (3.0, 6.0)                # 导航后的随机稳定等待（首次）
NAV_RETRY_SETTLE = (6.0, 11.0)         # 导航重试时等待更久
POLL_INTERVAL = (0.6, 1.1)             # 轮询等待时的随机步长

# 限流退避：首次等待后逐次翻倍，并叠加随机抖动。
RATE_LIMIT_BACKOFF = (60.0, 90.0)
RATE_LIMIT_BACKOFF_MAX = 600.0


def jitter(span):
    """返回区间内的随机秒数。"""
    low, high = span
    return random.uniform(float(low), float(high))


async def sleep_random(span):
    """按随机区间等待。"""
    await asyncio.sleep(jitter(span))


async def probe_blocked(c, sid):
    return await c.eval('''(function(){
      // innerText 会为每轮扫描触发布局计算；评论很多时这本身就会把浏览器
      // 拖慢。这里只做风控关键词探测，使用 textContent 足够且不会强制回流。
      const bodyText = document.body ? document.body.textContent : '';
      const isVisible = (el) => {
        const s = getComputedStyle(el); const r = el.getBoundingClientRect();
        return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0;
      };
      const candidates = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"],[class*="captcha"],[class*="Captcha"],[class*="verify"],[class*="Verify"],[class*="security"],[class*="Security"],iframe')].filter(isVisible);
      // 限流/验证文案只在可见弹窗、遮罩或验证容器里判定。整页 innerText
      // 会包含笔记正文和推荐内容，直接扫描容易把普通文案当成风控提示。
      const overlayNodes = candidates.filter(el => {
        const s = getComputedStyle(el);
        const role = (el.getAttribute('role') || '').toLowerCase();
        const identity = `${el.id || ''} ${typeof el.className === 'string' ? el.className : ''}`;
        const positioned = s.position === 'fixed' || s.position === 'absolute';
        const frame = el.tagName === 'IFRAME';
        return role === 'dialog' || (el.getAttribute('aria-modal') || '') === 'true'
          || positioned || frame || /captcha|verify|security/i.test(identity);
      });
      let t = '';
      for (const el of overlayNodes) t += ((el.textContent || el.innerText || '').trim() + '\\n');
      const overlayText = t.replace(/\\s+/g, ' ').trim();
      if (location.href.startsWith('chrome-extension:')) return 'bitbrowser拦截';
      // 小红书限流文案变体很多：“操作过于频繁”“请求太频繁”“访问频繁”等。
      // 旧正则 /操作频繁/ 匹配不到“操作过于频繁”，限流会被当成正常页面
      // 继续滚动，进而升级成验证码甚至账号处罚。
      const rateWords = /请求(?:过于|太)?频繁|操作(?:过于|太)?频繁|访问(?:过于|太)?频繁|操作过快|频率过高/;
      const retryHint = /一分钟后再试|请?稍后再试/;
      if (rateWords.test(overlayText)
          || (retryHint.test(overlayText) && /请求|操作|访问|频繁|频率/.test(overlayText))) return 'rate_limited';
      // 少数限流提示直接渲染在正文而没有弹窗容器，只对高辨识度短语兜底，
      // 避免笔记内容里出现“稍后再试”造成误判。
      if (/(?:请求|操作|访问)(?:过于|太)?频繁[\s\S]{0,24}(?:稍后|再试|分钟)/.test(bodyText)
          || /一分钟后再试/.test(bodyText)) return 'rate_limited';
      if (/登录后查看|扫码登录/.test(overlayText)) return '登录弹窗';
      // 小红书的手机号登录有时是整页登录态，不一定挂在 dialog 上。
      // 必须在自动重试/等待前抛给调度器，不能继续刷新页面打断验证码输入。
      if (/\/login(?:[/?#]|$)/.test(location.pathname)
          || /手机号登录|验证码登录|输入手机号|获取验证码/.test(bodyText)) return '登录页面';
      if (/滑块|拖动验证|滑动验证|安全验证|请完成验证|验证码|人机验证/.test(overlayText)) return '验证码';
      return null;
    })()''', sid)


def raise_for_block(reason, retry_after=None):
    """把 probe_blocked 的结果转成对应异常。

    限流抛 ``RateLimited``（可退避重试），登录/验证码抛 ``HumanBlock``
    （必须人工处理）。两者分开是避免一次限流就冻结账号的关键。
    """
    if not reason:
        return
    if reason == "rate_limited":
        raise RateLimited(retry_after=retry_after)
    raise HumanBlock(reason)


async def check_blocked(c, sid, retry_after=None):
    """探测风控状态，命中即抛对应异常。"""
    raise_for_block(await probe_blocked(c, sid), retry_after=retry_after)


async def load_feeds(c, sid, keyword, target_count=100,
                     pause_event=None, cancel_event=None,
                     search_sort="default"):
    kw = urllib.parse.quote(keyword)
    # 强制跳到搜索页（若已在别的页面则先导航，避免 __INITIAL_STATE__ 结构不是 search）
    url = f"https://www.xiaohongshu.com/search_result?keyword={kw}&source=web_explore_feed"
    # 小红书是 SPA，首次导航可能停留在探索页或拿到空 feeds；重试并且必须
    # 等到 feeds 真正有数据后才进入滚动阶段，避免把暂时空结果误判为失败。
    ready = False
    for attempt in range(3):
        await c.navigate(url, sid)
        # 首次导航与重试的稳定等待都随机化：固定「3 + attempt*2 秒」是可被
        # 平台统计的机器节奏，重试时也更容易连续命中限流。
        await sleep_random(NAV_SETTLE if attempt == 0 else NAV_RETRY_SETTLE)
        # 在任何 reload 前先确认页面没有真实的人机验证；否则 reload 会把验证页覆盖掉，
        # 造成“没有看到验证却被判定需人工”的错觉。
        await check_blocked(c, sid)
        # 不再执行 location.reload()。人工登录/输入短信验证码可能需要较长时间，
        # 自动刷新会直接清掉手机号和验证码状态。若页面确实是登录页，上一轮
        # probe_blocked 已经抛出对应异常，调度器会冻结等待人工处理。
        await sleep_random(NAV_SETTLE)
        await check_blocked(c, sid)
        for _ in range(12):
            try:
                ok = await c.eval("""(function(){
                  const s=window.__INITIAL_STATE__||{};
                  const f=s.search&&s.search.feeds;
                  const d=f?(f.value!==undefined?f.value:f._value):[];
                  return location.pathname.includes('search_result') && !!s.search && Array.isArray(d) && d.length>0;
                })()""", sid)
                if ok:
                    ready = True
                    break
            except Exception:
                pass
            await sleep_random(POLL_INTERVAL)
        if ready:
            break
    if not ready:
        raise RuntimeError("小红书搜索结果加载失败：页面未返回有效笔记数据，请检查登录状态或稍后重试")
    await check_blocked(c, sid)
    sort_labels = {"latest": ["最新"], "hot": ["最热", "热门"]}.get(str(search_sort), [])
    if sort_labels:
        await click_sort_option(c, sid, sort_labels)
        await sleep_random(POLL_INTERVAL)
    async def read_feeds():
        try:
            raw = await c.eval('''(function(){
              const s = window.__INITIAL_STATE__ || {};
              const f = s.search && s.search.feeds;
              const d = f ? (f.value !== undefined ? f.value : f._value) : [];
              return JSON.stringify(d || []);
            })()''', sid)
            return json.loads(raw)
        except Exception:
            return []

    # 首屏通常只有约 20 条；滚动触发小红书搜索接口继续加载，直到达到目标数。
    # 每轮重新读取 state，并按 note id 去重，避免只返回首屏 21 条。
    feeds_by_id = {}
    target = max(1, int(target_count or 100))
    stale_rounds = 0
    bottom_rounds = 0
    end_text_stale_rounds = 0
    last_end_text = False
    reached_target = False
    no_more_results = False
    rounds = 0
    # 目标越大允许的滚动轮次越多；安全上限只用于异常页面兜底，不能作为
    # 正常终止条件。正常结束必须是达到目标或确认页面没有更多结果。
    max_rounds = max(80, min(600, target // 8 + 80))
    max_load_wait = 10.0
    # 连续滚动是搜索阶段最密集的请求来源：每滚 N 轮（随机）插入一次长休，
    # 把请求密度摊平，而不是等被限流后再补救。
    scroll_break_every = random.randint(*SEARCH_BREAK_EVERY)
    scrolls_since_break = 0
    for _ in range(max_rounds):
        rounds += 1
        while pause_event is not None and not pause_event.is_set():
            if cancel_event is not None and cancel_event.is_set():
                break
            await asyncio.sleep(0.2)
        if cancel_event is not None and cancel_event.is_set():
            break
        current = await read_feeds()
        before = len(feeds_by_id)
        for f in current:
            fid = str(f.get("id") or "")
            if fid:
                feeds_by_id[fid] = f
        if len(feeds_by_id) >= target:
            reached_target = True
            break
        stale_rounds = stale_rounds + 1 if len(feeds_by_id) == before else 0
        try:
            state = await c.eval('''(function(){
              const text = document.body?.innerText || '';
              return {top: window.scrollY || 0,
                height: document.documentElement?.scrollHeight || 0,
                viewport: window.innerHeight || 0,
                endText: /暂时没有更多了|没有更多|暂无更多|到底了|已显示全部|没有找到更多/.test(text)};
            })()''', sid) or {}
            at_bottom = float(state.get("top") or 0) + float(state.get("viewport") or 0) >= float(state.get("height") or 0) - 24
            if at_bottom:
                bottom_rounds += 1
            else:
                bottom_rounds = 0
            # body 文本可能包含其它区域的“没有更多”，必须同时在底部、
            # 连续无新增且结束状态连续出现，避免搜索提前结束。
            if state.get("endText") and at_bottom and stale_rounds >= 1:
                end_text_stale_rounds = end_text_stale_rounds + 1 if last_end_text else 1
                last_end_text = True
            else:
                end_text_stale_rounds = 0
                last_end_text = False
            # 连续多轮停在底部且没有新笔记，或结束状态稳定出现，才认定无更多。
            if (end_text_stale_rounds >= 2) or (bottom_rounds >= 12 and stale_rounds >= 12):
                no_more_results = True
                break
        except Exception:
            if stale_rounds >= 12:
                no_more_results = True
                break
        await c.eval("window.scrollBy(0, Math.max(700, window.innerHeight * 0.8))", sid)
        # 滚动后最多等待 10 秒，直到 feeds 数量真正增加，避免短暂空响应
        # 被无进展计数器误判为没有更多。
        wait_started = asyncio.get_running_loop().time()
        while True:
            await sleep_random(POLL_INTERVAL)
            loaded = await read_feeds()
            before_loaded = len(feeds_by_id)
            for f in loaded:
                fid = str(f.get("id") or "")
                if fid:
                    feeds_by_id[fid] = f
            if len(feeds_by_id) > before_loaded:
                break
            if asyncio.get_running_loop().time() - wait_started >= max_load_wait:
                break
        # 每一页加载完成后立刻探测，不能等整个循环结束才发现限流/验证。
        await check_blocked(c, sid)
        # 翻页之间保持随机间隔，并按随机轮数插入长休。
        await sleep_random(SEARCH_SCROLL_INTERVAL)
        scrolls_since_break += 1
        if scrolls_since_break >= scroll_break_every:
            break_seconds = jitter(SEARCH_BREAK)
            print(f"[节流] 小红书搜索已连续滚动 {scrolls_since_break} 轮，长休 "
                  f"{break_seconds:.0f}s", flush=True)
            await asyncio.sleep(break_seconds)
            scrolls_since_break = 0
            scroll_break_every = random.randint(*SEARCH_BREAK_EVERY)
    feeds = list(feeds_by_id.values())[:target]
    out = []
    for f in feeds:
        nc = f.get("noteCard") or {}
        tag = (nc.get("cornerTagInfo") or [{}])[0].get("text", "")
        inter = nc.get("interactInfo") or {}
        out.append({
            "id": f.get("id"),
            "xsec_token": f.get("xsecToken") or (nc.get("user") or {}).get("xsecToken"),
            "title": nc.get("displayTitle") or nc.get("title") or "",
            "author": (nc.get("user") or {}).get("nickname") or (nc.get("user") or {}).get("nickName"),
            "author_id": (nc.get("user") or {}).get("userId"),
            "pub_time": tag,
            "type": nc.get("type") or f.get("modelType"),
            "comment_count": inter.get("commentCount", ""),
            "liked_count": inter.get("likedCount", ""),
            "keyword": keyword,
        })
    return SearchVideosResult(
        out,
        search_complete=(reached_target or no_more_results),
        reached_target=reached_target,
        no_more_results=no_more_results,
        rounds=rounds,
    )


async def fetch_note(c, sid, note, pause_event=None, cancel_event=None):
    nid = note["id"]
    token = note.get("xsec_token", "")
    url = f"https://www.xiaohongshu.com/explore/{nid}?xsec_token={urllib.parse.quote(token)}&xsec_source=pc_search"
    await c.navigate(url, sid)
    # 固定 4 秒打开详情页是最容易被统计的节奏特征，改为随机稳定等待。
    await sleep_random(NOTE_SETTLE)
    await check_blocked(c, sid)
    detail = await c.eval('''(function(){
      const s = window.__INITIAL_STATE__;
      if (!s || !s.note) return {err:'no state'};
      const map = s.note.noteDetailMap || {};
      const k0 = Object.keys(map)[0];
      if (!k0) return {err:'no map'};
      const nn = map[k0].note || {};
      return {title: nn.title || '', desc: (nn.desc||'').slice(0,1000), author: nn.user ? (nn.user.nickname||'') : '', time: nn.time || ''};
    })()''', sid)
    # 评论区是懒加载的，不能固定只滚 10 次或只取首屏 60 条。
    # 持续滚动到平台提示没有更多、滚动到底部且连续无新增；80 轮只作为
    # 页面异常时的安全兜底，不作为正常完成条件。
    expand_replies_js = r'''(() => {
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
      const scope = document.querySelector(
        '.comments-el,.comments-container,[class*="comments-container"]'
      ) || document;
      const candidates = [
        ...scope.querySelectorAll('.show-more,[class*="show-more"]'),
        ...scope.querySelectorAll('.comment-item button,.comment-item [role="button"],'
          + '.comment-item a,.comment-item [class*="reply"],'
          + '.comment-item [class*="Reply"],.comment-item [class*="more"],'
          + '.comment-item [class*="More"]')
      ];
      for (const el of candidates) {
          const label = clean(el.getAttribute('aria-label') || el.getAttribute('title')
            || el.textContent || el.innerText);
          if (!isExpandLabel(label)) continue;
          const control = el.closest('button,[role="button"],a') || el;
          if (visible(control)) controls.add(control);
      }
      const clicked = [];
      for (const control of controls) {
        const label = clean(control.textContent || control.innerText).slice(0, 80);
        control.focus?.();
        control.click();
        clicked.push(label);
      }
      return {expandedCount: clicked.length, labels: clicked.slice(0, 20)};
    })()'''
    idle_rounds = 0
    previous_count = 0
    # 评论滚动是详情页里请求最密集的动作。旧实现整段循环没有任何风控探测，
    # 只在进入循环前检查一次，于是被限流后会对着限流页继续滚 60~90 秒，
    # 把「限流」升级成「验证码」。这里每轮都探测，命中立即中断。
    scroll_break_every = random.randint(*COMMENT_BREAK_EVERY)
    scrolls_since_break = 0
    for _ in range(80):
        # 暂停/取消必须可响应：激进降频下单篇笔记的评论滚动可能持续数分钟，
        # 旧实现完全不检查事件，用户点停止后仍会继续滚到本轮结束。
        while pause_event is not None and not pause_event.is_set():
            if cancel_event is not None and cancel_event.is_set():
                break
            await asyncio.sleep(0.2)
        if cancel_event is not None and cancel_event.is_set():
            break
        await check_blocked(c, sid)
        await c.eval(expand_replies_js, sid)
        state = await c.eval('''(() => {
          const text = document.body?.textContent || '';
          const nodes = [...document.querySelectorAll('.comment-item')];
          const candidates = [
            ...document.querySelectorAll('.comment-mainContent, .comment-container, '
              + '[class*="comment-scroller"], [class*="comment-list"]')
          ].filter(el => el.scrollHeight > el.clientHeight + 20);
          const scroller = candidates.sort((a, b) =>
            (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0];
          const top = scroller ? scroller.scrollTop : (window.scrollY || 0);
          const height = scroller ? scroller.scrollHeight : document.documentElement.scrollHeight;
          const viewport = scroller ? scroller.clientHeight : window.innerHeight;
          return {count:nodes.length, noMore:/暂时没有更多|没有更多评论|已加载全部评论/.test(text),
            atBottom:top + viewport >= height - 24, hasScroller:!!scroller};
        })()''', sid) or {}
        count = int(state.get("count") or 0)
        idle_rounds = idle_rounds + 1 if count <= previous_count else 0
        previous_count = max(previous_count, count)
        if state.get("noMore") or (state.get("atBottom") and idle_rounds >= 3):
            break
        await c.eval('''(() => {
          const candidates = [
            ...document.querySelectorAll('.comment-mainContent, .comment-container, '
              + '[class*="comment-scroller"], [class*="comment-list"]')
          ].filter(el => el.scrollHeight > el.clientHeight + 20);
          const scroller = candidates.sort((a, b) =>
            (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0];
          if (scroller) scroller.scrollTop = scroller.scrollHeight;
          else window.scrollTo(0, document.body.scrollHeight);
          return !!scroller;
        })()''', sid)
        await sleep_random(POLL_INTERVAL)
        await c.eval(expand_replies_js, sid)
        # 滚动本身才真正触发评论接口请求，滚完立刻再探测一次并保持随机间隔。
        await check_blocked(c, sid)
        await sleep_random(COMMENT_SCROLL_INTERVAL)
        scrolls_since_break += 1
        if scrolls_since_break >= scroll_break_every:
            break_seconds = jitter(COMMENT_BREAK)
            print(f"[节流] 小红书评论已连续滚动 {scrolls_since_break} 轮，长休 "
                  f"{break_seconds:.0f}s", flush=True)
            await asyncio.sleep(break_seconds)
            scrolls_since_break = 0
            scroll_break_every = random.randint(*COMMENT_BREAK_EVERY)
    comments = await c.eval('''(function(){
      const out = []; const seen = new Set();
      const body = document.body ? document.body.textContent : '';
      if (body.includes('这是一片荒地') || body.includes('暂无评论')) return {desert:true, comments:[]};
      // 每条评论即一个 .comment-item（含头像、昵称、文本、日期-地区、赞、回复）
      const nodes = [...document.querySelectorAll('.comment-item')];
      const textOf = el => String(el?.textContent || '').replace(/\s+/g, ' ').trim();
      for (const n of nodes) {
        let user = '', user_id = '';
        // 评论者主页 userId
        const uidEl = n.querySelector('a[data-user-id]');
        if (uidEl) { user_id = uidEl.getAttribute('data-user-id') || ''; user = textOf(uidEl); }
        if (!user) {
          // 兜底：第一个非空文本块视为昵称（部分评论头像链接无文字）
          const nameEl = n.querySelector('.name, .user-name, span');
          user = nameEl ? textOf(nameEl) : '';
        }
        const contentEl = n.querySelector(
          '.comment-mainContent, .comment-content, .comment-text, '
          + '[class*="commentContent"], [class*="comment-content"]'
        );
        // 大量评论时 innerText 会反复触发布局计算；只有找不到正文节点时
        // 才使用它做兼容兜底，正常路径统一读取 textContent。
        const nodeText = contentEl ? textOf(n) : String(n.innerText || n.textContent || '');
        const lines = nodeText.split(String.fromCharCode(10)).filter(Boolean);
        // 小红书会把“作者”身份徽标放在评论行内部；如果真实正文为空、
        // 图片或表情，直接读取整行文本会把这个徽标误当成评论正文。
        // 先读取正文节点，回退到整行时也必须排除全部界面元数据。
        const contentLines = contentEl
          ? textOf(contentEl).split(String.fromCharCode(10)).filter(Boolean)
          : lines;
        const metadataLabels = new Set(['赞', '回复', '作者', '作者回复', '展开', '收起', '更多', '分享', '删除', '举报']);
        // text：跳过昵称、日期地区和操作/身份标签，取第一条真实正文
        let text = '';
        for (const ln of contentLines) {
          const s = ln.trim();
          if (!s || s === user || metadataLabels.has(s) || /^\\d+$/.test(s) || s.startsWith('展开')) continue;
          if (/^\\d{1,2}-\\d{1,2}/.test(s)) continue;  // 跳过日期-地区行
          text = s; break;
        }
        // 只从当前评论自己的日期节点读取，避免整页/整条楼中楼容器中
        // 混入作品发布日期或其它评论日期。明确年份与无年份使用互斥规则，
        // 防止“2025-12-25”被降级匹配成“12-25”。
        const dateText = textOf(n.querySelector('.date'));
        const full = dateText || nodeText;
        const explicitDate = full.match(/(?:^|\\n)\\s*(\\d{4})\\s*[-/年]\\s*(\\d{1,2})\\s*[-/月]\\s*(\\d{1,2})日?\\s*([\\u4e00-\\u9fff]{2,8})?/);
        const currentDate = explicitDate ? null : full.match(/(?:^|\\n)\\s*(\\d{1,2})\\s*[-/月]\\s*(\\d{1,2})日?\\s*([\\u4e00-\\u9fff]{2,8})?/);
        const rawTime = explicitDate
          ? `${explicitDate[1]}-${explicitDate[2]}-${explicitDate[3]}`
          : (currentDate ? `${currentDate[1]}-${currentDate[2]}` : '');
        const region = explicitDate
          ? (explicitDate[4] || '')
          : (currentDate ? (currentDate[3] || '') : '');
        const key = user + '|' + text.slice(0,40);
        if (seen.has(key) && !user_id) continue;
        seen.add(key);
        if (!user || !text) continue;
        out.push({user, user_id, text: text.slice(0,300), time: rawTime, raw_time: rawTime, region,
                  homepage: user_id ? 'https://www.xiaohongshu.com/user/profile/'+user_id : ''});
      }
      return {desert:false, comments: out};
    })()''', sid)
    result = dict(note)
    result.update(detail)
    result["comments"] = comments.get("comments", []) if comments.get("desert") is False else []
    result["note_id"] = nid
    return result


async def run(ws_url, keywords, target_notes, target_comments, cooldown):
    out = os.path.join(OUT_DIR, keywords[0])
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "notes3.json")
    if os.path.exists(path):
        notes = json.load(open(path, encoding="utf-8"))
    else:
        notes = []
    done_ids = {n.get("note_id") for n in notes}

    c = CdpSession(ws_url)
    await c.connect()
    sid = await c.attach_page()
    try:
        for kw in keywords:
            if len(notes) >= target_notes:
                print(f"[达标] 笔记数已达 {len(notes)}，停止更多关键词")
                break
            print(f"\n===== 关键词: {kw} =====")
            feeds = await load_feeds(c, sid, kw)
            print(f"  搜索到 {len(feeds)} 条，去重后待采 {sum(1 for f in feeds if f['id'] not in done_ids)} 条")
            for f in feeds:
                if len(notes) >= target_notes:
                    break
                if f["id"] in done_ids:
                    continue
                # 限流是暂时性的：先按退避等待重试同一篇，重试耗尽才交给上层
                # 决定是否冻结账号。旧实现遇到限流直接抛 HumanBlock 冻结。
                backoff = jitter(RATE_LIMIT_BACKOFF)
                for attempt in range(3):
                    try:
                        note = await fetch_note(c, sid, f)
                        if note.get("err"):
                            break
                        note["keyword"] = kw
                        notes.append(note)
                        done_ids.add(f["id"])
                        json.dump(notes, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                        cur_c = sum(len(n2["comments"]) for n2 in notes)
                        print(f"  ✅ {len(notes)}篇 | {note['title'][:16]} | 💬{len(note['comments'])} | 累计评论{cur_c}")
                        # 篇间间隔取「任务冷却」与「安全下限」的较大值，并保留随机性。
                        wait = max(float(cooldown or 0), jitter(NOTE_MIN_INTERVAL))
                        await asyncio.sleep(wait)
                        break
                    except RateLimited as rl:
                        if attempt >= 2:
                            raise
                        wait = min(backoff * (2 ** attempt), RATE_LIMIT_BACKOFF_MAX)
                        wait += jitter((0.0, 15.0))
                        print(f"  ⏳ 触发限流（{rl}），退避 {wait:.0f}s 后重试 "
                              f"({attempt + 1}/3)", flush=True)
                        await asyncio.sleep(wait)
                    except HumanBlock:
                        raise
                    except Exception as e:
                        print(f"  ⚠️ {f['title'][:16]} 异常 {repr(e)[:60]}")
                        await asyncio.sleep(2)
                        break
    finally:
        await c.close()
    total_c = sum(len(n["comments"]) for n in notes)
    print(f"\n[完成] notes3.json: {len(notes)} 篇 / {total_c} 条评论")
    print(f"[目标] {target_notes} 篇 / {target_comments} 条 → {'✅达标' if len(notes)>=target_notes and total_c>=target_comments else '未达标，需更多关键词'}")
    return notes


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", help="浏览器 CDP WebSocket；省略时读取 data/xhs_window.txt")
    ap.add_argument("--keywords", default="洗衣洗鞋店,洗衣店,洗鞋店,干洗店,洗衣加盟,洗鞋加盟")
    ap.add_argument("--target-notes", type=int, default=50)
    ap.add_argument("--target-comments", type=int, default=500)
    ap.add_argument("--cooldown", type=float, default=2.5)
    args = ap.parse_args()
    if not args.ws:
        ws_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "xhs_window.txt")
        try:
            args.ws = [line.strip() for line in open(ws_path, encoding="utf-8") if line.strip()][-1]
        except (OSError, IndexError):
            ap.error(f"缺少 --ws，且窗口文件不可用：{ws_path}")
    kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
    try:
        asyncio.run(run(args.ws, kws, args.target_notes, args.target_comments, args.cooldown))
    except HumanBlock as hb:
        print(f"\n🚨 P7 冻结: {hb}")
        sys.exit(3)
    except RateLimited as rl:
        print(f"\n⏳ 限流未恢复，已退出等待下次调度: {rl}")
        sys.exit(4)
