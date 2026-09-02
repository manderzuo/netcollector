# -*- coding: utf-8 -*-
"""douyin.py — 抖音平台采集器（新架构）。

迁移自旧 dy_collect 的核心采集逻辑，采用统一类型系统：
- VideoItem / CommentItem / SearchResult（见 base.py）
- 浏览器控制层通过注入的 CdpClient 驱动，与平台逻辑解耦。

采集策略（保留原经验）：
- 阶段 A：真实浏览器搜索页滚动，旁观拦截 /aweme/v1/web/ 响应提取作品
- 阶段 B：作品页滚动 + 点加载更多，旁观拦截 /comment/list/(reply/) 提取评论
- 风控：probe_blocked / probe_scroll_locked 全程检测，异常抛 HumanBlock
"""

from __future__ import annotations

import asyncio
import json
import time
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

# ---------------------------------------------------------------------------
# 调参表（原魔法数集中管理）
# ---------------------------------------------------------------------------
TUNING = {
    "fast": {"scroll_limit": 8, "default_target": 100, "interval": 2.0},
    "standard": {"scroll_limit": 50, "default_target": 500, "interval": 2.5},
    "deep": {"scroll_limit": 1000, "default_target": 10000, "interval": 3.0},
}

# ---------------------------------------------------------------------------
# 风控探测（迁移自原 probe_blocked / probe_scroll_locked）
# ---------------------------------------------------------------------------
_PROBE_JS = """(function(){
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
  const overlayText = t.replace(/\\s+/g, ' ').trim();
  const hasChallengeText = /滑块|拖动验证|滑动验证|安全验证|请完成验证|请完成安全验证|验证后继续|验证码|人机验证/.test(overlayText);
  const hasRateLimitText = /请求太频繁|操作频繁|一分钟后再试|访问频繁|请求过于频繁|稍后再试/.test(overlayText);
  const visibleChallengeFrame = [...document.querySelectorAll('iframe')].some(frame => {
    if (!isVisible(frame)) return false;
    const src = (frame.src || '').toLowerCase();
    return /(captcha|verify|challenge|secsdk|security)/i.test(src);
  });
  if (hasRateLimitText) return 'rate_limited';
  if (hasChallengeText) return 'captcha';
  if (visibleChallengeFrame) return 'captcha';
  if (t.includes('扫码登录') || (t.includes('请使用抖音扫码') && t.length < 300)) return 'login_qrcode';
  if (t.includes('登录后查看') || t.includes('请先登录')) return 'need_login';
  return null;
})()"""

# 触发 JS：滚动评论区 + 点加载更多
_TRIGGER_JS = """(function(){
  try{
    let acted=false;
    const visible=(el)=>{if(!el)return false;const s=getComputedStyle(el),r=el.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)>0&&r.width>0&&r.height>0;};
    const selectors=['.comment-mainContent','.comment-container','.route-scroll-container','[data-e2e="scroll-container"]','[class*="scroll-container"]','[class*="comment-"]','[class*="comment"]'];
    const sc=selectors.flatMap(sel=>[...document.querySelectorAll(sel)]).find(el=>visible(el)&&Number(el.scrollHeight||0)-Number(el.clientHeight||0)>20);
    if(sc){try{sc.focus?.();sc.scrollTop=sc.scrollHeight;sc.scrollTo?.({top:sc.scrollHeight,behavior:'instant'});acted=true;}catch(e){}}
    else{window.scrollTo(0,document.body.scrollHeight);acted=true;}
    const texts=['点击加载更多','加载更多','展开更多','查看全部','展开','更多评论'];
    const btns=[...document.querySelectorAll('button,span,div')].filter(e=>{let t='';try{t=(e.textContent||'').trim();}catch(ex){};return texts.some(k=>t.includes(k))&&t.length<30;});
    for(const b of btns.slice(0,3)){try{if(b&&b.click){b.click();acted=true;}}catch(e){}}
    return acted;
  }catch(e){return false;}
})()"""

# note(图文) 打开评论 Tab 的定位脚本
_OPEN_NOTE_COMMENTS_JS = """(function(){
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
})()"""


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _kind_of(aweme_info: dict) -> str:
    """判定作品类型：有 images 或 aweme_type==68 → note，否则 video。"""
    imgs = aweme_info.get("images") or []
    if isinstance(imgs, list) and len([x for x in imgs if x]) > 0:
        return "note"
    if str(aweme_info.get("aweme_type")) == "68":
        return "note"
    return "video"


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
            int(ch, 16)
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
# 抖音采集器
# ---------------------------------------------------------------------------
class DouyinAdapter(PlatformAdapter):
    """抖音平台采集器。"""

    platform = "douyin"

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
        import urllib.parse

        c, sid = await self._cdp.connect_window(window_id)
        kw = urllib.parse.quote(keyword)
        url = f"https://www.douyin.com/search/{kw}?type=general"
        responses = []
        c.on("Network.responseReceived", lambda p: responses.append(
            (p.get("response", {}).get("url", ""), p.get("requestId", ""))))
        await c.cmd("Network.enable", {}, session_id=sid)

        block = await self._probe_blocked(c, sid)
        if block:
            raise HumanBlock(block)

        sort_query = {"most_like": "1", "latest": "2"}.get(str(search_sort), "0")
        await c.navigate(f"{url}&sort_type={sort_query}", sid)
        await asyncio.sleep(8)

        cfg = TUNING.get(mode, TUNING["standard"])
        scroll_limit = cfg["scroll_limit"]
        default_target = cfg["default_target"]
        interval = cfg["interval"]
        target_count = int(target_count or default_target)
        max_rounds = max(scroll_limit, min(600, target_count // 8 + 80))
        min_no_more_rounds = 12
        max_load_wait = 10.0

        all_items = {}
        parsed_ids = set()
        last_candidate = 0
        last_height = 0
        last_top = -1
        bottom_stale = 0
        stale = 0
        reached = False
        no_more = False
        reason = ""
        rounds = 0

        async def visible_count():
            try:
                return int(await c.eval(
                    """(() => {
                      const ids = new Set();
                      for (const a of document.querySelectorAll('a[href*="/video/"],a[href*="/note/"]')) {
                        const m = (a.href || '').match(/\\/(?:video|note)\\/([A-Za-z0-9_-]+)/);
                        if (m) ids.add(m[1]);
                      }
                      return ids.size;
                    })()""", sid) or 0)
            except Exception:
                return 0

        async def absorb():
            for u, rid in list(responses):
                if rid in parsed_ids:
                    continue
                try:
                    body = await c.get_body(rid, sid)
                except Exception:
                    continue
                try:
                    if "general/search/stream" in u:
                        for ai in parse_stream_ndjson(body):
                            self._absorb_item(all_items, ai)
                    else:
                        data = json.loads(body)
                        for d in data.get("data") or []:
                            if isinstance(d, dict):
                                ai = d.get("aweme_info") or d
                                if ai.get("aweme_id"):
                                    self._absorb_item(all_items, ai)
                    parsed_ids.add(rid)
                except Exception:
                    continue

        for _ in range(max_rounds):
            rounds += 1
            if pause_event is not None and not pause_event.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    break
                await asyncio.sleep(0.2)
            if cancel_event is not None and cancel_event.is_set():
                break
            before_visible = await visible_count()
            await c.eval("window.scrollBy(0, 900)", sid)
            before_items = len(all_items)
            wait_start = asyncio.get_running_loop().time()
            while True:
                await asyncio.sleep(min(0.5, max(0.1, interval / 2)))
                await absorb()
                cur_visible = await visible_count()
                if len(all_items) > before_items or cur_visible > before_visible:
                    break
                if asyncio.get_running_loop().time() - wait_start >= max_load_wait:
                    break
            block = await self._probe_blocked(c, sid)
            if block:
                raise HumanBlock(block)
            await absorb()
            try:
                state = await c.eval(
                    """(function(){
                      const sc = document.scrollingElement || document.documentElement;
                      const text = (document.body ? document.body.innerText : '') || '';
                      const links = [...document.querySelectorAll('a[href*="/video/"],a[href*="/note/"]')];
                      const ids = new Set();
                      for (const a of links) {
                        const m = (a.href || '').match(/\\/(?:video|note)\\/([A-Za-z0-9_-]+)/);
                        if (m) ids.add(m[1]);
                      }
                      const top = Number(sc.scrollTop || window.scrollY || 0);
                      const height = Number(sc.scrollHeight || document.body?.scrollHeight || 0);
                      const viewport = Number(sc.clientHeight || window.innerHeight || 0);
                      return {
                        visible: ids.size, top, height, viewport,
                        atBottom: top + viewport >= height - 24,
                        endText: /暂时没有更多了|没有更多|暂无更多|到底了|已显示全部|没有找到更多/.test(text),
                        endMessage: (text.match(/暂时没有更多了|没有更多|暂无更多|到底了|已显示全部|没有找到更多/) || [])[0] || ''
                      };
                    })()""", sid) or {}
            except Exception:
                state = {}
            visible = int(state.get("visible") or 0)
            candidate = max(visible, len(all_items))
            if candidate >= target_count:
                reached = True
                reason = "reached_target"
                break
            progress = candidate > last_candidate
            stale = 0 if progress else stale + 1
            at_bottom = bool(state.get("atBottom"))
            height = int(state.get("height") or 0)
            top = int(state.get("top") or 0)
            if at_bottom and not progress:
                bottom_stale += 1
            else:
                bottom_stale = 0
            if bool(state.get("endText")) and stale >= 1:
                no_more = True
                reason = f"page_end:{state.get('endMessage')}"
                break
            if bottom_stale >= min_no_more_rounds:
                no_more = True
                reason = "bottom_stale"
                break
            last_candidate = max(last_candidate, candidate)
            last_height = height
            last_top = top

        final_block = await self._probe_blocked(c, sid)
        if final_block:
            raise HumanBlock(final_block)

        # 归一化
        items = []
        for aid, ai in all_items.items():
            author = ai.get("author") or {}
            kind = _kind_of(ai)
            items.append(VideoItem(
                vid=str(aid),
                url=f"https://www.douyin.com/{kind}/{aid}",
                kind=kind,
                title=(ai.get("desc") or "")[:300],
                author=author.get("nickname", ""),
                author_id=author.get("sec_uid", ""),
                create_time=ai.get("create_time"),
                comment_count=(ai.get("statistics") or {}).get("comment_count", ""),
                keyword=keyword,
            ))
        # DOM 兜底
        if not items:
            try:
                dom = await c.eval(
                    """(function(){
                      const out=[]; const seen=new Set();
                      for (const a of document.querySelectorAll('a[href*="/video/"],a[href*="/note/"]')) {
                        const href=a.href||''; const m=href.match(/\\/(video|note)\\/([A-Za-z0-9_-]+)/);
                        if(!m || seen.has(m[2])) continue;
                        seen.add(m[2]);
                        out.push({vid:m[2], kind:m[1], url:href,
                                  desc:(a.innerText||a.textContent||'').trim().slice(0,300)});
                      }
                      return out.slice(0,100);
                    })()""", sid) or []
                for d in dom:
                    items.append(VideoItem(
                        vid=str(d["vid"]), url=d.get("url", ""),
                        kind=d.get("kind", "video"), title=d.get("desc", ""),
                        keyword=keyword,
                    ))
            except Exception:
                pass

        return SearchResult(
            items=items[:target_count],
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
        quiet: int = 4,
        max_work: int = 300,
    ) -> List[CommentItem]:
        c, sid = await self._cdp.connect_window(window_id)
        responses = []
        await c.cmd("Network.enable", {}, session_id=sid)
        stop = c.on("Network.responseReceived", lambda p: responses.append(
            (p.get("response", {}).get("url", ""), p.get("requestId", ""))))

        await c.navigate(url, sid)
        await asyncio.sleep(6)
        block = await self._probe_blocked(c, sid)
        if block:
            raise HumanBlock(block)

        try:
            if "/note/" in str(url).lower():
                await c.eval(_OPEN_NOTE_COMMENTS_JS, sid)
            else:
                await c.eval("""(function(){
                  const els=[...document.querySelectorAll('span,div,button')];
                  const el=[...els].reverse().find(e=>{const t=(e.textContent||'').trim(); return /评论\\s*\\(?\\d/.test(t)&&t.length<25&&e.children.length<=2;});
                  if(el){el.focus?.();el.click(); return el.textContent.trim();}
                  return 'not-found';
                })()""", sid)
            await asyncio.sleep(2)
        except Exception:
            pass

        comment_resp = {}
        reply_resp = {}
        last_seen = time.time()
        last_trigger = time.time()
        deadline = time.time() + max_work

        while time.time() < deadline:
            if pause_event is not None and not pause_event.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    stop()
                    return []
                await asyncio.sleep(0.2)
            if cancel_event is not None and cancel_event.is_set():
                stop()
                return []
            if time.time() - last_trigger >= 1.5:
                try:
                    block = await self._probe_blocked(c, sid)
                    if block:
                        raise HumanBlock(block)
                except HumanBlock:
                    stop()
                    raise
            if time.time() - last_trigger > 2:
                last_trigger = time.time()
                try:
                    await c.eval(_TRIGGER_JS, sid)
                    await c.eval("window.scrollTo(0, document.body.scrollHeight)", sid)
                except Exception:
                    pass
            await asyncio.sleep(0.6)
            new_found = False
            for u, rid in list(responses):
                if "/comment/list/reply/" in u:
                    if rid not in reply_resp:
                        reply_resp[rid] = u
                        last_seen = time.time()
                        new_found = True
                elif "/comment/list/" in u:
                    if rid not in comment_resp:
                        comment_resp[rid] = u
                        last_seen = time.time()
                        new_found = True
            if time.time() - last_seen >= quiet:
                break

        all_comments = {}
        for rid in comment_resp:
            try:
                body = await c.get_body(rid, sid)
                data = json.loads(body)
                for cm in data.get("comments") or []:
                    self._absorb_comment(all_comments, cm, parent_id="")
            except Exception:
                continue
        for rid in reply_resp:
            try:
                body = await c.get_body(rid, sid)
                data = json.loads(body)
                for cm in data.get("comments") or []:
                    self._absorb_comment(all_comments, cm,
                                         parent_id=str(cm.get("parent_comment_id", "")))
            except Exception:
                continue
        stop()
        return list(all_comments.values())

    # ---- 同步包装（调度器调用） ----
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
    def _absorb_item(all_items: dict, ai: dict):
        aid = str(ai["aweme_id"])
        if aid not in all_items:
            all_items[aid] = ai

    @staticmethod
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
        all_comments[cid] = CommentItem(
            cid=cid,
            content=cm.get("text") or "",
            region=cm.get("ip_label") or "",
            comment_time=ct,
            user_id=user.get("uid", ""),
            nickname=user.get("nickname", ""),
            homepage="https://www.douyin.com/user/" + str(user.get("sec_uid", "")),
            parent_id=parent_id,
            extra={
                "create_time_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(ct_num)) if ct_num else "",
                "digg_count": cm.get("digg_count"),
                "reply_total": cm.get("reply_comment_total", 0),
            },
        )

    @staticmethod
    async def _probe_blocked(c, sid):
        """检测风控状态，返回原因或 None。"""
        reason = await c.eval(_PROBE_JS, sid)
        if reason:
            return reason
        # 滚动锁定探测（简化：检查灰屏遮罩）
        try:
            state = await c.eval(
                """(function(){
                  const vw = Math.max(document.documentElement.clientWidth || 0, window.innerWidth || 0);
                  const vh = Math.max(document.documentElement.clientHeight || 0, window.innerHeight || 0);
                  if (vw < 100 || vh < 100) return null;
                  const hintRe = /captcha|verify|challenge|secsdk|security|滑块|验证/i;
                  const points = [[vw*.5,vh*.5],[vw*.35,vh*.5],[vw*.65,vh*.5],[vw*.5,vh*.35],[vw*.5,vh*.65]];
                  const alphaOf = (color) => {
                    const m = String(color || '').match(/rgba?\\([^,]+,[^,]+,[^,]+(?:,\\s*([\\d.]+))?\\)/i);
                    return m ? (m[1] === undefined ? 1 : Number(m[1])) : 0;
                  };
                  let blocker = null;
                  for (const [x, y] of points) {
                    let el = document.elementFromPoint(x, y);
                    while (el && el !== document.body && el !== document.documentElement) {
                      const s = getComputedStyle(el);
                      const r = el.getBoundingClientRect();
                      const covers = r.width >= vw*.68 && r.height >= vh*.68;
                      const intercepts = s.pointerEvents !== 'none' && s.visibility !== 'hidden' &&
                        s.display !== 'none' && Number(s.opacity||1) > 0;
                      const frame = el.tagName === 'IFRAME' || !!el.querySelector('iframe');
                      const identity = `${el.id||''} ${typeof el.className==='string'?el.className:''} ` +
                        `${frame ? [...el.querySelectorAll('iframe')].map(f=>f.src||'').join(' ') : ''}`;
                      const dimmed = alphaOf(s.backgroundColor) >= .08 ||
                        (s.backdropFilter && s.backdropFilter !== 'none') ||
                        (s.webkitBackdropFilter && s.webkitBackdropFilter !== 'none');
                      const positioned = s.position === 'fixed' || s.position === 'absolute';
                      if (covers && intercepts && (positioned || frame) && (dimmed || frame || hintRe.test(identity))) {
                        blocker = el; break;
                      }
                      el = el.parentElement;
                    }
                    if (blocker) break;
                  }
                  return blocker ? 'scroll_locked' : null;
                })()""", sid)
            if state == "scroll_locked":
                return "captcha_gray_screen"
        except Exception:
            pass
        return None
