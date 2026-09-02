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
import sys
import urllib.parse

try:
    from .search_sort_dom import click_sort_option
except ImportError:
    from search_sort_dom import click_sort_option
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.cdp import CdpSession
try:
    from .dy_collect import SearchVideosResult
except ImportError:
    from dy_collect import SearchVideosResult

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")


class HumanBlock(Exception):
    pass


async def probe_blocked(c, sid):
    return await c.eval('''(function(){
      const bodyText = document.body ? document.body.innerText : '';
      const isVisible = (el) => {
        const s = getComputedStyle(el); const r = el.getBoundingClientRect();
        return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0;
      };
      const candidates = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"],[class*="captcha"],[class*="Captcha"],[class*="verify"],[class*="Verify"],[class*="security"],[class*="Security"],iframe')].filter(isVisible);
      const t = candidates.map(e => (e.innerText || '').trim()).filter(Boolean).join('\\n');
      if (location.href.startsWith('chrome-extension:')) return 'bitbrowser拦截';
      if (/请求太频繁|操作频繁|一分钟后再试|访问频繁/.test(bodyText)) return 'rate_limited';
      if (/登录后查看|扫码登录/.test(t)) return '登录弹窗';
      if (/滑块|拖动验证|滑动验证|安全验证|请完成验证|验证码|人机验证/.test(t)) return '验证码';
      return null;
    })()''', sid)


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
        await asyncio.sleep(3 + attempt * 2)
        # 在任何 reload 前先确认页面没有真实的人机验证；否则 reload 会把验证页覆盖掉，
        # 造成“没有看到验证却被判定需人工”的错觉。
        blk = await probe_blocked(c, sid)
        if blk:
            raise HumanBlock(blk)
        # 连续导航会污染 __INITIAL_STATE__，整页刷新拿到干净状态。
        try:
            await c.eval("location.reload()", sid)
        except Exception:
            pass
        await asyncio.sleep(5 + attempt * 2)
        blk = await probe_blocked(c, sid)
        if blk:
            raise HumanBlock(blk)
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
            await asyncio.sleep(1.0)
        if ready:
            break
    if not ready:
        raise RuntimeError("小红书搜索结果加载失败：页面未返回有效笔记数据，请检查登录状态或稍后重试")
    blk = await probe_blocked(c, sid)
    if blk:
        raise HumanBlock(blk)
    sort_labels = {"latest": ["最新"], "hot": ["最热", "热门"]}.get(str(search_sort), [])
    if sort_labels:
        await click_sort_option(c, sid, sort_labels)
        await asyncio.sleep(1.0)
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
    reached_target = False
    no_more_results = False
    rounds = 0
    # 目标越大允许的滚动轮次越多；安全上限只用于异常页面兜底，不能作为
    # 正常终止条件。正常结束必须是达到目标或确认页面没有更多结果。
    max_rounds = max(80, min(600, target // 8 + 80))
    max_load_wait = 10.0
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
            # 连续多轮停在底部且没有新笔记，或页面明确显示结束，才认定无更多。
            if (state.get("endText") and stale_rounds >= 2) or (bottom_rounds >= 12 and stale_rounds >= 12):
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
            await asyncio.sleep(0.5)
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
        blk = await probe_blocked(c, sid)
        if blk:
            raise HumanBlock(blk)
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


async def fetch_note(c, sid, note):
    nid = note["id"]
    token = note.get("xsec_token", "")
    url = f"https://www.xiaohongshu.com/explore/{nid}?xsec_token={urllib.parse.quote(token)}&xsec_source=pc_search"
    await c.navigate(url, sid)
    await asyncio.sleep(4)
    blk = await probe_blocked(c, sid)
    if blk:
        raise HumanBlock(blk)
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
          + '.comment-item a,.comment-item span,.comment-item div')
      ];
      for (const el of candidates) {
          const label = clean(el.getAttribute('aria-label') || el.getAttribute('title')
            || el.innerText || el.textContent);
          if (!isExpandLabel(label)) continue;
          const control = el.closest('button,[role="button"],a') || el;
          if (visible(control)) controls.add(control);
      }
      const clicked = [];
      for (const control of controls) {
        const label = clean(control.innerText || control.textContent).slice(0, 80);
        control.focus?.();
        control.click();
        clicked.push(label);
      }
      return {expandedCount: clicked.length, labels: clicked.slice(0, 20)};
    })()'''
    idle_rounds = 0
    previous_count = 0
    for _ in range(80):
        await c.eval(expand_replies_js, sid)
        state = await c.eval('''(() => {
          const text = document.body?.innerText || '';
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
        await asyncio.sleep(0.7)
        await c.eval(expand_replies_js, sid)
    comments = await c.eval('''(function(){
      const out = []; const seen = new Set();
      const body = document.body ? document.body.innerText : '';
      if (body.includes('这是一片荒地') || body.includes('暂无评论')) return {desert:true, comments:[]};
      // 每条评论即一个 .comment-item（含头像、昵称、文本、日期-地区、赞、回复）
      const nodes = [...document.querySelectorAll('.comment-item')];
      for (const n of nodes) {
        let user = '', user_id = '';
        // 评论者主页 userId
        const uidEl = n.querySelector('a[data-user-id]');
        if (uidEl) { user_id = uidEl.getAttribute('data-user-id') || ''; user = (uidEl.innerText||'').trim(); }
        if (!user) {
          // 兜底：第一个非空文本块视为昵称（部分评论头像链接无文字）
          const nameEl = n.querySelector('.name, .user-name, span');
          user = nameEl ? (nameEl.innerText||'').trim() : '';
        }
        const lines = (n.innerText||'').split(String.fromCharCode(10)).filter(Boolean);
        // 小红书会把“作者”身份徽标放在评论行内部；如果真实正文为空、
        // 图片或表情，直接读取整行文本会把这个徽标误当成评论正文。
        // 先读取正文节点，回退到整行时也必须排除全部界面元数据。
        const contentEl = n.querySelector(
          '.comment-mainContent, .comment-content, .comment-text, '
          + '[class*="commentContent"], [class*="comment-content"]'
        );
        const contentLines = contentEl
          ? (contentEl.innerText||'').split(String.fromCharCode(10)).filter(Boolean)
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
        const dateText = (n.querySelector('.date')?.innerText || '').trim();
        const full = dateText || (n.innerText || '');
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
                try:
                    note = await fetch_note(c, sid, f)
                    if note.get("err"):
                        continue
                    note["keyword"] = kw
                    notes.append(note)
                    done_ids.add(f["id"])
                    json.dump(notes, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                    cur_c = sum(len(n2["comments"]) for n2 in notes)
                    print(f"  ✅ {len(notes)}篇 | {note['title'][:16]} | 💬{len(note['comments'])} | 累计评论{cur_c}")
                    if cooldown > 0:
                        await asyncio.sleep(cooldown)
                except HumanBlock as hb:
                    raise
                except Exception as e:
                    print(f"  ⚠️ {f['title'][:16]} 异常 {repr(e)[:60]}")
                    await asyncio.sleep(2)
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
