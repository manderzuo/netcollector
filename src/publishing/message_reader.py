# -*- coding: utf-8 -*-
"""四个平台账号级消息中心的只读全量读取适配器。

消息中心不是“已发布作品评论缓存”。每个账号都要从平台自己的消息入口
读取可见的全部消息类别：评论/回复、点赞、@、关注、私信、群通知和系统
通知。这里仅导航和读取 DOM，不点击回复、发送、点赞或删除等有副作用的
控件；回复能力由 ``can_reply`` 明确标记给上层使用。

页面结构会随平台版本变化，因此适配器采用两层策略：优先使用已经验证过
的稳定选择器，选择器失效时再使用可见消息卡片的通用解析器。原始动作、
引用内容、来源链接和读取批次都会保存在 ``extra``，便于日志排查和后续
平台适配升级。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit


MESSAGE_ENTRY_URLS = {
    "douyin": "https://www.douyin.com/user/self?from_nav=1",
    "xhs": "https://www.xiaohongshu.com/notification",
    "bilibili": "https://message.bilibili.com/#/reply",
    "weibo": "https://weibo.com/comment/inbox",
}

MESSAGE_TYPE_LABELS = {
    "comment": "评论",
    "reply": "回复",
    "like": "点赞",
    "mention": "@提及",
    "follow": "关注",
    "private": "私信",
    "group": "群通知",
    "system": "系统通知",
    "other": "其他",
}

# 每个平台的路由是消息类别的读取计划。相同 URL 的路由（抖音）会在页面
# 内通过真实坐标点击右上角“消息”入口，避免 SPA 的普通 element.click 被吞掉。
MESSAGE_ROUTES = {
    "douyin": (
        {"category": "interaction", "url": MESSAGE_ENTRY_URLS["douyin"], "label": "消息"},
        {"category": "private", "url": MESSAGE_ENTRY_URLS["douyin"], "label": "私信"},
    ),
    "xhs": (
        {"category": "comment", "url": MESSAGE_ENTRY_URLS["xhs"], "label": "评论和@"},
        {"category": "like", "url": MESSAGE_ENTRY_URLS["xhs"], "label": "赞和收藏"},
        {"category": "follow", "url": MESSAGE_ENTRY_URLS["xhs"], "label": "新增关注"},
        {"category": "private", "url": "https://www.xiaohongshu.com/chat?channel_id=&channel_type=web_engagement_notification_page", "label": "私信"},
    ),
    "bilibili": (
        {"category": "reply", "url": "https://message.bilibili.com/#/reply", "label": "回复我的"},
        {"category": "mention", "url": "https://message.bilibili.com/#/at", "label": "@我的"},
        {"category": "like", "url": "https://message.bilibili.com/#/love", "label": "收到的赞"},
        {"category": "system", "url": "https://message.bilibili.com/#/system", "label": "系统通知"},
        {"category": "private", "url": "https://message.bilibili.com/#/whisper", "label": "我的消息"},
    ),
    "weibo": (
        {"category": "mention", "url": "https://weibo.com/at/weibo", "label": "@我的"},
        {"category": "comment", "url": "https://weibo.com/comment/inbox", "label": "评论"},
        {"category": "like", "url": "https://weibo.com/like/inbox", "label": "赞"},
        {"category": "private", "url": "https://api.weibo.com/chat", "label": "私信"},
        {"category": "group", "url": "https://api.weibo.com/chat/#/chat?to_uid=-101", "label": "群通知"},
    ),
}


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_message_user_id(platform: str, value: Any) -> str:
    """把页面链接形式的用户字段还原成真正的用户 ID。

    小红书通知页的用户头像链接经常被直接写进 ``user_id``，其中会带
    ``/user/profile/``、查询参数，甚至包含一次性的 ``xsec_token``。消息
    中心只需要稳定的用户 ID，不能把整条链接展示或持久化。
    """
    text = _clean_text(value)
    if not text:
        return ""
    if str(platform or "").strip().lower() != "xhs":
        return text
    try:
        parsed = urlsplit(text)
    except ValueError:
        parsed = None
    path = parsed.path if parsed is not None else text
    match = re.search(r"/user/profile/([^/?#]+)", path or text, flags=re.IGNORECASE)
    if match:
        return _clean_text(match.group(1))
    # 兼容已经是相对路径但没有开头斜杠的旧页面结果。
    match = re.search(r"(?:^|user/profile/)([^/?#]+)", text, flags=re.IGNORECASE)
    return _clean_text(match.group(1)) if match else text


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "可回复", "回复"}


def _message_type(raw: Mapping[str, Any]) -> str:
    explicit = _clean_text(raw.get("message_type") or "").lower()
    aliases = {
        "互动": "comment", "评论和@": "comment", "comment_and_mention": "comment",
        "评论": "comment", "回复": "reply", "reply_to_comment": "reply",
        "点赞": "like", "赞": "like", "赞和收藏": "like", "收藏": "like",
        "@": "mention", "@我的": "mention", "提及": "mention",
        "关注": "follow", "新增关注": "follow", "私信": "private",
        "群通知": "group", "系统通知": "system",
    }
    explicit = aliases.get(explicit, explicit)
    if explicit in MESSAGE_TYPE_LABELS:
        return explicit
    action = _clean_text(raw.get("action") or raw.get("message") or "")
    if "回复" in action or "评论" in action:
        return "reply" if "回复" in action else "comment"
    if "赞" in action or "点赞" in action or "收藏" in action:
        return "like"
    if "@" in action or "提及" in action:
        return "mention"
    if "关注" in action:
        return "follow"
    value = _clean_text(raw.get("category") or "").lower()
    value = aliases.get(value, value)
    if value in MESSAGE_TYPE_LABELS:
        return value
    return "other"


def _stable_message_id(account_id: int, platform: str, raw: Mapping[str, Any], kind: str) -> str:
    explicit = _clean_text(raw.get("message_id") or raw.get("id"))
    if explicit:
        return f"{kind}:{explicit}"
    stable = "|".join([
        str(int(account_id or 0)), platform, kind,
        _clean_text(raw.get("user_id") or raw.get("nickname")),
        _clean_text(raw.get("content") or raw.get("message")),
        _clean_text(raw.get("quote_content") or raw.get("reference")),
        _clean_text(raw.get("event_time") or raw.get("created_at") or raw.get("time")),
        _clean_text(raw.get("source_url") or raw.get("message_url")),
    ])
    return "auto-" + hashlib.sha1(stable.encode("utf-8", errors="replace")).hexdigest()


def normalize_messages(platform: str, account_id: int, raw_items: Any,
                       *, limit: int = 500, category: str = "") -> list[dict[str, Any]]:
    """统一四个平台的消息结构，并按账号/平台/消息特征稳定去重。"""
    platform = _clean_text(platform).lower()
    if platform not in MESSAGE_ROUTES or not isinstance(raw_items, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        kind = _message_type(raw)
        message_id = _stable_message_id(account_id, platform, raw, kind)
        if message_id in seen:
            continue
        seen.add(message_id)
        event_time = _clean_text(raw.get("event_time") or raw.get("created_at") or raw.get("time"))
        content = _clean_text(raw.get("content") or raw.get("message") or raw.get("text"))
        quote = _clean_text(raw.get("quote_content") or raw.get("reference") or raw.get("quote"))
        source_url = _clean_text(raw.get("source_url") or raw.get("content_url"))
        message_url = _clean_text(raw.get("message_url")) or source_url
        extra = raw.get("extra")
        extra = dict(extra) if isinstance(extra, Mapping) else {}
        extra.update({
            "category": _clean_text(category or raw.get("category") or kind),
            "action": _clean_text(raw.get("action") or ""),
            "event_time": event_time,
            "quote_content": quote,
            "source_title": _clean_text(raw.get("source_title") or ""),
            "fetched_at": _now(),
        })
        result.append({
            "message_id": message_id,
            "message_type": kind,
            "message_type_label": MESSAGE_TYPE_LABELS[kind],
            "user_id": _normalize_message_user_id(
                platform, raw.get("user_id") or raw.get("uid")
            ),
            "nickname": _clean_text(raw.get("nickname") or raw.get("user_name") or "匿名用户"),
            "content": content,
            # 时间原文（如“昨天”“4天前”）写入 extra；created_at 用于排序，
            # 页面没有时间时才回退到抓取时间，数据库不会写入空值。
            "created_at": event_time or _now(),
            "content_id": _clean_text(raw.get("content_id") or raw.get("source_id")),
            "source_url": source_url,
            "source_title": _clean_text(raw.get("source_title") or ""),
            "message_url": message_url,
            "can_reply": _bool(raw.get("can_reply")) or kind in {"comment", "reply", "mention", "private"},
            "reply_target_id": _clean_text(raw.get("reply_target_id") or raw.get("target_id")),
            "extra": extra,
        })
        if len(result) >= max(1, min(5000, int(limit or 500))):
            break
    return result


def _message_reader_js(platform: str, category: str) -> str:
    """返回页面内只读解析脚本；脚本不调用 click/输入/发送。"""
    payload = json.dumps({"platform": platform, "category": category}, ensure_ascii=False)
    return r"""(() => {
      const cfg = %s;
      const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
      const visible = el => {
        if (!el) return false;
        const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 2 && r.height > 2 && r.bottom >= -5 && r.top <= innerHeight
          && s.display !== 'none' && s.visibility !== 'hidden';
      };
      const absolute = href => { try { return new URL(href || '', location.href).href; } catch (_) { return ''; } };
      const link = root => Array.from((root || document).querySelectorAll('a[href]'))
        .map(a => absolute(a.href)).find(h => /\/video\/|\/note\/|\/explore\/|\/read\/|\/ttarticle\/|weibo\.com\/(?:u\/)?\d+\//i.test(h)) || '';
      const rowText = el => clean(el && (el.innerText || el.textContent || ''));
      const rowFor = (node, selectors) => {
        for (const selector of selectors) { const parent = node.closest(selector); if (parent && rowText(parent).length <= 2400) return parent; }
        let parent = node.parentElement;
        for (let i = 0; parent && i < 7; i++, parent = parent.parentElement) {
          const text = rowText(parent); if (text.length >= 4 && text.length <= 2400) return parent;
        }
        return node;
      };
      const rawFrom = (root, row, fallbackAction) => {
        const links = Array.from(row.querySelectorAll('a[href]'));
        const actor = row.querySelector('[data-e2e="user-name-card"], .interaction-item__uname, .user-info a, [class*="uname"], [class*="user-name"]');
        const times = Array.from(row.querySelectorAll('time,[class*="time"],[class*="date"],.interaction-time'))
          .map(rowText).filter(Boolean);
        const actionNodes = Array.from(row.querySelectorAll('[class*="action"],[class*="hint"],.interaction-item__action'))
          .map(rowText).filter(Boolean);
        const contentNode = row.querySelector('pre,.interaction-item__msg,.interaction-content,[class*="content"]');
        const referenceNode = row.querySelector('.interaction-item__reference,.quote-info,[class*="reference"],[class*="quote"]');
        const action = actionNodes.find(v => /回复|评论|赞|点赞|收藏|关注|提及|@|系统|消息/.test(v)) || fallbackAction || '';
        return {
          message_id: row.getAttribute('data-id') || row.getAttribute('data-message-id') || '',
          user_id: actor && ((actor.closest('a') || actor).getAttribute('href') || ''),
          nickname: rowText(actor),
          action,
          content: rowText(contentNode),
          quote_content: rowText(referenceNode),
          event_time: times[times.length - 1] || '',
          source_url: link(row),
          source_title: '',
          message_url: location.href,
          can_reply: Boolean(row.querySelector('.interaction-item__btn.reply,.action-reply,[class*="reply"]')) || /回复|评论|@|提及/.test(action),
          extra: {row_text: rowText(row).slice(0, 2400), links: links.slice(0, 5).map(a => absolute(a.href))}
        };
      };
      const uniqueRows = (nodes, selectors) => {
        const rows = []; const seen = new Set();
        for (const node of nodes) {
          if (!visible(node)) continue;
          const row = rowFor(node, selectors); const key = rowText(row);
          if (!key || seen.has(key)) continue; seen.add(key); rows.push(row);
        }
        return rows;
      };
      let rows = [];
      if (cfg.platform === 'douyin') {
        const root = document.querySelector('[data-e2e="listDlgTest-container"]') || document.body;
        rows = uniqueRows(Array.from(root.querySelectorAll('[data-e2e="user-name-card"], pre, [class*="v4ybwR9T"]')),
          ['[class*="v4ybwR9T"]','[class*="message"]','li']);
      } else if (cfg.platform === 'xhs') {
        const root = document.querySelector('.tabs-content-container') || document.body;
        rows = uniqueRows(Array.from(root.querySelectorAll('.container,.user-info,.interaction-content,.interaction-hint')),
          ['.container','.interaction-item','li']);
      } else if (cfg.platform === 'bilibili') {
        rows = uniqueRows(Array.from(document.querySelectorAll('.interaction-item,[class*="interaction-item"],[class*="notice-item"],[class*="message-item"]')),
          ['.interaction-item','[class*="interaction-item"]','li']);
      } else {
        rows = uniqueRows(Array.from(document.querySelectorAll('[class*="WB_feed_type"],[class*="woo-box-item"],[class*="message"],[class*="notice"],article,li')),
          ['[class*="WB_feed_type"]','[class*="woo-box-item"]','article','li']);
      }
      const items = rows.map(row => rawFrom(document, row, cfg.category)).filter(item => item.nickname || item.content || item.action);
      return {ok:true, platform:cfg.platform, category:cfg.category, url:location.href,
        title:document.title || '', items, body:clean(document.body?.innerText || '').slice(0, 4000)};
    })()""" % payload


def _douyin_conversation_targets_js() -> str:
    """读取抖音右侧消息弹窗中当前可见的会话头像坐标。"""
    return r"""(() => {
      const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
      const visible = el => {
        if (!el) return false;
        const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 2 && r.height > 2 && r.right >= 0 && r.left <= innerWidth
          && r.bottom >= 0 && r.top <= innerHeight && s.display !== 'none'
          && s.visibility !== 'hidden';
      };
      const root = document.querySelector('#imSaasContainerId')
        || document.querySelector('[class*="imContainer"]');
      if (!root) return {found: false, items: []};
          const rows = Array.from(root.querySelectorAll('.conversationConversationItemwrapper'))
        .map((row, index) => {
          const target = row.querySelector('img') || row;
          if (!visible(row) || !visible(target)) return null;
          const r = target.getBoundingClientRect();
          // 只返回点击中心完全落在视口内的头像；列表底部的半截头像
          // 虽然与视口相交，但中心点在视口外，坐标点击不会触发会话。
          if (r.top < 0 || r.bottom > innerHeight || r.left < 0 || r.right > innerWidth) return null;
          const text = clean(row.innerText || row.textContent);
          const key = row.getAttribute('data-id') || row.getAttribute('data-conversation-id')
            || text || ('conversation-' + index);
          const nickname = clean((row.querySelector('.conversationConversationItemtitle') || {}).innerText || '');
          const eventTime = clean((row.querySelector('.ConversationItemTagNextToTitletimeStr') || {}).innerText || '');
          const preview = clean((row.querySelector('.ConversationItemHinttextBox') || {}).innerText || '');
          return {key, row_text: text.slice(0, 800), x: r.left + r.width / 2,
            y: r.top + r.height / 2, width: r.width, height: r.height,
            nickname: nickname || text.slice(0, 80), event_time: eventTime, preview};
        }).filter(Boolean);
      return {found: true, items: rows};
    })()"""


def _douyin_message_scroll_js() -> str:
    """只滚动抖音消息弹窗左侧会话列表，不影响作品主页。"""
    return r"""(() => {
      const root = document.querySelector('#imSaasContainerId .conversationConversationListwrapper')
        || document.querySelector('#imSaasContainerId .componentsLeftPanelboxList')
        || document.querySelector('[class*="conversationConversationListwrapper"]');
      if (!root) return {found: false, moved: false, bottom: true};
      const before = Number(root.scrollTop || 0);
      const step = Math.max(300, Math.floor((root.clientHeight || 420) * .86));
      root.scrollBy(0, step);
      const after = Number(root.scrollTop || 0);
      return {found: true, before, after, moved: after > before + 2,
        bottom: after + root.clientHeight >= root.scrollHeight - 8,
        height: root.scrollHeight || 0};
    })()"""


def _douyin_detail_reader_js() -> str:
    """读取已点击会话的右侧详情；只读DOM，不执行输入或发送。"""
    return r"""(() => {
      const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
      const root = document.querySelector('#imSaasContainerId .componentsRightPanelwrapper')
        || document.querySelector('[class*="componentsRightPanelwrapper"]');
      if (!root) return {found: false, items: [], text: ''};
      const boxes = Array.from(root.querySelectorAll('.messageMessageBoxmessageBox'));
      const items = boxes.map((box, index) => {
        const system = box.classList.contains('messageMessageBoxisFullRowCenterMessage')
          || Boolean(box.querySelector('[class*="SystemNormalSystem"]'));
        const time = clean((box.querySelector('.MessageBoxTimetimeLayout') || {}).innerText || '');
        const bubble = box.querySelector('.MessageItemTextbubbleTextContent')
          || box.querySelector('.messageMessageBoxfullRowContent')
          || box.querySelector('[class*="bubbleTextContent"]')
          || box.querySelector('[class*="SystemNormalSystemcontent"]');
        const content = clean((bubble || box).innerText || (bubble || box).textContent);
        if (!content && !time) return null;
        const explicit = box.getAttribute('data-id') || box.getAttribute('data-message-id') || '';
        return {
          message_id: explicit || ('detail-' + index + '-' + content.slice(0, 80)),
          message_type: system ? 'system' : 'private',
          content, event_time: time,
          action: system ? '系统消息' : '私信互动',
          can_reply: Boolean(root.querySelector('[class*="inputArea"], [contenteditable="true"], textarea, input')),
          extra: {detail_text: content.slice(0, 1200), system}
        };
      }).filter(Boolean);
      return {found: true, items, text: clean(root.innerText || '').slice(0, 4000)};
    })()"""


def _douyin_back_to_conversations_js() -> str:
    """定位抖音详情弹层左上角的返回箭头。"""
    return r"""(() => {
      const root = document.querySelector('#imSaasContainerId');
      if (!root) return null;
      const rootRect = root.getBoundingClientRect();
      const visible = el => {
        const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 10 && r.height > 10 && r.left >= 0 && r.top >= 0
          && r.right <= innerWidth + 2 && r.bottom <= innerHeight + 2
          && s.display !== 'none' && s.visibility !== 'hidden'
          && s.cursor === 'pointer';
      };
      const arrows = Array.from(root.querySelectorAll(
        '.StackLayoutStackTitleBartitleBar svg, .StackLayoutStackChatHeader svg'
      )).filter(visible).map(el => {
        const r = el.getBoundingClientRect();
        return {x:r.left + r.width / 2, y:r.top + r.height / 2, width:r.width, height:r.height};
      }).filter(rect => rect.x - rootRect.left < rootRect.width * .2
        && rect.y - rootRect.top < rootRect.height * .2);
      return arrows.length ? arrows[0] : null;
    })()"""


def _scroll_message_js() -> str:
    return r"""(() => {
      const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 10 && r.height > 10 && r.bottom >= 0 && r.top <= innerHeight && s.display !== 'none' && s.visibility !== 'hidden'; };
      const roots = [document.scrollingElement, ...Array.from(document.querySelectorAll('*'))]
        .filter(el => el && visible(el) && (el.scrollHeight - el.clientHeight > 80));
      roots.sort((a,b) => (b.scrollHeight-b.clientHeight) - (a.scrollHeight-a.clientHeight));
      const root = roots[0] || document.scrollingElement || document.documentElement;
      const before = Number(root.scrollTop || 0); const step = Math.max(360, Math.floor((root.clientHeight || innerHeight) * .84));
      root.scrollBy(0, step);
      const after = Number(root.scrollTop || 0); const bottom = after + (root.clientHeight || innerHeight) >= root.scrollHeight - 8;
      return {before, after, moved:after > before + 2, bottom, height:root.scrollHeight || 0};
    })()"""


def _label_rect_js(label: str) -> str:
    encoded = json.dumps(label, ensure_ascii=False)
    return r"""(() => {
      const wanted = %s;
      const clean = v => String(v || '').replace(/\s+/g, ' ').trim();
      const candidates = [];
      for (const el of Array.from(document.querySelectorAll('button,a,[role="button"],div,span'))) {
        if (clean(el.innerText || el.textContent) !== wanted) continue;
        const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        if (r.width > 3 && r.height > 3 && s.display !== 'none' && s.visibility !== 'hidden') {
          candidates.push({
            x:r.left+r.width/2, y:r.top+r.height/2, width:r.width, height:r.height,
            pointer:s.cursor === 'pointer',
            preferred:/^(BUTTON|A)$/.test(el.tagName) || el.getAttribute('role') === 'button',
          });
        }
      }
      candidates.sort((a, b) => {
        if (a.pointer !== b.pointer) return a.pointer ? -1 : 1;
        if (a.preferred !== b.preferred) return a.preferred ? -1 : 1;
        return (a.width * a.height) - (b.width * b.height);
      });
      return candidates.length ? candidates[0] : null;
    })()""" % encoded


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("消息中心读取不能在当前 asyncio 事件循环中同步执行")


def _same_platform(url: str, platform: str) -> bool:
    host = urlsplit(str(url or "")).netloc.lower().split(":", 1)[0]
    root = {
        "douyin": "douyin.com", "xhs": "xiaohongshu.com",
        "bilibili": "bilibili.com", "weibo": "weibo.com",
    }.get(platform, "")
    return bool(root and (host == root or host.endswith("." + root)))


async def _click_label(session, sid: str, label: str) -> bool:
    rect = await session.eval(_label_rect_js(label), sid)
    if not isinstance(rect, Mapping):
        return False
    x, y = float(rect.get("x") or 0), float(rect.get("y") or 0)
    await session.cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, session_id=sid)
    await session.cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, "x": x, "y": y}, session_id=sid)
    await session.cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": x, "y": y}, session_id=sid)
    await asyncio.sleep(.8)
    return True


async def _click_rect(session, sid: str, rect: Mapping[str, Any]) -> None:
    x, y = float(rect.get("x") or 0), float(rect.get("y") or 0)
    await session.cmd("Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": x, "y": y,
    }, session_id=sid)
    await session.cmd("Input.dispatchMouseEvent", {
        "type": "mousePressed", "button": "left", "clickCount": 1,
        "x": x, "y": y,
    }, session_id=sid)
    await session.cmd("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "button": "left", "clickCount": 1,
        "x": x, "y": y,
    }, session_id=sid)


async def _read_douyin_popup(session, sid: str, limit: int) -> dict[str, Any]:
    """读取抖音右侧消息弹窗：逐个打开会话头像并采集详情。"""
    if not await _click_label(session, sid, "消息"):
        return {"items": [], "errors": ["未找到抖音右上角消息入口"]}
    await asyncio.sleep(.8)
    raw_items: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    # 抖音消息弹层先渲染外壳，再异步加载会话头像；只检查一次会把
    # 空弹层误判成“没有消息”。最多轮询10秒，有列表就立即进入处理。
    targets: Mapping[str, Any] | None = None
    for _ in range(20):
        candidate = await session.eval(_douyin_conversation_targets_js(), sid, timeout=20.0)
        if isinstance(candidate, Mapping):
            targets = candidate
            if isinstance(candidate.get("items"), list) and candidate.get("items"):
                break
        await asyncio.sleep(.5)
    for _ in range(80):
        targets = await session.eval(_douyin_conversation_targets_js(), sid, timeout=20.0)
        if not isinstance(targets, Mapping) or not targets.get("found"):
            if not seen:
                errors.append("未找到抖音消息右侧弹窗")
            break
        visible_items = targets.get("items") if isinstance(targets.get("items"), list) else []
        added = 0
        for target in visible_items:
            if not isinstance(target, Mapping):
                continue
            key = _clean_text(target.get("key"))
            if not key or key in seen:
                continue
            seen.add(key)
            added += 1
            await _click_rect(session, sid, target)
            detail: Mapping[str, Any] | None = None
            for _ in range(10):
                await asyncio.sleep(.35)
                candidate = await session.eval(_douyin_detail_reader_js(), sid, timeout=20.0)
                if isinstance(candidate, Mapping):
                    detail = candidate
                    if isinstance(candidate.get("items"), list) and candidate.get("items"):
                        break
            detail_items = detail.get("items") if isinstance(detail, Mapping) else []
            nickname = _clean_text(target.get("row_text"))
            # 会话列表中的第一段文本通常是昵称；保留完整预览到 extra，
            # 详情消息本身作为 content 写入，方便后续回复定位。
            if isinstance(detail_items, list) and detail_items:
                for item in detail_items:
                    if not isinstance(item, Mapping):
                        continue
                    normalized = dict(item)
                    normalized.update({
                        "nickname": _clean_text(target.get("nickname")) or nickname,
                        "category": "message",
                        "source_url": MESSAGE_ENTRY_URLS["douyin"],
                        "message_url": MESSAGE_ENTRY_URLS["douyin"],
                    })
                    extra = dict(normalized.get("extra") or {})
                    extra["conversation_key"] = key
                    extra["conversation_preview"] = _clean_text(target.get("row_text"))
                    extra["conversation_time"] = _clean_text(target.get("event_time"))
                    normalized["extra"] = extra
                    raw_items.append(normalized)
            else:
                # 某些系统类型没有可展开的正文，仍保留会话预览，避免漏采。
                preview = _clean_text(target.get("row_text"))
                if preview:
                    raw_items.append({
                        "message_id": "conversation-" + key,
                        "message_type": "other",
                        "nickname": _clean_text(target.get("nickname")) or nickname,
                        "content": preview,
                        "action": "抖音消息预览",
                        "can_reply": False,
                        "category": "message",
                        "source_url": MESSAGE_ENTRY_URLS["douyin"],
                        "message_url": MESSAGE_ENTRY_URLS["douyin"],
                        "extra": {"conversation_key": key, "conversation_preview": preview,
                                  "conversation_time": _clean_text(target.get("event_time"))},
                    })
            back = await session.eval(_douyin_back_to_conversations_js(), sid, timeout=20.0)
            if isinstance(back, Mapping):
                await _click_rect(session, sid, back)
                await asyncio.sleep(.45)
            elif visible_items:
                errors.append(f"会话“{_clean_text(target.get('nickname')) or key}”读取后未找到返回按钮")
            if len(raw_items) >= max(1, min(5000, int(limit or 500))):
                return {"items": raw_items[:limit], "errors": errors}
        scroll = await session.eval(_douyin_message_scroll_js(), sid, timeout=20.0)
        if not isinstance(scroll, Mapping) or not scroll.get("found"):
            break
        if scroll.get("bottom") and added == 0:
            break
        if not scroll.get("moved") and added == 0:
            break
        await asyncio.sleep(.35)
    return {"items": raw_items[:limit], "errors": errors}


async def _read_async(bitbrowser, window_id: str, platform: str, limit: int,
                      account_id: int = 0) -> dict[str, Any]:
    try:
        from ..cdp import CdpSession
    except ImportError:  # pragma: no cover
        from cdp import CdpSession  # type: ignore
    if platform not in MESSAGE_ENTRY_URLS:
        raise ValueError(f"不支持的平台：{platform}")
    opened = bitbrowser.open_browser(window_id, ignore_default_urls=True, new_page_url=MESSAGE_ENTRY_URLS[platform])
    if not isinstance(opened, dict):
        raise RuntimeError("BitBrowser 未返回浏览器连接信息")
    ws_url = str(opened.get("ws") or opened.get("webSocketDebuggerUrl") or "").strip()
    if not ws_url:
        raise RuntimeError("账号窗口已打开，但未返回 CDP 连接地址")
    session = CdpSession(ws_url, timeout=35.0)
    await session.connect()
    try:
        targets = await session.cmd("Target.getTargets")
        pages = [t for t in targets.get("targetInfos", []) if isinstance(t, Mapping) and t.get("type") == "page"]
        platform_pages = [
            page for page in pages
            if _same_platform(str(page.get("url") or ""), platform)
        ]
        selected = max(
            platform_pages,
            key=lambda t: str(t.get("targetId") or ""),
            default=None,
        )
        if not selected:
            created = await session.cmd("Target.createTarget", {"url": MESSAGE_ENTRY_URLS[platform]})
            selected = {"targetId": str(created.get("targetId") or "")}
        attached = await session.cmd("Target.attachToTarget", {"targetId": selected["targetId"], "flatten": True})
        sid = str(attached.get("sessionId") or "")
        if not sid:
            raise RuntimeError("消息中心页面附着失败")
        await session.cmd("Page.enable", session_id=sid)
        await session.cmd("Runtime.enable", session_id=sid)
        await session.navigate(MESSAGE_ENTRY_URLS[platform], sid, wait_load=False)
        await asyncio.sleep(1.5)
        if platform == "douyin":
            # 抖音先渲染顶部文字，再绑定右上角消息入口的交互事件；
            # 文字已出现不代表此时点击事件已经可用。
            await asyncio.sleep(1.5)
            popup = await _read_douyin_popup(session, sid, limit)
            raw_items = popup.get("items") if isinstance(popup, Mapping) else []
            items = normalize_messages(platform, account_id, raw_items, limit=limit, category="message")
            popup_errors = list(popup.get("errors") or []) if isinstance(popup, Mapping) else []
            return {
                "ok": bool(items) or not popup_errors,
                "logged_in": True,
                "platform": platform,
                "entry_url": MESSAGE_ENTRY_URLS[platform],
                "items": items,
                "fetched_count": len(items),
                "categories": ["interaction", "private"],
                "errors": popup_errors,
                "reason": "" if items else ("消息弹窗已打开，但未读取到可见消息" if not popup_errors else "；".join(popup_errors)),
            }
        all_items: list[dict[str, Any]] = []
        route_errors: list[str] = []
        routes = MESSAGE_ROUTES[platform]
        douyin_message_opened = False
        for route in routes:
            category, url, label = route["category"], route["url"], route["label"]
            try:
                current = str(await session.eval("location.href", sid) or "")
                if current.split("#", 1)[0].rstrip("/") != url.split("#", 1)[0].rstrip("/") or ("#" in url and current.split("#", 1)[-1] != url.split("#", 1)[-1]):
                    await session.navigate(url, sid, wait_load=False)
                    await asyncio.sleep(1.2)
                # 同 URL 的入口是 SPA 弹层；小红书通知分类也通过真实 tab 切换。
                if platform == "douyin":
                    # 抖音右上角实际入口统一叫“消息”；互动消息和私信
                    # 都从这里进入。它是一个弹层开关，第二个分类不能
                    # 再次点击，否则会把已经打开的消息弹层关闭。
                    if not douyin_message_opened:
                        if not await _click_label(session, sid, "消息"):
                            raise RuntimeError("未找到抖音消息入口")
                        douyin_message_opened = True
                elif platform == "xhs" and category in {"comment", "like", "follow"}:
                    if not await _click_label(session, sid, label):
                        raise RuntimeError(f"未找到小红书消息分类入口：{label}")
                await asyncio.sleep(.6)
                route_items: list[dict[str, Any]] = []
                stale = 0
                last_body = ""
                for _ in range(32):
                    payload = await session.eval(_message_reader_js(platform, category), sid, timeout=20.0)
                    raw = payload.get("items") if isinstance(payload, Mapping) else []
                    last_body = _clean_text(payload.get("body") if isinstance(payload, Mapping) else "")
                    before = len(route_items)
                    known = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in route_items}
                    for item in raw if isinstance(raw, list) else []:
                        if isinstance(item, Mapping):
                            key = json.dumps(dict(item), ensure_ascii=False, sort_keys=True)
                            if key not in known:
                                route_items.append(dict(item)); known.add(key)
                    stale = stale + 1 if len(route_items) == before else 0
                    scroll = await session.eval(_scroll_message_js(), sid)
                    if isinstance(scroll, Mapping) and scroll.get("bottom") and stale >= 1:
                        break
                    if isinstance(scroll, Mapping) and not scroll.get("moved") and stale >= 2:
                        break
                    await asyncio.sleep(.65)
                if not route_items and re.search(r"扫码登录|请先登录|登录后查看", last_body):
                    raise RuntimeError("当前账号未登录或消息中心需要重新登录")
                all_items.extend({**item, "category": category} for item in route_items)
            except Exception as exc:
                route_errors.append(f"{label}: {type(exc).__name__}: {exc}")
        items = normalize_messages(platform, account_id, all_items, limit=limit)
        return {
            "ok": bool(items) or not route_errors,
            "logged_in": True,
            "platform": platform,
            "entry_url": MESSAGE_ENTRY_URLS[platform],
            "items": items,
            "fetched_count": len(items),
            "categories": [route["category"] for route in routes],
            "errors": route_errors,
            "reason": "" if items else ("消息入口已打开，但未读取到可见消息" if not route_errors else "；".join(route_errors)),
        }
    finally:
        await session.close()


def read_account_messages(bitbrowser, window_id: str, platform: str, *, limit: int = 500,
                          account_id: int = 0) -> dict[str, Any]:
    """读取一个账号的全量消息；只读，不点击回复或发送。"""
    platform = _clean_text(platform).lower()
    window_id = _clean_text(window_id)
    if not window_id:
        raise ValueError("账号尚未绑定浏览器窗口")
    return _run(_read_async(
        bitbrowser, window_id, platform,
        max(1, min(5000, int(limit or 500))), int(account_id or 0),
    ))


__all__ = [
    "MESSAGE_ENTRY_URLS", "MESSAGE_ROUTES", "MESSAGE_TYPE_LABELS",
    "normalize_messages", "read_account_messages", "_message_reader_js",
]
