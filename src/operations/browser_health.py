# -*- coding: utf-8 -*-
"""平台真实浏览器只读健康诊断。

诊断会选择对应平台页面，并在每次检测前强制回到平台首页，
防止旧的作品页、评论页或上一次测试状态影响本次结果；
然后只读取页面状态，并用可恢复的滚轮探测判断页面是否被验证层锁住；
不会导航到作品、点击控件、输入文字或发送回复。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime
from urllib.parse import urlsplit

from .health import HealthStatus, HealthStore


PLATFORM_INFO = {
    "douyin": ("抖音", ("douyin.com",), "https://www.douyin.com/user/self?from_nav=1"),
    "xhs": ("小红书", ("xiaohongshu.com", "xhslink.com"), "https://www.xiaohongshu.com/explore"),
    "weibo": ("微博", ("weibo.com", "weibo.cn"), "https://weibo.com/"),
    "bilibili": ("B站", ("bilibili.com", "b23.tv"), "https://www.bilibili.com/"),
    "kuaishou": ("快手", ("kuaishou.com", "gifshow.com"), "https://www.kuaishou.com/new-reco"),
}


_PAGE_PROBE = r'''(() => {
  const visible = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' &&
      Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0;
  };
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const text = document.body ? clean(document.body.innerText) : '';
  const visibleText = [...document.querySelectorAll('body *')]
    .filter(visible).slice(0, 2500).map(x => clean(x.innerText)).join(' ');
  const inputs = [...document.querySelectorAll('input, textarea, [contenteditable="true"]')]
    .filter(visible).map(el => ({
      tag: el.tagName.toLowerCase(), type: el.type || '',
      placeholder: el.getAttribute('placeholder') || '',
      aria: el.getAttribute('aria-label') || '',
      cls: typeof el.className === 'string' ? el.className : ''
    })).slice(0, 60);
  const controls = [...document.querySelectorAll('button, a, [role="button"], span, div')]
    .filter(visible).map(el => clean(el.innerText)).filter(Boolean);
  const commentRe = /评论|留言|comment|commentapp/i;
  const replyRe = /回复|评论|reply|comment/i;
  const searchRe = /搜索|search/i;
  const commentNodes = [...document.querySelectorAll(
    '[class*="comment"], [class*="Comment"], [id*="comment"], [id*="Comment"], ' +
    '[data-e2e*="comment"], [data-testid*="comment"], bili-comments, #commentapp'
  )].filter(visible).length;
  const searchInputs = inputs.filter(x => searchRe.test(
    `${x.placeholder} ${x.aria} ${x.cls}`)).length;
  const searchSurfaces = [...document.querySelectorAll(
    '[class*="search"], [class*="Search"], [id*="search"], [id*="Search"], '
    + '[data-testid*="search"], [aria-label*="搜索"]'
  )].filter(visible).length;
  const replyInputs = inputs.filter(x => replyRe.test(
    `${x.placeholder} ${x.aria} ${x.cls}`)).length;
  const replyButtons = controls.filter(x => /回复|reply/i.test(x)).length;
  const loginEntry = [...document.querySelectorAll(
    '.header-login-entry, .login-entry, [class*="login-entry"], [class*="loginEntry"], '
    + '[id*="login"], [href*="login"]'
  )].some(visible);
  const scroller = document.scrollingElement || document.documentElement || document.body;
  const maxScroll = scroller ? Math.max(0, Number(scroller.scrollHeight || 0) - Number(scroller.clientHeight || 0)) : 0;
  const frameSrc = [...document.querySelectorAll('iframe')].filter(visible)
    .map(x => x.src || '').join(' ');
  const all = `${text} ${visibleText} ${frameSrc}`;
  return {
    url: location.href, title: document.title || '', bodyLength: text.length,
    loginRequired: /扫码登录|请先登录|请登录|登录后查看|登录后可见|一键登录|登录后继续|登录账号|立即登录/i.test(all) || loginEntry,
    loginEntry,
    captcha: /滑块|拖动验证|滑动验证|安全验证|请完成验证|人机验证|验证码/.test(all) ||
      /(captcha|verify|challenge|secsdk|security)/i.test(frameSrc),
    searchInputs, searchSurfaces, commentNodes, replyInputs, replyButtons,
    scrollTop: Number(scroller?.scrollTop || window.scrollY || 0),
    maxScroll, viewportHeight: Number(scroller.clientHeight || window.innerHeight || 0)
  };
})()'''


class BrowserHealthChecker:
    """按已绑定账号逐个检查当前浏览器页，并记录健康事件。"""

    def __init__(self, conn, bitbrowser, clock=None, screenshot_dir=None):
        self.conn = conn
        self.bb = bitbrowser
        self.clock = clock or (lambda: datetime.now().isoformat(timespec="seconds"))
        self.screenshot_dir = os.path.abspath(screenshot_dir) if screenshot_dir else None

    def run(self) -> dict:
        accounts = self.conn.execute(
            "SELECT id, name, platform, bb_window_id FROM accounts "
            "WHERE bb_window_id IS NOT NULL AND TRIM(bb_window_id) <> '' ORDER BY id"
        ).fetchall()
        grouped = {platform: [] for platform in PLATFORM_INFO}
        for row in accounts:
            if row["platform"] in grouped:
                grouped[row["platform"]].append(dict(row))
        store = HealthStore(self.conn, clock=self.clock)
        rows = []
        run_id = f"browser-health-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
        screenshot_paths = []
        if self.screenshot_dir:
            os.makedirs(self.screenshot_dir, exist_ok=True)
        for platform, (label, _domains, _home_url) in PLATFORM_INFO.items():
            details = []
            for account in grouped[platform]:
                result = self._check_account(account, self.screenshot_dir)
                store.record(
                    platform, "真实浏览器诊断", result["status"], result["detail"],
                    account_id=account["id"],
                    metadata={"account_name": account["name"], "checks": result.get("checks", {}),
                              "diagnostic_run_id": run_id,
                              "screenshot_available": bool(result.get("screenshot_path"))},
                )
                if result.get("screenshot_path"):
                    screenshot_paths.append(result["screenshot_path"])
                details.append({"account": account["name"], **result})
            rows.append(self._platform_row(platform, label, details))
        return {"rows": rows, "checked_at": self.clock(), "mode": "browser_read_only",
                "diagnostic_run_id": run_id, "screenshot_paths": screenshot_paths,
                "screenshot_count": len(screenshot_paths)}

    def _check_account(self, account: dict, screenshot_dir: str | None = None) -> dict:
        try:
            opened = self.bb.open_browser(account["bb_window_id"], ignore_default_urls=True)
            ws_url = opened.get("ws") if isinstance(opened, dict) else None
            if not ws_url:
                raise RuntimeError("BitBrowser 未返回 CDP 地址")
            screenshot_path = None
            if screenshot_dir:
                # 截图文件名不使用原始窗口/账号标识；仅用于当前诊断包关联。
                account_key = hashlib.sha256(
                    str(account["id"]).encode("utf-8")
                ).hexdigest()[:10]
                screenshot_path = os.path.join(
                    screenshot_dir, f"{account['platform']}_account_{account_key}.png"
                )
            result = asyncio.run(self._probe_page(
                ws_url, account["platform"], screenshot_path=screenshot_path
            ))
            return result
        except Exception as exc:  # noqa: BLE001
            return {"status": HealthStatus.FAILED,
                    "detail": f"浏览器诊断失败：{type(exc).__name__}: {exc}", "checks": {}}

    def check_current_account(self, account: dict) -> dict:
        """只复核账号当前页面，不导航、不滚动、不改变用户正在看的内容。

        该入口专门用于清理重启后残留的 ``waiting_human`` 冻结状态。完整诊断
        需要回到平台首页并做滚轮探测，而冻结复核只需要确认当前页面是否仍有
        验证/登录拦截，不能为了检查状态再次干扰浏览器页面。
        """
        try:
            opened = self.bb.open_browser(account["bb_window_id"], ignore_default_urls=True)
            ws_url = opened.get("ws") if isinstance(opened, dict) else None
            if not ws_url:
                raise RuntimeError("BitBrowser 未返回 CDP 地址")
            return asyncio.run(self._probe_current_page(ws_url, account.get("platform", "")))
        except Exception as exc:  # noqa: BLE001
            return {"status": HealthStatus.FAILED,
                    "detail": f"人工冻结复核失败：{type(exc).__name__}: {exc}", "checks": {}}

    async def _probe_current_page(self, ws_url: str, platform: str) -> dict:
        """读取当前已打开的平台页，专用于冻结状态复核。"""
        try:
            from cdp import CdpSession
        except ImportError:
            from ..cdp import CdpSession
        session = CdpSession(ws_url, timeout=15.0)
        await session.connect()
        try:
            sid, target_url = await self._attach_health_page(session, platform)
            state = await session.eval(_PAGE_PROBE, sid) or {}
            url = str(state.get("url") or target_url or "")
            _label, expected_domains, _home_url = PLATFORM_INFO.get(platform, ("", (), ""))
            checks = {
                "page_connected": True,
                "domain_match": any(domain in url.lower() for domain in expected_domains),
                "login_required": bool(state.get("loginRequired")),
                "login_entry": bool(state.get("loginEntry")),
                "captcha": bool(state.get("captcha")),
                "current_page": True,
            }
            if checks["captcha"]:
                status, reason = HealthStatus.HUMAN_REQUIRED, "当前页面仍有人工验证"
            elif checks["login_required"]:
                status, reason = HealthStatus.LOGIN_REQUIRED, "当前页面仍有登录提示"
            elif not checks["domain_match"]:
                status, reason = HealthStatus.WARNING, "当前页面不是对应平台页面"
            else:
                status, reason = HealthStatus.OK, "当前页面未发现验证码或登录拦截"
            return {
                "status": status,
                "detail": f"{reason}；地址：{url or '—'}",
                "checks": checks,
            }
        finally:
            await session.close()

    async def _probe_page(self, ws_url: str, platform: str, screenshot_path: str | None = None) -> dict:
        try:
            from cdp import CdpSession
        except ImportError:
            from ..cdp import CdpSession
        session = CdpSession(ws_url, timeout=15.0)
        await session.connect()
        try:
            sid, _current_url = await self._attach_health_page(session, platform)
            _label, expected_domains, home_url = PLATFORM_INFO.get(platform, ("", (), ""))
            # 每次诊断都从平台首页开始，不能因为当前页面仍属于该平台，
            # 就复用上一次测试留下的作品页、评论页或回复弹窗。
            # BitBrowser 的导航页也通过同一条路径处理，避免不同入口产生两套行为。
            await session.navigate(home_url, sid, wait_load=False)
            await asyncio.sleep(2.5)
            state = await session.eval(_PAGE_PROBE, sid) or {}
            scroll = await self._probe_scroll(session, sid, state)
            url = str(state.get("url") or "")
            domain_match = any(domain in url.lower() for domain in expected_domains)
            checks = {
                "page_connected": True,
                "domain_match": domain_match,
                "login_required": bool(state.get("loginRequired")),
                "login_entry": bool(state.get("loginEntry")),
                "captcha": bool(state.get("captcha")),
                "search_input": int(state.get("searchInputs") or 0) > 0,
                "search_surface": int(state.get("searchSurfaces") or 0) > 0,
                "comment_area": int(state.get("commentNodes") or 0) > 0,
                "reply_button": int(state.get("replyButtons") or 0) > 0,
                "reply_input": int(state.get("replyInputs") or 0) > 0,
                "scroll": scroll["status"],
                "home_reset": True,
            }
            checks["platform_home"] = self._is_platform_home(url, platform)
            if checks["captcha"] or scroll["status"] == "locked":
                status = HealthStatus.HUMAN_REQUIRED
                reason = "疑似出现人工验证或页面滚轮被拦截"
            elif checks["login_required"]:
                status = HealthStatus.LOGIN_REQUIRED
                reason = "当前页面出现登录提示"
            elif not checks["domain_match"]:
                status = HealthStatus.WARNING
                reason = "已回到平台初始页，但当前地址仍不是对应平台页面"
            elif (not checks["platform_home"] and not checks["comment_area"]
                  and not checks["search_input"] and not checks["search_surface"]):
                status = HealthStatus.WARNING
                reason = "页面已连接，但暂未识别搜索入口或评论区"
            else:
                status = HealthStatus.OK
                reason = ("平台首页已打开，浏览器连接、登录状态和滚轮探测正常；"
                          "未执行点击、输入或发送" if checks["platform_home"] else
                          "浏览器连接、页面状态和滚轮探测正常；未执行点击、输入或发送")
            detail = f"{reason}；地址：{url or '—'}；滚动：{scroll['label']}"
            saved_screenshot = await self._capture_blurred_screenshot(
                session, sid, screenshot_path
            )
            return {"status": status, "detail": detail, "checks": checks,
                    "screenshot_path": saved_screenshot}
        finally:
            await session.close()

    @staticmethod
    async def _capture_blurred_screenshot(session, sid, screenshot_path: str | None) -> str | None:
        """只保留模糊截图，原始 PNG 在函数返回前删除。"""
        if not screenshot_path:
            return None
        raw_path = f"{screenshot_path}.raw"
        try:
            os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
            await session.screenshot(sid, raw_path)
            from PIL import Image, ImageFilter
            with Image.open(raw_path) as image:
                image = image.convert("RGB")
                radius = max(5, min(24, max(image.size) // 90))
                image.filter(ImageFilter.GaussianBlur(radius=radius)).save(
                    screenshot_path, format="PNG"
                )
            return screenshot_path
        except Exception:  # noqa: BLE001
            try:
                if os.path.exists(screenshot_path):
                    os.remove(screenshot_path)
            except OSError:
                pass
            return None
        finally:
            try:
                if os.path.exists(raw_path):
                    os.remove(raw_path)
            except OSError:
                pass

    @staticmethod
    async def _probe_scroll(session, sid, state: dict) -> dict:
        max_scroll = float(state.get("maxScroll") or 0)
        if max_scroll < 160:
            return {"status": "not_scrollable", "label": "页面暂无足够滚动空间"}
        before = float(state.get("scrollTop") or 0)
        positions = [before]
        try:
            for delta in (520, -520):
                await session.cmd("Input.dispatchMouseEvent", {
                    "type": "mouseWheel", "x": 500, "y": 400,
                    "deltaX": 0, "deltaY": delta,
                }, session_id=sid, timeout=3.0)
                await asyncio.sleep(0.18)
                current = await asyncio.wait_for(session.eval(
                    "(() => { const s = document.scrollingElement || document.documentElement || document.body; "
                    "return Number(s?.scrollTop || window.scrollY || 0); })()", sid,
                ), timeout=3.0)
                positions.append(float(current or 0))
            await asyncio.wait_for(session.eval(
                f"window.scrollTo(0, {max(0, int(before))}); true", sid
            ), timeout=3.0)
        except Exception as exc:  # noqa: BLE001
            return {"status": "unknown", "label": f"滚轮探测失败：{type(exc).__name__}"}
        moved = any(abs(positions[idx] - positions[idx - 1]) >= 2
                    for idx in range(1, len(positions)))
        if moved:
            return {"status": "ok", "label": "上下滚动均可响应"}
        return {"status": "locked", "label": "上下滚动均无变化"}

    @staticmethod
    def _platform_row(platform: str, label: str, details: list[dict]) -> dict:
        priority = {HealthStatus.OK: 0, HealthStatus.WARNING: 1,
                    HealthStatus.LOGIN_REQUIRED: 2, HealthStatus.HUMAN_REQUIRED: 3,
                    HealthStatus.FAILED: 4}
        status = max((item.get("status") for item in details),
                     key=lambda value: priority.get(value, 4), default=HealthStatus.WARNING)
        if not details:
            status = HealthStatus.WARNING
        messages = [f"{item['account']}：{item['detail']}" for item in details]
        return {
            "platform": platform, "platform_label": label, "status": status,
            "status_label": {HealthStatus.OK: "正常", HealthStatus.WARNING: "需检查",
                              HealthStatus.LOGIN_REQUIRED: "需登录",
                              HealthStatus.HUMAN_REQUIRED: "需人工",
                              HealthStatus.FAILED: "异常"}.get(status, "未知"),
            "adapter": "浏览器级", "reply": "只读检查", "accounts": len(details),
            "detail": "；".join(messages) if messages else "没有绑定账号，未执行浏览器诊断",
            "account_details": details,
        }

    @staticmethod
    def _is_platform_home(url: str, platform: str) -> bool:
        """判断当前是否为平台首页；首页没有评论区不应被判为异常。"""
        _label, domains, home_url = PLATFORM_INFO.get(platform, ("", (), ""))
        current = urlsplit(str(url or "").lower())
        home = urlsplit(home_url.lower())
        if not current.netloc or not any(domain in current.netloc for domain in domains):
            return False
        current_path = current.path.rstrip("/") or "/"
        home_path = home.path.rstrip("/") or "/"
        return current_path == home_path

    @staticmethod
    async def _attach_health_page(session, platform: str):
        """选择真实平台页，避免盲目附着 BitBrowser 导航页。"""
        try:
            targets = await session.cmd("Target.getTargets", timeout=5.0)
        except Exception:
            sid = await session.attach_page(create_if_missing=False)
            return sid, ""
        pages = [item for item in targets.get("targetInfos", [])
                 if item.get("type") == "page"]
        if not pages:
            sid = await session.attach_page(create_if_missing=False)
            return sid, ""
        domains = PLATFORM_INFO.get(platform, ("", (), ""))[1]
        expected = next((item for item in pages if any(
            domain in str(item.get("url") or "").lower() for domain in domains)), None)
        fallback = next((item for item in pages if not any(
            marker in str(item.get("url") or "").lower()
            for marker in ("console.bitbrowser.net", "about:blank"))), pages[0])
        selected = expected or fallback
        result = await session.cmd(
            "Target.attachToTarget", {"targetId": selected["targetId"], "flatten": True},
            timeout=5.0,
        )
        sid = result["sessionId"]
        await session.cmd("Page.enable", session_id=sid, timeout=5.0)
        await session.cmd("Runtime.enable", session_id=sid, timeout=5.0)
        return sid, str(selected.get("url") or "")
