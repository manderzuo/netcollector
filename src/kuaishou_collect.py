# -*- coding: utf-8 -*-
"""快手（Kuaishou）网页采集适配器。

快手桌面网页与现有四个平台的差异主要在于：作品详情路由使用
``/short-video/<photoId>``，评论接口是 ``/rest/v/photo/comment/list``，楼中楼
通过 ``/rest/v/photo/comment/sublist`` 懒加载。适配器只旁观浏览器已经发出的
响应、读取页面 DOM 和执行滚动/展开评论的页面动作，不直接调用写接口。

当前窗口未登录时，搜索/评论阶段会抛出 ``HumanBlock('login')``，交给调度器
显示“待人工验证/登录”，不会把登录弹窗当成采集完成。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import urllib.parse
from collections.abc import Mapping
from datetime import datetime

try:
    from .dy_collect import SearchVideosResult
    from .kuaishou_comment_fields import (
        extract_kuaishou_time,
        normalize_kuaishou_dom_fields,
    )
except ImportError:  # pragma: no cover
    from dy_collect import SearchVideosResult  # type: ignore
    from kuaishou_comment_fields import (  # type: ignore
        extract_kuaishou_time,
        normalize_kuaishou_dom_fields,
    )


SEARCH_URL_PREFIX = "https://www.kuaishou.com/search/video?searchKey="


class HumanBlock(Exception):
    """需要人工处理的登录、验证码或风控状态。"""

    def __init__(self, reason: str, message: str | None = None):
        self.reason = str(reason or "unknown")
        super().__init__(str(message or self.reason))


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _trace_emit(c, event: str, **fields):
    """把平台采集阶段写入 CDP 结构化日志，但不影响业务流程。"""
    trace = getattr(c, "_trace", None)
    emit = getattr(trace, "emit", None)
    if not callable(emit):
        return
    try:
        emit(event, **fields)
    except Exception:
        pass


def _first(mapping: Mapping, *keys):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return ""


def _id_from(value: Mapping) -> str:
    raw = _first(value, "photoId", "photo_id", "photoID", "id", "objectId")
    if isinstance(raw, Mapping):
        raw = _first(raw, "photoId", "photo_id", "id")
    return _clean(raw)


def _user_from(value: Mapping) -> tuple[str, str, str]:
    user = _first(value, "author", "user", "userInfo", "authorInfo", "creator")
    if not isinstance(user, Mapping):
        user = {}
    uid = _clean(_first(user, "id", "userId", "user_id", "profileId", "eid"))
    nickname = _clean(_first(user, "name", "nickname", "userName", "user_name", "userDefineId"))
    avatar = _clean(_first(user, "headUrl", "headurl", "avatar", "avatarUrl", "userHead"))
    return uid, nickname, avatar


def _photo_url(photo_id: str, raw_url: str = "") -> str:
    value = _clean(raw_url)
    if value and "kuaishou.com" in value:
        return value
    return f"https://www.kuaishou.com/short-video/{photo_id}"


def _photo_record(value: Mapping) -> dict | None:
    photo = value.get("photo")
    if isinstance(photo, Mapping):
        source = dict(photo)
        # 搜索结果外层也经常有一个通用 ``id``（可能是 feed/业务对象 ID）。
        # 嵌套 photo 的 ID 和标题优先，不能被外层同名字段覆盖，否则去重和
        # 详情地址会偶发指向错误作品。
        for key in (
            "author", "user", "userInfo", "authorInfo", "creator", "url",
            "photoId", "photo_id", "id", "caption", "title", "content",
        ):
            outer = value.get(key)
            if outer not in (None, "", [], {}) and source.get(key) in (None, "", [], {}):
                source[key] = outer
    else:
        source = value
    photo_id = _id_from(source)
    if not photo_id:
        return None
    # 搜索响应内还有很多业务对象带 id；必须同时出现作品语义字段，
    # 防止把作者/标签/分页游标误当成作品。
    semantic = any(key in source for key in (
        "caption", "title", "content", "photoType", "photo_type", "playUrl",
        "photoUrl", "coverUrl", "duration", "timestamp", "user", "author",
    )) or isinstance(value.get("photo"), Mapping)
    if not semantic:
        return None
    user_id, nickname, avatar = _user_from(source)
    title = _clean(_first(source, "caption", "title", "content", "description", "desc"))
    cover = _clean(_first(source, "coverUrl", "cover_url", "photoUrl", "thumbnailUrl", "headUrl"))
    published = _clean(_first(source, "timestamp", "time", "createTime", "createdAt", "publishTime"))
    try:
        # 快手接口常见时间字段为毫秒时间戳；保持原值，统一展示在后端层处理。
        if published.isdigit() and len(published) >= 10:
            stamp = int(published)
            if stamp > 10**11:
                stamp //= 1000
            published = datetime.fromtimestamp(stamp).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        pass
    kind = _clean(_first(source, "photoType", "photo_type", "type", "contentType")).lower()
    return {
        "vid": photo_id,
        "url": _photo_url(photo_id, _clean(_first(source, "url", "photoUrl"))),
        "title": title[:240] or "未命名作品",
        "author": nickname,
        "author_id": user_id,
        "cover_url": cover,
        "create_time": published,
        "kind": "image" if kind in {"image", "picture", "图文", "2"} else "video",
        "extra": {"avatar": avatar, "source": "kuaishou_search_response"},
    }


def parse_search_payload(payload) -> list[dict]:
    """解析快手搜索响应，按 photoId 去重。

    站点的响应外层字段可能随版本变化，因此只依赖作品对象的语义字段，
    不依赖某一层固定的 ``feeds``/``searchList`` 路径。
    """
    result: list[dict] = []
    seen: set[str] = set()

    def walk(value):
        if isinstance(value, Mapping):
            record = _photo_record(value)
            if record and record["vid"] not in seen:
                seen.add(record["vid"])
                result.append(record)
            for child in value.values():
                if isinstance(child, (Mapping, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return result


def _comment_id(value: Mapping) -> str:
    return _clean(_first(value, "cid", "comment_id", "commentId", "commentID", "id"))


def _comment_text(value: Mapping) -> str:
    raw = _first(
        value, "content", "text", "comment", "message", "commentText",
        "commentContent", "body",
    )
    if isinstance(raw, Mapping):
        raw = _first(raw, "content", "text", "value", "message")
    return _clean(raw)


def _looks_like_comment(value: Mapping) -> bool:
    """判断递归响应中的对象是否像评论，兼容接口外层字段变化。"""
    text = _comment_text(value)
    if not text:
        return False
    identity = any(value.get(key) not in (None, "", [], {}) for key in (
        "cid", "comment_id", "commentId", "commentID", "author_id",
        "authorId", "user_id", "userId", "author_name", "authorName",
        "nickname", "userName", "author", "user", "userInfo",
    ))
    context = any(value.get(key) not in (None, "", [], {}) for key in (
        "createTime", "createdAt", "timestamp", "time", "replyCount",
        "subCommentCount", "rootCommentId", "parentId",
    ))
    return bool(identity or context)


def _comment_record(value: Mapping, parent_id: str = "") -> dict | None:
    comment_id = _comment_id(value)
    text = _comment_text(value)
    author = value.get("author") or value.get("user") or value.get("userInfo")
    if not isinstance(author, Mapping):
        author = {}
    user_id = _clean(_first(value, "author_id", "authorId", "user_id", "userId"))
    user_id = user_id or _clean(_first(author, "id", "userId", "user_id", "eid"))
    nickname = _clean(_first(value, "author_name", "authorName", "nickname", "userName"))
    nickname = nickname or _clean(_first(author, "name", "nickname", "userName", "userDefineId"))
    if not comment_id and not (text or user_id or nickname):
        return None
    if not comment_id:
        # 接口异常缺 ID 时，给上层一个稳定的本地临时键；不会拿它当平台 ID 回复。
        stable = "|".join((user_id, nickname, text))
        comment_id = "auto-" + hashlib.sha1(stable.encode("utf-8", errors="replace")).hexdigest()
    timestamp = _clean(_first(
        value, "timestamp", "time", "createTime", "createdAt", "create_time",
    ))
    return {
        "cid": comment_id,
        "text": text,
        "user_id": user_id,
        "nickname": nickname or "匿名用户",
        "create_time": timestamp,
        "create_time_str": timestamp,
        "digg_count": _first(value, "likeCount", "likedCount", "diggCount", "likes"),
        "parent_id": parent_id or _clean(_first(
            value, "rootCommentId", "root_comment_id", "parentId",
            "parent_id", "replyToCommentId",
        )),
        "reply_total": _first(value, "subCommentCount", "sub_comment_count", "replyCount", "reply_count"),
        "homepage": f"https://www.kuaishou.com/profile/{user_id}" if user_id else "",
        "extra": {"source": "kuaishou_comment_response"},
    }


def parse_comment_payload(payload, *, parent_id: str = "") -> list[dict]:
    """解析一级或楼中楼接口响应并去重。"""
    result: list[dict] = []
    seen: set[str] = set()
    comment_keys = {
        "rootComments", "rootCommentsV2", "rootCommentList", "comments",
        "commentList", "commentInfos", "commentInfo", "commentFeeds",
        "subComments", "subCommentsV2", "subCommentList", "replyList",
        "replies", "list", "items",
    }

    def add(value, inherited_parent=""):
        if not isinstance(value, Mapping):
            return
        if not _looks_like_comment(value):
            return
        record = _comment_record(value, inherited_parent)
        if record and record["cid"] not in seen:
            seen.add(record["cid"])
            result.append(record)

    def walk(value, inherited_parent=""):
        if isinstance(value, Mapping):
            explicit_parent = _clean(_first(value, "rootCommentId", "root_comment_id", "parentId", "parent_id"))
            if _looks_like_comment(value):
                add(value, explicit_parent or inherited_parent)
            for key, child in value.items():
                if key in comment_keys and isinstance(child, list):
                    for item in child:
                        add(item, explicit_parent or inherited_parent)
                        walk(item, explicit_parent or inherited_parent)
                elif isinstance(child, (Mapping, list)):
                    walk(child, explicit_parent or inherited_parent)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    add(item, inherited_parent)
                walk(item, inherited_parent)

    walk(payload, parent_id)
    return result


def _is_comment_response_url(response_url: str) -> bool:
    """兼容快手评论接口的路径变体，只接收评论相关响应。"""
    value = str(response_url or "").strip()
    if not value:
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or "").lower()
        path = urllib.parse.unquote(parsed.path or "").lower()
    except Exception:
        host, path = "", value.lower()
    if host and not (host.endswith("kuaishou.com") or host.endswith("gifshow.com")):
        return False
    return (
        "/rest/v/photo/comment" in path
        or "/rest/v/comment/" in path
        or ("/comment/" in path and "/rest/" in path)
    )


def _log_url_path(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
        return f"{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path
    except Exception:
        return str(value or "").split("?", 1)[0]


def _blocked_probe_js() -> str:
    return r'''(() => {
      const visible = el => { if (!el) return false; const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
        return r.width>2 && r.height>2 && s.display!=='none' && s.visibility!=='hidden' && Number(s.opacity||1)>0; };
      const body = String(document.body?.innerText || '').replace(/\s+/g,' ');
      const frames = [...document.querySelectorAll('iframe')].filter(visible).map(x=>x.src||'').join(' ');
      // 推荐页可能包含“验证码”相关的预加载文案或普通 verify 样式，
      // 不能仅凭一个宽泛词语就冻结任务。只有明确的验证文案、验证 iframe
      // 或尺寸足够的可见验证容器才算人工验证。
      const challengeText = /滑块|拖动验证|滑动验证|安全验证|请完成验证|人机验证|验证失败|验证码\s*(?:错误|不正确|过期|输入|校验)/.test(body);
      const challengeNode = [...document.querySelectorAll(
        '[class*="captcha"],[id*="captcha"],[class*="challenge"],[id*="challenge"],'
        + '[class*="secsdk"],[id*="secsdk"],[class*="security"],[id*="security"],'
        + '[class*="verify"],[id*="verify"]'
      )].some(el => {
        if (!visible(el)) return false;
        const r = el.getBoundingClientRect();
        return r.width >= 120 && r.height >= 50;
      });
      const challenge = challengeText
        || challengeNode
        || /(captcha|verify|challenge|secsdk|security)/i.test(frames)
        ;
      const login = [...document.querySelectorAll('.login-popup,.login-modal,[class*="login-modal"],[class*="login-popup"],.sidebar-login-button')].some(visible)
        || /登录后看更多|请登录|扫码登录|登录后查看/.test(body);
      const commentNodes = document.querySelectorAll('.comment-side,.comment-list,.comment-root,[class*="comment"]').length;
      return {challenge, login, commentNodes, url:location.href, text:body.slice(0,1200)};
    })()'''


async def probe_blocked(c, sid):
    state = await c.eval(_blocked_probe_js(), sid) or {}
    if state.get("challenge"):
        return "验证码"
    if state.get("login"):
        return "需登录"
    return None


_SEARCH_DOM_JS = r'''(() => {
  const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
  const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>3&&r.height>3&&r.bottom>=0&&r.top<=innerHeight&&s.display!=='none'&&s.visibility!=='hidden'};
  const abs=h=>{try{return new URL(h||'',location.href).href}catch(_){return ''}};
  const re=/\/short-video\/([\w-]+)|\/video\/([\w-]+)|\/movie\/video\/([\w-]+)|\/featured\/([\w-]+)/i;
  const anchors=[...document.querySelectorAll('a[href]')].filter(visible);
  const seen=new Set(), items=[];
  for(const a of anchors){
    const href=abs(a.href), m=href.match(re); if(!m) continue;
    const id=(m[1]||m[2]||m[3]||m[4]||''); if(!id||seen.has(id)) continue;
    let card=a; for(let i=0;i<6&&card.parentElement;i++,card=card.parentElement){
      const t=clean(card.innerText||''); if(t.length>=8&&t.length<1800&&(card.querySelector('img,video')||/video|photo|search|card/i.test(String(card.className||'')))) break;
    }
    const text=clean(card.innerText||a.innerText||'');
    const author=clean(card.querySelector('.name,[class*="author"],[class*="user"]')?.innerText||'').replace(/^@/,'');
    const title=clean(card.querySelector('.caption,[class*="title"],[class*="desc"]')?.innerText||a.getAttribute('title')||a.getAttribute('aria-label')||text).slice(0,240);
    const time=clean(card.querySelector('.timestamp,time,[class*="time"]')?.innerText||'');
    const image=card.querySelector('img');
    items.push({vid:id,url:href,title,author,create_time:time,kind:'video',cover_url:image?.currentSrc||image?.src||'',extra:{source:'kuaishou_dom'}}); seen.add(id);
  }
  return {items,url:location.href,body:clean(document.body?.innerText||'').slice(0,3000)};
})()'''


_SEARCH_STATE_JS = r'''(() => {
  const text=String(document.body?.innerText||'').replace(/\s+/g,' ');
  const ids=new Set();
  for(const a of document.querySelectorAll('a[href]')) { const m=(a.href||'').match(/\/(?:short-video|video|movie\/video|featured)\/([\w-]+)/i); if(m) ids.add(m[1]); }
  const root=document.scrollingElement||document.documentElement;
  const top=Number(root.scrollTop||window.scrollY||0), height=Math.max(Number(root.scrollHeight||0),Number(document.body?.scrollHeight||0)), viewport=Number(root.clientHeight||innerHeight||0);
  const end=text.match(/暂时没有更多了|没有更多|暂无更多|到底了|已显示全部/)||[];
  return {visible:ids.size,top,height,viewport,atBottom:top+viewport>=height-24,endText:!!end[0],endMessage:end[0]||''};
})()'''


_OPEN_COMMENT_JS = r'''(() => {
  const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
  const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>2&&r.height>2&&s.display!=='none'&&s.visibility!=='hidden'};
  const panelSelectors='.comment-container,.vertical-comment,.comment-panel,.comment-side,.comment-list,[class*="comment-container"],[class*="commentPanel"],[class*="comment-panel"]';
  const panels=[...document.querySelectorAll(panelSelectors)].filter(visible);
  const openPanel=panels.find(el=>{
    const r=el.getBoundingClientRect();
    return r.width>=180&&r.height>=100;
  });
  // 有些快手页面进入作品页时评论面板已经挂载，此时不能再次点击面板本身。
  if(openPanel){
    return {ok:true,already_open:true,text:clean(openPanel.innerText||'').slice(0,120),
      x:openPanel.getBoundingClientRect().left+openPanel.getBoundingClientRect().width/2,
      y:openPanel.getBoundingClientRect().top+24,cls:String(openPanel.className||'')};
  }
  const candidates=[...document.querySelectorAll(
    '.tab-item,[role="tab"],button,[role="button"],input[type="button"],'
    + '[aria-label],[title],[data-e2e],[data-testid],.comment,.photo-btns .comment,'
    + '.comment-btn,.comment-button,[class*="comment-btn"],[class*="commentBtn"],'
    + '[class*="comment-icon"],[class*="commentIcon"],[class~="comment"]'
  )].filter(visible).map(el=>{
    const t=clean(el.innerText||el.textContent).slice(0,80);
    const a=clean((el.getAttribute('aria-label')||'')+' '+(el.getAttribute('title')||''));
    const d=clean((el.getAttribute('data-e2e')||'')+' '+(el.getAttribute('data-testid')||''));
    const cls=String(el.className||'');
    const r=el.getBoundingClientRect();
    return {el,t,a,d,cls,r,meta:`${t} ${a} ${d} ${cls}`};
  }).filter(x=>{
    const meta=x.meta;
    const textMatch=/^(评论|评论区)(?:\s*[（(]?\s*\d+(?:\.\d+)?\s*[万kK]?\s*[）)]?)?$/.test(x.t)
      || /(?:评论|comment)\s*\d/i.test(x.t);
    const semanticMatch=/(评论|comment)/i.test(meta);
    const actionHint=/(btn|button|icon|action|toolbar|operate|operation|photo-btn|tab)/i.test(meta);
    const containerHint=/(container|panel|list|side|maincontent|row|item|content)/i.test(x.cls)
      && !/(btn|button|icon)/i.test(x.cls);
    return !containerHint && (textMatch || (semanticMatch && actionHint));
  });
  candidates.sort((a,b)=>{
    const score=x=>(/^(评论|评论区)/.test(x.t)?100:0)
      + (/(aria-label|title|data-e2e|data-testid)/i.test(x.meta)?30:0)
      + (/(btn|button|icon|action|toolbar)/i.test(x.meta)?20:0);
    return score(b)-score(a)||(a.r.width*a.r.height)-(b.r.width*b.r.height);
  });
  const hit=candidates[0];
  if(!hit){
    const diagnostic=[...document.querySelectorAll('button,[role="button"],[aria-label],[title],[data-e2e],[data-testid]')]
      .filter(visible).slice(0,40).map(el=>({text:clean(el.innerText||el.textContent).slice(0,40),
        aria:clean(el.getAttribute('aria-label')||''),title:clean(el.getAttribute('title')||''),
        cls:String(el.className||'').slice(0,100)}));
    return {ok:false,reason:'comment_button_not_found',candidates:0,diagnostic};
  }
  hit.el.scrollIntoView?.({block:'center',inline:'nearest'}); hit.el.focus?.(); hit.el.click();
  const r=hit.el.getBoundingClientRect(); return {ok:true,text:hit.t||hit.a,x:r.left+r.width/2,y:r.top+r.height/2,cls:String(hit.el.className||'')};
})()'''


_COMMENT_SCROLL_JS = r'''(() => {
  const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>3&&r.height>3&&r.bottom>=0&&r.top<=innerHeight&&s.display!=='none'&&s.visibility!=='hidden'};
  const candidates=[...document.querySelectorAll('.comment-list,.comment-side,.comment-mainContent,.comment-container,[class*="comment-list"],[class*="comment-side"],[class*="comment-scroller"]')]
    .filter(e=>visible(e)&&Number(e.scrollHeight||0)-Number(e.clientHeight||0)>20);
  candidates.sort((a,b)=>(b.scrollHeight-b.clientHeight)-(a.scrollHeight-a.clientHeight));
  const root=candidates[0], before=Number(root?.scrollTop||0);
  if(root){root.focus?.();root.scrollBy(0,Math.max(360,Math.floor((root.clientHeight||innerHeight)*.84)));}
  else {const sc=document.scrollingElement||document.documentElement;window.scrollBy(0,Math.max(500,Math.floor(innerHeight*.8)));}
  const after=Number(root?.scrollTop||window.scrollY||0), height=Number(root?.scrollHeight||document.documentElement.scrollHeight||0), viewport=Number(root?.clientHeight||innerHeight||0);
  const scope=root||document.body, text=String(scope?.innerText||'').replace(/\s+/g,' '), end=text.match(/暂时没有更多了|没有更多|暂无更多|到底了|已显示全部/)||[];
  return {found:!!root,before,after,moved:after>before+2,height,viewport,bottom:after+viewport>=height-18,endText:!!end[0],endMessage:end[0]||''};
})()'''


_EXPAND_SUBCOMMENTS_JS = r'''(() => {
  const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>2&&r.height>2&&r.bottom>=0&&r.top<=innerHeight&&s.display!=='none'&&s.visibility!=='hidden'};
  const nodes=[...document.querySelectorAll('.comment-root span.expand,.comment-root .expand,[class*="comment"] span.expand,[class*="comment"] .expand')].filter(visible);
  let clicked=0; for(const el of nodes.slice(0,30)){el.scrollIntoView?.({block:'center',inline:'nearest'});el.click();clicked++;}
  return {candidates:nodes.length,clicked};
})()'''


_COMMENT_DOM_JS = r'''(() => {
  const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
  const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>3&&r.height>3&&r.bottom>=0&&r.top<=innerHeight&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)>0;};
  const cls=e=>String(e?.className||'');
  const all=[...document.querySelectorAll(
    '.comment-list,.comment-side,.comment-container,.comment-mainContent,'
    + '[data-testid*="comment"],[data-e2e*="comment"],[class*="comment-list"],'
    + '[class*="commentList"],[class*="comment-side"],[class*="commentSide"],'
    + '[class*="comment-container"],[class*="commentContainer"]'
  )].filter(visible);
  const scrollable=all.filter(e=>Number(e.scrollHeight||0)-Number(e.clientHeight||0)>20);
  const panel=(scrollable.length?scrollable:all).sort((a,b)=>{
    const score=e=>(Number(e.scrollHeight||0)-Number(e.clientHeight||0))*10+(e.innerText||'').length;
    return score(b)-score(a);
  })[0]||null;
  const scope=panel||document.body;
  const rowSelectors=[
    '[data-testid*="comment-item"]','[data-e2e*="comment-item"]',
    '.comment-item,.commentItem,[class*="comment-item"],[class*="commentItem"]',
    '[class*="comment-row"],[class*="commentRow"]'
  ];
  let rows=[...new Set(rowSelectors.flatMap(s=>[...scope.querySelectorAll(s)]))].filter(visible);
  // 快手样式更新时可能只保留 comment 语义 class，按“最小可见评论块”兜底。
  if(!rows.length){
    rows=[...scope.querySelectorAll('[class*="comment"]')].filter(e=>{
      const t=clean(e.innerText||e.textContent);
      return visible(e)&&t.length>=2&&t.length<=800&&e.children.length>=1;
    }).filter(e=>![...e.children].some(ch=>{
      const t=clean(ch.innerText||ch.textContent); return t.length>=2&&t.length<=800;
    }));
  }
  const getAttr=(e,names)=>{for(const n of names){const v=e?.getAttribute?.(n);if(v)return clean(v);}return '';};
  const linesOf=e=>String(e?.innerText||e?.textContent||'')
    .split(/\r?\n/).map(x=>clean(x)).filter(Boolean);
  const findLines=(root,selectors)=>{
    for(const s of selectors){
      if(!s||!String(s).trim())continue;
      let el=null;
      try{el=root.querySelector(s);}catch(_){continue;}
      const lines=linesOf(el);if(lines.length)return lines;
    }
    return [];
  };
  const timeRe=/(?:\d{4}[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}(?:日)?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?|\d{1,2}[-/.月]\s*\d{1,2}(?:日)?|刚刚|昨天|今天|前天|\d+\s*(?:秒|分钟|小时|天|周|月|年)前)/;
  const timeFrom=value=>{const m=clean(value).match(timeRe);return m?clean(m[0]):'';};
  const stripTime=value=>clean(clean(value).replace(timeRe,' '));
  const timeOnly=value=>{const s=clean(value),t=timeFrom(s);return !!t&&!stripTime(s);};
  const actionRe=/^(赞|回复|分享|举报|删除|更多|展开|收起|作者|作者回复)(?:\s*\d+)?$/;
  const noise=line=>{const t=clean(line);return !t||timeOnly(t)||actionRe.test(t)||/^\d+(?:\.\d+)?(?:万)?$/.test(t);};
  const firstName=values=>{
    for(const value of values){
      for(const line of linesOf({innerText:String(value||'')})){
        if(!noise(line))return stripTime(line);
      }
    }
    return '';
  };
  const firstContent=(values,nickname)=>{
    for(const value of values){
      for(const line of linesOf({innerText:String(value||'')})){
        if(noise(line))continue;
        let body=stripTime(line);
        if(!body||body===nickname)continue;
        if(nickname&&body.indexOf(nickname+' ')===0)body=clean(body.slice(nickname.length));
        if(body&&body!==nickname)return body;
      }
    }
    return '';
  };
  const mediaMarker=row=>{
    const nodes=[...row.querySelectorAll('img,video,[class*="emoji"],[class*="Emoji"],[class*="sticker"],[class*="expression"]')];
    for(const el of nodes){
      const hint=clean(el.getAttribute?.('alt')||el.getAttribute?.('title')||el.getAttribute?.('aria-label')||cls(el));
      if(/头像|avatar|用户/.test(hint))continue;
      if(/表情|图片|emoji|sticker|expression|gif/i.test(hint))return /表情|emoji|sticker|expression|gif/i.test(hint)?'[表情]':'[图片]';
    }
    return '';
  };
  const out=[],seen=new Set();
  for(const row of rows){
    const link=row.querySelector('a[href*="/profile/"],a[href*="/user/"]');
    const href=link?.href||'';
    const uid=(href.match(/\/(?:profile|user)\/([^/?#]+)/i)||[])[1]||'';
    const nameValues=[...(link?[link.innerText||link.textContent]:[]),...findLines(row,[
      '[class*="author"],[class*="Author"],[class*="user-name"],[class*="userName"],'
      + '[class*="nickname"],[class*="nickName"]'
    ]),getAttr(row,['data-user-name','data-nickname'])];
    const nickname=firstName(nameValues);
    const cid=getAttr(row,['data-comment-id','data-commentid','data-cid','data-id']);
    const timeText=timeFrom(findLines(row,['time','[class*="time"],[class*="Time"],[class*="date"],[class*="Date"]']).join(' '))
      || linesOf(row).map(timeFrom).find(Boolean)||'';
    const contentValues=[...findLines(row,[
      '.comment-content,.commentContent,.comment-text,.commentText,.comment-mainContent',
      '[class*="content"],[class*="Content"],[data-testid*="content"]'
    ]),...linesOf(row)];
    let content=firstContent(contentValues,nickname)||mediaMarker(row);
    if(!content||content===nickname||content.length>1000)continue;
    const key=(cid||uid+'|'+nickname+'|'+content).slice(0,500);
    if(seen.has(key))continue;seen.add(key);
    out.push({cid:cid||'',text:content.slice(0,500),user_id:uid,nickname:nickname||'匿名用户',
      create_time_str:timeText,parent_id:getAttr(row,['data-root-comment-id','data-parent-id']),
      extra:{source:'kuaishou_comment_dom'}});
  }
  return {comments:out,panelFound:!!panel,rowCount:rows.length,panelClass:cls(panel).slice(0,160)};
})()'''


_COMMENT_INITIAL_WAIT_SECONDS = 8.0
_COMMENT_SETTLE_SECONDS = 1.5


def _cancelled(pause_event, cancel_event) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


async def _wait_pause(pause_event, cancel_event):
    while pause_event is not None and not pause_event.is_set():
        if _cancelled(pause_event, cancel_event):
            return False
        await asyncio.sleep(0.2)
    return not _cancelled(pause_event, cancel_event)


async def search_videos(c, sid, keyword, *, mode="standard", target_count=None,
                        pause_event=None, cancel_event=None, search_sort="default"):
    """搜索快手作品，直到达到目标或确认页面没有更多内容。"""
    target = max(1, int(target_count or {"fast": 100, "deep": 10000}.get(mode, 500)))
    responses: list[tuple[str, str]] = []
    parsed_ids: set[str] = set()
    all_items: dict[str, dict] = {}
    stop = c.on("Network.responseReceived", lambda p: responses.append((
        str((p.get("response") or {}).get("url") or ""), str(p.get("requestId") or "")
    )))
    try:
        await c.cmd("Network.enable", {}, session_id=sid)
        url = SEARCH_URL_PREFIX + urllib.parse.quote(str(keyword or ""))
        # _PlatformCtx 建立连接后会先进入快手推荐页。推荐页不是任务的搜索页，
        # 不能在这里先做验证码判断，否则推荐页的预加载文案会被误报为 captcha，
        # 任务也就永远不会真正导航到搜索地址。
        await c.navigate(url, sid, wait_load=False)
        await asyncio.sleep(2.0)

        # 页面导航可能被 SPA 延迟或重定向，先确认当前地址已经进入搜索路由。
        # 若仍停留在推荐页，记录真实的导航失败原因，不伪装成人工验证。
        current_url = ""
        search_route_reached = False
        for _ in range(10):
            current_url = _clean(await c.eval("location.href", sid) or "")
            if re.search(r"/search/video(?:[/?#]|$)", current_url, re.I):
                search_route_reached = True
                break
            await asyncio.sleep(0.5)
        after_navigation = await c.eval(_blocked_probe_js(), sid) or {}
        if after_navigation.get("challenge"):
            raise HumanBlock(
                "captcha",
                f"快手搜索页检测到人工验证；当前地址：{current_url or '未知'}",
            )
        if after_navigation.get("login") and not search_route_reached:
            raise HumanBlock(
                "login",
                f"快手搜索页需要先登录；当前地址：{current_url or '未知'}",
            )
        if not search_route_reached:
            raise RuntimeError(
                "快手搜索页导航未生效，当前仍未进入搜索路由；"
                f"当前地址：{current_url or '未知'}"
            )

        async def absorb_responses():
            for response_url, request_id in list(responses):
                if request_id in parsed_ids or "/rest/v/search/feed" not in response_url:
                    continue
                try:
                    body = await c.get_body(request_id, sid, timeout=15.0)
                    payload = json.loads(body)
                    items = parse_search_payload(payload)
                except Exception:
                    continue
                for item in items:
                    all_items.setdefault(item["vid"], item)
                parsed_ids.add(request_id)

        await absorb_responses()
        dom = await c.eval(_SEARCH_DOM_JS, sid) or {}
        for item in dom.get("items") or []:
            if isinstance(item, Mapping) and item.get("vid"):
                all_items.setdefault(str(item["vid"]), dict(item))
        page_state = await c.eval(_SEARCH_STATE_JS, sid) or {}
        login_state = await c.eval(_blocked_probe_js(), sid) or {}
        if login_state.get("login") and not all_items:
            raise HumanBlock("login", "快手搜索页需要先登录")

        max_rounds = max(20, min(800, target // 8 + 100))
        stale_rounds = 0
        bottom_stale_rounds = 0
        last_height = -1
        last_top = -1
        rounds = 0
        reached_target = len(all_items) >= target
        no_more = bool(page_state.get("endText"))
        termination = "达到目标数量" if reached_target else (f"页面提示：{page_state.get('endMessage')}" if no_more else "")
        while not reached_target and not no_more and rounds < max_rounds:
            if not await _wait_pause(pause_event, cancel_event):
                break
            rounds += 1
            before_count = len(all_items)
            before_visible = int(page_state.get("visible") or 0)
            await c.eval("window.scrollBy(0, Math.max(650, Math.floor(window.innerHeight * .82)))", sid)
            started = time.monotonic()
            while time.monotonic() - started < 10.0:
                await asyncio.sleep(0.5)
                await absorb_responses()
                dom = await c.eval(_SEARCH_DOM_JS, sid) or {}
                for item in dom.get("items") or []:
                    if isinstance(item, Mapping) and item.get("vid"):
                        all_items.setdefault(str(item["vid"]), dict(item))
                state = await c.eval(_SEARCH_STATE_JS, sid) or {}
                if len(all_items) > before_count or int(state.get("visible") or 0) > before_visible or state.get("endText"):
                    page_state = state
                    break
                page_state = state
            blocked = await c.eval(_blocked_probe_js(), sid) or {}
            if blocked.get("challenge"):
                raise HumanBlock("captcha", "快手搜索过程中出现人工验证")
            await absorb_responses()
            state = await c.eval(_SEARCH_STATE_JS, sid) or page_state
            candidate_count = len(all_items)
            progress = candidate_count > before_count
            stale_rounds = 0 if progress else stale_rounds + 1
            at_bottom = bool(state.get("atBottom"))
            height, top = int(state.get("height") or 0), int(state.get("top") or 0)
            if at_bottom and not progress and height == last_height and top == last_top:
                bottom_stale_rounds += 1
            elif at_bottom and not progress:
                bottom_stale_rounds += 1
            else:
                bottom_stale_rounds = 0
            if candidate_count >= target:
                reached_target, termination = True, "达到目标数量"
                break
            if state.get("endText") and not progress:
                no_more, termination = True, f"页面提示：{state.get('endMessage') or '暂无更多内容'}"
                break
            if bottom_stale_rounds >= 8:
                no_more, termination = True, "已滚动到底部且连续无新增作品"
                break
            last_height, last_top = height, top
        final_block = await c.eval(_blocked_probe_js(), sid) or {}
        if final_block.get("challenge"):
            raise HumanBlock("captcha", "快手搜索结束时存在人工验证")
        return SearchVideosResult(
            list(all_items.values())[:target],
            search_complete=bool(reached_target or no_more),
            reached_target=reached_target,
            no_more_results=no_more,
            rounds=rounds,
            termination_reason=termination,
        )
    finally:
        try:
            stop()
        except Exception:
            pass


async def fetch_comments(c, sid, vid_url, *, quiet=4, max_work=300,
                         pause_event=None, cancel_event=None):
    """打开快手作品并读取一级评论与可展开的楼中楼（包括
    ``/rest/v/photo/comment/list``、``/rest/v/photo/comment/sublist`` 等接口）。

    评论加载是异步的：点击评论按钮只负责挂载面板，接口请求通常还会在
    后续几个事件循环中才发出。因此这里同时旁观网络响应和读取已渲染 DOM，
    并在首次点击后给页面一个明确的加载窗口，不能用一次空扫描判定“没有评论”。
    """
    url = str(vid_url or "").strip()
    if not url:
        raise ValueError("快手作品地址不能为空")
    responses: list[tuple[str, str]] = []
    parsed_ids: set[str] = set()
    response_attempts: dict[str, int] = {}
    all_comments: dict[str, dict] = {}
    stats = {
        "response_seen": 0,
        "response_matched": 0,
        "response_body_errors": 0,
        "response_json_errors": 0,
        "response_items": 0,
        "dom_snapshots": 0,
        "dom_rows": 0,
        "dom_items": 0,
    }
    stop = c.on("Network.responseReceived", lambda p: responses.append((
        str((p.get("response") or {}).get("url") or ""), str(p.get("requestId") or "")
    )))
    try:
        _trace_emit(c, "kuaishou_comment_scan_started", url=_log_url_path(url), session_id=sid)
        await c.cmd("Network.enable", {}, session_id=sid)
        await c.navigate(url, sid, wait_load=False)
        await asyncio.sleep(2.5)
        blocked = await c.eval(_blocked_probe_js(), sid) or {}
        if blocked.get("challenge"):
            raise HumanBlock("captcha", "快手作品页出现人工验证")
        opened = await c.eval(_OPEN_COMMENT_JS, sid) or {}
        _trace_emit(c, "kuaishou_comment_panel_opened", opened=opened)
        if not opened.get("ok"):
            # 未登录时评论面板不会挂载；让调度器显示真实原因，不误报评论为空。
            if blocked.get("login"):
                raise HumanBlock("login", "快手评论区需要先登录")
            # 评论按钮没有找到时不能继续把该作品标记为已完成。否则本次
            # 采集会静默得到 0 条评论，恢复任务也不会再重试这个作品。
            raise RuntimeError(
                "快手评论区入口未定位到，未执行评论采集；请查看 cdp.jsonl 中的 "
                "kuaishou_comment_panel_opened 诊断"
            )

        async def absorb_responses():
            changed = False
            for response_url, request_id in list(responses):
                if not request_id:
                    continue
                if request_id in parsed_ids:
                    continue
                stats["response_seen"] += 1
                if not _is_comment_response_url(response_url):
                    continue
                stats["response_matched"] += 1
                attempts = response_attempts.get(request_id, 0)
                if attempts >= 3:
                    parsed_ids.add(request_id)
                    continue
                response_attempts[request_id] = attempts + 1
                try:
                    body = await c.get_body(request_id, sid, timeout=15.0)
                except Exception as exc:
                    stats["response_body_errors"] += 1
                    _trace_emit(c, "kuaishou_comment_response_body_error",
                                request_id=request_id, url=_log_url_path(response_url),
                                attempt=attempts + 1, error=f"{type(exc).__name__}: {exc}")
                    continue
                try:
                    payload = json.loads(body)
                except Exception as exc:
                    stats["response_json_errors"] += 1
                    parsed_ids.add(request_id)
                    _trace_emit(c, "kuaishou_comment_response_json_error",
                                request_id=request_id, url=_log_url_path(response_url),
                                body_preview=str(body or "")[:240],
                                error=f"{type(exc).__name__}: {exc}")
                    continue
                try:
                    parent_id = ""
                    match = re.search(r"rootCommentId=([^&]+)", response_url)
                    if match:
                        parent_id = urllib.parse.unquote(match.group(1))
                    items = parse_comment_payload(payload, parent_id=parent_id)
                except Exception as exc:
                    _trace_emit(c, "kuaishou_comment_response_parse_error",
                                request_id=request_id, url=_log_url_path(response_url),
                                error=f"{type(exc).__name__}: {exc}")
                    continue
                stats["response_items"] += len(items)
                for item in items:
                    if item["cid"] not in all_comments:
                        all_comments[item["cid"]] = item
                        changed = True
                parsed_ids.add(request_id)
                _trace_emit(c, "kuaishou_comment_response_parsed",
                            request_id=request_id, url=_log_url_path(response_url),
                            items=len(items), total=len(all_comments))
            return changed

        def absorb_dom_snapshot(snapshot):
            changed = False
            if not isinstance(snapshot, Mapping):
                return changed
            rows = int(snapshot.get("rowCount") or 0)
            comments = snapshot.get("comments") or []
            stats["dom_snapshots"] += 1
            stats["dom_rows"] = max(stats["dom_rows"], rows)
            for value in comments:
                if not isinstance(value, Mapping):
                    continue
                nickname, text, comment_time = normalize_kuaishou_dom_fields(
                    value.get("nickname"),
                    value.get("text"),
                    value.get("create_time_str"),
                )
                if not text:
                    _trace_emit(c, "kuaishou_comment_dom_skip",
                                reason="empty_or_ui_metadata",
                                nickname=nickname[:80],
                                raw_text=_clean(value.get("text"))[:160],
                                raw_time=_clean(value.get("create_time_str"))[:80])
                    continue
                cid = _clean(value.get("cid"))
                user_id = _clean(value.get("user_id"))
                nickname = nickname or "匿名用户"
                if not cid:
                    stable = "|".join((user_id, nickname, text, comment_time))
                    cid = "dom-" + hashlib.sha1(stable.encode("utf-8", errors="replace")).hexdigest()
                item = {
                    "cid": cid,
                    "text": text,
                    "user_id": user_id,
                    "nickname": nickname,
                    "create_time": comment_time,
                    "create_time_str": comment_time,
                    "digg_count": "",
                    "parent_id": _clean(value.get("parent_id")),
                    "reply_total": "",
                    "homepage": f"https://www.kuaishou.com/profile/{user_id}" if user_id else "",
                    "extra": {"source": "kuaishou_comment_dom"},
                }
                stats["dom_items"] += 1
                if cid not in all_comments:
                    all_comments[cid] = item
                    changed = True
            _trace_emit(c, "kuaishou_comment_dom_snapshot",
                        panel_found=bool(snapshot.get("panelFound")), rows=rows,
                        extracted=len(comments), total=len(all_comments),
                        panel_class=_clean(snapshot.get("panelClass"))[:160])
            return changed

        async def absorb_dom():
            try:
                return absorb_dom_snapshot(await c.eval(_COMMENT_DOM_JS, sid) or {})
            except Exception as exc:
                _trace_emit(c, "kuaishou_comment_dom_error",
                            error=f"{type(exc).__name__}: {exc}")
                return False

        async def wait_for_initial_comments():
            """等待评论按钮触发的首个请求/DOM 挂载，避免空扫描提前结束。"""
            started = time.monotonic()
            last_new = started
            while time.monotonic() - started < _COMMENT_INITIAL_WAIT_SECONDS:
                changed = await absorb_responses()
                changed = await absorb_dom() or changed
                if changed:
                    last_new = time.monotonic()
                # 有数据后再稳定一小段时间；无数据至少等待 2 秒，让懒加载请求发出。
                min_wait = 2.0 if not all_comments else 0.5
                if time.monotonic() - started >= min_wait and time.monotonic() - last_new >= _COMMENT_SETTLE_SECONDS:
                    break
                await asyncio.sleep(0.3)

        await wait_for_initial_comments()

        deadline = time.monotonic() + max(20.0, float(max_work or 300))
        stale_rounds = 0
        last_new = time.monotonic()
        while time.monotonic() < deadline:
            if not await _wait_pause(pause_event, cancel_event):
                return list(all_comments.values())
            changed = await absorb_responses()
            expanded = await c.eval(_EXPAND_SUBCOMMENTS_JS, sid) or {}
            if expanded.get("clicked", 0):
                # 点击展开楼中楼后，接口请求不是同步完成的。
                await asyncio.sleep(0.35)
            scroll = await c.eval(_COMMENT_SCROLL_JS, sid) or {}
            changed = await absorb_responses() or changed
            changed = await absorb_dom() or changed
            if changed:
                stale_rounds = 0
                last_new = time.monotonic()
            else:
                stale_rounds += 1
            blocked = await c.eval(_blocked_probe_js(), sid) or {}
            if blocked.get("challenge"):
                raise HumanBlock("captcha", "快手评论采集中出现人工验证")
            if blocked.get("login") and not all_comments and not scroll.get("found"):
                raise HumanBlock("login", "快手评论区需要先登录")
            if scroll.get("endText") and not changed and expanded.get("clicked", 0) == 0:
                break
            if scroll.get("bottom") and stale_rounds >= 5:
                break
            if stale_rounds >= 8 and time.monotonic() - last_new >= max(2.0, float(quiet or 4)):
                break
            await asyncio.sleep(0.6)
        await absorb_responses()
        await absorb_dom()
        # 最后一轮网络响应可能恰好在滚动结束后到达，再留一个短窗口，
        # 但不改变“明确到底/无更多”时的正常结束行为。
        if not all_comments and time.monotonic() < deadline:
            end = time.monotonic() + 2.0
            while time.monotonic() < end:
                changed = await absorb_responses()
                changed = await absorb_dom() or changed
                if changed:
                    break
                await asyncio.sleep(0.25)
        _trace_emit(c, "kuaishou_comment_scan_finished", total=len(all_comments), stats=stats)
        return list(all_comments.values())
    finally:
        try:
            stop()
        except Exception:
            pass


__all__ = [
    "HumanBlock", "SearchVideosResult", "parse_search_payload",
    "parse_comment_payload", "probe_blocked", "search_videos", "fetch_comments",
    "SEARCH_URL_PREFIX",
]
