#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dy_collect.py — 抖音采集器（BitBrowser + Python CDP + Network 旁观拦截）。

方法论遵循昨天经验证的正确管线（真实浏览器请求自动带签名，不逆向 a_bogus）：
  阶段 A 搜索：导航 type=general 搜索页 → 拦所有 /aweme/v1/web/ 响应 →
              解析 data[].aweme_info → 图文(note) + 视频(video) 清单
              关键：note 走 /note/{id}，video 走 /video/{id}，URL 类型严格匹配，
              避免「图文当视频打开 → 视频不存在/回推荐流」。
  阶段 B 评论：逐作品 Page.navigate 进作品页 → 滚动 .route-scroll-container +
              点「加载更多」类按钮 → 拦 /comment/list/ + /comment/list/reply/ →
              安静窗口(QuietSeconds)数据驱动终止，按 cid 去重合并。
  评论采集：持续读取页面/接口能加载到的一级评论和楼中回复，按平台结束信号停止。

用法:
  python dy_collect.py --keyword 快递柜 [--limit 20] [--cooldown 3] [--quiet 4] [--max-work 300]
输出: out/抖音-<keyword>/videos.json + comments.json + dy_意向客户.csv (7列)
"""

import argparse
import asyncio
import csv
import json
import os
import sys
import time as _time
import urllib.parse

try:
    from .search_sort_dom import click_sort_option
except ImportError:
    from search_sort_dom import click_sort_option
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    # 源码包运行时使用相对导入；PyInstaller 将入口依赖收进顶层时，
    # 回退到顶层模块名，避免 live_collector 导入链启动失败。
    from .cdp import CdpSession
except ImportError:
    from cdp import CdpSession

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")


class HumanBlock(Exception):
    """需要人工处理的页面状态，并保留明确原因。"""

    def __init__(self, reason, message=None):
        self.reason = str(reason or "unknown")
        super().__init__(str(message or self.reason))


class SearchVideosResult(list):
    """阶段 A 的结果，同时携带“达到目标/确认无更多”的终止信息。

    继承 list 保持现有采集器接口兼容；调度器可以读取元数据，避免把
    一次只返回了半页结果误认为搜索已经完成。
    """

    def __init__(self, values=(), *, search_complete=True,
                 reached_target=False, no_more_results=False, rounds=0,
                 termination_reason=""):
        super().__init__(values)
        self.search_complete = bool(search_complete)
        self.reached_target = bool(reached_target)
        self.no_more_results = bool(no_more_results)
        self.rounds = int(rounds or 0)
        self.termination_reason = str(termination_reason or "")


# ---------------------------------------------------------------------------
# 阶段 A：搜索（拦所有 /aweme/v1/web/ 响应，note/video 类型匹配 URL）
# ---------------------------------------------------------------------------
async def _attach_response_list(c, sid):
    """注册 Network.responseReceived 旁观收集器，返回 (append 函数)。"""
    responses = []
    c.on("Network.responseReceived", lambda p: responses.append((p.get("response", {}).get("url", ""), p.get("requestId", ""), p.get("response", {}).get("mimeType", ""))))
    return responses


async def probe_blocked(c, sid):
    reason = await c.eval('''(function(){
      // 抖音风控有时使用 iframe/Shadow DOM，不一定把文字放在 role=dialog 中。
      // 同时检查验证容器、iframe 地址、页面 URL/标题和明确的风控文案。
      const nodes = document.querySelectorAll(
        '[role="dialog"], [class*="modal"], [class*="Modal"], ' +
        '[class*="captcha"], [class*="Captcha"], [id*="captcha"], [id*="Captcha"], ' +
        '[class*="verify"], [class*="Verify"], [id*="verify"], [id*="Verify"], ' +
        '[class*="secsdk"], [id*="secsdk"], [class*="security"], [id*="security"], ' +
        '[data-testid*="captcha"], [data-testid*="verify"], ' +
        'iframe[src*="captcha"], iframe[src*="verify"], iframe[src*="security"]'
      );
      const isVisible = (el) => {
        const s = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s.display !== 'none' && s.visibility !== 'hidden' &&
          Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0;
      };
      const visibleNodes = [...nodes].filter(isVisible);
      // 页面本身也可能带有包含“modal”字样的普通布局类（例如 Note 的
      // LookModalFrameFast）。只有对话框、定位层或明确的验证容器才算风控候选，
      // 避免把作品正文/推荐内容中的“稍后再试”当成限流提示。
      const overlayNodes = visibleNodes.filter(el => {
        const s = getComputedStyle(el);
        const role = (el.getAttribute('role') || '').toLowerCase();
        const identity = `${el.id || ''} ${typeof el.className === 'string' ? el.className : ''}`;
        const positioned = s.position === 'fixed' || s.position === 'absolute';
        return role === 'dialog' || positioned || /captcha|verify|secsdk|security/i.test(identity);
      });
      let t = '';
      for (let i = 0; i < overlayNodes.length; i++) t += (overlayNodes[i].innerText || '').trim() + '\\n';
      const frameSrc = [...document.querySelectorAll('iframe')].filter(isVisible).map(x => x.src || '').join(' ');
      const urlTitle = `${location.href} ${document.title} ${frameSrc}`;
      // 不能因为普通页面的 class/URL 中出现 security、verify、modal 就判定验证码。
      // 必须有“可见验证容器 + 明确验证文案”，或明确的验证 iframe 才能冻结账号。
      const overlayText = t.replace(/\\s+/g, ' ').trim();
      const hasChallengeText = /滑块|拖动验证|滑动验证|安全验证|请完成验证|请完成安全验证|验证后继续|验证码|人机验证/.test(overlayText);
      // 限流文案只能在可见弹窗/遮罩中判定。不能扫描整个 body：Note 页的推荐
      // 作品文案可能包含“服务繁忙，请稍后再试”等普通文本，会造成误冻结。
      const hasRateLimitText = /请求太频繁|操作频繁|一分钟后再试|访问频繁|请求过于频繁|稍后再试/.test(overlayText);
      const visibleChallengeFrame = [...document.querySelectorAll('iframe')].some(frame => {
        if (!isVisible(frame)) return false;
        const src = (frame.src || '').toLowerCase();
        return /(captcha|verify|challenge|secsdk|security)/i.test(src);
      });
      if (hasRateLimitText) return 'rate_limited';
      if (hasChallengeText) return '验证码';
      // 跨域验证码 iframe 的文字无法从父页面读取；只要验证 iframe 本身可见，
      // 就已经足以判定，不能再要求外层 overlayText 非空。
      if (visibleChallengeFrame) return '验证码';
      if (t.includes('扫码登录') || (t.includes('请使用抖音扫码') && t.length < 300)) return '扫码弹窗';
      if (t.includes('登录后查看') || t.includes('请先登录')) return '需登录';
      return null;
    })()''', sid)
    if reason:
        return reason
    return await probe_scroll_locked(c, sid)


async def probe_scroll_locked(c, sid):
    """检测“灰色遮罩拦截交互 + 页面滚轮失效”的人工验证状态。

    先确认存在覆盖大部分视口、可拦截鼠标事件的遮罩，再发送一次真实 CDP
    滚轮事件。页面本身必须有可滚动空间；若滚动位置完全不变，则返回人工
    接管理由。滚动成功时立即恢复原位置，避免探测改变采集页面状态。
    """
    state = await c.eval('''(function(){
      const vw = Math.max(document.documentElement.clientWidth || 0, window.innerWidth || 0);
      const vh = Math.max(document.documentElement.clientHeight || 0, window.innerHeight || 0);
      if (vw < 100 || vh < 100) return null;
      const hintRe = /captcha|verify|challenge|secsdk|security|滑块|验证/i;
      const points = [
        [vw * .5, vh * .5], [vw * .35, vh * .5], [vw * .65, vh * .5],
        [vw * .5, vh * .35], [vw * .5, vh * .65]
      ];
      const alphaOf = (color) => {
        const m = String(color || '').match(/rgba?\([^,]+,[^,]+,[^,]+(?:,\s*([\d.]+))?\)/i);
        return m ? (m[1] === undefined ? 1 : Number(m[1])) : 0;
      };
      let blocker = null;
      for (const [x, y] of points) {
        let el = document.elementFromPoint(x, y);
        while (el && el !== document.body && el !== document.documentElement) {
          const s = getComputedStyle(el);
          const r = el.getBoundingClientRect();
          const covers = r.width >= vw * .68 && r.height >= vh * .68;
          const intercepts = s.pointerEvents !== 'none' && s.visibility !== 'hidden' &&
            s.display !== 'none' && Number(s.opacity || 1) > 0;
          const frame = el.tagName === 'IFRAME' || !!el.querySelector('iframe');
          const identity = `${el.id || ''} ${typeof el.className === 'string' ? el.className : ''} ` +
            `${frame ? [...el.querySelectorAll('iframe')].map(f => f.src || '').join(' ') : ''}`;
          const dimmed = alphaOf(s.backgroundColor) >= .08 ||
            (s.backdropFilter && s.backdropFilter !== 'none') ||
            (s.webkitBackdropFilter && s.webkitBackdropFilter !== 'none');
          const positioned = s.position === 'fixed' || s.position === 'absolute';
          if (covers && intercepts && (positioned || frame) && (dimmed || frame || hintRe.test(identity))) {
            blocker = el;
            break;
          }
          el = el.parentElement;
        }
        if (blocker) break;
      }
      if (!blocker) return null;
      const isVisible = (el) => {
        if (!el) return false;
        const s = getComputedStyle(el), r = el.getBoundingClientRect();
        return s.display !== 'none' && s.visibility !== 'hidden' &&
          Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0;
      };
      const isScrollable = (el) => el && isVisible(el) &&
        Number(el.scrollHeight || 0) - Number(el.clientHeight || 0) >= 160;
      const commentSelectors = [
        '.comment-mainContent', '.comment-container',
        '[data-e2e="scroll-container"]', '[class*="comment-"]',
        '[class*="comment"]', '[class*="scroll-container"]'
      ];
      const commentTarget = commentSelectors
        .flatMap(sel => [...document.querySelectorAll(sel)])
        .find(isScrollable);
      // note 页主文档通常不可滚动，评论面板才是实际滚动上下文。
      // 没找到可滚动目标时，不把主文档滚不动直接判为验证码。
      if (commentTarget) {
        const r = commentTarget.getBoundingClientRect();
        return {
          before: Number(commentTarget.scrollTop || 0),
          max: Math.max(0, Number(commentTarget.scrollHeight || 0) - Number(commentTarget.clientHeight || 0)),
          x: Math.round(Math.max(r.left + 10, Math.min(r.right - 10, r.left + r.width / 2))),
          y: Math.round(Math.max(r.top + 10, Math.min(r.bottom - 10, r.top + r.height / 2))),
          kind: 'comment'
        };
      }
      const scroller = document.scrollingElement || document.documentElement;
      const max = Math.max(0, Number(scroller.scrollHeight || 0) - Number(scroller.clientHeight || vh));
      if (max < 160) return null;
      return {before: Number(scroller.scrollTop || window.scrollY || 0), max,
        x: Math.round(vw / 2), y: Math.round(vh / 2), kind: 'document'};
    })()''', sid)
    if not isinstance(state, dict):
        return None
    try:
        before = float(state.get("before", 0))
        positions = [before]
        # 两个方向都测试：位于页面顶部时向上本来就不会动，位于底部时
        # 向下也不会动；只有向上、向下均无位移才视为滚轮被验证层锁死。
        kind = str(state.get("kind") or "document")
        if kind == "comment":
            read_scroll = '''(() => {
              const sels = ['.comment-mainContent', '.comment-container',
                '[data-e2e="scroll-container"]', '[class*="comment-"]',
                '[class*="comment"]', '[class*="scroll-container"]'];
              const visible = (el) => {
                if (!el) return false;
                const s = getComputedStyle(el), r = el.getBoundingClientRect();
                return s.display !== 'none' && s.visibility !== 'hidden' &&
                  Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0;
              };
              const el = sels.flatMap(sel => [...document.querySelectorAll(sel)])
                .find(x => visible(x) && Number(x.scrollHeight || 0) - Number(x.clientHeight || 0) >= 160);
              return Number(el?.scrollTop || 0);
            })()'''
            restore_scroll = '''(() => {
              const sels = ['.comment-mainContent', '.comment-container',
                '[data-e2e="scroll-container"]', '[class*="comment-"]',
                '[class*="comment"]', '[class*="scroll-container"]'];
              const visible = (el) => {
                if (!el) return false;
                const s = getComputedStyle(el), r = el.getBoundingClientRect();
                return s.display !== 'none' && s.visibility !== 'hidden' &&
                  Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0;
              };
              const el = sels.flatMap(sel => [...document.querySelectorAll(sel)])
                .find(x => visible(x) && Number(x.scrollHeight || 0) - Number(x.clientHeight || 0) >= 160);
              if (el) el.scrollTop = %s;
            })()''' % repr(before)
        else:
            read_scroll = "Number((document.scrollingElement || document.documentElement).scrollTop || window.scrollY || 0)"
            restore_scroll = f"(document.scrollingElement || document.documentElement).scrollTop = {before!r}"
        for delta in (-520, 520):
            await c.cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": int(state.get("x", 0)),
                    "y": int(state.get("y", 0)),
                    "deltaX": 0,
                    "deltaY": delta,
                },
                session_id=sid,
            )
            await asyncio.sleep(0.25)
            current = await c.eval(read_scroll, sid)
            positions.append(float(current or 0))
    except Exception:
        # 探测本身失败不能直接判定验证码，交给原采集异常处理。
        return None
    moved = any(abs(current - previous) >= 2
                for previous, current in zip(positions, positions[1:]))
    if not moved:
        return "验证码（页面灰屏且滚轮失效）"
    try:
        await c.eval(
            restore_scroll,
            sid,
        )
    except Exception:
        pass
    return None


def _kind_of(aweme_info: dict) -> str:
    """判定作品类型：有 images 或 aweme_type==68 → note，否则 video。"""
    imgs = aweme_info.get("images") or []
    if isinstance(imgs, list) and len([x for x in imgs if x]) > 0:
        return "note"
    if str(aweme_info.get("aweme_type")) == "68":
        return "note"
    return "video"


def _iter_aweme(responses):
    """从拦截到的 /aweme/v1/web/ 响应里提取 aweme_info 对象。"""
    for item in responses:
        u, rid = item[0], item[1]
        if "/aweme/v1/web/" not in u:
            continue
        yield u, rid


async def search_videos(c, sid, keyword, responses=None, mode="standard",
                        target_count=None, pause_event=None, cancel_event=None,
                        search_sort="default") -> list:
    """阶段 A：持续滚动搜索，直到达到目标或确认没有更多结果。

    返回 ``SearchVideosResult``，内容仍是兼容原接口的作品列表，但带有
    ``reached_target``、``no_more_results`` 和 ``rounds`` 元数据。
    """
    kw = urllib.parse.quote(keyword)
    url = f"https://www.douyin.com/search/{kw}?type=general"
    if responses is None:
        responses = []
        c.on("Network.responseReceived", lambda p: responses.append((p.get("response", {}).get("url", ""), p.get("requestId", ""))))
    await c.cmd("Network.enable", {}, session_id=sid)
    # 如果上一次操作已经留下验证层，禁止用 navigate 覆盖它，必须先交给人工处理。
    existing_block = await probe_blocked(c, sid)
    if existing_block:
        raise HumanBlock(existing_block)
    sort_query = {"most_like": "1", "latest": "2"}.get(str(search_sort), "0")
    await c.navigate(f"{url}&sort_type={sort_query}", sid)
    await asyncio.sleep(8)
    sort_labels = {
        "most_like": ["最多点赞", "点赞最多"],
        "latest": ["最新发布", "最新"],
    }.get(str(search_sort), [])
    if sort_labels:
        await click_sort_option(c, sid, sort_labels)
        await asyncio.sleep(1.0)
    # 滚动触发翻页加载（多列布局）。不同模式使用不同采集深度。
    scroll_limit, default_target, interval = {
        "fast": (8, 100, 2.0),
        "standard": (50, 500, 2.5),
        "deep": (1000, 10000, 3.0),
    }.get(mode, (50, 500, 2.5))
    # 抖音搜索结果是懒加载的；每次滚动后最多等待 10 秒，期间持续
    # 检查网络响应和页面结果，不能用短固定睡眠把“仍在加载”当成无更多。
    max_load_wait = 10.0
    target_count = int(target_count or default_target)
    # 标准模式原来的 50 轮对 1000 条目标明显不够；上限只是防止页面
    # 异常时无限等待，真正终止必须由“达到目标”或“确认无更多”触发。
    max_rounds = max(scroll_limit, min(600, target_count // 8 + 80))
    min_no_more_rounds = 12
    stale_rounds = 0
    bottom_stale_rounds = 0
    end_text_stale_rounds = 0
    last_end_message = ""
    last_stream_count = 0
    last_candidate_count = 0
    last_scroll_height = 0
    last_scroll_top = -1
    parsed_response_ids = set()
    all_items = {}
    reached_target = False
    no_more_results = False
    termination_reason = ""
    rounds = 0

    async def visible_unique_count():
        try:
            return int(await c.eval('''(() => {
              const ids = new Set();
              for (const a of document.querySelectorAll('a[href*="/video/"],a[href*="/note/"]')) {
                const m = (a.href || '').match(/\/(?:video|note)\/([A-Za-z0-9_-]+)/);
                if (m) ids.add(m[1]);
              }
              return ids.size;
            })()''', sid) or 0)
        except Exception:
            return 0

    async def absorb_new_responses():
        """尽早解析已完成响应，搜索过程中就能知道真实去重数量。"""
        for u, rid in list(responses):
            if rid in parsed_response_ids:
                continue
            try:
                body = await c.get_body(rid, sid)
            except Exception:
                continue
            try:
                if "general/search/stream" in u:
                    payload = parse_stream_ndjson(body)
                    for ai in payload:
                        _absorb(all_items, ai)
                else:
                    data = json.loads(body)
                    for d in data.get("data") or []:
                        if isinstance(d, dict):
                            ai = d.get("aweme_info") or d
                            if ai.get("aweme_id"):
                                _absorb(all_items, ai)
                parsed_response_ids.add(rid)
            except Exception:
                # 响应可能仍在传输中，下轮重试，不把一次解析失败当成无更多。
                continue

    for _ in range(max_rounds):
        rounds += 1
        while pause_event is not None and not pause_event.is_set():
            if cancel_event is not None and cancel_event.is_set():
                break
            await asyncio.sleep(0.2)
        if cancel_event is not None and cancel_event.is_set():
            break
        before_visible = await visible_unique_count()
        await c.eval("window.scrollBy(0, 900)", sid)
        before_items = len(all_items)
        wait_started = asyncio.get_running_loop().time()
        while True:
            await asyncio.sleep(min(0.5, max(0.1, interval / 2)))
            await absorb_new_responses()
            current_visible = await visible_unique_count()
            # 只把去重后的新作品视为进度；重复作品/重复响应不算新内容。
            if len(all_items) > before_items or current_visible > before_visible:
                break
            if asyncio.get_running_loop().time() - wait_started >= max_load_wait:
                break
        # 每一页完成后立即检查验证层；不能等整个翻页循环结束后才发现。
        blocked = await probe_blocked(c, sid)
        if blocked:
            raise HumanBlock(blocked)
        await absorb_new_responses()
        try:
            state = await c.eval('''(function(){
              const sc = document.scrollingElement || document.documentElement;
              const text = (document.body ? document.body.innerText : '') || '';
              const links = [...document.querySelectorAll('a[href*="/video/"],a[href*="/note/"]')];
              const ids = new Set();
              for (const a of links) {
                const m = (a.href || '').match(/\/(?:video|note)\/([A-Za-z0-9_-]+)/);
                if (m) ids.add(m[1]);
              }
              const top = Number(sc.scrollTop || window.scrollY || 0);
              const height = Number(sc.scrollHeight || document.body?.scrollHeight || 0);
              const viewport = Number(sc.clientHeight || window.innerHeight || 0);
              return {
                visible: ids.size,
                top, height, viewport,
                atBottom: top + viewport >= height - 24,
                endText: /暂时没有更多了|没有更多|暂无更多|到底了|已显示全部|没有找到更多/.test(text),
                endMessage: (text.match(/暂时没有更多了|没有更多|暂无更多|到底了|已显示全部|没有找到更多/) || [])[0] || ''
              };
            })()''', sid) or {}
            visible = int(state.get("visible") or 0)
            candidate_count = max(visible, len(all_items))
            if candidate_count >= target_count:
                reached_target = True
                termination_reason = "达到目标数量"
                break
        except Exception:
            state = {}
            candidate_count = len(all_items)
        # 进度只能由去重后的候选数量增加产生，不能由重复接口响应数量产生。
        stream_count = sum(1 for u, _rid in responses if "general/search/stream" in u)
        progress = candidate_count > last_candidate_count
        if progress:
            stale_rounds = 0
        else:
            stale_rounds += 1
        at_bottom = bool(state.get("atBottom"))
        scroll_height = int(state.get("height") or 0)
        scroll_top = int(state.get("top") or 0)
        if at_bottom and scroll_height == last_scroll_height and scroll_top == last_scroll_top and not progress:
            bottom_stale_rounds += 1
        elif at_bottom and not progress:
            bottom_stale_rounds += 1
        else:
            bottom_stale_rounds = 0
        # 结束文案可能来自页面其它区域（推荐卡片、弹层或旧 DOM），不能
        # 只因 body 文本出现一次就结束搜索。必须已经在底部，并连续两轮
        # 没有新增且结束文案保持一致，才确认确实没有更多结果。
        end_message = str(state.get("endMessage") or "")
        if bool(state.get("endText")) and at_bottom and not progress:
            if end_message and end_message == last_end_message:
                end_text_stale_rounds += 1
            else:
                end_text_stale_rounds = 1
            last_end_message = end_message
        else:
            end_text_stale_rounds = 0
            last_end_message = ""
        if end_text_stale_rounds >= 2:
            no_more_results = True
            termination_reason = f"页面提示：{end_message or '暂时没有更多了'}"
            break
        if bottom_stale_rounds >= min_no_more_rounds:
            no_more_results = True
            termination_reason = "已滚动到底部且连续无新增视频"
            break
        last_candidate_count = max(last_candidate_count, candidate_count)
        last_stream_count = max(last_stream_count, stream_count)
        last_scroll_height = scroll_height
        last_scroll_top = scroll_top
    # 结束条件也必须经过最后一次风控检查，不能把验证页当作“翻页结束”。
    final_block = await probe_blocked(c, sid)
    if final_block:
        raise HumanBlock(final_block)
    # 最后一轮再解析一次响应，兼容响应刚好在循环结束时到达的情况。
    seen_rid = set()
    for u, rid in _iter_aweme(responses):
        if rid in seen_rid or rid in parsed_response_ids:
            continue
        seen_rid.add(rid)
        try:
            body = await c.get_body(rid, sid)
        except Exception:
            continue
        # general/search/stream 是 NDJSON(SSE)，其它是 JSON
        if "general/search/stream" in u:
            for ai in parse_stream_ndjson(body):
                    _absorb(all_items, ai)
        else:
            try:
                data = json.loads(body)
            except Exception:
                continue
            for d in data.get("data") or []:
                if isinstance(d, dict):
                    ai = d.get("aweme_info") or d
                    if ai.get("aweme_id"):
                        _absorb(all_items, ai)

    # 归一化为统一字段
    videos = []
    for aid, ai in all_items.items():
        author = (ai.get("author") or {})
        kind = _kind_of(ai)
        videos.append({
            "vid": aid,
            "url": f"https://www.douyin.com/{kind}/{aid}",
            "kind": kind,
            "desc": (ai.get("desc") or "")[:300],
            "nickname": author.get("nickname", ""),
            "sec_uid": author.get("sec_uid", ""),
            "create_time": ai.get("create_time"),
            "comment_count": (ai.get("statistics") or {}).get("comment_count", ""),
            "keyword": keyword,
        })
    # 网络响应体可能因浏览器回收 requestId 而无法读取；用当前搜索页 DOM 兜底。
    # 这能避免“页面有结果但 API 解析为空”导致任务永远停在阶段 A。
    if not videos:
        try:
            dom_items = await c.eval('''(function(){
              const out=[]; const seen=new Set();
              for (const a of document.querySelectorAll('a[href*="/video/"],a[href*="/note/"]')) {
                const href=a.href||''; const m=href.match(/\/(video|note)\/([A-Za-z0-9_-]+)/);
                if(!m || seen.has(m[2])) continue;
                seen.add(m[2]);
                out.push({vid:m[2], kind:m[1], url:href,
                          desc:(a.innerText||a.textContent||'').trim().slice(0,300)});
              }
              return out.slice(0,100);
            })()''', sid) or []
            for d in dom_items:
                if d.get("vid"):
                    videos.append({
                        "vid": str(d["vid"]), "url": d.get("url", ""),
                        "kind": d.get("kind", "video"), "desc": d.get("desc", ""),
                        "nickname": "", "sec_uid": "", "create_time": None,
                        "comment_count": "", "keyword": keyword,
                    })
        except Exception:
            pass
    return SearchVideosResult(
        videos[:target_count],
        search_complete=(reached_target or no_more_results),
        reached_target=reached_target,
        no_more_results=no_more_results,
        rounds=rounds,
        termination_reason=termination_reason,
    )


def _absorb(all_items: dict, ai: dict):
    aid = str(ai["aweme_id"])
    if aid not in all_items:
        all_items[aid] = ai


def parse_stream_ndjson(raw: str) -> list:
    """解析 /general/search/stream/ 的 NDJSON(SSE) 流，提取 aweme_info 列表。"""
    items = []
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
        for d in obj.get("data") or []:
            if isinstance(d, dict):
                ai = d.get("aweme_info") or d
                if ai.get("aweme_id"):
                    items.append(ai)
    seen = set()
    out = []
    for ai in items:
        aid = str(ai["aweme_id"])
        if aid not in seen:
            seen.add(aid)
            out.append(ai)
    return out


# ---------------------------------------------------------------------------
# 阶段 B：评论（旁观拦截 + 安静窗口数据驱动终止）
# ---------------------------------------------------------------------------
# 触发 JS：滚动评论区容器 + 点"加载更多"类按钮（多文案兜底）
# note(图文) 评论区在右侧面板，容器为 .comment-mainContent 等；video 在 .route-scroll-container
TRIGGER_JS = '''(function(){
  try{
    let acted=false;
    const clicked=[];
    // 优先寻找真正可滚动的评论容器，不能命中单条评论节点。
    const visible=(el)=>{if(!el)return false;const s=getComputedStyle(el),r=el.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)>0&&r.width>0&&r.height>0;};
    const selectors=['.comment-mainContent','.comment-container','.route-scroll-container','[data-e2e="scroll-container"]','[class*="scroll-container"]','[class*="comment-"]','[class*="comment"]'];
    const sc=selectors.flatMap(sel=>[...document.querySelectorAll(sel)]).find(el=>visible(el)&&Number(el.scrollHeight||0)-Number(el.clientHeight||0)>20);
    if(sc){try{sc.focus?.();sc.scrollTop=sc.scrollHeight;sc.scrollTo?.({top:sc.scrollHeight,behavior:'instant'});acted=true;}catch(e){}}
    else{window.scrollTo(0,document.body.scrollHeight);acted=true;}
    const texts=['点击加载更多','加载更多','展开更多','查看全部','展开','更多评论'];
    const expandRe=/(展开|查看|显示|更多)\s*(?:更多\s*)?\d*\s*(?:条)?\s*(?:回复|评论)/;
    const isVisible=(e)=>{if(!e)return false;const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)>0&&r.width>0&&r.height>0;};
    const shortText=(e)=>{try{return String(e.innerText||e.textContent||'').replace(/\s+/g,' ').trim();}catch(ex){return '';}};
    // 抖音评论楼中楼的真实入口是 button.comment-reply-expand-btn。
    // 旧逻辑按通用“展开”文本取前三个节点，容易命中外层评论行或隐藏副本，
    // 结果看似执行了点击，实际没有触发楼中楼加载。优先精确命中真实按钮，
    // 同时保留其他版本的文案兜底。
    const exact=[...new Set([
      ...document.querySelectorAll('button.comment-reply-expand-btn,[class*="comment-reply-expand-btn"]'),
      ...document.querySelectorAll('button,span,div')
    ])].filter(e=>{
      const t=shortText(e);
      return isVisible(e)&&t.length<30&&!/(收起|隐藏)/.test(t)&&
        (e.matches?.('button.comment-reply-expand-btn,[class*="comment-reply-expand-btn"]') ||
         texts.some(k=>t.includes(k))&&expandRe.test(t));
    });
    for(const b of exact.slice(0,8)){
      try{
        b.scrollIntoView?.({block:'center',inline:'nearest'});
        b.focus?.();
        b.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true,view:window}));
        b.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,cancelable:true,view:window}));
        b.click?.();
        acted=true;
        clicked.push(shortText(b).slice(0,80) || 'reply-expand-button');
      }catch(e){}
    }
    return {acted, clicked:clicked.length, labels:clicked};
  }catch(e){return {acted:false, clicked:0, labels:[]};}
})()'''


OPEN_NOTE_COMMENTS_JS = '''(function(){
  try{
    const visible=(el)=>{if(!el)return false;const s=getComputedStyle(el),r=el.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)>0&&r.width>0&&r.height>0;};
    const norm=(v)=>String(v||'').replace(/\\s+/g,' ').trim();
    const candidates=[...document.querySelectorAll(
      'button,[role="button"],[aria-label*="评论"],[title*="评论"],[data-e2e*="comment"],[class*="comment"],span,div'
    )].filter(visible).map(el=>{
      const text=norm(el.innerText||el.textContent), aria=norm(el.getAttribute('aria-label')),
        title=norm(el.getAttribute('title')), data=norm(el.getAttribute('data-e2e')),
        cls=norm(typeof el.className==='string'?el.className:'');
      const shortText=text.length<=18 && el.children.length<=3;
      let score=0;
      if(/^(评论|评论区)(?:[（(]?\\d+(?:万)?[）)]?)?$/.test(text)&&shortText)score=70;
      if(/评论|comment/i.test(`${aria} ${title} ${data}`))score=Math.max(score,100);
      if(/comment/i.test(cls)&&/(icon|action|tab|button)/i.test(cls))score=Math.max(score,85);
      return {el,text,score};
    }).filter(x=>x.score>0).sort((a,b)=>b.score-a.score);
    const hit=candidates[0];
    if(!hit)return {ok:false,reason:'comment_tab_not_found',candidates:candidates.length};
    hit.el.focus?.();hit.el.click();
    return {ok:true,text:hit.text.slice(0,40),candidates:candidates.length};
  }catch(e){return {ok:false,reason:String(e)};}
})()'''


async def fetch_comments(c, sid, vid_url, quiet=4, max_work=300,
                         pause_event=None, cancel_event=None) -> list:
    """阶段 B：导航作品页，拦 /comment/list/ + reply 全量翻页抓评论。

    quiet: 连续无新评论响应的安静秒数，达到即认为拉完（数据驱动终止）。
    max_work: 单作品最长采集窗口（秒）。
    """
    recv = [
        {"id": "doc", "url": ""},  # placeholder to keep list ref
    ]
    responses = []
    await c.cmd("Network.enable", {}, session_id=sid)
    stop = c.on("Network.responseReceived", lambda p: responses.append((p.get("response", {}).get("url", ""), p.get("requestId", ""))))
    # 导航并等待
    await c.navigate(vid_url, sid)
    await asyncio.sleep(6)
    blk = await probe_blocked(c, sid)
    if blk:
        raise HumanBlock(blk)

    # 打开评论区（note 默认收起）：先让评论控件获得焦点，再点击。
    # note 页评论滚动绑定在右侧面板，不能只滚主文档。
    try:
        if "/note/" in str(vid_url).lower():
            await c.eval(OPEN_NOTE_COMMENTS_JS, sid)
        else:
            await c.eval('''(function(){
              const els=[...document.querySelectorAll('span,div,button')];
              const el=[...els].reverse().find(e=>{const t=(e.textContent||'').trim(); return /评论\\s*\\(?\\d/.test(t)&&t.length<25&&e.children.length<=2;});
              if(el){el.focus?.();el.click(); return el.textContent.trim();}
              return 'not-found';
            })()''', sid)
        await asyncio.sleep(2)
    except Exception:
        pass

    comment_resp = {}   # rid -> url (一级)
    reply_resp = {}     # rid -> url (二级)
    last_seen = _time.time()
    last_trigger = _time.time()
    work_deadline = _time.time() + max_work
    # 楼中楼入口是动态节点。验证码/灰屏/接口失败时，页面可能不断重新
    # 渲染同一批入口；没有熔断就会反复点击，拖到单作品的最大工作时长。
    expand_signature = ""
    expand_stalled_rounds = 0
    expand_click_total = 0
    expand_disabled = False

    while _time.time() < work_deadline:
        while pause_event is not None and not pause_event.is_set():
            if cancel_event is not None and cancel_event.is_set():
                stop()
                return []
            await asyncio.sleep(.2)
        if cancel_event is not None and cancel_event.is_set():
            stop()
            return []
        # 验证可能在滚动/加载评论过程中才出现，不能只在导航后检查一次。
        if _time.time() - last_trigger >= 1.5:
            try:
                blk = await probe_blocked(c, sid)
                if blk:
                    raise HumanBlock(blk)
            except HumanBlock:
                stop()
                raise
        # 每 ~2s 触发一次滚动/加载更多
        if _time.time() - last_trigger > 2:
            last_trigger = _time.time()
            try:
                if not expand_disabled:
                    trigger_result = await c.eval(TRIGGER_JS, sid)
                    if isinstance(trigger_result, dict):
                        labels = [str(v or "")[:80] for v in (trigger_result.get("labels") or [])]
                        clicked = int(trigger_result.get("clicked") or len(labels) or 0)
                        if clicked:
                            signature = "|".join(labels)
                            if signature and signature == expand_signature:
                                expand_stalled_rounds += 1
                            else:
                                expand_stalled_rounds = 0
                            expand_signature = signature
                            expand_click_total += clicked
                            # 3 轮点击结果完全不变，或累计点击达到安全上限，
                            # 认定展开失败；后续仍继续滚动和采集一级评论。
                            if expand_stalled_rounds >= 3 or expand_click_total >= 24:
                                expand_disabled = True
                await c.eval("window.scrollTo(0, document.body.scrollHeight)", sid)
            except Exception:
                pass
        await asyncio.sleep(0.6)
        # 读已收集响应（旁观收集器是异步追加的，直接扫列表）
        n_new = False
        for u, rid in list(responses):
            if "/comment/list/reply/" in u:
                if rid not in reply_resp:
                    reply_resp[rid] = u
                    last_seen = _time.time()
                    n_new = True
            elif "/comment/list/" in u:
                if rid not in comment_resp:
                    comment_resp[rid] = u
                    last_seen = _time.time()
                    n_new = True
        # 数据驱动终止：安静窗口已到
        if _time.time() - last_seen >= quiet:
            break

    # 拉取并解析一级评论
    all_comments = {}
    for rid in comment_resp:
        try:
            body = await c.get_body(rid, sid)
            data = json.loads(body)
            for cm in data.get("comments") or []:
                _absorb_comment(all_comments, cm, parent_id="")
        except Exception:
            continue
    # 拉取并解析二级评论
    for rid in reply_resp:
        try:
            body = await c.get_body(rid, sid)
            data = json.loads(body)
            for cm in data.get("comments") or []:
                _absorb_comment(all_comments, cm, parent_id=str(cm.get("parent_comment_id", "")))
        except Exception:
            continue
    stop()
    return list(all_comments.values())


def _absorb_comment(all_comments: dict, cm: dict, parent_id: str):
    cid = str(cm.get("cid"))
    if cid in all_comments:
        return
    ct = cm.get("create_time")
    try:
        ct_num = int(ct)
    except (TypeError, ValueError):
        ct_num = 0
    user = cm.get("user") or {}
    all_comments[cid] = {
        "cid": cid,
        "text": (cm.get("text") or ""),
        "region": (cm.get("ip_label") or ""),   # IP属地（省份），如 河北/广东
        "create_time": ct,
        "create_time_str": _time.strftime("%Y-%m-%d %H:%M", _time.localtime(ct_num)) if ct_num else "",
        "digg_count": cm.get("digg_count"),
        "user_id": user.get("uid", ""),
        "sec_uid": user.get("sec_uid", ""),
        "nickname": user.get("nickname", ""),
        "homepage": "https://www.douyin.com/user/" + str(user.get("sec_uid", "")),
        "reply_total": cm.get("reply_comment_total", 0),
        "parent_id": parent_id,
    }


# ---------------------------------------------------------------------------
# 意图分析（沿用昨天 intent-rules 三级表）
# ---------------------------------------------------------------------------
HIGH_KW = ["怎么联系", "联系方式", "私信", "多少钱", "多少米", "怎么卖", "怎么买", "哪里买", "哪里", "购买", "入手", "加盟", "代理", "合作", "招商", "报价", "询价", "厂家", "供应商", "哪里能买", "有货", "样品", "对接", "求购", "要买", "想了解", "求推荐", "推荐一下", "多少钱一个", "怎么收费", "怎么购", "卖不卖"]
MEDIUM_KW = ["投放", "怎么用", "怎么操作", "使用", "功能", "格口", "尺寸", "多大", "质保", "售后", "保修", "维修", "对比", "区别", "哪个好", "丰巢", "菜鸟", "驿站", "成本", "回本", "收益", "赚钱", "效率", "省力", "方便", "小区", "物业", "柜子"]
UNWANTED = ["招人", "找工作", "兼职", "招聘"]


def rate(text):
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return "low"
    if any(k in text for k in UNWANTED):
        return "recruit"
    if any(k in text for k in HIGH_KW):
        return "high"
    if any(k in text for k in MEDIUM_KW):
        return "medium"
    return "low"


def write_intent_csv(videos, out_csv):
    """8 列：信息提取时间/地区/用户名/用户主页/发布时间/发布内容/意向评级/整体评论内容分析"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = ["信息提取时间", "地区", "用户名", "用户主页", "发布时间", "发布内容", "意向评级", "整体评论内容分析"]
    rows = []
    for v in videos:
        pub = ""
        ct = v.get("create_time")
        if ct:
            try:
                pub = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(int(ct)))
            except Exception:
                pub = ""
        content = (v.get("desc") or "").strip()[:80]
        for cm in v.get("comments", []):
            user = (cm.get("nickname") or "").strip()
            text = (cm.get("text") or "").strip()
            if not user or not text:
                continue
            rows.append([now, cm.get("region", ""), user, cm.get("homepage", ""), pub, content, rate(text), text])
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    rating = {}
    region_cnt = {}
    for r in rows:
        rating[r[6]] = rating.get(r[6], 0) + 1
        region_cnt[r[1]] = region_cnt.get(r[1], 0) + 1
    print(f"✅ CSV: {out_csv}")
    print(f"   总评论 {len(rows)} | 意向分布: {rating}")
    print(f"   地区分布: {region_cnt}")
    return rows


# ---------------------------------------------------------------------------
async def run(ws_url, keyword, limit, cooldown, quiet, max_work):
    base = os.path.join(OUT_DIR, f"抖音-{keyword}")
    os.makedirs(base, exist_ok=True)
    videos_path = os.path.join(base, "videos.json")
    comments_path = os.path.join(base, "comments.json")

    videos = json.load(open(videos_path, encoding="utf-8")) if os.path.exists(videos_path) else []
    comments_all = json.load(open(comments_path, encoding="utf-8")) if os.path.exists(comments_path) else []
    done_vids = {v["vid"] for v in videos}

    c = CdpSession(ws_url)
    await c.connect()
    sid = await c.attach_page()
    try:
        all_videos = await search_videos(c, sid, keyword)
        print(f"[阶段A] 搜索到 {len(all_videos)} 条（图文+视频）| 视频={sum(1 for v in all_videos if v['kind']=='video')} 图文={sum(1 for v in all_videos if v['kind']=='note')} | 待采 {sum(1 for v in all_videos if v['vid'] not in done_vids)} 条")
        targets = [v for v in all_videos if v["vid"] not in done_vids][:limit] if limit else [v for v in all_videos if v["vid"] not in done_vids]

        ok = 0
        for i, v in enumerate(targets):
            try:
                if not v["url"]:
                    continue
                v["collected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                v["comments"] = await fetch_comments(c, sid, v["url"], quiet=quiet, max_work=max_work)
                videos.append(v)
                json.dump(videos, open(videos_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                for cm in v["comments"]:
                    rec = dict(cm)
                    rec["vid"] = v["vid"]
                    rec["video_desc"] = v["desc"][:80]
                    comments_all.append(rec)
                json.dump(comments_all, open(comments_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                total_c = sum(len(vv.get("comments", [])) for vv in videos)
                print(f"  [{i+1}/{len(targets)}] ✅ {v['nickname'][:10]} | 💬{len(v['comments'])} | 累计{total_c} | [{v['kind']}] {v['desc'][:18]}")
                ok += 1
                if cooldown > 0:
                    await asyncio.sleep(cooldown)
            except HumanBlock as hb:
                raise
            except Exception as e:
                print(f"  [{i+1}/{len(targets)}] ⚠️ {v['vid'][-8:]} 异常 {repr(e)[:50]} | url={v['url']}")
                await asyncio.sleep(2)
        print(f"[完成] {len(videos)} 作品 / 累计 {sum(len(v.get('comments', [])) for v in videos)} 条可加载评论")
        if videos:
            write_intent_csv(videos, os.path.join(base, "dy_意向客户.csv"))
    finally:
        await c.close()
    return videos


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", help="浏览器 CDP WebSocket；省略时读取 data/dy_window.txt")
    ap.add_argument("--keyword", default="快递柜")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--cooldown", type=float, default=2.5)
    ap.add_argument("--quiet", type=float, default=4.0)
    ap.add_argument("--max-work", type=float, default=45.0)
    args = ap.parse_args()
    if not args.ws:
        ws_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "dy_window.txt")
        try:
            args.ws = [line.strip() for line in open(ws_path, encoding="utf-8") if line.strip()][-1]
        except (OSError, IndexError):
            ap.error(f"缺少 --ws，且窗口文件不可用：{ws_path}")
    try:
        asyncio.run(run(args.ws, args.keyword, args.limit, args.cooldown, args.quiet, args.max_work))
    except HumanBlock as hb:
        print(f"\n🚨 P7 冻结: {hb}（已停止操作，等待用户处理）")
        sys.exit(3)
