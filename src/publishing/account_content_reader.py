# -*- coding: utf-8 -*-
"""四个平台账号主页作品的只读提取适配器。

账号信息页展示的是账号自己发布的作品，不能把采集任务里“分配给该账号”的
作品误当成账号主页内容。这个模块通过 BitBrowser/CDP 打开当前账号的主页，
只读取可见 DOM 中的作品卡片和链接，不点击发布、点赞、评论等有副作用的
控件。真实浏览器主页读取失败时不使用采集缓存冒充账号作品，并把原因返回给界面；
只有未配置浏览器连接时，后端才允许使用作者匹配的本地兼容数据。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit


PROFILE_ENTRY_URLS = {
    "douyin": "https://www.douyin.com/user/self?from_nav=1",
    # 小红书账号自己的内容在创作者后台的笔记管理页，不在公开探索页。
    # 该页面依赖当前 BitBrowser 会话已经登录。
    "xhs": "https://creator.xiaohongshu.com/new/note-manager?source=official",
    "bilibili": "https://www.bilibili.com/",
    "weibo": "https://weibo.com/",
    "kuaishou": "https://www.kuaishou.com/new-reco",
}

# 写入同步诊断，便于不同机器排查是否仍在运行旧的主页解析器。
ACCOUNT_CONTENT_READER_VERSION = "2026.09.01-profile-scope-2"

PLATFORM_HOSTS = {
    "douyin": {"douyin.com", "www.douyin.com"},
    "xhs": {"xiaohongshu.com", "www.xiaohongshu.com", "creator.xiaohongshu.com"},
    "bilibili": {"bilibili.com", "www.bilibili.com", "space.bilibili.com"},
    "weibo": {"weibo.com", "www.weibo.com"},
    "kuaishou": {"kuaishou.com", "www.kuaishou.com", "gifshow.com"},
}

CONTENT_PATTERNS = {
    "douyin": re.compile(r"/(?:video|note)/([0-9A-Za-z_-]+)"),
    "xhs": re.compile(r"/(?:explore|discovery/item)/([0-9A-Za-z_-]+)"),
    "bilibili": re.compile(r"/(?:video/(BV[0-9A-Za-z]+)|read/(cv[0-9]+))", re.I),
    "weibo": re.compile(
        r"/(?:u/)?([0-9]{5,})/([0-9A-Za-z]+)(?:[/?#]|$)"
        r"|/ttarticle/p/show\?id=([0-9A-Za-z]+)",
        re.I,
    ),
    "kuaishou": re.compile(
        r"/(?:short-video|video|movie/video|featured)/([0-9A-Za-z_-]+)", re.I
    ),
}


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _safe_count(value: Any) -> int:
    """统计字段来自页面文本，异常占位符不能让整次同步失败。"""
    if isinstance(value, bool):
        return int(value)
    try:
        return max(0, int(float(str(value or "0").replace(",", ""))))
    except (TypeError, ValueError):
        return 0


def _content_id(platform: str, url: str) -> str:
    match = CONTENT_PATTERNS.get(platform).search(url) if platform in CONTENT_PATTERNS else None
    if not match:
        return ""
    for group in match.groups()[::-1]:
        if group:
            return str(group)
    return ""


def normalize_profile_items(platform: str, raw_items: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    """把页面 JS 返回的卡片统一为 account_contents 可写入的结构。

    这个函数不依赖浏览器，既用于后端写入，也用于回归测试页面解析结果。
    """
    platform = str(platform or "").strip().lower()
    if platform not in CONTENT_PATTERNS or not isinstance(raw_items, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        url = _clean_text(raw.get("url"))
        if not url:
            continue
        content_id = _clean_text(raw.get("content_id")) or _content_id(platform, url)
        if not content_id or content_id in seen:
            continue
        seen.add(content_id)
        path = urlsplit(url).path.lower()
        if platform == "douyin":
            content_type = "note" if "/note/" in path else "video"
        elif platform == "xhs":
            content_type = "note"
        elif platform == "bilibili":
            content_type = "article" if "/read/" in path else "video"
        elif platform == "kuaishou":
            content_type = "image" if "/featured/" in path else "video"
        else:
            content_type = "article"
        extra = raw.get("extra")
        if not isinstance(extra, Mapping):
            extra = {}
        extra = dict(extra)
        extra["__account_content_origin"] = "profile_sync"
        extra["profile_url"] = _clean_text(raw.get("profile_url"))
        item = {
            "content_id": content_id,
            "content_type": content_type,
            "url": url,
            "title": _clean_text(raw.get("title")) or "未命名内容",
            "description": _clean_text(raw.get("description")),
            "cover_url": _clean_text(raw.get("cover_url")),
            "published_at": _clean_text(raw.get("published_at")) or None,
            "like_count": _safe_count(raw.get("like_count")),
            "comment_count": _safe_count(raw.get("comment_count")),
            "share_count": _safe_count(raw.get("share_count")),
            "favorite_count": _safe_count(raw.get("favorite_count")),
            "extra": extra,
        }
        result.append(item)
        if len(result) >= max(1, min(500, int(limit or 100))):
            break
    return result


def _same_host(left: str, right: str) -> bool:
    left_host = urlsplit(str(left or "")).netloc.lower().split(":", 1)[0]
    right_host = urlsplit(str(right or "")).netloc.lower().split(":", 1)[0]
    if not left_host or not right_host:
        return False
    # 允许 www/space/creator 等同一平台子域名，仍不接受跨平台跳转。
    def base(host: str) -> str:
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    return base(left_host) == base(right_host)


def _target_platform_score(target: Mapping[str, Any], platform: str,
                           entry_url: str) -> int:
    """给浏览器 page target 排序，排除 BitBrowser 自带控制台页。

    BitBrowser 的 browser-level CDP 会同时返回控制台页、扩展页和实际平台页。
    旧逻辑直接取第一个 page，正是 B 站读到推荐内容、微博读不到作品的根因。
    这个函数保持纯函数，便于在不触碰真实浏览器的情况下回归测试。
    """
    if not isinstance(target, Mapping) or str(target.get("type") or "") != "page":
        return -100000
    raw_url = str(target.get("url") or "").strip()
    parsed = urlsplit(raw_url)
    host = parsed.netloc.lower().split(":", 1)[0]
    if not host or parsed.scheme not in {"http", "https"}:
        return -100000
    if host in {"console.bitbrowser.net", "bitbrowser.net"}:
        return -100000
    if platform == "douyin" and host == "creator.douyin.com":
        return -100000
    expected = urlsplit(entry_url)
    expected_host = expected.netloc.lower().split(":", 1)[0]
    score = 10
    if _same_host(raw_url, entry_url):
        score += 100
        if host == expected_host:
            score += 25
        if parsed.path.rstrip("/") == expected.path.rstrip("/"):
            score += 25
    if platform == "bilibili" and host == "space.bilibili.com":
        score += 15
    if platform == "weibo" and re.search(r"/u/\d+", parsed.path):
        score += 15
    if platform == "xhs" and host == "creator.xiaohongshu.com":
        score += 30
    if platform == "kuaishou" and host in {"kuaishou.com", "www.kuaishou.com"}:
        score += 20
    return score


def select_profile_target(targets: Any, platform: str, entry_url: str) -> dict[str, Any] | None:
    """从 Target.getTargets 结果中选出真正的平台 page。"""
    if not isinstance(targets, list):
        return None
    candidates = [
        dict(target) for target in targets
        if isinstance(target, Mapping)
        and _target_platform_score(target, platform, entry_url) > -100000
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda target: (
            _target_platform_score(target, platform, entry_url),
            str(target.get("targetId") or ""),
        ),
    )


def _profile_url_reached(url: str, expected_url: str, platform: str) -> bool:
    """确认导航已经到达账号主页，而不是仅仅仍在同一平台域名。"""
    if not _same_host(url, expected_url):
        return False
    actual_path = urlsplit(str(url or "")).path.rstrip("/")
    expected_path = urlsplit(str(expected_url or "")).path.rstrip("/")
    if not actual_path or not expected_path:
        return False
    if platform == "bilibili":
        expected_uid = re.search(r"/(\d+)(?:/|$)", expected_path)
        if not expected_uid:
            return False
        expected_prefix = f"/{expected_uid.group(1)}"
        if expected_path.endswith("/upload"):
            return actual_path == expected_path or actual_path.startswith(expected_path + "/")
        return actual_path.startswith(expected_prefix)
    if platform == "weibo":
        expected_uid = re.search(r"/(?:u/)?(\d+)(?:/|$)", expected_path)
        return bool(expected_uid and re.search(rf"/(?:u/)?{expected_uid.group(1)}$", actual_path))
    if platform == "xhs":
        return actual_path.startswith(expected_path)
    if platform == "douyin":
        # /user/self 可能会被抖音重定向为当前账号的数字主页，两个路由都
        # 属于账号主页；首页、推荐页、搜索页等路由必须明确拒绝。
        return bool(re.fullmatch(r"/user/(?:self|[0-9A-Za-z_-]+)", actual_path, re.I))
    if platform == "kuaishou":
        return actual_path == expected_path or actual_path.startswith(expected_path + "/")
    return actual_path == expected_path


def _reader_js(platform: str) -> str:
    platform_json = json.dumps(platform, ensure_ascii=False)
    return r"""(() => {
      const platform = %s;
      const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
      const visible = el => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 2 && r.height > 2 && r.bottom >= 0 && r.top <= innerHeight
          && s.display !== 'none' && s.visibility !== 'hidden';
      };
      const absolute = href => { try { return new URL(href || '', location.href).href; } catch (_) { return ''; } };
      const matches = href => {
        const value = String(href || '');
        if (platform === 'douyin') return /\/\/(?:www\.)?douyin\.com\/(?:video|note)\/[0-9A-Za-z_-]+/.test(value);
        if (platform === 'xhs') return /\/\/(?:www\.|creator\.)?xiaohongshu\.com\/(?:explore|discovery\/item)\/[0-9A-Za-z_-]+/.test(value);
        if (platform === 'kuaishou') return /\/\/(?:www\.)?(?:kuaishou\.com|gifshow\.com)\/(?:short-video|video|movie\/video|featured)\/[0-9A-Za-z_-]+/i.test(value);
        if (platform === 'bilibili') return /\/\/(?:www\.)?bilibili\.com\/video\/BV[0-9A-Za-z]+|\/\/www\.bilibili\.com\/read\/cv[0-9]+/i.test(value);
        return /\/\/(?:www\.)?weibo\.com\/(?:u\/)?[0-9]{5,}\/[0-9A-Za-z]+(?:[/?#]|$)/i.test(value)
          || /\/\/(?:www\.)?weibo\.com\/ttarticle\/p\/show\?id=[0-9A-Za-z]+/i.test(value);
      };
      const numberNear = (text, words) => {
        const source = clean(text);
        for (const word of words) {
          const m = source.match(new RegExp('(?:' + word + ')\\s*([0-9.,万亿Kk+]+)', 'i'));
          if (!m) continue;
          const v = String(m[1]).replace(/,/g, '');
          const n = parseFloat(v);
          if (!Number.isNaN(n)) return Math.round(n * (v.includes('万') ? 10000 : v.includes('亿') ? 100000000 : v.toLowerCase().includes('k') ? 1000 : 1));
        }
        return 0;
      };
      const cardFor = anchor => {
        // 链接本身经常只是封面层（例如B站 article-card__cover），先从父级
        // 卡片开始查找，避免把封面上的“0 0”统计值当成标题和正文。
        let node = anchor.parentElement || anchor;
        if (platform === 'bilibili') {
          for (let candidate = node, depth = 0; candidate && depth < 7;
               candidate = candidate.parentElement, depth++) {
            const cls = String(candidate.className || '');
            const text = clean(candidate.innerText || '');
            if (/(^|\s)(?:article-card|bili-video-card)(?:\s|$)/.test(cls)
                && text.length <= 1800) {
              return candidate;
            }
          }
        }
        for (let depth = 0; node && depth < 7; depth++, node = node.parentElement) {
          const text = clean(node.innerText || '');
          if (platform === 'bilibili'
              && /(?:^|\s)(?:article-card|bili-video-card)(?:\s|$)/.test(String(node.className || ''))
              && text.length <= 1800) {
            return node;
          }
          if (node.matches('article,li') || node.querySelector('img') || text.length > 60) {
            if (text.length <= 1800) return node;
          }
        }
        return anchor;
      };
      const statOnly = text => {
        const value = clean(text);
        return !value || /^(?:[\d.,万亿Kk+]+\s*){1,4}(?:\d{1,2}:\d{2})?$/.test(value)
          || /^(?:[\d.,万亿Kk+]+\s*){1,5}$/.test(value);
      };
      const titleFrom = (anchor, card, lines) => {
        const candidates = [];
        const add = value => { const text = clean(value); if (text && candidates.indexOf(text) < 0) candidates.push(text); };
        add(anchor.getAttribute('title'));
        add(anchor.getAttribute('aria-label'));
        for (const node of Array.from(card.querySelectorAll('h1,h2,h3,h4,[title],.title,.video-name,.bili-video-card__info--tit'))) {
          add(node.getAttribute('title') || node.innerText || node.textContent);
        }
        for (const line of lines) add(line);
        add(card.innerText || '');
        add(anchor.innerText || '');
        return candidates.find(value => value.length <= 240 && !statOnly(value)) || candidates[0] || '';
      };
      const cards = [];
      const seen = new Set();
      // 抖音个人页下方还会渲染“热门/推荐”作品。只有 user-post-list
      // 才是当前登录账号的作品区，不能扫描整个 document，否则会把他人作品
      // 写入账号信息页。
      const ownerRoot = platform === 'douyin'
        ? document.querySelector('[data-e2e="user-post-list"]')
        : document;
      // 抖音找不到明确的本人作品容器时必须返回空结果，不能退回整个
      // document；个人页下方存在“热门/推荐”内容，扫描 document 会串入他人作品。
      const root = ownerRoot;
      const addCard = (url, title, description, coverUrl, publishedAt, text) => {
        const absoluteUrl = absolute(url);
        if (!absoluteUrl || seen.has(absoluteUrl)) return;
        seen.add(absoluteUrl);
        cards.push({
          url: absoluteUrl,
          title: clean(title).slice(0, 240),
          description: clean(description).slice(0, 1200),
          cover_url: absolute(coverUrl || ''),
          published_at: clean(publishedAt || ''),
          like_count: numberNear(text, ['赞','点赞','like']),
          comment_count: numberNear(text, ['评论','comment']),
          share_count: numberNear(text, ['分享','share']),
          favorite_count: numberNear(text, ['收藏','favorite'])
        });
      };
      // 小红书创作者后台的笔记卡片没有作品 a[href]，作品 ID 放在
      // data-impression.noteTarget.value.noteId 中，必须直接解析卡片。
      if (platform === 'xhs') {
        for (const card of Array.from(document.querySelectorAll('.note-card'))) {
          const text = clean(card.innerText || '');
          const titleNode = card.querySelector('.note-card__title');
          const title = clean(titleNode ? (titleNode.innerText || titleNode.textContent) : '');
          let noteId = '';
          try {
            const raw = JSON.parse(card.getAttribute('data-impression') || '{}');
            const target = raw && raw.noteTarget && raw.noteTarget.value;
            noteId = clean(target && target.noteId);
          } catch (_) {}
          if (!noteId) {
            const idNode = card.querySelector('[data-note-id],[data-id]');
            noteId = clean(idNode && (idNode.getAttribute('data-note-id') || idNode.getAttribute('data-id')));
          }
          if (!noteId) continue;
          const timeMatch = text.match(/\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:\s+\d{1,2}:\d{2})?/);
          const image = card.querySelector('img');
          addCard(
            'https://www.xiaohongshu.com/explore/' + noteId,
            title || '未命名内容',
            text,
            image && (image.currentSrc || image.src),
            timeMatch ? timeMatch[0] : '',
            text,
          );
        }
      }
      // B站个人空间会同时展示“最近投币/点赞”等他人作品，不能以整个
      // document 为扫描范围。只读取本人作品区：视频区和专栏区；当前账号
      // 没有视频时，专栏区仍能稳定读取图文内容。
      const scanRoots = platform === 'bilibili'
        ? Array.from(document.querySelectorAll('.video-section,.article-section'))
        : [root];
      const safeRoots = platform === 'douyin'
        ? (ownerRoot ? [ownerRoot] : [])
        : (scanRoots.length ? scanRoots : (platform === 'bilibili' ? [] : [root]));
      for (const scanRoot of safeRoots) {
        for (const anchor of Array.from(scanRoot.querySelectorAll('a[href]'))) {
          // 部分平台用零尺寸覆盖层承载作品链接，链接本身不可见但卡片是可见的；
          // 不能只按 anchor 的可见性过滤，否则微博主页会被判定为无作品。
          if (!matches(anchor.href)) continue;
          const url = absolute(anchor.href);
          if (seen.has(url)) continue;
          const card = cardFor(anchor);
          const text = clean(card.innerText || anchor.innerText || '');
          const lines = text.split(' · ').filter(Boolean);
          const title = titleFrom(anchor, card, lines);
          const timeNode = card.querySelector('time[datetime],time');
          const image = card.querySelector('img');
          addCard(
            url,
            title,
            text,
            image && (image.currentSrc || image.src),
            timeNode && (timeNode.getAttribute('datetime') || timeNode.innerText),
            text,
          );
        }
      }
      const body = clean(document.body?.innerText || '');
      return {
        items: cards.slice(0, 120),
        body: body.slice(0, 3000),
        url: location.href,
        title: document.title || '',
        profile_scope_found: platform !== 'douyin' || Boolean(ownerRoot),
        profile_scope: platform === 'douyin' ? '[data-e2e="user-post-list"]' : 'document',
      };
    })()""" % platform_json


def _profile_link_js(platform: str) -> str:
    platform_json = json.dumps(platform, ensure_ascii=False)
    return r"""(() => {
      const platform = %s;
      const visible = el => { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2 && r.top >= -5 && r.top < 220; };
      const links = Array.from(document.querySelectorAll('a[href]')).filter(visible);
      if (platform === 'douyin') return location.href;
      if (platform === 'xhs') {
        try {
          const current = new URL(location.href);
          if (current.hostname === 'creator.xiaohongshu.com'
              && current.pathname.replace(/\/+$/, '') === '/new/note-manager') {
            return current.href;
          }
        } catch (_) {}
        const link = links.find(a => /xiaohongshu\.com\/user\/profile\//.test(a.href));
        return link?.href || '';
      }
      if (platform === 'bilibili') {
        const link = links.find(a => /space\.bilibili\.com\/\d+/.test(a.href));
        return link?.href || '';
      }
      if (platform === 'kuaishou') {
        const profile = links.find(a => /(?:kuaishou|gifshow)\.com\/profile\//i.test(a.href));
        return profile?.href || '';
      }
      const link = links.find(a => {
        try {
          const path = new URL(a.href, location.href).pathname.replace(/\/+$/, '');
          return /^\/(?:u\/)?\d{5,}$/.test(path);
        } catch (_) { return false; }
      });
      return link?.href || '';
    })()""" % platform_json


def _scroll_js() -> str:
    return r"""(() => {
      const root = document.scrollingElement || document.documentElement;
      const before = Number(root.scrollTop || window.scrollY || 0);
      const step = Math.max(560, Math.floor(window.innerHeight * 0.82));
      window.scrollBy(0, step);
      const after = Number(root.scrollTop || window.scrollY || 0);
      const height = Math.max(root.scrollHeight || 0, document.body?.scrollHeight || 0);
      const bottom = after + window.innerHeight >= height - 8;
      return {before, after, height, bottom, moved: after > before + 2};
    })()"""


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("账号主页读取不能在当前 asyncio 事件循环中同步执行")


async def _read_async(bitbrowser, window_id: str, platform: str, limit: int) -> dict[str, Any]:
    try:
        from ..cdp import CdpSession
    except ImportError:  # pragma: no cover
        from cdp import CdpSession  # type: ignore

    entry_url = PROFILE_ENTRY_URLS[platform]
    opened = bitbrowser.open_browser(
        window_id, ignore_default_urls=True, new_page_url=entry_url
    )
    if not isinstance(opened, dict):
        raise RuntimeError("BitBrowser 未返回浏览器连接信息")
    ws_url = str(opened.get("ws") or opened.get("webSocketDebuggerUrl") or "").strip()
    if not ws_url:
        raise RuntimeError("账号窗口已打开，但未返回 CDP 连接地址")
    session = CdpSession(ws_url, timeout=35.0)
    await session.connect()
    try:
        target_info = await session.cmd("Target.getTargets")
        selected = select_profile_target(
            target_info.get("targetInfos") if isinstance(target_info, Mapping) else [],
            platform,
            entry_url,
        )
        if selected is None:
            # 只有控制台页/扩展页时，不能误附着控制台后再读取；新建一个明确
            # 的平台页，避免把 BitBrowser 自身 DOM 当成账号主页。
            created = await session.cmd("Target.createTarget", {"url": entry_url})
            selected = {"targetId": str(created.get("targetId") or "")}
        if not selected.get("targetId"):
            raise RuntimeError("未找到可用的平台页面")
        attached = await session.cmd(
            "Target.attachToTarget",
            {"targetId": selected["targetId"], "flatten": True},
        )
        sid = str(attached.get("sessionId") or "")
        if not sid:
            raise RuntimeError("平台页面附着失败")
        await session.cmd("Page.enable", session_id=sid)
        await session.cmd("Runtime.enable", session_id=sid)
        current = await session.eval("location.href", sid)
        # 每次同步都回到主页入口，避免复用上一次停留的作品详情页、搜索
        # 页或导航页；这是“账号信息读取”与采集/发布窗口的关键隔离点。
        if platform == "douyin":
            # 抖音 /user/self 可能被 SPA 重定向成 /user/<uid>，只要仍然是
            # /user/ 路由即可；如果落在首页、推荐页或搜索页，必须先导航并
            # 等待路由真正切换，不能把当前页面当作账号主页继续解析。
            if not _profile_url_reached(str(current or ""), entry_url, platform):
                await session.navigate(entry_url, sid, wait_load=False)
            for _ in range(20):
                current = await session.eval("location.href", sid)
                if _profile_url_reached(str(current or ""), entry_url, platform):
                    break
                await asyncio.sleep(0.35)
            else:
                return {
                    "ok": True,
                    "logged_in": True,
                    "items": [],
                    "profile_url": str(current or entry_url),
                    "source": "profile_sync",
                    "read_only": True,
                    "reader_version": ACCOUNT_CONTENT_READER_VERSION,
                    "reason_code": "profile_route_not_reached",
                    "reason": "抖音账号主页导航未生效，已拒绝读取推荐页内容",
                }
        elif str(current or "").split("#", 1)[0].rstrip("/") != entry_url.rstrip("/"):
            await session.navigate(entry_url, sid, wait_load=False)
            # Page.navigate 返回后 current 仍是附着前页面的地址。刷新它，
            # 否则后续主页导航会误判为已在目标页。
            current = await session.eval("location.href", sid)
        await asyncio.sleep(1.8)
        profile_url = await session.eval(_profile_link_js(platform), sid)
        if platform == "douyin":
            profile_url = str(profile_url or current or entry_url)
            if not _profile_url_reached(profile_url, entry_url, platform):
                return {
                    "ok": True,
                    "logged_in": True,
                    "items": [],
                    "profile_url": profile_url,
                    "source": "profile_sync",
                    "read_only": True,
                    "reader_version": ACCOUNT_CONTENT_READER_VERSION,
                    "reason_code": "profile_route_mismatch",
                    "reason": "抖音当前页面不是账号主页，已拒绝读取推荐页内容",
                }
        elif not profile_url:
            # 主页入口没有识别出当前登录账号时，不能把探索页/首页上的
            # 别人作品误写进账号信息；安全返回空结果并交代原因。
            return {
                "ok": True,
                "logged_in": True,
                "items": [],
                "profile_url": str(current or entry_url),
                "source": "profile_sync",
                "read_only": True,
                "reader_version": ACCOUNT_CONTENT_READER_VERSION,
                "reason_code": "profile_identity_not_detected",
                "reason": "已打开平台页面，但未识别到当前账号主页入口",
            }
        if platform != "douyin" and profile_url and _same_host(str(profile_url), entry_url):
            profile_url = str(profile_url).split("#", 1)[0]
            if platform == "weibo" and "tabtype=" not in profile_url:
                profile_url += "&tabtype=feed" if "?" in profile_url else "?tabtype=feed"
            if str(current or "") != profile_url:
                await session.navigate(profile_url, sid, wait_load=False)
            # Page.navigate 返回时 SPA 可能还没完成路由切换；确认 URL 已经
            # 到达账号主页后再开始读卡片，避免把入口页/旧页面混进结果。
            for _ in range(20):
                current_after_nav = str(await session.eval("location.href", sid) or "")
                if _profile_url_reached(current_after_nav, profile_url, platform):
                    break
                await asyncio.sleep(0.35)
            else:
                return {
                    "ok": True,
                    "logged_in": True,
                    "items": [],
                    "profile_url": profile_url,
                    "source": "profile_sync",
                    "read_only": True,
                    "reader_version": ACCOUNT_CONTENT_READER_VERSION,
                    "reason_code": "profile_navigation_failed",
                    "reason": "账号主页导航未生效，未读取其他页面作品",
                }
            await asyncio.sleep(2.0)
        else:
            profile_url = str(current or entry_url)

        all_raw: list[Any] = []
        stale_rounds = 0
        scope_missing_rounds = 0
        read_rounds = 0
        last_scroll: dict[str, Any] = {}
        for _ in range(36):
            read_rounds += 1
            payload = await session.eval(_reader_js(platform), sid, timeout=20.0)
            if isinstance(payload, Mapping):
                page_url = str(payload.get("url") or "")
                if not _profile_url_reached(page_url, profile_url, platform):
                    return {
                        "ok": True,
                        "logged_in": True,
                        "items": [],
                        "profile_url": profile_url,
                        "source": "profile_sync",
                        "read_only": True,
                        "reader_version": ACCOUNT_CONTENT_READER_VERSION,
                        "reason_code": "profile_route_changed",
                        "reason": "读取期间页面离开账号主页，已丢弃非本人作品",
                    }
                if platform == "douyin" and payload.get("profile_scope_found") is not True:
                    # 个人作品容器可能还在加载。最多等待约 10 秒；等待期间
                    # 不滚动整页，避免触发推荐区或改变页面状态。
                    scope_missing_rounds += 1
                    if scope_missing_rounds >= 20:
                        return {
                            "ok": True,
                            "logged_in": True,
                            "items": [],
                            "profile_url": profile_url,
                            "source": "profile_sync",
                            "read_only": True,
                            "reader_version": ACCOUNT_CONTENT_READER_VERSION,
                            "diagnostics": {
                                "page_url": page_url,
                                "scope_selector": '[data-e2e="user-post-list"]',
                                "scope_found": False,
                                "read_rounds": read_rounds,
                                "raw_unique": 0,
                                "returned": 0,
                            },
                            "reason": "抖音本人作品区未加载，已拒绝读取热门/推荐内容",
                        }
                    await asyncio.sleep(0.5)
                    continue
                elif platform == "douyin":
                    scope_missing_rounds = 0
            raw = payload.get("items") if isinstance(payload, dict) else []
            before_count = len(all_raw)
            known = {_clean_text(item.get("url")) for item in all_raw if isinstance(item, Mapping)}
            for item in raw if isinstance(raw, list) else []:
                if isinstance(item, Mapping) and _clean_text(item.get("url")) not in known:
                    all_raw.append(dict(item))
                    known.add(_clean_text(item.get("url")))
            if len(all_raw) == before_count:
                stale_rounds += 1
            else:
                stale_rounds = 0
            items = normalize_profile_items(platform, all_raw, limit=limit)
            if len(items) >= max(1, min(500, int(limit or 100))):
                break
            last_scroll = await session.eval(_scroll_js(), sid)
            if isinstance(last_scroll, Mapping) and last_scroll.get("bottom") and stale_rounds >= 1:
                break
            if isinstance(last_scroll, Mapping) and not last_scroll.get("moved") and stale_rounds >= 2:
                break
            await asyncio.sleep(0.85)
        items = normalize_profile_items(platform, all_raw, limit=limit)
        body = ""
        if isinstance(payload, Mapping):
            body = _clean_text(payload.get("body"))
        not_logged_in = bool(re.search(r"登录|扫码登录|请登录", body)) and not bool(items)
        return {
            "ok": True,
            "logged_in": not not_logged_in,
            "items": items,
            "profile_url": profile_url,
            "source": "profile_sync",
            "read_only": True,
            "reader_version": ACCOUNT_CONTENT_READER_VERSION,
            "reason_code": (
                "not_logged_in" if not_logged_in
                else ("profile_read_success" if items else "profile_empty")
            ),
            "diagnostics": {
                "page_url": str(payload.get("url") or "") if isinstance(payload, Mapping) else "",
                "scope_selector": '[data-e2e="user-post-list"]' if platform == "douyin" else "document",
                "scope_found": (
                    bool(payload.get("profile_scope_found"))
                    if platform == "douyin" and isinstance(payload, Mapping)
                    else True
                ),
                "read_rounds": read_rounds,
                "raw_unique": len(all_raw),
                "returned": len(items),
                "last_scroll": dict(last_scroll) if isinstance(last_scroll, Mapping) else {},
            },
            "reason": "" if items else "主页已打开，但当前未找到可读取的作品卡片",
        }
    finally:
        await session.close()


def read_account_contents(bitbrowser, window_id: str, platform: str, *, limit: int = 100) -> dict[str, Any]:
    """读取账号主页作品；只读，不执行任何发布或互动操作。"""
    platform = str(platform or "").strip().lower()
    if platform not in PROFILE_ENTRY_URLS:
        raise ValueError(f"不支持的平台：{platform}")
    window_id = str(window_id or "").strip()
    if not window_id:
        raise ValueError("账号尚未绑定浏览器窗口")
    return _run(_read_async(bitbrowser, window_id, platform, limit))


__all__ = [
    "CONTENT_PATTERNS", "PROFILE_ENTRY_URLS", "normalize_profile_items",
    "select_profile_target",
    "read_account_contents",
]
