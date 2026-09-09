#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""account_reader.py — 从已打开的平台页面读取当前登录账号信息。

核心目的：账号管理页显示「平台账号昵称 + 用户ID」，方便区分是哪个账号。

每个平台一个读取函数，输入 window 的 ws，返回 {"nick", "uid", "sec_uid", "logged_in"}。
登录态识别 + 账号信息提取都在这层做，GUI 只负责展示。

注意：JS 字符串用单引号包裹、内部双引号转义，避免 querySelector 选择器语法错误。
"""

import asyncio
import json
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_IDENTITY_LABELS = re.compile(
    r"^(?:登录|注册|我的|关注|粉丝|获赞|简介|作品|收藏|喜欢|消息|设置|编辑资料)$"
)
_ACCOUNT_KEY_PATTERN = re.compile(r"^(?:\d{5,}|[A-Za-z0-9_-]{8,})$")


def looks_like_account_key(value, *, window_id: str = "") -> bool:
    """判断旧版 ``accounts.name`` 是否更像内部键/窗口标识而非昵称。

    旧版本允许把平台 UID、窗口名直接写入 ``name``，例如
    ``1234567899``、``12345678a`` 或一串 profile ID。它们仍需保留用于
    任务绑定，但不能继续作为账号昵称展示。
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(
        r"^(?:抖音号|账号|用户名|用户ID|UID)\s*[:：]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    if not text:
        return True
    bound_window = re.sub(r"\s+", " ", str(window_id or "")).strip()
    if bound_window and text == bound_window:
        return True
    return bool(_ACCOUNT_KEY_PATTERN.fullmatch(text))


def _clean_nickname(value, *, allow_numeric: bool = False) -> str:
    """清理页面文本，只保留可作为展示昵称的短文本。

    ``allow_numeric`` 只给平台已经明确标记为“昵称”的元素使用。抖音等
    平台允许用户把纯数字设置为昵称；普通兜底文本仍然拒绝纯数字，避免
    把 UID、窗口 ID 或作品编号显示成昵称。
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip().lstrip("@")
    if not text or len(text) > 80 or _IDENTITY_LABELS.fullmatch(text):
        return ""
    # 平台用户 ID 常常是纯数字；它应放到 uid，不应冒充昵称。
    if not allow_numeric and text.isdigit() and len(text) >= 5:
        return ""
    return text


def _normalise_expected_uid(platform: str, value) -> str:
    """返回可用于校验页面账号归属的已知平台 ID。

    ``accounts.name`` 在旧版本中同时承担了“内部账号名”和“平台 ID”两种
    角色，不能把所有值都拼进 URL。这里只接受三个页面读取器能确认的
    平台 ID 格式；抖音和快手的旧账号名继续只作为内部标识使用。
    """
    text = re.sub(r"\s+", "", str(value or "")).strip()
    key = str(platform or "").strip().lower()
    if key == "weibo" and re.fullmatch(r"\d{5,}", text):
        return text
    if key == "xhs" and re.fullmatch(r"[0-9a-fA-F]{20,}", text):
        return text
    if key == "bilibili" and re.fullmatch(r"\d{3,}", text):
        return text
    return ""


def _connect(ws_url):
    try:
        from .cdp import CdpSession
    except ImportError:
        from cdp import CdpSession
    return CdpSession(ws_url)


def read_douyin(ws_url, timeout=25, expected_uid=None):
    """从抖音页面读当前登录账号昵称。

    方式（用户澄清）：回到个人主页（/user/self），屏幕正中央靠上的用户信息区
    （头像旁边）就是昵称。返回 {"logged_in","nick","sec_uid"}。
    """
    async def _do():
        c = _connect(ws_url)
        await c.connect()
        sid = await c.attach_page()
        try:
            # 先看当前是否已登录（有无 self 入口 / 无登录按钮）
            base = await c.eval('''(function(){
              var t = document.body ? document.body.innerText : '';
              var hasLoginBtn = t.indexOf('\u767b\u5f55\u6309\u94ae') >= 0
                             || t.indexOf('\u4e00\u952e\u767b\u5f55') >= 0;
              var hasSelf = document.querySelectorAll('a[href*="/user/self"]').length > 0;
              return { hasLoginBtn: hasLoginBtn, hasSelf: hasSelf };
            })()''', sid)
            # 导航到个人主页（用户信息中心区）
            try:
                await c.cmd("Page.navigate", {"url": "https://www.douyin.com/user/self?from_nav=1"}, session_id=sid)
                await asyncio.sleep(9)
            except Exception:
                pass
            # 通过语义选择器读取账号信息，不依赖窗口坐标；窗口移动、缩放和
            # 页面响应式布局变化都不会改变定位逻辑。
            acc = await c.eval('''(function(){
              var clean=function(v){return String(v||'').replace(/\\s+/g,' ').trim().replace(/^@/,'');};
              var blocked=/^(登录|注册|我的|关注|粉丝|获赞|简介|作品|收藏|喜欢|消息|设置|编辑资料)$/;
              var nick='';
              var add=function(v){
                var x=clean(v); if(!nick && x && x.length<=80 && !blocked.test(x)) nick=x;
              };
              var selectors=[
                '[data-e2e*="user-name"]','[data-e2e*="nickname"]',
                '[data-e2e="user-info"] h1','[data-e2e="user-info"] h2',
                '[data-e2e="user-info"] span','[class*="user-name"]',
                '[class*="nickname"]','[class*="userInfo"] h1',
                '[class*="userInfo"] span','h1'
              ];
              for(var s=0;s<selectors.length && !nick;s++){
                var nodes=document.querySelectorAll(selectors[s]);
                for(var i=0;i<nodes.length && !nick;i++){
                  var el=nodes[i], r=el.getBoundingClientRect();
                  if(r.width>2 && r.height>2 && r.bottom>=0 && r.top<=innerHeight){
                add(el.getAttribute('title')); add(el.getAttribute('aria-label')); add(el.innerText||el.textContent);
                  }
                }
              }
              var path=(location.pathname||'').match(/\\/user\\/([^/?#]+)/i);
              return {nick:nick, uid:path ? path[1] : ''};
            })()''', sid)
            # 这里的候选来自抖音个人主页的用户信息区，纯数字也可能是
            # 用户真实设置的昵称（例如截图中的 1234567899）。
            nick = _clean_nickname((acc or {}).get("nick"), allow_numeric=True)
            uid = str((acc or {}).get("uid") or "").strip()
            # 若语义选择器没抓到，再尝试标题；标题只作为昵称候选，
            # 纯数字标题仍会被 _clean_nickname 丢弃。
            if not nick:
                try:
                    nick = _clean_nickname(await c.eval('''(function(){
                      var t=document.title||''; return t.indexOf('-')>0?t.split('-')[0].trim():t;
                    })()''', sid))
                except Exception:
                    nick = ""
            return {"logged_in": True, "nick": nick, "uid": uid,
                    "sec_uid": uid or "self", "note": "已登录·抖音"}
        finally:
            await c.close()
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_do())
    except Exception as e:
        return {"logged_in": False, "error": str(e)[:120]}
    finally:
        loop.close()


def read_xhs(ws_url, timeout=25):
    """从小红书页面读当前登录账号。返回 dict。"""
    async def _do():
        c = _connect(ws_url)
        await c.connect()
        sid = await c.attach_page()
        try:
            # 账号昵称位于创作服务平台发布页右上角；不要复用上一次停留的
            # 探索页或作品页，否则页面上的作者昵称会被误当成当前账号。
            await c.cmd(
                "Page.navigate",
                {"url": "https://creator.xiaohongshu.com/publish/publish?source=official"},
                session_id=sid,
            )
            await asyncio.sleep(5)
            r = await c.eval('''(function(){
              var t = document.body ? document.body.innerText : '';
              // 小红书未登录特征
              var notLogin = t.indexOf('\u626b\u7801\u767b\u5f55') >= 0
                          || t.indexOf('\u8bf7\u767b\u5f55') >= 0
                          || t.indexOf('\u767b\u5f55\u540e\u67e5\u770b') >= 0;
              // 已登录：从个人入口附近的语义节点读取昵称；不要把用户 ID
              // 当昵称，也不要依赖固定坐标。
              var links = [], nick = '';
              var clean=function(v){return String(v||'').replace(/\\s+/g,' ').trim().replace(/^@/,'');};
              var blocked=/^(登录|注册|关注|粉丝|笔记|收藏|设置|消息)$/;
              var add=function(v){var x=clean(v); if(!nick && x && x.length<=80
                && !blocked.test(x) && !/^\\d{5,}$/.test(x)) nick=x;};
              try {
                var as = document.querySelectorAll("a[href*='/user/profile/']");
                for (var i=0;i<Math.min(5,as.length);i++){
                  var a=as[i]; links.push({href:a.getAttribute('href')||'',
                    text:a.innerText||a.textContent||'', title:a.getAttribute('title')||'',
                    aria:a.getAttribute('aria-label')||''});
                  add(a.getAttribute('title')); add(a.getAttribute('aria-label')); add(a.innerText||a.textContent);
                  var parent=a.parentElement;
                  for(var depth=0;parent && depth<3 && !nick;depth++,parent=parent.parentElement){
                    var line=clean(parent.innerText||'').split(' ')[0]; add(line);
                  }
                }
              } catch(e){}
              if(!nick){
                var selectors=['[class*="nickname"]','[class*="user-name"]','[class*="username"]','h1'];
                for(var s=0;s<selectors.length && !nick;s++){
                  var nodes=document.querySelectorAll(selectors[s]);
                  for(var j=0;j<nodes.length && !nick;j++){
                    var el=nodes[j], rr=el.getBoundingClientRect();
                    if(rr.width>2 && rr.height>2 && rr.bottom>=0 && rr.top<=innerHeight){ add(el.innerText||el.textContent); }
                  }
                }
              }
              return { notLogin: notLogin, links: links, nick: nick };
            })()''', sid)
            # 右上角账号入口的 DOM 结构会随创作后台版本变化，补一层只限
            # 顶部右侧的候选，避免抓到发布页菜单文字或其他作者昵称。
            top = await c.eval(r'''(() => {
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim()
                .replace(/^@/, '').replace(/[▾▼⌄]$/, '').trim();
              const blocked = /^(发布笔记|上传视频|上传图文|写长文|发播客|草稿箱|首页|笔记管理|登录|注册|设置|消息|帮助|创作服务平台)$/;
              const visible = el => {
                if (!el) return false;
                const box = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return box.width > 2 && box.height > 2 && box.top >= -5
                  && box.top < 170 && box.left > innerWidth * 0.60
                  && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const nameFrom = value => String(value || '').split(/\r?\n/)
                .map(clean).filter(x => x && x.length <= 80 && !blocked.test(x))
                .find(x => !/^(\d+|搜索|发布)$/.test(x)) || '';
              const nodes = Array.from(document.querySelectorAll(
                'header a, header button, header [class*="user"], header [class*="account"], '
                + '[class*="header"] a, [class*="header"] button, '
                + '[class*="account"], [class*="user"], [class*="avatar"], button, a'
              )).filter(visible);
              const values = [];
              for (const el of nodes) {
                values.push(el.getAttribute('title') || '');
                values.push(el.getAttribute('aria-label') || '');
                values.push(el.innerText || el.textContent || '');
              }
              const body = clean(document.body?.innerText || '');
              return {
                nick: values.map(nameFrom).find(Boolean) || '',
                notLogin: /扫码登录|手机号登录|登录后查看|请先登录/.test(body)
                  && !/退出登录/.test(body),
              };
            })()''', sid)
            if isinstance(top, dict) and top.get("nick"):
                r = dict(r or {})
                r["nick"] = top["nick"]
                r["notLogin"] = bool(top.get("notLogin"))
            if r.get("notLogin"):
                return {"logged_in": False, "nick": "", "uid": "", "sec_uid": ""}
            uid = ""
            for item in r.get("links", []):
                l = str(item.get("href") if isinstance(item, dict) else item)
                m = l.split("/user/profile/")
                if len(m) > 1:
                    uid = m[1].split("?")[0]
                    break
            return {"logged_in": True, "nick": _clean_nickname(r.get("nick"), allow_numeric=True),
                    "uid": uid, "sec_uid": uid, "note": "已登录·小红书"}
        finally:
            await c.close()
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_do())
    except Exception as e:
        return {"logged_in": False, "error": str(e)[:120]}
    finally:
        loop.close()


def read_weibo(ws_url, timeout=25):
    """从微博当前页面读取可见登录态和用户主页链接。"""
    async def _do():
        c = _connect(ws_url)
        await c.connect()
        sid = await c.attach_page()
        try:
            await c.cmd("Page.navigate", {"url": "https://weibo.com/"}, session_id=sid)
            await asyncio.sleep(3)
            r = await c.eval(r'''(() => {
              const body = document.body?.innerText || '';
              const links = Array.from(document.querySelectorAll('a[href]'))
                .map(a => ({href: a.href, text: (a.innerText || '').trim()}))
                .filter(x => /weibo\.com\/u\/\d+|weibo\.com\/\d+/.test(x.href));
              const notLogin = /登录|注册/.test(body) && !/我的|关注|好友圈|管理/.test(body);
              return {body: body.slice(0, 5000), links};
            })()''', sid)
            links = r.get("links", []) if r else []
            body = r.get("body", "") if r else ""
            # 微博未登录页也会展示公开作者链接，不能仅凭 /weibo.com/数字 判断登录。
            if not ("全部关注" in body and "好友圈" in body):
                return {"logged_in": False, "nick": "", "uid": "", "sec_uid": ""}
            user = next((x for x in links if "/u/" in x["href"]), None)
            if not user:
                user = next((x for x in links if re.search(r"weibo\.com/\d+", x["href"])), None)
            if not user:
                return {"logged_in": False, "nick": "", "uid": "", "sec_uid": ""}
            path = user["href"].split("weibo.com/", 1)[-1].split("?", 1)[0].strip("/")
            uid = path.split("/")[-1]
            return {"logged_in": True, "nick": _clean_nickname(user.get("text", "")), "uid": uid,
                    "sec_uid": uid, "note": "已登录·微博"}
        finally:
            await c.close()
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_do())
    except Exception as e:
        return {"logged_in": False, "error": str(e)[:120]}
    finally:
        loop.close()


def read_bilibili(ws_url, timeout=25):
    """从B站当前 Chrome 页面读取可见登录状态和昵称。"""
    async def _do():
        c = _connect(ws_url)
        await c.connect()
        sid = await c.attach_page()
        try:
            await c.cmd("Page.navigate", {"url": "https://www.bilibili.com/"}, session_id=sid)
            await asyncio.sleep(3)
            r = await c.eval(r'''(() => {
              const body = document.body?.innerText || '';
              const login = Array.from(document.querySelectorAll('a,span,button')).find(x =>
                /登录|头像/.test((x.innerText || '').trim()));
              // 只读取顶部个人入口；视频作者链接不能作为当前登录账号。
              const clean = v => String(v || '').replace(/\\s+/g, ' ').trim().replace(/^@/, '');
              const candidates = Array.from(document.querySelectorAll(
                'a[href*="space.bilibili.com/"], [class*="nickname"], [class*="user-name"], [class*="username"]'
              )).map(a => ({a, r: a.getBoundingClientRect()}))
                .filter(x => x.r.width > 2 && x.r.height > 2 && x.r.top >= -5 && x.r.top < 220)
                .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
              const user = candidates.find(x => /space\\.bilibili\\.com\\/\\d+/.test(x.a.href || ''))?.a || candidates[0]?.a;
              const uid = (user?.href || '').match(/space\.bilibili\.com\/(\d+)/)?.[1] || '';
              let nick = clean(user?.getAttribute('title') || user?.getAttribute('aria-label')
                || user?.innerText || user?.textContent || '');
              if (!nick && user?.parentElement) nick = clean(user.parentElement.innerText || '');
              return {body: body.slice(0, 3000), nick, uid};
            })()''', sid)
            body = r.get("body", "") if r else ""
            if "登录" in body and not r.get("uid"):
                return {"logged_in": False, "nick": "", "uid": "", "sec_uid": ""}
            return {"logged_in": bool(r and (r.get("uid") or r.get("nick"))),
                    "nick": _clean_nickname((r or {}).get("nick", "")), "uid": (r or {}).get("uid", ""),
                    "sec_uid": (r or {}).get("uid", ""), "note": "已登录·B站"}
        finally:
            await c.close()
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_do())
    except Exception as e:
        return {"logged_in": False, "error": str(e)[:120]}
    finally:
        loop.close()


def read_kuaishou(ws_url, timeout=25):
    """从快手桌面网页读取当前登录账号。

    快手未登录页会在侧栏保留 ``.sidebar-login-button``，作品流中的作者
    链接不能作为当前账号。因此只读取侧栏/顶部可见的 profile 链接，并把
    登录弹窗作为未登录处理。
    """
    async def _do():
        c = _connect(ws_url)
        await c.connect()
        sid = await c.attach_page()
        try:
            await c.cmd("Page.navigate", {"url": "https://www.kuaishou.com/new-reco"}, session_id=sid)
            await asyncio.sleep(3)
            r = await c.eval(r'''(() => {
              const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
              const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect();const s=getComputedStyle(e);
                return r.width>2&&r.height>2&&r.bottom>=0&&r.top<=innerHeight&&s.display!=='none'&&s.visibility!=='hidden'};
              const body=clean(document.body?.innerText||'');
              const loginButton=[...document.querySelectorAll(
                '.sidebar-login-button,[class*="login-button"],[class*="login-entry"],[aria-label*="登录"]'
              )]
                .some(visible);
              const profiles=[...document.querySelectorAll('a[href*="/profile/"]')].filter(visible).map(a=>{
                const r=a.getBoundingClientRect(); return {href:a.href,text:clean(a.innerText||a.textContent),left:r.left,top:r.top};
              });
              const sidebar=profiles.filter(x=>x.left<230 && x.top<700);
              return {body:body.slice(0,3000),loginButton,profiles:sidebar.slice(0,5)};
            })()''', sid)
            payload = r or {}
            if payload.get("loginButton") or re.search(r"登录后看更多|扫码登录|请登录", payload.get("body", "")):
                return {"logged_in": False, "nick": "", "uid": "", "sec_uid": "", "note": "未登录·快手"}
            profiles = payload.get("profiles") or []
            user = profiles[0] if profiles else {}
            href = str(user.get("href") or "")
            match = re.search(r"/profile/([^/?#]+)", href)
            uid = match.group(1) if match else ""
            nick = _clean_nickname(user.get("text"))
            if not uid and not nick:
                return {"logged_in": False, "nick": "", "uid": "", "sec_uid": ""}
            return {"logged_in": True, "nick": nick, "uid": uid,
                    "sec_uid": uid, "note": "已登录·快手"}
        finally:
            await c.close()
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_do())
    except Exception as e:
        return {"logged_in": False, "error": str(e)[:120]}
    finally:
        loop.close()


def read_xhs_profile(ws_url, timeout=25, expected_uid=None):
    """小红书：进入发布页，从右上角账号入口读取昵称。"""
    expected = _normalise_expected_uid("xhs", expected_uid)
    expected_json = json.dumps(expected, ensure_ascii=False)
    async def _do():
        c = _connect(ws_url)
        await c.connect()
        sid = await c.attach_page()
        try:
            await c.cmd(
                "Page.navigate",
                {"url": "https://creator.xiaohongshu.com/publish/publish?source=official"},
                session_id=sid,
            )
            await asyncio.sleep(5)
            script = r'''(() => {
              const expectedUid = __EXPECTED_UID__;
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim()
                .replace(/^@/, '').replace(/[▾▼⌄]$/, '').trim();
              const blocked = /^(发布笔记|上传视频|上传图文|写长文|发播客|草稿箱(?:\(\d+\))?|首页|笔记管理|登录|注册|设置|消息|帮助|创作服务平台|搜索|发布|创作中心|创作者中心|个人中心|账号管理)$/;
              const visible = el => {
                if (!el) return false;
                const box = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return box.width > 2 && box.height > 2 && box.top >= -5
                  && box.top < 180 && box.left > innerWidth * 0.60
                  && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const profileId = el => (String(el?.href || '').match(
                /\/user\/profile\/([^/?#]+)/i) || [,''])[1];
              const nameFrom = value => String(value || '').split(/\r?\n/)
                .map(clean)
                .filter(x => x && x.length <= 80 && !blocked.test(x)
                  && !/^(搜索|发布|上传|返回|草稿箱|首页)$/.test(x)
                  && !/^\d+$/.test(x))
                .find(Boolean) || '';
              const accountish = el => /account|user|profile|avatar|nickname|username|name/i.test(
                `${String(el.className || '')} ${el.id || ''} ${el.getAttribute('data-testid') || ''}`
              );
              const candidates = [];
              const seen = new Set();
              const add = (el, source) => {
                if (!visible(el) || seen.has(el)) return;
                seen.add(el);
                const box = el.getBoundingClientRect();
                const href = String(el.href || el.getAttribute('data-href') || '');
                const uid = profileId(el) || (href.match(/(?:user[_-]?id|profile[_-]?id)[=:]([^&#?]+)/i) || [,''])[1];
                const values = [el.getAttribute('title') || '', el.getAttribute('aria-label') || '',
                  el.getAttribute('data-nickname') || '', el.innerText || el.textContent || ''];
                const nick = values.map(nameFrom).find(Boolean) || '';
                if (!nick) return;
                let score = 0;
                if (source === 'profile') score += 240;
                if (expectedUid && uid === expectedUid) score += 700;
                if (accountish(el)) score += 130;
                if (el.matches('button,[role="button"]')) score += 25;
                if (/小红薯|红薯/.test(nick)) score += 100;
                if (/header/i.test(String(el.className || ''))) score += 50;
                candidates.push({nick, uid, score, area: box.width * box.height,
                  top: box.top, left: box.left, source});
              };

              // 先处理右上角的个人主页链接；它的身份强度高于普通标题和导航文本。
              for (const el of document.querySelectorAll('a[href*="/user/profile/"]')) {
                if (visible(el)) add(el, 'profile');
              }
              // 发布页的账号入口可能是 button，也可能是 div[role=button]；只在
              // 顶部右侧收集语义账号节点和可点击节点，禁止扫描作品/作者列表。
              const selectors = [
                // 当前创作服务平台实际使用的账号入口：
                // <div class="cursor-pointer flex-center user-info">昵称</div>。
                // 它不一定位于 header，也不一定带 button/role 属性，因此必须
                // 单独纳入选择器；否则只能读到已绑定 UID，读不到昵称。
                '.user-info', '[class~="user-info"]', '[class*="user-info"]',
                '[class*="account"]', '[class*="Account"]', '[class*="nickname"]',
                '[class*="user-name"]', '[class*="userName"]', '[class*="username"]',
                '[class*="profile"]', '[class*="avatar"]',
                'header a', 'header button', 'header [role="button"]',
                '[class*="header"] a', '[class*="header"] button',
                '[class*="header"] [role="button"]', 'button', '[role="button"]'
              ];
              for (const selector of selectors) {
                for (const el of document.querySelectorAll(selector)) {
                  if (visible(el)) add(el, accountish(el) ? 'semantic' : 'clickable');
                }
              }
              const body = clean(document.body?.innerText || '');
              const matching = candidates.filter(x => !expectedUid || !x.uid || x.uid === expectedUid);
              matching.sort((a, b) => b.score - a.score || a.area - b.area
                || a.top - b.top || b.left - a.left);
              const chosen = matching[0] || {};
              return {
                nick: chosen.nick || '',
                uid: chosen.uid || expectedUid || '',
                candidateSource: chosen.source || '',
                notLogin: /扫码登录|手机号登录|登录后查看|请先登录/.test(body)
                  && !/退出登录/.test(body),
                url: location.href,
              };
            })()'''.replace('__EXPECTED_UID__', expected_json)
            payload = await c.eval(script, sid)
            payload = payload or {}
            if payload.get("notLogin"):
                return {"logged_in": False, "nick": "", "uid": "", "sec_uid": ""}
            nick = _clean_nickname(payload.get("nick"), allow_numeric=True)
            uid = str(payload.get("uid") or "").strip()
            return {"logged_in": True, "nick": nick, "uid": uid,
                    "sec_uid": uid, "note": "已登录·小红书·发布页"}
        finally:
            await c.close()
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_do())
    except Exception as e:
        return {"logged_in": False, "error": str(e)[:120]}
    finally:
        loop.close()


def read_weibo_profile(ws_url, timeout=25, expected_uid=None):
    """微博：先找自己的主页入口，再在主页头部读取昵称。"""
    expected = _normalise_expected_uid("weibo", expected_uid)
    async def _do():
        c = _connect(ws_url)
        await c.connect()
        sid = await c.attach_page()
        try:
            initial = str(await c.eval("location.href", sid) or "")
            profile_url = (
                f"https://weibo.com/u/{expected}" if expected else (
                    initial if re.search(r"weibo\.com/(?:u/)?\d+(?:[/?#]|$)", initial)
                    else ""
                )
            )
            if not profile_url:
                await c.cmd("Page.navigate", {"url": "https://weibo.com/"}, session_id=sid)
                await asyncio.sleep(3)
                entry = await c.eval(r'''(() => {
                  // 右侧“你可能感兴趣的人”也有 /u/数字 链接，不能再取
                  // 页面中第一个匹配项，否则会把推荐用户当成当前账号。
                  const recommendationAncestor = el => {
                    for (let node = el, depth = 0; node && depth < 5; node = node.parentElement, depth++) {
                      const marker = [node.id || '', String(node.className || ''),
                        node.getAttribute?.('aria-label') || '', node.getAttribute?.('data-testid') || '']
                        .join(' ');
                      const text = String(node.innerText || '').trim();
                      if (text.length < 240 && /recommend|interest|可能感兴趣|推荐联系人|推荐用户/i.test(`${marker} ${text}`)) return true;
                    }
                    return false;
                  };
                  const links = Array.from(document.querySelectorAll('a[href]')).map(a => {
                    const r = a.getBoundingClientRect();
                    return {element: a, href: a.href, text: (a.innerText || '').trim(), top: r.top,
                      left: r.left, width: r.width, height: r.height,
                      aria: a.getAttribute('aria-label') || '', title: a.getAttribute('title') || ''};
                  }).filter(x => x.width > 2 && x.height > 2 && x.top >= -5 && x.top < 120
                    && x.left > innerWidth * 0.45
                    && !recommendationAncestor(x.element)
                    && /weibo\.com\/(?:u\/)?\d+(?:[/?#]|$)/.test(x.href));
                  const scored = links.map(x => {
                    let score = 0;
                    if (x.top < 90) score += 260;
                    if (/avatar|profile|user|account|我的|个人/i.test(
                      `${x.aria} ${x.title} ${x.element.className || ''}`)) score += 120;
                    if (x.left > innerWidth * 0.55) score += 40;
                    return {...x, score};
                  }).filter(x => x.score >= 260)
                    .sort((a, b) => b.score - a.score || a.top - b.top || b.left - a.left);
                  const chosen = scored[0];
                  return {profileUrl: chosen?.href || '',
                    uid: (chosen?.href || '').match(/weibo\.com\/(?:u\/)?(\d+)/)?.[1] || '',
                    candidateSource: chosen ? 'top-account-entry' : '',
                    body: (document.body?.innerText || '').slice(0, 5000)};
                })()''', sid)
                profile_url = str((entry or {}).get("profileUrl") or "")
            if not profile_url:
                return {"logged_in": False, "nick": "", "uid": "", "sec_uid": ""}
            uid_match = re.search(r"weibo\.com/(?:u/)?(\d+)(?:[/?#]|$)", profile_url)
            uid = uid_match.group(1) if uid_match else ""
            current = str(await c.eval("location.href", sid) or "")
            if current.split("?", 1)[0].rstrip("/") != profile_url.split("?", 1)[0].rstrip("/"):
                await c.cmd("Page.navigate", {"url": profile_url}, session_id=sid)
            for _ in range(24):
                current = str(await c.eval("location.href", sid) or "")
                if uid and re.search(
                    rf"weibo\.com/(?:u/)?{re.escape(uid)}(?:[/?#]|$)", current
                ):
                    break
                await asyncio.sleep(0.35)
            current = str(await c.eval("location.href", sid) or "")
            if uid and not re.search(
                rf"weibo\.com/(?:u/)?{re.escape(uid)}(?:[/?#]|$)", current
            ):
                return {"logged_in": False, "nick": "", "uid": "", "sec_uid": "",
                        "note": "未确认微博当前账号主页"}
            await asyncio.sleep(2)
            script = r'''(() => {
              const expectedUid = __EXPECTED_UID__;
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim().replace(/^@/, '');
              const blocked = /^(个人主页|我的主页|微博|关注|粉丝|获赞|动态|相册|简介|暂无简介|返回|编辑资料|登录|注册|退出登录|私信|收藏|转发|评论)$/;
              const statOnly = /^(?:\d+[粉丝关注微博动态转评赞]|粉丝|关注|微博|获赞|转评赞)$/;
              const recommendationAncestor = el => {
                for (let node = el, depth = 0; node && depth < 6; node = node.parentElement, depth++) {
                  const marker = [node.id || '', String(node.className || ''),
                    node.getAttribute?.('aria-label') || '', node.getAttribute?.('data-testid') || '']
                    .join(' ');
                  const text = String(node.innerText || '').trim();
                  if (text.length < 240 && /recommend|interest|可能感兴趣|推荐联系人|推荐用户/i.test(`${marker} ${text}`)) return true;
                }
                return false;
              };
              const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 2 && r.height > 2 && r.top >= 80 && r.top < innerHeight
                  && r.left >= 120 && r.left < innerWidth * 0.82
                  && r.width < 560 && r.height < 120
                  && !recommendationAncestor(el)
                  && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const nameFrom = value => String(value || '').split(/\r?\n/)
                .map(clean)
                .filter(x => x && x.length <= 80 && !blocked.test(x)
                  && !statOnly.test(x) && !/^\d+$/.test(x)
                  && !/^(登录|注册|返回|编辑资料)$/.test(x))
                .find(Boolean) || '';
              const ownHref = el => {
                const href = String(el?.href || '');
                const uid = (href.match(/weibo\.com\/(?:u\/)?(\d+)/i) || [,''])[1];
                return expectedUid ? uid === expectedUid : !!uid;
              };
              const ownLinks = Array.from(document.querySelectorAll('a[href]'))
                .filter(el => visible(el) && ownHref(el));
              const ownLink = ownLinks[0] || null;
              const avatarCandidates = Array.from(document.querySelectorAll(
                'img, [class*="avatar"], [class*="head"], [class*="face"]'
              )).filter(el => {
                if (!visible(el)) return false;
                const r = el.getBoundingClientRect();
                return r.top > 100 && r.left >= 120 && r.left < innerWidth * 0.60
                  && r.width >= 24 && r.height >= 24;
              });
              const seed = ownLink || avatarCandidates[0] || null;
              const hasProfileStats = el => {
                const text = clean(el?.innerText || '');
                return text.length < 6000 && /粉丝/.test(text) && /关注/.test(text)
                  && (/微博|动态|转评赞/.test(text) || !!el.querySelector?.('img'));
              };
              let scope = null;
              for (let node = seed, depth = 0; node && depth < 9; node = node.parentElement, depth++) {
                if (hasProfileStats(node)) { scope = node; break; }
              }
              if (!scope) {
                const scopes = Array.from(document.querySelectorAll(
                  'main, [class*="profile"], [class*="Profile"], [class*="user"], [class*="User"]'
                )).filter(el => visible(el) && hasProfileStats(el));
                scopes.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                scope = scopes[0] || null;
              }
              // 找不到包含粉丝/关注/微博统计的个人资料区域时，宁可不返回昵称，
              // 也不能退回整页扫描，把右侧推荐联系人识别成当前账号。
              const root = scope;
              const candidates = [];
              const seen = new Set();
              const add = (el, source) => {
                if (!el || seen.has(el) || !visible(el)) return;
                seen.add(el);
                const values = [el.getAttribute('title') || '', el.getAttribute('aria-label') || '',
                  el.getAttribute('data-name') || '', el.innerText || el.textContent || ''];
                const nick = values.map(nameFrom).find(Boolean) || '';
                if (!nick) return;
                const r = el.getBoundingClientRect();
                const cls = String(el.className || '');
                let score = 0;
                if (source === 'strong') score += 160;
                if (ownHref(el)) score += 300;
                if (/name|nick/i.test(`${cls} ${el.id || ''}`)) score += 150;
                if (/^H[12]$/.test(el.tagName)) score += 70;
                if (!el.children.length) score += 35;
                if (seed) {
                  const sr = seed.getBoundingClientRect();
                  score += Math.max(0, 80 - Math.abs(r.top - sr.top) - Math.max(0, sr.left - r.left) * 0.1);
                }
                candidates.push({nick, score, area: r.width * r.height, top: r.top, left: r.left});
              };
              if (root) {
                const strongSelectors = [
                  '[node-type="name"]', '[class*="ProfileHeader"] [class*="name"]',
                  '[class*="profile-header"] [class*="name"]', '[class*="user-info"] [class*="name"]',
                  '[class*="nickname"]', '[class*="nick"]', 'h1', 'h2',
                  'a[href*="/u/"]'
                ];
                for (const selector of strongSelectors) {
                  for (const el of root.querySelectorAll(selector)) add(el, 'strong');
                }
                // 微博头像旁的昵称在不同版本中可能没有稳定 class；只允许
                // profile header 内的文本叶子节点参与兜底，绝不扫描整页作者列表。
                for (const el of root.querySelectorAll('a,span,h1,h2,p,div')) {
                  if (!el.children.length) add(el, 'leaf');
                }
              }
              const sorted = candidates.sort((a, b) => b.score - a.score || a.area - b.area
                || a.top - b.top || a.left - b.left);
              const chosen = sorted[0] || {};
              const body = clean(document.body?.innerText || '');
              const pathUid = (location.pathname.match(/(?:^|\/)u\/(\d+)/)
                || location.pathname.match(/^\/(\d+)/) || [,''])[1];
              return {nick: chosen.nick || '', uid: pathUid || expectedUid || '',
                scopeFound: !!scope, candidateSource: chosen.score ? 'profile-scope' : '',
                notLogin: /扫码登录|手机号登录|请先登录/.test(body)
                  && !/退出登录|个人主页|我的主页/.test(body)};
            })()'''.replace('__EXPECTED_UID__', json.dumps(expected, ensure_ascii=False))
            payload = await c.eval(script, sid)
            payload = payload or {}
            if payload.get("notLogin"):
                return {"logged_in": False, "nick": "", "uid": "", "sec_uid": ""}
            if not payload.get("scopeFound"):
                return {"logged_in": False, "nick": "", "uid": "", "sec_uid": "",
                        "note": "未找到微博中间个人资料区域"}
            nick = _clean_nickname(payload.get("nick"), allow_numeric=True)
            uid = str(payload.get("uid") or uid).strip()
            return {"logged_in": bool(uid or nick), "nick": nick, "uid": uid,
                    "sec_uid": uid, "note": "已登录·微博·个人主页"}
        finally:
            await c.close()
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_do())
    except Exception as e:
        return {"logged_in": False, "error": str(e)[:120]}
    finally:
        loop.close()


def read_bilibili_profile(ws_url, timeout=25, expected_uid=None):
    """B站：悬停右上角头像，读取展开账号菜单中的昵称。"""
    async def _do():
        c = _connect(ws_url)
        await c.connect()
        sid = await c.attach_page()
        try:
            await c.cmd("Page.navigate", {"url": "https://www.bilibili.com/"}, session_id=sid)
            await asyncio.sleep(4)
            hover = await c.eval(r'''(() => {
              const visible = el => {
                const r = el.getBoundingClientRect();
                return r.width > 8 && r.height > 8 && r.top >= -5 && r.top < 170
                  && r.left > innerWidth * 0.55;
              };
              const items = Array.from(document.querySelectorAll(
                'a[href*="space.bilibili.com/"], img, [class*="avatar"], [class*="face"], [class*="user"]'
              )).filter(visible).map(el => {
                const r = el.getBoundingClientRect();
                return {x: r.left + r.width / 2, y: r.top + r.height / 2,
                  tag: el.tagName, cls: String(el.className || ''), href: el.href || ''};
              });
              const chosen = items.find(x => x.tag === 'IMG' || /avatar|face/i.test(x.cls))
                || items.find(x => /space\.bilibili\.com\/\d+/.test(x.href)) || items[0];
              return chosen || {};
            })()''', sid)
            if isinstance(hover, dict) and hover.get("x") is not None:
                await c.cmd(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": float(hover["x"]), "y": float(hover["y"])},
                    session_id=sid,
                )
                await asyncio.sleep(1.2)
            payload = await c.eval(r'''(() => {
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim().replace(/^@/, '');
              const blocked = /^(登录|注册|头像|年度大会员|大会员中心|个人中心|投稿管理|推荐服务|主题|退出登录|消息|动态|收藏|历史记录)$/;
              const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 2 && r.height > 2 && r.top >= -5 && r.top < 430
                  && r.left > innerWidth * 0.45 && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const nameFrom = value => String(value || '').split(/\r?\n/).map(clean)
                .filter(x => x && x.length <= 80 && !blocked.test(x))
                .find(x => !/^(\d+|\d+粉丝|\d+关注|\d+动态|\d+硬币)$/.test(x)) || '';
              const ownLinks = Array.from(document.querySelectorAll('a[href*="space.bilibili.com/"]'))
                .filter(visible);
              const uid = (ownLinks[0]?.href || '').match(/space\.bilibili\.com\/(\d+)/)?.[1] || '';
              let nick = '';
              const selectors = [
                '#i_menu [class*="name"]', '#i_menu [class*="nickname"]',
                '[class*="dropdown"] [class*="name"]', '[class*="drop-down"] [class*="name"]',
                '[class*="user-center"] [class*="name"]', '[class*="userCenter"] [class*="name"]',
                '[class*="nickname"]', '[class*="user-name"]', '[class*="username"]'
              ];
              for (const selector of selectors) {
                for (const el of document.querySelectorAll(selector)) {
                  if (visible(el)) nick = nameFrom(el.getAttribute('title') || el.innerText || el.textContent);
                  if (nick) break;
                }
                if (nick) break;
              }
              if (!nick && ownLinks[0]) {
                let node = ownLinks[0];
                for (let depth = 0; node && depth < 4 && !nick; depth++, node = node.parentElement) {
                  nick = nameFrom(node.getAttribute?.('title') || node.innerText || node.textContent);
                }
              }
              const body = clean(document.body?.innerText || '');
              return {nick, uid, notLogin: /登录|注册/.test(body) && !uid && !nick};
            })()''', sid)
            payload = payload or {}
            if payload.get("notLogin"):
                return {"logged_in": False, "nick": "", "uid": "", "sec_uid": ""}
            nick = _clean_nickname(payload.get("nick"), allow_numeric=True)
            uid = str(payload.get("uid") or "").strip()
            return {"logged_in": bool(uid or nick), "nick": nick, "uid": uid,
                    "sec_uid": uid, "note": "已登录·B站·头像菜单"}
        finally:
            await c.close()
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_do())
    except Exception as e:
        return {"logged_in": False, "error": str(e)[:120]}
    finally:
        loop.close()


def read_kuaishou_profile(ws_url, timeout=25, expected_uid=None):
    """快手：读取左下角固定账号入口中的昵称。"""
    async def _do():
        c = _connect(ws_url)
        await c.connect()
        sid = await c.attach_page()
        try:
            await c.cmd("Page.navigate", {"url": "https://www.kuaishou.com/new-reco"}, session_id=sid)
            await asyncio.sleep(4)
            payload = await c.eval(r'''(() => {
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim().replace(/^@/, '');
              const blocked = /^(推荐|发现|关注|直播|赛事|登录|注册|搜索|上传作品|添加桌面|快手轻量版|快手充值|返回旧版|AcFun|可灵AI|帮助|设置|消息|首页|个人主页|退出登录)$/;
              const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 2 && r.height > 2 && r.left >= -5 && r.left < 330
                  && r.top > innerHeight * 0.52 && r.top < innerHeight
                  && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const nameFrom = value => String(value || '').split(/\r?\n/).map(clean)
                .filter(x => x && x.length <= 80 && !blocked.test(x)
                  && !/^\d+$/.test(x) && !/^(搜索|登录|注册)$/.test(x))
                .find(Boolean) || '';
              const profileId = el => (String(el?.href || '').match(
                /\/(?:profile|user)\/([^/?#]+)/i) || [,''])[1];
              const isPanelAnchor = el => {
                let node = el;
                for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
                  const r = node.getBoundingClientRect();
                  const s = getComputedStyle(node);
                  const cls = `${String(node.className || '')} ${node.id || ''}`;
                  const fixed = s.position === 'fixed' || s.position === 'sticky';
                  const sidebar = /sidebar|side-bar|user-panel|account-panel|profile-panel/i.test(cls);
                  const reachesBottom = r.bottom >= innerHeight - 6 && r.top > innerHeight * 0.52
                    && r.left < 300 && r.width < 360;
                  if (fixed || sidebar || reachesBottom) return {fixed, sidebar, reachesBottom};
                }
                return null;
              };
              const candidates = [];
              const seen = new Set();
              const add = (el, source) => {
                if (!visible(el) || seen.has(el)) return;
                const panel = isPanelAnchor(el);
                if (!panel) return;
                seen.add(el);
                const r = el.getBoundingClientRect();
                const cls = `${String(el.className || '')} ${el.id || ''}`;
                const values = [el.getAttribute('title') || '', el.getAttribute('aria-label') || '',
                  el.getAttribute('data-nickname') || '', el.innerText || el.textContent || ''];
                const nick = values.map(nameFrom).find(Boolean) || '';
                if (!nick) return;
                let score = 0;
                if (/user|account|profile|avatar|nickname|username|name/i.test(cls)) score += 180;
                if (source === 'profile-link') score += 90;
                if (source === 'semantic') score += 70;
                if (el.matches('button,[role="button"]')) score += 45;
                if (!el.children.length) score += 35;
                if (panel.fixed) score += 35;
                if (panel.sidebar) score += 30;
                score += Math.max(0, 35 - Math.abs(innerHeight - r.bottom));
                candidates.push({nick, uid: profileId(el), score, area: r.width * r.height,
                  top: r.top, left: r.left});
              };
              // 只从左下角账号面板收集候选；作品流里其他用户的 profile 链接
              // 即使可见，也没有固定侧栏/账号面板祖先，不会进入候选集。
              const selectors = [
                '[class*="sidebar"]', '[class*="SideBar"]', '[class*="side-bar"]',
                '[class*="user"]', '[class*="account"]', '[class*="profile"]',
                '[class*="avatar"]', 'a[href*="/profile/"]', 'a[href*="/user/"]',
                'button', '[role="button"]'
              ];
              for (const selector of selectors) {
                for (const el of document.querySelectorAll(selector)) {
                  if (visible(el)) add(el, /profile|user/i.test(selector) ? 'semantic' : 'panel');
                }
              }
              // 快手登录后左下角按钮有时只有一层通用 div，没有稳定 class；
              // 在已经确认的面板内再检查文本叶子节点，而不是全页面兜底。
              for (const el of document.querySelectorAll('div,span,a,p')) {
                if (visible(el) && !el.children.length) add(el, 'leaf');
              }
              candidates.sort((a, b) => b.score - a.score || a.area - b.area
                || b.top - a.top || a.left - b.left);
              const chosen = candidates[0] || {};
              const body = clean(document.body?.innerText || '');
              const notLogin = /登录后看更多|扫码登录|请登录/.test(body) && !chosen.nick && !chosen.uid;
              return {nick: chosen.nick || '', uid: chosen.uid || '', notLogin,
                candidateSource: chosen.score ? 'bottom-left-panel' : ''};
            })()''', sid)
            payload = payload or {}
            if payload.get("notLogin"):
                return {"logged_in": False, "nick": "", "uid": "", "sec_uid": "", "note": "未登录·快手"}
            nick = _clean_nickname(payload.get("nick"), allow_numeric=True)
            uid = str(payload.get("uid") or "").strip()
            return {"logged_in": bool(uid or nick), "nick": nick, "uid": uid,
                    "sec_uid": uid, "note": "已登录·快手·左下角账号入口"}
        finally:
            await c.close()
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_do())
    except Exception as e:
        return {"logged_in": False, "error": str(e)[:120]}
    finally:
        loop.close()


# 兼容旧的直接导入调用，统一走按截图标注入口实现的读取器。
read_xhs = read_xhs_profile
read_weibo = read_weibo_profile
read_bilibili = read_bilibili_profile
read_kuaishou = read_kuaishou_profile


READERS = {
    "douyin": read_douyin,
    "xhs": read_xhs_profile,
    "weibo": read_weibo_profile,
    "bilibili": read_bilibili_profile,
    "kuaishou": read_kuaishou_profile,
}


def read_account(platform, ws_url, expected_uid=None):
    fn = READERS.get(platform)
    if not fn:
        return {"logged_in": False, "error": f"no reader for {platform}"}
    return fn(ws_url, expected_uid=expected_uid)


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "douyin"
    f = {"douyin": "dy_window.txt", "xhs": "xhs_window.txt",
         "weibo": "weibo_chrome_window.txt",
         "bilibili": "bilibili_chrome_window.txt",
         "kuaishou": "kuaishou_window.txt"}[p]
    path = os.path.join(PROJECT_ROOT, "data", f)
    lines = [l.strip() for l in open(path, encoding="utf-8") if l.strip()]
    ws = lines[-1]
    print(read_account(p, ws))
