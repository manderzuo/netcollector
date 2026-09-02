#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""account_reader.py — 从已打开的平台页面读取当前登录账号信息。

核心目的：账号管理页显示「平台账号昵称 + 用户ID」，方便区分是哪个账号。

每个平台一个读取函数，输入 window 的 ws，返回 {"nick", "uid", "sec_uid", "logged_in"}。
登录态识别 + 账号信息提取都在这层做，GUI 只负责展示。

注意：JS 字符串用单引号包裹、内部双引号转义，避免 querySelector 选择器语法错误。
"""

import asyncio
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _connect(ws_url):
    try:
        from .cdp import CdpSession
    except ImportError:
        from cdp import CdpSession
    return CdpSession(ws_url)


def read_douyin(ws_url, timeout=25):
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
            # 读中心账号信息卡昵称（屏幕居中靠上首个用户区短文本 + 粉丝/关注区）
            acc = await c.eval('''(function(){
              var nick = '';
              try{
                var all = document.querySelectorAll('span,div,h1,h2,a');
                var bestTxt='';
                for (var i=0;i<all.length;i++){
                  var el=all[i]; var r=el.getBoundingClientRect();
                  var x=(el.innerText||'').trim().split('\\n')[0];
                  // 用户信息卡: 居中靠上(80~120), 左半(300~500), 短昵称文本
                  if(x && x.length>=1 && x.length<=16 && r.top>=75 && r.top<=140
                     && r.left>=280 && r.left<=560 && !/关注|粉丝|获赞|简介|作品|收藏|喜欢/.test(x)){
                    if(nick==='' ) nick=x;
                  }
                }
              }catch(e){}
              return {nick:nick};
            })()''', sid)
            nick = acc.get("nick", "")
            # 若中心区没抓到, 尝试导航栏 self 附近
            if not nick:
                try:
                    nick = await c.eval('''(function(){
                      var t=document.title||''; return t.indexOf('-')>0?t.split('-')[0].trim():'';
                    })()''', sid)
                except Exception:
                    nick = ""
            return {"logged_in": True, "nick": nick, "sec_uid": "self", "note": "已登录·抖音"}
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
            r = await c.eval('''(function(){
              var t = document.body ? document.body.innerText : '';
              // 小红书未登录特征
              var notLogin = t.indexOf('\u626b\u7801\u767b\u5f55') >= 0
                          || t.indexOf('\u8bf7\u767b\u5f55') >= 0
                          || t.indexOf('\u767b\u5f55\u540e\u67e5\u770b') >= 0;
              // 已登录：找 /user/profile/ 链接 + 昵称
              var links = [];
              try {
                var as = document.querySelectorAll("a[href*='/user/profile/']");
                for (var i=0;i<Math.min(2,as.length);i++){ links.push(as[i].getAttribute('href')||''); }
              } catch(e){}
              return { notLogin: notLogin, links: links };
            })()''', sid)
            if r.get("notLogin"):
                return {"logged_in": False, "nick": "", "uid": "", "sec_uid": ""}
            uid = ""
            for l in r.get("links", []):
                m = l.split("/user/profile/")
                if len(m) > 1:
                    uid = m[1].split("?")[0]
                    break
            return {"logged_in": True, "nick": "", "uid": uid, "sec_uid": uid, "note": ""}
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
            return {"logged_in": True, "nick": user.get("text", ""), "uid": uid,
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
              const user = Array.from(document.querySelectorAll('a[href*="space.bilibili.com/"]'))
                .map(a => ({a, r: a.getBoundingClientRect()}))
                .filter(x => x.r.top >= 0 && x.r.top < 150)
                .sort((a,b) => a.r.left - b.r.left)[0]?.a;
              const uid = (user?.href || '').match(/space\.bilibili\.com\/(\d+)/)?.[1] || '';
              return {body: body.slice(0, 3000), nick: (user?.innerText || '').trim(), uid};
            })()''', sid)
            body = r.get("body", "") if r else ""
            if "登录" in body and not r.get("uid"):
                return {"logged_in": False, "nick": "", "uid": "", "sec_uid": ""}
            return {"logged_in": bool(r and (r.get("uid") or r.get("nick"))),
                    "nick": (r or {}).get("nick", ""), "uid": (r or {}).get("uid", ""),
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
            nick = str(user.get("text") or "").lstrip("@").strip()
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


READERS = {
    "douyin": read_douyin,
    "xhs": read_xhs,
    "weibo": read_weibo,
    "bilibili": read_bilibili,
    "kuaishou": read_kuaishou,
}


def read_account(platform, ws_url):
    fn = READERS.get(platform)
    if not fn:
        return {"logged_in": False, "error": f"no reader for {platform}"}
    return fn(ws_url)


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
