# -*- coding: utf-8 -*-
"""私信互动适配器。

私信与评论回复使用不同的目标：私信只需要定位到用户主页/会话，不需要来源
作品和原评论。适配器仍然遵循“先填充、后确认发送”的安全边界；默认调用
``confirm=False``，只验证输入框，不点击平台最终发送按钮。

平台页面经常是 SPA 且 DOM 会变化，因此这里不保存屏幕坐标，只使用当前页
实时解析出的可见元素和盒模型信息。具体平台选择器集中在同一个脚本中，便于
后续按真实浏览器诊断结果逐个平台收敛。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

from debug_trace import DebugTrace
from .models import ReplyActionResult


class PrivateMessageError(RuntimeError):
    """私信浏览器连接、目标定位或输入失败。"""


@dataclass(frozen=True)
class PrivateMessageTarget:
    lead_id: int
    draft_id: Optional[int]
    platform: str
    account_id: Optional[int]
    account_name: Optional[str]
    account_status: Optional[str]
    bb_window_id: Optional[str]
    platform_user_id: Optional[str]
    nickname: Optional[str]
    profile_url: Optional[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "draft_id": self.draft_id,
            "platform": self.platform,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "account_status": self.account_status,
            "bb_window_id": self.bb_window_id,
            "platform_user_id": self.platform_user_id,
            "nickname": self.nickname,
            "profile_url": self.profile_url,
        }

    def validate_for_browser(self) -> None:
        missing = []
        if not self.platform:
            missing.append("平台")
        if not self.nickname and not self.platform_user_id:
            missing.append("用户标识")
        if not self.profile_url:
            missing.append("个人主页地址")
        if not self.bb_window_id:
            missing.append("绑定的浏览器窗口")
        if missing:
            raise PrivateMessageError("私信目标信息不完整: " + "、".join(missing))


class PrivateMessageAdapter:
    """私信浏览器适配器接口。"""

    def send_message(self, target: PrivateMessageTarget, content: str,
                     *, confirm: bool = False) -> ReplyActionResult:
        raise NotImplementedError


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise PrivateMessageError("私信操作不能在当前 asyncio 事件循环中同步执行")


def _platform_host_matches(url: str, platform: str) -> bool:
    host = (urlsplit(str(url or "")).hostname or "").lower()
    suffixes = {
        "douyin": ("douyin.com", "iesdouyin.com"),
        "xhs": ("xiaohongshu.com",),
        "bilibili": ("bilibili.com",),
        "weibo": ("weibo.com", "weibo.cn"),
        "kuaishou": ("kuaishou.com", "kuaishou.cn"),
    }.get(str(platform or "").lower(), ())
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _same_page_url(current_url: str, target_url: str) -> bool:
    """比较目标页面，不能只比较平台域名。

    ``/user/self`` 与某个用户主页都属于抖音域名，但前者没有目标用户的
    私信按钮。忽略查询参数后比较 host + path，兼容平台追加的追踪参数。
    """
    current = urlsplit(str(current_url or ""))
    target = urlsplit(str(target_url or ""))
    current_host = (current.hostname or "").lower().rstrip(".")
    target_host = (target.hostname or "").lower().rstrip(".")
    current_path = (current.path or "/").rstrip("/") or "/"
    target_path = (target.path or "/").rstrip("/") or "/"
    return bool(current_host and target_host and
                current_host == target_host and current_path == target_path)


PRIVATE_MESSAGE_PAGE_WAIT_SECONDS = 12.0
PRIVATE_MESSAGE_SCRIPT_RETRY_COUNT = 2
PRIVATE_MESSAGE_RETRY_STAGES = frozenset({
    "private_message_target_page_not_ready",
    "private_message_button_not_found",
    "private_message_input_not_found",
    "private_message_fill_unverified",
})


def _private_page_score(page: Any, target: PrivateMessageTarget) -> int:
    """给私信窗口中的 page target 排序，避免附着到导航/控制台页。

    BitBrowser 的 browser-level CDP 可能同时暴露控制台页、创作者中心页、
    旧的消息页和真正的平台页。旧实现直接取第一个 page，页面切换较快时
    就会在错误的 DOM 上点击私信按钮。这里先按 URL 选页，随后仍会强制导航
    到目标用户主页。
    """
    if not isinstance(page, dict) or str(page.get("type") or "") != "page":
        return -100000
    raw_url = str(page.get("url") or "").strip()
    parsed = urlsplit(raw_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return -100000
    if host in {"console.bitbrowser.net", "bitbrowser.net"}:
        return -100000
    if str(target.platform or "").lower() == "douyin" and host == "creator.douyin.com":
        return -100000
    score = 10
    if _same_page_url(raw_url, target.profile_url or ""):
        score += 300
    elif _platform_host_matches(raw_url, target.platform):
        score += 150
    if page.get("attached"):
        score += 2
    return score


def _private_page_state_script() -> str:
    """返回轻量页面状态，供导航后的稳定性轮询和详细日志使用。"""
    return r"""(() => {
      const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
      const bodyText = clean(document.body?.innerText || '');
      const controls = [...document.querySelectorAll('button,[role="button"],a')];
      const privateButtonCount = controls.filter(el => {
        const text = clean(el.innerText || el.textContent || el.getAttribute('aria-label') ||
          el.getAttribute('title') || '');
        const style = getComputedStyle(el), rect = el.getBoundingClientRect();
        return /^(私信|发私信)$/.test(text) && style.display !== 'none' &&
          style.visibility !== 'hidden' && rect.width > 3 && rect.height > 3;
      }).length;
      const privateNotice = /该账号为私密账号|该用户为私密账号|账号为私密账号|私密账号|私密账户|账号设为私密/.test(bodyText);
      return {
        url: location.href,
        title: document.title,
        readyState: document.readyState,
        visible: document.visibilityState === 'visible',
        hidden: document.hidden,
        timeOrigin: Number(performance.timeOrigin || 0),
        bodyTextLength: bodyText.length,
        privateButtonCount,
        privateNotice,
        hasFocus: document.hasFocus()
      };
    })()"""


async def _attach_private_message_page(session, target: PrivateMessageTarget,
                                       trace: DebugTrace) -> tuple[str, dict[str, Any]]:
    """选择真实平台 page，绝不盲目附着 CDP 返回的第一个 page。"""
    targets = await session.cmd("Target.getTargets", timeout=10.0)
    pages = [item for item in (targets.get("targetInfos", []) if isinstance(targets, dict) else [])
             if isinstance(item, dict) and item.get("type") == "page"]
    trace.emit(
        "page_targets_query_result",
        total_targets=len(targets.get("targetInfos", [])) if isinstance(targets, dict) else 0,
        page_count=len(pages),
        pages=[{
            "target_id": page.get("targetId"),
            "url": page.get("url"),
            "title": page.get("title"),
            "attached": page.get("attached"),
            "score": _private_page_score(page, target),
        } for page in pages],
    )
    candidates = [page for page in pages if _private_page_score(page, target) > -100000]
    if candidates:
        selected = max(
            candidates,
            key=lambda page: (_private_page_score(page, target),
                              str(page.get("targetId") or "")),
        )
        trace.emit(
            "page_selection_decision",
            reason="platform_or_target_url",
            target_id=selected.get("targetId"),
            page_url=selected.get("url", ""),
            score=_private_page_score(selected, target),
        )
        attached = await session.cmd(
            "Target.attachToTarget",
            {"targetId": selected["targetId"], "flatten": True},
            timeout=10.0,
        )
        sid = str(attached.get("sessionId") or "")
        if not sid:
            raise PrivateMessageError("私信页面附着失败")
        await session.cmd("Page.enable", session_id=sid, timeout=10.0)
        await session.cmd("Runtime.enable", session_id=sid, timeout=10.0)
        current = await session.eval(_private_page_state_script(), sid, timeout=10.0)
        trace.emit("page_attach_completed", session_id=sid, page=current)
        return sid, current if isinstance(current, dict) else {}

    # 只有控制台页或空白页时，才使用兼容性回退；后续仍会强制导航到目标页。
    sid = await session.attach_page(create_if_missing=True)
    current = await session.eval(_private_page_state_script(), sid, timeout=10.0)
    trace.emit("page_attach_fallback", session_id=sid, reason="no_platform_page", page=current)
    return sid, current if isinstance(current, dict) else {}


async def _navigate_private_message_page(session, sid: str,
                                         target: PrivateMessageTarget,
                                         trace: DebugTrace, *, attempt: int,
                                         reason: str,
                                         settle_seconds: float) -> dict[str, Any]:
    """每次尝试都回到目标主页，并等待 URL 与 DOM 同时稳定。"""
    current = await session.eval(_private_page_state_script(), sid, timeout=10.0)
    same_page_before = isinstance(current, dict) and _same_page_url(
        current.get("url", ""), target.profile_url or ""
    )
    previous_time_origin = (current or {}).get("timeOrigin") if isinstance(current, dict) else None
    trace.emit(
        "navigation_requested",
        attempt=attempt,
        reason=reason,
        from_page=current,
        to_url=target.profile_url,
    )
    if same_page_before:
        # 同一用户主页可能还残留上一次打开的私信弹层；仅再次 navigate 在
        # 某些 SPA 页面上会保留旧 DOM。强制 reload，确保每一轮从干净页面开始。
        trace.emit("page_reload_requested", attempt=attempt, reason="same_target_page")
        await session.cmd(
            "Page.reload", {"ignoreCache": False}, session_id=sid, timeout=15.0
        )
    else:
        await session.navigate(target.profile_url or "", sid, wait_load=False)

    deadline = asyncio.get_running_loop().time() + PRIVATE_MESSAGE_PAGE_WAIT_SECONDS
    last_state: dict[str, Any] = {}
    last_signature = None
    stable_ready_probes = 0
    probe = 0
    while asyncio.get_running_loop().time() < deadline:
        probe += 1
        state = await session.eval(_private_page_state_script(), sid, timeout=10.0)
        last_state = state if isinstance(state, dict) else {}
        route_ok = isinstance(state, dict) and _same_page_url(
            state.get("url", ""), target.profile_url or ""
        )
        dom_ready = str((state or {}).get("readyState") or "") in {"interactive", "complete"}
        content_ready = bool(
            (state or {}).get("privateButtonCount")
            or (state or {}).get("privateNotice")
            or int((state or {}).get("bodyTextLength") or 0) >= 40
        )
        signature = (
            (state or {}).get("url"), (state or {}).get("readyState"),
            (state or {}).get("privateButtonCount"),
            (state or {}).get("privateNotice"),
            (state or {}).get("bodyTextLength"),
            (state or {}).get("timeOrigin"),
        )
        if probe == 1 or signature != last_signature or probe % 4 == 0:
            trace.emit(
                "private_page_ready_probe",
                attempt=attempt,
                probe=probe,
                route_ok=route_ok,
                dom_ready=dom_ready,
                content_ready=content_ready,
                page=state,
            )
        if route_ok and dom_ready and content_ready:
            stable_ready_probes = (
                stable_ready_probes + 1
                if signature == last_signature else 1
            )
        else:
            stable_ready_probes = 0
        last_signature = signature
        new_document = (
            not same_page_before
            or previous_time_origin in (None, 0)
            or (state or {}).get("timeOrigin") != previous_time_origin
        )
        if (route_ok and dom_ready and content_ready
                and stable_ready_probes >= 2
                and (new_document or probe >= 10)):
            break
        await asyncio.sleep(0.25)

    trace.emit(
        "navigation_completed",
        attempt=attempt,
        page=last_state,
        route_ok=_same_page_url(last_state.get("url", ""), target.profile_url or ""),
        ready_state=last_state.get("readyState"),
    )
    try:
        await session.cmd("Page.bringToFront", session_id=sid, timeout=5.0)
        trace.emit("page_bring_to_front_completed", attempt=attempt)
    except Exception as exc:
        trace.exception("page_bring_to_front_failed", exc, attempt=attempt)
    await asyncio.sleep(max(0.2, float(settle_seconds)))
    return last_state


def _private_message_result_should_retry(result: Any) -> bool:
    return isinstance(result, dict) and str(result.get("stage") or "") in PRIVATE_MESSAGE_RETRY_STAGES


class BitBrowserPrivateMessageAdapter(PrivateMessageAdapter):
    """通过 BitBrowser/CDP 定位个人主页并填入私信内容。"""

    def __init__(self, bitbrowser_client, *, settle_seconds: float = 1.2,
                 trace_log_path: Optional[str] = None):
        self._bb = bitbrowser_client
        self._settle_seconds = max(0.5, float(settle_seconds))
        self._trace_log_path = trace_log_path

    def send_message(self, target: PrivateMessageTarget, content: str,
                     *, confirm: bool = False) -> ReplyActionResult:
        trace = DebugTrace("browser_private_message", log_path=self._trace_log_path)
        trace.emit(
            "private_message_requested",
            target=target.as_dict(),
            content=content,
            confirm=confirm,
        )
        try:
            if not isinstance(content, str) or not content.strip():
                raise PrivateMessageError("私信内容不能为空")
            target.validate_for_browser()
            if not _platform_host_matches(target.profile_url or "", target.platform):
                raise PrivateMessageError("个人主页地址与线索平台不匹配")
            result = _run(self._send_async(target, content.strip(), confirm, trace))
            trace.emit("private_message_completed", result=_result_payload(result))
            return result
        except Exception as exc:
            trace.exception("private_message_failed", exc, confirm=confirm)
            raise

    async def _send_async(self, target: PrivateMessageTarget, content: str,
                          confirm: bool, trace: DebugTrace) -> ReplyActionResult:
        try:
            from cdp import CdpSession
        except ImportError:  # pragma: no cover
            from ..cdp import CdpSession  # type: ignore

        trace.emit(
            "browser_open_requested",
            window_id=target.bb_window_id,
            platform=target.platform,
            profile_url=target.profile_url,
            ignore_default_urls=True,
        )
        opened = self._bb.open_browser(
            target.bb_window_id,
            ignore_default_urls=True,
            new_page_url=target.profile_url,
        )
        if not isinstance(opened, dict):
            raise PrivateMessageError("BitBrowser 未返回浏览器连接信息")
        ws_url = str(opened.get("ws") or opened.get("webSocketDebuggerUrl") or "").strip()
        if not ws_url:
            raise PrivateMessageError("BitBrowser 未返回 CDP 连接地址")

        session = CdpSession(ws_url, timeout=35.0)
        try:
            await session.connect()
            trace.emit("cdp_connect_completed")
            sid, current = await _attach_private_message_page(
                session, target, trace
            )
            trace.emit("page_state_read", page=current)
            result: Any = None
            for attempt in range(1, PRIVATE_MESSAGE_SCRIPT_RETRY_COUNT + 1):
                if attempt > 1:
                    trace.emit(
                        "private_message_retry_started",
                        attempt=attempt,
                        reason="页面状态或私信控件尚未稳定",
                    )
                    await asyncio.sleep(0.6)
                navigation_page = await _navigate_private_message_page(
                    session,
                    sid,
                    target,
                    trace,
                    attempt=attempt,
                    reason=("initial_target_reset" if attempt == 1
                            else "retry_target_reset"),
                    settle_seconds=self._settle_seconds,
                )
                route_ok = isinstance(navigation_page, dict) and _same_page_url(
                    navigation_page.get("url", ""), target.profile_url or ""
                )
                dom_ready = str((navigation_page or {}).get("readyState") or "") in {
                    "interactive", "complete"
                }
                content_ready = bool(
                    (navigation_page or {}).get("privateButtonCount")
                    or (navigation_page or {}).get("privateNotice")
                    or int((navigation_page or {}).get("bodyTextLength") or 0) >= 40
                )
                if not (route_ok and dom_ready and content_ready):
                    result = {
                        "ok": False,
                        "verified": False,
                        "stage": "private_message_target_page_not_ready",
                        "message": "目标用户主页尚未稳定，未执行任何输入",
                        "page": navigation_page,
                        "trace": [{
                            "step": "private_message_target_page_not_ready",
                            "route_ok": route_ok,
                            "dom_ready": dom_ready,
                            "content_ready": content_ready,
                        }],
                    }
                else:
                    result = await session.eval(
                        _private_message_script(
                            target.nickname or "", target.platform_user_id or "", content,
                            confirm, target.platform,
                        ),
                        sid,
                        timeout=45.0,
                    )
                trace.emit(
                    "private_message_script_result",
                    attempt=attempt,
                    result=result,
                )
                if not isinstance(result, dict):
                    result = {
                        "ok": False,
                        "verified": False,
                        "stage": "private_message_invalid_result",
                        "message": "私信页面没有返回可识别结果",
                        "trace": [],
                    }
                if (attempt < PRIVATE_MESSAGE_SCRIPT_RETRY_COUNT
                        and _private_message_result_should_retry(result)):
                    trace.emit(
                        "private_message_retry_scheduled",
                        attempt=attempt,
                        stage=result.get("stage"),
                        message=result.get("message"),
                    )
                    continue
                return ReplyActionResult(
                    ok=bool(result.get("ok")),
                    stage=str(result.get("stage") or "private_message_failed"),
                    message=str(result.get("message") or "私信操作未完成"),
                    verified=bool(result.get("verified")),
                    details=result,
                    target=target,
                )
            raise PrivateMessageError("私信流程重试后仍未完成")
        finally:
            try:
                await session.close()
            except Exception as exc:
                # 点击发送后的关闭异常不应把已经确认的私信改写为失败。
                trace.exception("cdp_session_close_failed", exc)


def _private_message_script(nickname: str, user_id: str, content: str,
                            confirm: bool, platform: str = "") -> str:
    target_json = json.dumps({
        "nickname": nickname,
        "userId": user_id,
        "content": content,
        "confirm": bool(confirm),
        "platform": platform,
    }, ensure_ascii=False).replace("</", "<\\/")
    return f"""
(async function() {{
  const target = {target_json};
  const trace = [];
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
  const visible = el => {{
    if (!el || !el.isConnected) return false;
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' &&
      Number(s.opacity || 1) > 0 && r.width > 3 && r.height > 3;
  }};
  const enabled = el => !el.disabled && el.getAttribute('aria-disabled') !== 'true';
  const label = el => clean(
    el.innerText || el.textContent || el.getAttribute('aria-label') ||
    el.getAttribute('title') || el.getAttribute('placeholder') || ''
  );
  const record = (step, extra) => trace.push(Object.assign({{step}}, extra || {{}}));
  const userText = (target.nickname || target.userId || '').toLowerCase();
  await sleep(300);
  const pageText = clean(document.body?.innerText || '').toLowerCase();
  if (userText && !pageText.includes(userText)) record('target_user_text_not_visible');
  const privateNoticeWords = /该账号为私密账号|该用户为私密账号|账号为私密账号|私密账号|私密账户|账号设为私密/;
  const privateNotice = Array.from(document.querySelectorAll('body *'))
    .filter(el => visible(el) && privateNoticeWords.test(label(el)))
    .sort((a,b) => label(a).length - label(b).length)[0];
  if (privateNotice || privateNoticeWords.test(clean(document.body?.innerText || ''))) {{
    const notice = privateNotice ? label(privateNotice) : '页面提示账号为私密账号';
    const rect = privateNotice?.getBoundingClientRect();
    record('private_account_detected', {{message:notice,
      centered:!!rect && rect.left < innerWidth * .75 && rect.right > innerWidth * .25 &&
        rect.top < innerHeight * .8 && rect.bottom > innerHeight * .2,
      rect:rect ? {{x:rect.x,y:rect.y,w:rect.width,h:rect.height}} : null}});
    return {{ok:false, verified:false, reason:'private_account',
      stage:'private_account_not_supported',
      message:'目标账号为私密账号，无法发送私信', trace}};
  }}
  const platform = String(target.platform || '').toLowerCase();
  // 抖音右上角的“消息”是全局消息中心，不是目标用户私信；抖音只接受
  // 目标主页上的“私信/发私信”，不能把通用“发消息”当作候选按钮。
  const privateWords = platform === 'douyin'
    ? /^(私信|发私信)$/
    : /^(发私信|私信|发送消息|发消息|联系他|联系对方)$/;
  const isControl = el => ['BUTTON','A','INPUT'].includes(el.tagName) ||
    el.getAttribute('role') === 'button';
  // 页面导航完成后仍可能继续挂载组件。每次探测都重新查询 DOM，不能沿用
  // 导航前的 elements 快照，否则会出现“按钮偶尔找不到”的冷启动竞态。
  const privateCandidates = () => Array.from(
    document.querySelectorAll('button,[role="button"],a,span,div')
  ).filter(el => isControl(el) && visible(el) && enabled(el) &&
    privateWords.test(label(el))).sort((a,b) => {{
      const score = el => {{
        const text = label(el);
        let value = text === '私信' ? 50 : (text === '发私信' ? 45 : 20);
        const context = clean(el.closest('main,section,article,[class*="profile"],[class*="user"]')?.innerText || '');
        if (target.nickname && context.includes(target.nickname)) value += 25;
        if (target.userId && context.includes(target.userId)) value += 25;
        if (el.closest('[role="dialog"],.modal,.drawer')) value += 8;
        if (el.tagName.toLowerCase() === 'button' || el.getAttribute('role') === 'button') value += 4;
        return value;
      }};
      return score(b) - score(a);
    }});
  const buttonSummary = candidates => candidates.slice(0, 8).map(el => {{
    const r=el.getBoundingClientRect();
    return {{text:label(el), tag:el.tagName, x:r.x, y:r.y, w:r.width, h:r.height}};
  }});
  let privateButton = null;
  let latestCandidates = [];
  let previousButtonSignature = '';
  let stableButtonProbes = 0;
  for (let attempt = 1; attempt <= 32; attempt++) {{
    latestCandidates = privateCandidates();
    const candidate = latestCandidates[0];
    if (candidate) {{
      const r = candidate.getBoundingClientRect();
      const signature = [candidate.tagName, label(candidate),
        Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)].join('|');
      stableButtonProbes = signature === previousButtonSignature
        ? stableButtonProbes + 1 : 1;
      previousButtonSignature = signature;
      record('private_message_button_probe', {{attempt, count:latestCandidates.length,
        stableProbes:stableButtonProbes, candidate:{{text:label(candidate),
          tag:candidate.tagName, x:r.x, y:r.y, w:r.width, h:r.height}}}});
      if (stableButtonProbes >= 2) {{
        privateButton = candidate;
        break;
      }}
    }} else {{
      stableButtonProbes = 0;
      previousButtonSignature = '';
      record('private_message_button_probe', {{attempt, count:0,
        stableProbes:0, candidate:null}});
    }}
    await sleep(250);
  }}
  // 二次取最新节点，避免 React/Vue 重绘后继续使用已脱离 DOM 的按钮。
  const freshPrivateButton = privateCandidates()[0];
  if (freshPrivateButton) privateButton = freshPrivateButton;
  latestCandidates = privateCandidates();
  record('private_message_button_candidates', {{count:latestCandidates.length,
    items:buttonSummary(latestCandidates)}});
  if (!privateButton || !privateButton.isConnected || !visible(privateButton) || !enabled(privateButton)) return {{ok:false, verified:false,
    stage:'private_message_button_not_found', message:'未找到明确的私信按钮', trace}};
  record('private_message_button_selected', {{text:label(privateButton), tag:privateButton.tagName,
    rect:(() => {{ const r=privateButton.getBoundingClientRect(); return {{x:r.x,y:r.y,w:r.width,h:r.height}}; }})()}});
  privateButton.click();
  const afterClickUrl = location.href;
  record('private_message_button_clicked', {{page:{{url:location.href, title:document.title,
    readyState:document.readyState, visibility:document.visibilityState,
    hidden:document.hidden}}}});
  const inputSelector = 'textarea,input:not([type="hidden"]):not([type="search"]),[contenteditable="true"],[role="textbox"],[data-placeholder]';
  const inputContext = el => clean(el.closest(
    '[role="dialog"],.modal,.drawer,[class*="dialog"],[class*="modal"],[class*="drawer"],[class*="chat"],[class*="message"],[class*="im"]'
  )?.innerText || '');
  const inputCandidates = () => Array.from(document.querySelectorAll(inputSelector))
    .filter(el => visible(el) && enabled(el))
    .map(el => {{
      const placeholder = clean(el.getAttribute('placeholder') || el.getAttribute('data-placeholder') ||
        el.getAttribute('aria-label') || '').toLowerCase();
      const context = inputContext(el).toLowerCase();
      const description = placeholder + ' ' + context;
      const searchOnly = /搜索|查找|关键词|search|搜索用户|搜索内容/.test(description) &&
        !/私信|消息|发送|输入|写下/.test(description);
      let score = searchOnly ? -100 : 0;
      if (inputContext(el)) score += 100;
      if (/私信|消息|发送|输入|写下/.test(description)) score += 40;
      if (el.isContentEditable || el.getAttribute('contenteditable') === 'true') score += 30;
      if (el.tagName === 'TEXTAREA') score += 20;
      if (el.getAttribute('role') === 'textbox' || el.hasAttribute('data-placeholder')) score += 10;
      return {{el, score, placeholder, contextLength:inputContext(el).length}};
    }})
    .filter(item => item.score > 0)
    .sort((a,b) => b.score - a.score);
  let candidates = [];
  for (let attempt = 1; attempt <= 40; attempt++) {{
    candidates = inputCandidates();
    const currentUrl = location.href;
    if (attempt === 1 || attempt % 5 === 0 || currentUrl !== afterClickUrl) {{
      record('private_message_input_state_probe', {{attempt,
        url:currentUrl, title:document.title, readyState:document.readyState,
        visibility:document.visibilityState, hidden:document.hidden,
        candidateCount:candidates.length}});
    }}
    if (candidates.length) break;
    await sleep(250);
  }}
  const input = candidates.length ? candidates[0].el : null;
  record('private_message_input_probe', {{count:candidates.length,
    page:{{url:location.href, title:document.title, readyState:document.readyState,
      visibility:document.visibilityState, hidden:document.hidden}},
    candidates:candidates.slice(0, 8).map(item => {{ const r=item.el.getBoundingClientRect();
      return {{score:item.score, tag:item.el.tagName, placeholder:item.placeholder,
        x:r.x, y:r.y, w:r.width, h:r.height}}; }})}});
  if (!input) return {{ok:false, verified:false,
    stage:'private_message_input_not_found', message:'点击私信后未找到消息输入框', trace}};
  record('private_message_input_selected', {{tag:input.tagName,
    placeholder:input.getAttribute('placeholder') || '', contenteditable:input.isContentEditable}});
  input.focus();
  if (input.isContentEditable || input.getAttribute('contenteditable') === 'true' ||
      input.getAttribute('role') === 'textbox' || input.hasAttribute('data-placeholder')) {{
    input.textContent = target.content;
  }} else {{
    const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(input, target.content); else input.value = target.content;
  }}
  input.dispatchEvent(new InputEvent('input', {{bubbles:true, inputType:'insertText', data:target.content}}));
  input.dispatchEvent(new Event('change', {{bubbles:true}}));
  await sleep(250);
  const actual = clean(input.isContentEditable ? input.innerText || input.textContent : input.value);
  const verified = actual === clean(target.content) || actual.includes(clean(target.content));
  record('private_message_input_verified', {{verified, actualLength:actual.length,
    expectedLength:clean(target.content).length}});
  if (!verified) return {{ok:false, verified:false,
    stage:'private_message_fill_unverified', message:'私信内容已尝试填入，但页面未确认输入', trace}};
  if (!target.confirm) return {{ok:true, verified:true,
    stage:'filled_waiting_confirmation', message:'私信内容已填入，未点击发送', trace}};
  const sendWords = /^(发送|发消息|发送消息|确认发送|立即发送)$/;
  const sends = Array.from(document.querySelectorAll('button,[role="button"],a,input'))
    .filter(el => isControl(el) && visible(el) && enabled(el) && sendWords.test(label(el)))
    .filter(el => {{
      const nearInput = Math.abs(el.getBoundingClientRect().bottom - input.getBoundingClientRect().bottom) < 180;
      return el !== privateButton &&
        (el.closest('[role="dialog"],.modal,.drawer') || nearInput);
    }})
    .sort((a,b) => {{
      const area = el => {{ const r=el.getBoundingClientRect(); return r.width*r.height; }};
      return area(a)-area(b);
    }});
  const send = sends[0];
  if (!send) return {{ok:false, verified:true,
    stage:'private_message_send_button_not_found', message:'已填入私信，但未找到明确的发送按钮', trace}};
  record('private_message_send_button_selected', {{text:label(send), tag:send.tagName,
    rect:(() => {{ const r=send.getBoundingClientRect(); return {{x:r.x,y:r.y,w:r.width,h:r.height}}; }})()}});
  send.click();
  record('private_message_send_button_clicked');
  // 点击后私信面板可能要等待网络回执才清空输入框。只采样一次会把
  // 已经发出的私信误判成失败，随后用户再次点击就可能重复发送。
  let after = '';
  let sent = false;
  for (let verifyAttempt = 1; verifyAttempt <= 24; verifyAttempt++) {{
    await sleep(500);
    after = clean(input.isConnected ? (input.isContentEditable
      ? input.innerText || input.textContent : input.value) : '');
    sent = !input.isConnected || after.length === 0 || after !== clean(target.content);
    record('private_message_send_confirmation_probe', {{attempt:verifyAttempt,
      inputConnected:input.isConnected, remainingLength:after.length, sent}});
    if (sent) break;
  }}
  return {{ok:sent, verified:sent, clicked:true,
    stage:sent ? 'private_message_sent' : 'private_message_send_unconfirmed',
    message:sent ? '已点击私信发送按钮并确认输入框变化' : '已点击私信发送按钮，但页面未确认发送结果', trace}};
}})()
"""


def _result_payload(result: ReplyActionResult) -> dict[str, Any]:
    return {
        "ok": bool(result.ok),
        "stage": result.stage,
        "message": result.message,
        "verified": bool(result.verified),
    }


__all__ = [
    "PrivateMessageError", "PrivateMessageTarget", "PrivateMessageAdapter",
    "BitBrowserPrivateMessageAdapter",
]
