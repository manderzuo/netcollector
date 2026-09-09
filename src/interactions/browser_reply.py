# -*- coding: utf-8 -*-
"""BitBrowser/CDP 回复适配器。

本模块只负责把已审核文本定位并填入当前网页；真正点击发送必须由调用方
显式传入 ``confirm=True``。平台 DOM 经常变化，定位器采用“来源作品内的
昵称 + 评论正文/平台评论 ID”组合匹配，并把不确定状态返回给上层。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

from debug_trace import DebugTrace
from .models import ReplyActionResult
from .reply_target import ReplyTarget, ReplyTargetError


class BrowserReplyError(RuntimeError):
    """浏览器连接、定位或输入失败。"""


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise BrowserReplyError("浏览器回复不能在当前 asyncio 事件循环中同步执行")


class BrowserReplyAdapter:
    """浏览器回复适配器接口。"""

    def reply(self, target: ReplyTarget, content: str,
              *, confirm: bool = False) -> ReplyActionResult:
        raise NotImplementedError


class BitBrowserReplyAdapter(BrowserReplyAdapter):
    """通过 BitBrowser 返回的浏览器级 CDP WS 执行定位和回复。"""

    def __init__(self, bitbrowser_client, *, settle_seconds: float = 1.2,
                 trace_log_path: Optional[str] = None):
        self._bb = bitbrowser_client
        self._settle_seconds = max(0.2, float(settle_seconds))
        self._trace_log_path = trace_log_path

    def reply(self, target: ReplyTarget, content: str,
              *, confirm: bool = False) -> ReplyActionResult:
        trace = DebugTrace("browser_reply", log_path=self._trace_log_path)
        trace.emit(
            "reply_requested",
            target=target.as_dict() if isinstance(target, ReplyTarget) else target,
            content=content,
            confirm=confirm,
        )
        try:
            if not isinstance(content, str) or not content.strip():
                trace.emit("validation_failed", reason="empty_reply_content")
                raise ReplyTargetError("回复内容不能为空")
            target.validate_for_browser()
            trace.emit("target_validated", target=target.as_dict())
            result = _run(
                self._reply_async(target, content, confirm=confirm, trace=trace)
            )
            result = _decorate_result(result, trace)
            trace.emit("reply_completed", result=_result_payload(result))
            return result
        except Exception as exc:
            trace.exception("reply_failed", exc, confirm=confirm)
            raise

    async def _reply_async(self, target: ReplyTarget, content: str,
                           *, confirm: bool, trace: DebugTrace) -> ReplyActionResult:
        from cdp import CdpSession

        trace.emit(
            "browser_open_requested",
            window_id=target.bb_window_id,
            platform=target.platform,
            video_url=target.video_url,
            ignore_default_urls=True,
            new_page_url=None,
        )
        try:
            opened = self._bb.open_browser(
                target.bb_window_id, ignore_default_urls=True
            )
        except Exception as exc:
            trace.exception("browser_open_failed", exc, window_id=target.bb_window_id)
            raise
        ws_url = opened.get("ws") if isinstance(opened, dict) else None
        trace.emit(
            "browser_open_result",
            window_id=target.bb_window_id,
            response_keys=list(opened.keys()) if isinstance(opened, dict) else [],
            ws_available=bool(ws_url),
        )
        if not ws_url:
            trace.emit("browser_open_failed", reason="missing_cdp_websocket")
            raise BrowserReplyError("BitBrowser 未返回 CDP WebSocket 地址")

        # 浏览器刚启动时首个 CDP 命令可能明显慢于常规页面操作，
        # 把默认命令窗口提高到 45 秒，具体脚本仍使用各自的更长超时。
        session = CdpSession(ws_url, timeout=45.0)
        trace.emit("cdp_connect_started", websocket_available=True)
        try:
            await session.connect()
        except Exception as exc:
            trace.exception("cdp_connect_failed", exc)
            raise
        trace.emit("cdp_connect_completed")
        try:
            sid, current_url, target_already_loaded = await _attach_reply_page(
                session, target, trace=trace
            )
            trace.emit(
                "page_selected",
                session_id=sid,
                current_url=current_url,
                target_already_loaded=target_already_loaded,
                target_url=target.video_url,
                same_url=_same_url(current_url, target.video_url),
            )
            if not (target_already_loaded and _same_url(current_url, target.video_url)):
                trace.emit(
                    "navigation_requested",
                    session_id=sid,
                    from_url=current_url,
                    to_url=target.video_url,
                    reason=("target_not_loaded" if not target_already_loaded
                            else "selected_page_not_target_url"),
                )
                await session.navigate(target.video_url, sid, wait_load=False)
                trace.emit("navigation_command_completed", session_id=sid)
                await asyncio.sleep(self._settle_seconds)
                trace.emit("page_settle_completed", seconds=self._settle_seconds)
            else:
                await asyncio.sleep(min(self._settle_seconds, 1.0))
                trace.emit(
                    "page_settle_completed",
                    seconds=min(self._settle_seconds, 1.0),
                    reason="target_page_already_loaded",
                )
            # BitBrowser 打开窗口后，Page.navigate 已返回并不代表目标页
            # 已经完成路由切换。慢机器上这里若只采样一次，会把仍在加载的
            # 正确作品误判成“目标页面不匹配”。
            page_state = await _wait_for_target_page_state(
                session, sid, target.video_url, trace=trace,
            )
            trace.emit("page_state_checked", session_id=sid, page_state=page_state)
            if (isinstance(page_state, dict)
                    and _same_url(page_state.get("url"), target.video_url)
                    and (page_state.get("hidden")
                         or page_state.get("visibility") != "visible")):
                page_state = await _bring_target_page_to_front(
                    session, sid, target.video_url, trace=trace,
                )
            if (not isinstance(page_state, dict)
                    or not _same_url(page_state.get("url"), target.video_url)):
                trace.emit(
                    "target_page_mismatch",
                    expected_url=target.video_url,
                    page_state=page_state,
                )
                return ReplyActionResult(
                    ok=False,
                    stage="target_page_mismatch",
                    message="当前可见页面不是目标作品，未执行任何输入",
                    details=page_state if isinstance(page_state, dict) else {},
                    target=target,
                )
            if (page_state.get("hidden")
                    or page_state.get("visibility") != "visible"):
                trace.emit(
                    "target_page_not_visible",
                    expected_url=target.video_url,
                    page_state=page_state,
                )
                return ReplyActionResult(
                    ok=False,
                    stage="target_page_not_visible",
                    message="目标作品已打开，但页面未激活，未执行任何输入",
                    details=page_state,
                    target=target,
                )
            target_probe = await _wait_for_target_match(
                session, target, sid, trace=trace
            )
            trace.emit(
                "target_content_wait_completed",
                matched=bool(isinstance(target_probe, dict)
                             and target_probe.get("matched")),
                probe=target_probe,
            )
            probe_url = (target_probe or {}).get("url") if isinstance(target_probe, dict) else ""
            if probe_url and not _same_url(probe_url, target.video_url):
                trace.emit(
                    "target_page_redirected",
                    expected_url=target.video_url,
                    observed_url=probe_url,
                    probe=target_probe,
                )
                return ReplyActionResult(
                    ok=False,
                    stage="target_page_redirected",
                    message="目标作品地址已跳转到其他页面，原作品可能已失效或当前账号不可访问",
                    details={"expected_url": target.video_url,
                             "observed_url": probe_url},
                    target=target,
                )
            trace.emit("fill_script_started", session_id=sid)
            # 评论区滚动加载、Note 面板挂载和虚拟列表重绘可能共同超过
            # CDP 默认 30 秒。填充脚本需要独立的执行窗口，不能被外层
            # 命令超时截断后误报“未找到评论”。
            filled = await session.eval(
                _fill_script(target, content), sid, timeout=75.0
            )
            if isinstance(filled, dict) and filled.get("requiresDomReplyActivation"):
                # 抖音评论列表是虚拟化 DOM；脚本 click 在某些节点上不会触发
                # 站点真实的事件链。先实时解析目标 DOM 控件，再由浏览器自动化
                # 根据该控件当前的盒模型动态点击；不保存、不写死任何坐标。
                filled = dict(filled)
                trace_items = list(filled.get("trace") or [])
                native_result = None
                dynamic_result = None
                for attempt in range(1, 3):
                    dynamic_result = await _click_reply_button_dynamically(
                        session, sid, target, trace=trace
                    )
                    trace.emit(
                        "reply_button_dynamic_click_completed",
                        session_id=sid,
                        attempt=attempt,
                        result=dynamic_result,
                    )
                    trace_items.append({
                        "step": "reply_button_dynamic_click_completed",
                        "attempt": attempt,
                        "result": dynamic_result,
                    })
                    if not (isinstance(dynamic_result, dict)
                            and dynamic_result.get("ok")):
                        await asyncio.sleep(0.25)
                        continue
                    await asyncio.sleep(0.45)
                    native_result = await session.eval(
                        _reply_input_probe_script(target, content), sid
                    )
                    trace.emit(
                        "dynamic_reply_click_probe_completed",
                        session_id=sid,
                        attempt=attempt,
                        result=native_result,
                    )
                    trace_items.append({
                        "step": "dynamic_reply_click_probe_completed",
                        "attempt": attempt,
                        "result": native_result,
                    })
                    if isinstance(native_result, dict) and native_result.get("ok"):
                        break
                    if attempt < 2:
                        await asyncio.sleep(0.35)
                if isinstance(native_result, dict) and native_result.get("ok"):
                    filled = dict(native_result)
                    filled["trace"] = trace_items + list(filled.get("trace") or [])
                    filled["dynamicDomClickUsed"] = True
                else:
                    filled["trace"] = trace_items
                    filled["ok"] = False
                    filled["stage"] = "reply_input_not_found"
                    filled["message"] = "动态点击目标回复控件后未挂载回复输入框，未执行任何输入"
                    filled["verified"] = False
            if isinstance(filled, dict) and filled.get("requiresCdpInput"):
                trace.emit("cdp_text_input_started", session_id=sid)
                filled = dict(filled)
                filled["trace"] = list(filled.get("trace") or [])
                try:
                    await session.cmd(
                        "Input.insertText", {"text": content}, session_id=sid
                    )
                    filled["trace"].append({
                        "step": "cdp_input_insert_text_completed",
                        "method": "Input.insertText",
                    })
                    trace.emit(
                        "cdp_text_input_command_completed",
                        session_id=sid,
                        method="Input.insertText",
                    )
                    immediate = await session.eval(
                        _input_feedback_script(content, target.platform), sid
                    )
                    trace.emit(
                        "cdp_text_input_immediate_feedback",
                        feedback=immediate,
                    )
                    await asyncio.sleep(0.1)
                    settled = await session.eval(
                        _input_feedback_script(content, target.platform), sid
                    )
                    trace.emit(
                        "cdp_text_input_settle_feedback",
                        feedback=settled,
                    )
                    # B 站评论组件在 CDP 输入后可能短暂卸载再挂载；
                    # 单次 100ms 采样会把这种过渡态误判为输入失败。
                    if (_platform_key(target.platform) in ("bilibili", "bili")
                            and not (isinstance(settled, dict)
                                     and settled.get("ok"))):
                        recovery_deadline = (
                            asyncio.get_running_loop().time() + 2.0
                        )
                        recovery_attempt = 0
                        while (asyncio.get_running_loop().time()
                               < recovery_deadline):
                            recovery_attempt += 1
                            await asyncio.sleep(0.15)
                            recovery = await session.eval(
                                _input_feedback_script(
                                    content, target.platform
                                ), sid
                            )
                            trace.emit(
                                "cdp_text_input_recovery_feedback",
                                attempt=recovery_attempt,
                                feedback=recovery,
                            )
                            settled = recovery
                            if (isinstance(recovery, dict)
                                    and recovery.get("ok")):
                                break
                    feedback = settled if isinstance(settled, dict) else immediate
                    filled["cdpInputFeedback"] = {
                        "immediate": immediate,
                        "settled": settled,
                    }
                    filled["trace"].append({
                        "step": "cdp_input_feedback_checked",
                        "immediate": immediate,
                        "settled": settled,
                    })
                    verified = bool(isinstance(feedback, dict)
                                    and feedback.get("ok"))
                    filled.update({
                        "ok": verified,
                        "stage": "filled" if verified else "fill_unverified",
                        "message": ("回复已填入输入框" if verified
                                    else "浏览器级输入后输入框或评论区状态异常"),
                        "verified": verified,
                    })
                except Exception as exc:
                    filled["ok"] = False
                    filled["stage"] = "cdp_input_failed"
                    filled["message"] = "浏览器级输入失败"
                    filled["verified"] = False
                    filled["trace"].append({
                        "step": "cdp_input_insert_text_failed",
                        "error": str(exc),
                    })
                    trace.exception("cdp_text_input_failed", exc)
            trace.emit("fill_script_result", result=filled)
            if not isinstance(filled, dict) or not filled.get("ok"):
                trace.emit(
                    "reply_fill_failed",
                    stage=(filled.get("stage", "fill_failed")
                           if isinstance(filled, dict) else "fill_failed"),
                )
                return ReplyActionResult(
                    ok=False,
                    stage=filled.get("stage", "fill_failed") if isinstance(filled, dict) else "fill_failed",
                    message=(filled.get("message", "无法填入回复框")
                             if isinstance(filled, dict) else "无法填入回复框"),
                    details=filled if isinstance(filled, dict) else {},
                    target=target,
                )
            if not confirm:
                trace.emit(
                    "confirmation_required",
                    stage="filled_waiting_confirmation",
                    send_script_executed=False,
                    filled=filled,
                )
                return ReplyActionResult(
                    ok=True,
                    stage="filled_waiting_confirmation",
                    message="回复已填入，等待人工确认发送",
                    verified=bool(filled.get("verified")),
                    details=filled,
                    target=target,
                )

            trace.emit("submit_script_started", session_id=sid)
            submitted = await session.eval(
                _submit_script(content, target.platform), sid
            )
            trace.emit("submit_script_result", result=submitted)
            if not isinstance(submitted, dict) or not submitted.get("clicked"):
                trace.emit(
                    "reply_submit_failed",
                    stage=(submitted.get("stage", "submit_failed")
                           if isinstance(submitted, dict) else "submit_failed"),
                )
                return ReplyActionResult(
                    ok=False,
                    stage=(submitted.get("stage", "submit_failed")
                           if isinstance(submitted, dict) else "submit_failed"),
                    message=(submitted.get("message", "未找到明确的发送按钮")
                             if isinstance(submitted, dict) else "未找到明确的发送按钮"),
                    verified=False,
                    details=submitted if isinstance(submitted, dict) else {},
                    target=target,
                )
            await asyncio.sleep(0.8)
            trace.emit("post_submit_settle_completed", seconds=0.8)
            trace.emit("verify_script_started", session_id=sid)
            # 平台点击发送后，评论列表和输入框经常异步刷新。给结果一段
            # 独立的确认窗口，避免页面慢时把已经发出的评论误记成失败。
            verify_deadline = asyncio.get_running_loop().time() + 12.0
            verified = None
            verify_attempt = 0
            while True:
                verify_attempt += 1
                verified = await session.eval(
                    _verify_script(content, target.platform), sid, timeout=20.0,
                )
                trace.emit(
                    "verify_script_result",
                    attempt=verify_attempt,
                    result=verified,
                )
                if isinstance(verified, dict) and verified.get("ok"):
                    break
                if asyncio.get_running_loop().time() >= verify_deadline:
                    break
                await asyncio.sleep(0.5)
            verified_ok = bool(isinstance(verified, dict) and verified.get("ok"))
            return ReplyActionResult(
                ok=verified_ok,
                stage="sent" if verified_ok else "submit_uncertain",
                message=("已发送并在页面确认" if verified_ok
                         else "已点击发送，但页面未确认回复结果"),
                verified=verified_ok,
                details={"submit": submitted, "verify": verified},
                target=target,
            )
        finally:
            trace.emit("cdp_session_close_started")
            try:
                await session.close()
                trace.emit("cdp_session_closed")
            except Exception as exc:
                # 回复结果已经返回后，CDP 关闭异常不能覆盖“已发送”状态；
                # 记录即可，避免后台把一次成功发送改写成失败。
                trace.exception("cdp_session_close_failed", exc)
                pass


def _result_payload(result: ReplyActionResult) -> dict:
    """把动作结果压成日志可读的快照，不记录不可序列化对象。"""
    return {
        "ok": result.ok,
        "stage": result.stage,
        "message": result.message,
        "verified": result.verified,
        "details": result.details,
        "target": result.target.as_dict() if result.target else None,
    }


def _decorate_result(result: ReplyActionResult, trace: DebugTrace) -> ReplyActionResult:
    """在返回结果中带上日志索引，便于从 UI/后台结果反查完整过程。"""
    details = dict(result.details or {})
    details.setdefault("debug_run_id", trace.run_id)
    details.setdefault("debug_log_path", trace.path)
    result.details = details
    return result


def _same_url(left: str, right: str) -> bool:
    """比较作品地址的稳定部分，兼容平台重编码/重排 query 参数。"""
    left_raw = str(left or '').split('#', 1)[0].rstrip('/')
    right_raw = str(right or '').split('#', 1)[0].rstrip('/')
    if left_raw == right_raw:
        return True
    try:
        left_parts = urlsplit(left_raw)
        right_parts = urlsplit(right_raw)
        left_key = (
            left_parts.scheme.lower(), left_parts.netloc.lower(),
            unquote(left_parts.path).rstrip('/') or '/',
        )
        right_key = (
            right_parts.scheme.lower(), right_parts.netloc.lower(),
            unquote(right_parts.path).rstrip('/') or '/',
        )
        # 作品 ID 位于 path；平台常会解码 token、丢弃 token 或重排 query，
        # 不能因此把同一个作品误判成错误页面。
        return left_key == right_key and bool(left_key[2] not in ('', '/'))
    except ValueError:
        return False


def _page_state_script() -> str:
    return """(() => ({
      url: location.href,
      title: document.title,
      hidden: document.hidden,
      visibility: document.visibilityState,
      hasFocus: document.hasFocus()
    }))()"""


async def _bring_target_page_to_front(session, sid: str, target_url: str,
                                      *, trace: Optional[DebugTrace] = None,
                                      timeout: float = 2.0) -> dict:
    """URL 已正确但标签页隐藏时，激活该页并等待可见。

    激活失败或浏览器窗口最小化时仍保持只读，不继续定位/输入；调用方会
    返回准确的 ``target_page_not_visible``，不再误报“不是目标作品”。
    """
    if trace:
        trace.emit("target_page_activation_started", session_id=sid,
                   target_url=target_url)
    try:
        await session.cmd("Page.bringToFront", session_id=sid)
        if trace:
            trace.emit("target_page_activation_command_completed", session_id=sid)
    except Exception as exc:
        if trace:
            trace.exception("target_page_activation_failed", exc, session_id=sid)
    deadline = asyncio.get_running_loop().time() + max(0.2, float(timeout))
    attempt = 0
    last = {}
    while True:
        attempt += 1
        try:
            last = await session.eval(_page_state_script(), sid)
        except Exception as exc:
            if trace:
                trace.exception("target_page_activation_probe_failed", exc,
                                session_id=sid, attempt=attempt)
            return {}
        visible = bool(
            isinstance(last, dict)
            and _same_url(last.get("url"), target_url)
            and not last.get("hidden")
            and last.get("visibility") == "visible"
        )
        if trace:
            trace.emit("target_page_activation_probe", session_id=sid,
                       attempt=attempt, visible=visible, page_state=last)
        if visible or asyncio.get_running_loop().time() >= deadline:
            return last if isinstance(last, dict) else {}
        await asyncio.sleep(0.2)


async def _wait_for_target_page_state(session, sid: str, target_url: str,
                                      *, trace: Optional[DebugTrace] = None,
                                      timeout: float = 15.0) -> dict:
    """等待导航真正切换到目标作品页，再开始评论定位。"""
    deadline = asyncio.get_running_loop().time() + max(1.0, float(timeout))
    attempt = 0
    last = {}
    while True:
        attempt += 1
        try:
            last = await session.eval(_page_state_script(), sid, timeout=15.0)
        except Exception as exc:
            if trace:
                trace.exception("target_page_state_probe_failed", exc,
                                attempt=attempt)
            last = {}
        route_ok = isinstance(last, dict) and _same_url(
            last.get("url"), target_url
        )
        visible = bool(
            isinstance(last, dict)
            and not last.get("hidden")
            and last.get("visibility") == "visible"
        )
        if trace:
            trace.emit(
                "target_page_state_probe",
                attempt=attempt,
                route_ok=route_ok,
                visible=visible,
                page_state=last,
            )
        if route_ok and (visible or not isinstance(last, dict)):
            return last
        if route_ok and isinstance(last, dict):
            # 页面 URL 已到位但标签暂时隐藏，交给后续 bringToFront 流程处理。
            return last
        if asyncio.get_running_loop().time() >= deadline:
            return last if isinstance(last, dict) else {}
        await asyncio.sleep(0.35)


async def _wait_for_target_match(session, target: ReplyTarget, sid: str,
                                 *, trace: Optional[DebugTrace] = None,
                                 timeout: float = 20.0):
    """等待平台评论组件挂载，避免动态评论尚未出现就开始定位。"""
    platform = _platform_key(target.platform)
    # 微博评论列表是懒加载的，首屏命中前需要给分段滚动和网络回填留出时间。
    effective_timeout = max(float(timeout), 14.0) if platform in ("weibo", "wb") else float(timeout)
    deadline = asyncio.get_running_loop().time() + max(0.5, effective_timeout)
    attempt = 0
    last = None
    if platform in ("bilibili", "bili"):
        try:
            await session.eval(
                "document.querySelector('#commentapp')?.scrollIntoView({block:'center'}); true",
                sid,
            )
            if trace:
                trace.emit("platform_comment_mount_prepared",
                           platform="bilibili", action="scroll_to_commentapp")
        except Exception as exc:
            if trace:
                trace.exception("platform_comment_mount_prepare_failed", exc,
                                platform="bilibili")
    while True:
        attempt += 1
        try:
            last = await session.eval(_probe_target_script(target), sid)
        except Exception as exc:
            if trace:
                trace.exception("target_content_probe_failed", exc,
                                attempt=attempt, platform=platform)
        if trace:
            trace.emit("target_content_probe", attempt=attempt, probe=last,
                       platform=platform)
        if isinstance(last, dict) and last.get("matched"):
            return last
        # 四个平台都可能需要滚动逐批加载评论。只要评论组件已经挂载，
        # 立即交给对应填充脚本执行分段滚动扫描，不在这里原地等待。
        if (isinstance(last, dict)
                and int(last.get("commentNodes") or 0) > 0):
            if trace:
                trace.emit(
                    "target_probe_ready_for_scroll_scan",
                    platform=platform,
                    comment_nodes=int(last.get("commentNodes") or 0),
                )
            return last
        if asyncio.get_running_loop().time() >= deadline:
            return last
        await asyncio.sleep(0.4)


def _platform_key(platform: str) -> str:
    return str(platform or "").strip().lower()


def _probe_target_script(target: ReplyTarget) -> str:
    if _platform_key(target.platform) in ("bilibili", "bili"):
        return _probe_bilibili_target_script(target)
    if _platform_key(target.platform) in ("weibo", "wb"):
        return _probe_weibo_target_script(target)
    payload = json.dumps({
        "nickname": target.nickname or "",
        "user_id": target.platform_user_id or "",
        "comment_id": target.platform_comment_id or "",
        "comment": target.content or "",
        "comment_time": target.comment_time or "",
    }, ensure_ascii=False)
    return f"""(() => {{
      const cfg = {payload};
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      // 只按可见文字匹配；平台的图片表情/占位符（如 [赞]）不参与定位。
      const textNorm = s => norm(String(s || '')
        .replace(/\\[[^\\]\\r\\n]{{1,20}}\\]/g, '')
        .replace(/[\\u{{1F000}}-\\u{{1FAFF}}\\u{{2600}}-\\u{{27BF}}\\u{{FE0F}}]/gu, ''));
      const body = textNorm(document.body ? document.body.innerText : '');
      const wantedId = norm(cfg.comment_id);
      const wantedUser = textNorm(cfg.nickname || cfg.user_id);
      const wantedComment = textNorm(cfg.comment);
      const wantedTime = norm(cfg.comment_time);
      const timeKey = value => {{
        const text = String(value || '');
        const match = text.match(/(?:\\d{{2,4}}[-/年]\\s*)?\\d{{1,2}}[-/月]\\s*\\d{{1,2}}日?(?:\\s+\\d{{1,2}}:\\d{{2}})?/);
        return match ? match[0].replace(/\\D/g, '') : '';
      }};
      const targetTimeKey = timeKey(wantedTime);
      const nodes = [...document.querySelectorAll(
        '[data-e2e="comment-item"],[data-e2e*="comment"],.comment-item,[class*="comment-item"],[class*="commentItem"]'
      )];
      const nodeHasValue = (node, value) => {{
        if (!node || !value) return false;
        const pool = [node, ...node.querySelectorAll('*')];
        return pool.some(el => [...(el.attributes || [])]
          .some(attr => String(attr.value || '').includes(value)));
      }};
      const nodeMatches = n => {{
        const text = textNorm(n.innerText || '');
        const idHit = wantedId && (text.includes(wantedId) || nodeHasValue(n, wantedId));
        if (idHit) return true;
        const userHit = wantedUser && (text.includes(wantedUser)
          || (wantedUser !== textNorm(cfg.nickname) && nodeHasValue(n, norm(cfg.user_id))));
        if (!userHit) return false;
        if (wantedComment) return text.includes(wantedComment);
        return !targetTimeKey || timeKey(n.innerText || '') === targetTimeKey;
      }};
      const identityMatches = nodes.filter(n => {{
        const text = textNorm(n.innerText || '');
        return wantedUser && (text.includes(wantedUser)
          || nodeHasValue(n, norm(cfg.user_id)));
      }});
      const contentMatches = identityMatches.filter(n =>
        wantedComment && textNorm(n.innerText || '').includes(wantedComment));
      const timedMatches = identityMatches.filter(n =>
        targetTimeKey && timeKey(n.innerText || '') === targetTimeKey);
      const matches = nodes.filter(nodeMatches);
      const matched = (wantedId && matches.length > 0)
        || (wantedComment && contentMatches.length > 0)
        || (!wantedComment && (identityMatches.length === 1 || timedMatches.length === 1));
      return {{url:location.href, hidden:document.hidden,
        visibility:document.visibilityState, hasFocus:document.hasFocus(),
        matched, bodyHasUser:!!wantedUser && body.includes(wantedUser),
        bodyHasComment:!!wantedComment && body.includes(wantedComment),
        commentNodes:nodes.length, matchCount:matches.length,
        nonTextComment:!wantedComment,
        identityMatchCount:identityMatches.length,
        timeMatchCount:timedMatches.length}};
    }})()"""


def _probe_bilibili_target_script(target: ReplyTarget) -> str:
    payload = json.dumps({
        "nickname": target.nickname or "",
        "user_id": target.platform_user_id or "",
        "comment_id": target.platform_comment_id or "",
        "comment": target.content or "",
        "comment_time": target.comment_time or "",
    }, ensure_ascii=False)
    return f"""(() => {{
      const cfg = {payload};
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      const clean = s => String(s || '').replace(/\\s+/g, ' ').trim();
      const host = document.querySelector('bili-comments');
      const root = host?.shadowRoot;
      const threads = [...(root?.querySelectorAll(
        'bili-comment-thread-renderer'
      ) || [])];
      const wantedUser = norm(cfg.nickname || cfg.user_id);
      const wantedComment = norm(cfg.comment);
      const wantedTime = norm(cfg.comment_time);
      const timeKey = value => {{
        const text = String(value || '');
        const match = text.match(/(?:\\d{{2,4}}[-/年]\\s*)?\\d{{1,2}}[-/月]\\s*\\d{{1,2}}日?(?:\\s+\\d{{1,2}}:\\d{{2}})?/);
        return match ? match[0].replace(/\\D/g, '') : '';
      }};
      const targetTimeKey = timeKey(wantedTime);
      const matched = threads.some(thread => {{
        const comment = thread.shadowRoot?.querySelector(
          'bili-comment-renderer'
        )?.shadowRoot;
        const user = comment?.querySelector('bili-comment-user-info')
          ?.shadowRoot?.querySelector('#user-name a');
        const rich = comment?.querySelector('bili-rich-text')
          ?.shadowRoot?.querySelector('#contents');
        const userText = norm(user?.innerText);
        const commentText = norm(rich?.innerText);
        const userIdHit = wantedUser && cfg.user_id
          && String(user?.getAttribute('href') || '').includes(norm(cfg.user_id));
        const userHit = wantedUser && (userText.includes(wantedUser) || userIdHit);
        const idHit = cfg.comment_id && String(thread.innerHTML || '').includes(norm(cfg.comment_id));
        if (idHit) return true;
        if (!userHit) return false;
        if (wantedComment) return commentText.includes(wantedComment);
        return !targetTimeKey || timeKey(thread.innerText || '') === targetTimeKey;
      }});
      return {{url:location.href, hidden:document.hidden,
        visibility:document.visibilityState, hasFocus:document.hasFocus(),
        matched, bodyHasUser:false, bodyHasComment:false,
        commentNodes:threads.length, shadowDom:true,
        nonTextComment:!wantedComment,
        commentPreview:threads.slice(0, 3).map(thread => {{
          const comment = thread.shadowRoot?.querySelector(
            'bili-comment-renderer'
          )?.shadowRoot;
          const user = comment?.querySelector('bili-comment-user-info')
            ?.shadowRoot?.querySelector('#user-name a');
          const rich = comment?.querySelector('bili-rich-text')
            ?.shadowRoot?.querySelector('#contents');
          return {{nickname:clean(user?.innerText),
            content:clean(rich?.innerText)}};
        }})}};
    }})()"""


def _probe_weibo_target_script(target: ReplyTarget) -> str:
    payload = json.dumps({
        "nickname": target.nickname or "",
        "user_id": target.platform_user_id or "",
        "comment_id": target.platform_comment_id or "",
        "comment": target.content or "",
        "comment_time": target.comment_time or "",
    }, ensure_ascii=False)
    return f"""(() => {{
      const cfg = {payload};
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      // 图片表情、[赞] 等占位符不参与评论定位，只按文字匹配。
      const textNorm = s => norm(String(s || '')
        .replace(/\\[[^\\]\\r\\n]{{1,20}}\\]/g, '')
        .replace(/[\\u{{1F000}}-\\u{{1FAFF}}\\u{{2600}}-\\u{{27BF}}\\u{{FE0F}}]/gu, ''));
      const body = textNorm(document.body ? document.body.innerText : '');
      const wantedUser = textNorm(cfg.nickname || cfg.user_id);
      const wantedComment = textNorm(cfg.comment);
      const wantedTime = norm(cfg.comment_time);
      const timeKey = value => {{
        const text = String(value || '');
        const match = text.match(/(?:\\d{{2,4}}[-/年]\\s*)?\\d{{1,2}}[-/月]\\s*\\d{{1,2}}日?(?:\\s+\\d{{1,2}}:\\d{{2}})?/);
        return match ? match[0].replace(/\\D/g, '') : '';
      }};
      const targetTimeKey = timeKey(wantedTime);
      const findScroller = seed => {{
        const preferred = document.querySelector('#scroller');
        if (preferred && (preferred.scrollHeight > preferred.clientHeight + 20
            || /(auto|scroll|overlay)/.test(`${{getComputedStyle(preferred).overflowY}} ${{getComputedStyle(preferred).overflow}}`))) return preferred;
        let node = seed?.parentElement;
        while (node) {{
          const style = getComputedStyle(node);
          if (node.scrollHeight > node.clientHeight + 20
              && /(auto|scroll|overlay)/.test(`${{style.overflowY}} ${{style.overflow}}`)) return node;
          node = node.parentElement;
        }}
        const candidates = [...document.querySelectorAll(
          '[id*="scroller"],[class*="scroller"]'
        )].filter(el => el.scrollHeight > el.clientHeight + 20);
        if (candidates.length) return candidates.sort(
          (a, b) => (b.scrollHeight - b.clientHeight) -
            (a.scrollHeight - a.clientHeight)
        )[0];
        const root = document.scrollingElement;
        return root && root.scrollHeight > root.clientHeight + 20 ? root : null;
      }};
      const scrollState = el => el ? {{
        id:el.id || '',
        className:typeof el.className === 'string' ? el.className.slice(0,120) : '',
        scrollTop:el === document.scrollingElement ? window.scrollY : el.scrollTop,
        scrollHeight:el.scrollHeight,
        clientHeight:el === document.scrollingElement ? innerHeight : el.clientHeight,
        maxTop:Math.max(0, el.scrollHeight -
          (el === document.scrollingElement ? innerHeight : el.clientHeight))
      }} : null;
      const scrollOneStep = el => {{
        if (!el) return {{moved:false, before:null, after:null}};
        const before = scrollState(el);
        const step = Math.max(480, Math.floor(before.clientHeight * 0.8));
        const nextTop = Math.min(before.maxTop, before.scrollTop + step);
        if (el === document.scrollingElement) window.scrollTo(0, nextTop);
        else el.scrollTop = nextTop;
        el.dispatchEvent(new Event('scroll', {{bubbles:true}}));
        const after = scrollState(el);
        return {{moved:after.scrollTop > before.scrollTop + 1, before, after}};
      }};
      const collect = () => [...new Set(
        [...document.querySelectorAll('.wbpro-scroller-item')]
      )].filter(node => node.isConnected);
      const nodeHasValue = (node, value) => {{
        if (!node || !value) return false;
        const pool = [node, ...node.querySelectorAll('*')];
        return pool.some(el => [...(el.attributes || [])]
          .some(attr => String(attr.value || '').includes(value)));
      }};
      const findMatches = nodes => nodes.filter(node => {{
        const text = textNorm(node.innerText || '');
        const userHit = wantedUser && (text.includes(wantedUser)
          || nodeHasValue(node, norm(cfg.user_id)));
        const idHit = cfg.comment_id && (text.includes(norm(cfg.comment_id))
          || nodeHasValue(node, norm(cfg.comment_id)));
        if (idHit) return true;
        if (!userHit) return false;
        if (wantedComment) return text.includes(wantedComment);
        return !targetTimeKey || timeKey(node.innerText || '') === targetTimeKey;
      }});
      let nodes = collect();
      let matches = findMatches(nodes);
      const scroller = findScroller(nodes[0]);
      let scrollAction = 'none';
      if (!matches.length && scroller) {{
        const step = scrollOneStep(scroller);
        scrollAction = step.moved ? 'advanced' : 'at_end_or_not_scrollable';
        nodes = collect();
        matches = findMatches(nodes);
      }}
      const identityMatches = nodes.filter(node => {{
        const text = textNorm(node.innerText || '');
        return wantedUser && (text.includes(wantedUser)
          || nodeHasValue(node, norm(cfg.user_id)));
      }});
      const timeMatches = identityMatches.filter(node =>
        targetTimeKey && timeKey(node.innerText || '') === targetTimeKey
      );
      const matched = matches.length > 0
        && (wantedComment || identityMatches.length === 1 || timeMatches.length === 1);
      return {{url:location.href, hidden:document.hidden,
        visibility:document.visibilityState, hasFocus:document.hasFocus(),
        matched, bodyHasUser:!!wantedUser && body.includes(wantedUser),
        bodyHasComment:!!wantedComment && body.includes(wantedComment),
        commentNodes:nodes.length, matchCount:matches.length,
        nonTextComment:!wantedComment, identityMatchCount:identityMatches.length,
        timeMatchCount:timeMatches.length,
        scrollAction, scrollState:scrollState(scroller), commentPreview:nodes.slice(0, 3).map(
          node => String(node.innerText || '').replace(/\\s+/g, ' ').trim()
            .slice(0, 300))}};
    }})()"""


async def _attach_reply_page(session: CdpSession, target: ReplyTarget,
                             *, trace: Optional[DebugTrace] = None):
    """在多标签窗口中选择真正包含目标评论的页面，而不是盲取第一个 page。"""
    if trace:
        trace.emit("page_targets_query_started")
    targets = await session.cmd("Target.getTargets")
    pages = [t for t in targets.get("targetInfos", []) if t.get("type") == "page"]
    if trace:
        trace.emit(
            "page_targets_query_result",
            total_targets=len(targets.get("targetInfos", [])),
            page_count=len(pages),
            pages=[{
                "target_id": page.get("targetId"),
                "url": page.get("url"),
                "title": page.get("title"),
                "attached": page.get("attached"),
            } for page in pages],
        )
    if not pages:
        sid = await session.attach_page()
        if trace:
            trace.emit("page_attach_fallback", session_id=sid, reason="no_page_targets")
        return sid, "", False

    ordered = sorted(
        pages,
        key=lambda t: (not _same_url(t.get("url", ""), target.video_url),
                       t.get("url", "")),
    )
    attached = []
    for page in ordered:
        target_id = page.get("targetId")
        if trace:
            trace.emit(
                "page_probe_started",
                target_id=target_id,
                page_url=page.get("url", ""),
            )
        try:
            result = await session.cmd(
                "Target.attachToTarget",
                {"targetId": page["targetId"], "flatten": True},
            )
            sid = result["sessionId"]
            await session.cmd("Page.enable", session_id=sid)
            await session.cmd("Runtime.enable", session_id=sid)
            probe = await session.eval(_probe_target_script(target), sid)
            row = (sid, (probe or {}).get("url") or page.get("url", ""),
                   bool(probe and probe.get("matched")),
                   bool(probe and not probe.get("hidden")
                        and probe.get("visibility") == "visible"))
            attached.append(row)
            if trace:
                trace.emit(
                    "page_probe_result",
                    target_id=target_id,
                    session_id=sid,
                    probe=probe,
                    selected_url=row[1],
                    matched=row[2],
                    visible=row[3],
                )
        except Exception as exc:
            if trace:
                trace.exception("page_probe_failed", exc, target_id=target_id)
            continue

    visible_pages = [row for row in attached if row[3]]
    visible_same_url = [row for row in visible_pages
                        if _same_url(row[1], target.video_url)]
    if visible_same_url:
        matched = [row for row in visible_same_url if row[2]]
        row = matched[0] if matched else visible_same_url[0]
        if trace:
            trace.emit("page_selection_decision", reason="visible_target_url",
                       matched=bool(matched), session_id=row[0], url=row[1])
        return row[:3]
    if visible_pages:
        if trace:
            trace.emit("page_selection_decision", reason="first_visible_page",
                       session_id=visible_pages[0][0], url=visible_pages[0][1])
        return visible_pages[0][:3]
    same_url = [row for row in attached if _same_url(row[1], target.video_url)]
    if same_url:
        if trace:
            trace.emit("page_selection_decision", reason="same_url_not_visible",
                       session_id=same_url[0][0], url=same_url[0][1])
        return same_url[0][:3]
    if attached:
        if trace:
            trace.emit("page_selection_decision", reason="first_attached_page",
                       session_id=attached[0][0], url=attached[0][1])
        return attached[0][:3]
    sid = await session.attach_page()
    if trace:
        trace.emit("page_attach_fallback", session_id=sid, reason="all_page_probes_failed")
    return sid, "", False


def _target_json(target: ReplyTarget, content: str) -> str:
    return json.dumps({
        "nickname": target.nickname or "",
        "user_id": target.platform_user_id or "",
        "comment_id": target.platform_comment_id or "",
        "comment": target.content or "",
        "comment_time": target.comment_time or "",
        "reply": content,
    }, ensure_ascii=False)


def _fill_bilibili_script(target: ReplyTarget, content: str) -> str:
    payload = _target_json(target, content)
    return f"""(async function() {{
      const cfg = {payload};
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const trace = [];
      const record = (step, data = {{}}) => trace.push(Object.assign({{step}}, data));
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      // 图片表情、[赞] 等占位符不参与评论定位，只按文字匹配。
      const textNorm = s => norm(String(s || '')
        .replace(/\\[[^\\]\\r\\n]{{1,20}}\\]/g, '')
        .replace(/[\\u{{1F000}}-\\u{{1FAFF}}\\u{{2600}}-\\u{{27BF}}\\u{{FE0F}}]/gu, ''));
      const clean = s => String(s || '').replace(/\\s+/g, ' ').trim();
      const visible = el => {{
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0
          && st.visibility !== 'hidden' && st.display !== 'none';
      }};
      const rectData = el => {{
        const r = el?.getBoundingClientRect?.();
        return r ? {{left:r.left, top:r.top, right:r.right, bottom:r.bottom,
          width:r.width, height:r.height}} : null;
      }};
      record('script_started', {{platform:'bilibili', url:location.href,
        hidden:document.hidden, visibility:document.visibilityState,
        hasFocus:document.hasFocus()}});
      const host = document.querySelector('bili-comments');
      const root = host?.shadowRoot;
      const wantedUser = norm(cfg.nickname || cfg.user_id);
      const wantedComment = norm(cfg.comment);
      const wantedTime = norm(cfg.comment_time);
      const timeKey = value => {{
        const text = String(value || '');
        const match = text.match(/(?:\\d{{2,4}}[-/年]\\s*)?\\d{{1,2}}[-/月]\\s*\\d{{1,2}}日?(?:\\s+\\d{{1,2}}:\\d{{2}})?/);
        return match ? match[0].replace(/\\D/g, '') : '';
      }};
      const targetTimeKey = timeKey(wantedTime);
      const collectThreads = () => [...(root?.querySelectorAll(
        'bili-comment-thread-renderer'
      ) || [])].filter(thread => thread.isConnected);
      const matchStats = {{identityCount:0, idCount:0, contentCount:0, timeCount:0}};
      const findMatches = threads => {{
        matchStats.identityCount = 0;
        matchStats.idCount = 0;
        matchStats.contentCount = 0;
        matchStats.timeCount = 0;
        return threads.filter(thread => {{
          const comment = thread.shadowRoot?.querySelector(
            'bili-comment-renderer'
          )?.shadowRoot;
          const user = comment?.querySelector('bili-comment-user-info')
            ?.shadowRoot?.querySelector('#user-name a');
          const rich = comment?.querySelector('bili-rich-text')
            ?.shadowRoot?.querySelector('#contents');
          const userText = textNorm(user?.innerText);
          const userIdHit = wantedUser && cfg.user_id
            && String(user?.getAttribute('href') || '').includes(norm(cfg.user_id));
          const userHit = wantedUser && (userText.includes(wantedUser) || userIdHit);
          const commentText = textNorm(rich?.innerText);
          const idHit = cfg.comment_id && String(thread.innerHTML || '')
            .includes(norm(cfg.comment_id));
          if (userHit) matchStats.identityCount++;
          if (idHit) {{ matchStats.idCount++; return true; }}
          if (!userHit) return false;
          if (wantedComment) {{
            const contentHit = commentText.includes(wantedComment);
            if (contentHit) matchStats.contentCount++;
            return contentHit;
          }}
          const timeHit = !targetTimeKey || timeKey(thread.innerText || '') === targetTimeKey;
          if (timeHit) matchStats.timeCount++;
          return timeHit;
        }});
      }};
      const findScroller = () => {{
        let node = host?.parentElement;
        while (node) {{
          const style = getComputedStyle(node);
          if (node.scrollHeight > node.clientHeight + 20
              && /(auto|scroll|overlay)/.test(`${{style.overflowY}} ${{style.overflow}}`)) return node;
          node = node.parentElement;
        }}
        return document.scrollingElement;
      }};
      const scrollState = el => el ? {{
        name:el === document.scrollingElement ? 'document.scrollingElement' :
          ((el.tagName || '').toLowerCase() + (el.id ? '#' + el.id : '')),
        scrollTop:el === document.scrollingElement ? window.scrollY : el.scrollTop,
        scrollHeight:el.scrollHeight,
        clientHeight:el === document.scrollingElement ? innerHeight : el.clientHeight,
        maxTop:Math.max(0, el.scrollHeight -
          (el === document.scrollingElement ? innerHeight : el.clientHeight))
      }} : null;
      let threads = collectThreads();
      let matches = findMatches(threads);
      if (!matches.length && host) {{
        host.scrollIntoView({{block:'start', inline:'nearest'}});
        await sleep(500);
        const scroller = findScroller();
        for (let attempt = 1; attempt <= 24; attempt++) {{
          const before = scrollState(scroller);
          if (!before) break;
          const step = Math.max(420, Math.floor(before.clientHeight * 0.75));
          const nextTop = Math.min(before.maxTop, before.scrollTop + step);
          if (scroller === document.scrollingElement) window.scrollTo(0, nextTop);
          else scroller.scrollTop = nextTop;
          scroller.dispatchEvent(new Event('scroll', {{bubbles:true}}));
          await sleep(600);
          threads = collectThreads();
          matches = findMatches(threads);
          const after = scrollState(scroller);
          record('comment_scan_step', {{attempt, before, after,
            threadCount:threads.length, matchCount:matches.length}});
          if (matches.length) break;
          if (after && after.scrollTop >= after.maxTop - 2) await sleep(900);
        }}
      }}
      record('comment_candidates_collected', {{threadCount:threads.length,
        matchCount:matches.length, wantedUser:cfg.nickname || cfg.user_id,
        wantedComment:cfg.comment, nonTextComment:!wantedComment,
        identityCount:matchStats.identityCount, idCount:matchStats.idCount,
        contentCount:matchStats.contentCount, timeCount:matchStats.timeCount}});
      const thread = matches[0];
      if (!thread) return {{ok:false, stage:'comment_not_found',
        message:'B站作品中未找到目标评论', matched:false, trace}};
      const comment = thread.shadowRoot?.querySelector(
        'bili-comment-renderer'
      )?.shadowRoot;
      record('comment_matched', {{tag:'BILI-COMMENT-THREAD-RENDERER',
        text:String(comment?.innerText || '').slice(0,300),
        beforeScroll:{{pageY:scrollY, rect:rectData(thread)}}}});
      thread.scrollIntoView({{block:'center', inline:'nearest'}});
      await sleep(450);
      record('comment_scroll_completed', {{after:{{pageY:scrollY,
        rect:rectData(thread)}}}});

      const findEditor = () => {{
        const box = thread.shadowRoot?.querySelector(
          '#reply-container bili-comment-box'
        );
        const boxRoot = box?.shadowRoot;
        const editor = boxRoot?.querySelector(
          'bili-comment-rich-textarea'
        )?.shadowRoot?.querySelector('.brt-editor[contenteditable="true"]');
        return {{box, boxRoot, editor}};
      }};
      let {{box, boxRoot, editor}} = findEditor();
      const action = comment?.querySelector(
        'bili-comment-action-buttons-renderer'
      )?.shadowRoot;
      const replyButton = [...(action?.querySelectorAll('button') || [])]
        .find(button => norm(button.innerText) === '回复')
        || action?.querySelector('#reply');
      record('reply_controls_detected', {{replyButtonFound:!!replyButton,
        replyButtonText:clean(replyButton?.innerText),
        existingEditor:!!editor, editorRect:rectData(editor)}});
      let selectionMethod = '';
      if (editor) {{
        selectionMethod = 'existing_bilibili_reply_editor';
        record('reply_input_reused', {{reason:'existing_shadow_dom_editor'}});
      }} else if (!replyButton) {{
        return {{ok:false, stage:'reply_button_not_found',
          message:'B站目标评论内未找到回复按钮，未执行任何输入',
          matched:true, trace}};
      }} else {{
        record('reply_button_click_started', {{text:clean(replyButton.innerText),
          tag:replyButton.tagName}});
        replyButton.focus?.();
        replyButton.click();
        record('reply_button_click_completed', {{after:{{pageY:scrollY,
          rect:rectData(thread)}}}});
        await sleep(800);
        ({{box, boxRoot, editor}} = findEditor());
        selectionMethod = 'new_bilibili_editor_after_reply_click';
      }}
      record('post_reply_click_wait_completed', {{selectionMethod,
        pageY:scrollY, editorFound:!!editor, editorRect:rectData(editor)}});
      if (!editor || !visible(editor)) return {{ok:false,
        stage:'reply_input_not_found',
        message:'B站未找到回复编辑器，未执行任何输入', matched:true,
        replyButtonFound:!!replyButton, selectionMethod, trace}};
      editor.focus();
      const range = document.createRange();
      range.selectNodeContents(editor);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      record('contenteditable_ready_for_cdp_input', {{
        insertionMethod:'Input.insertText',
        execCommandUsed:false,
        syntheticBeforeInputDispatched:false,
        syntheticInputDispatched:false,
        syntheticChangeDispatched:false,
        selectionText:String(selection.toString() || '').slice(0,300),
        inputText:String(editor.innerText || '').slice(0,300),
        editorRect:rectData(editor)}});
      return {{ok:true, stage:'input_ready',
        message:'B站回复输入框已定位，等待浏览器级输入', verified:false,
        matched:true, requiresCdpInput:true, replyButtonFound:!!replyButton,
        selectionMethod, inputTag:editor.tagName,
        inputPlaceholder:'回复 @' + clean(
          comment?.querySelector('bili-comment-user-info')?.shadowRoot
            ?.querySelector('#user-name a')?.innerText
        ), trace}};
    }})()"""


def _fill_weibo_script(target: ReplyTarget, content: str) -> str:
    payload = _target_json(target, content)
    return f"""(async function() {{
      const cfg = {payload};
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const trace = [];
      const record = (step, data = {{}}) => trace.push(Object.assign({{step}}, data));
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      // 图片表情、[赞] 等占位符不参与评论定位，只按文字匹配。
      const textNorm = s => norm(String(s || '')
        .replace(/\\[[^\\]\\r\\n]{{1,20}}\\]/g, '')
        .replace(/[\\u{{1F000}}-\\u{{1FAFF}}\\u{{2600}}-\\u{{27BF}}\\u{{FE0F}}]/gu, ''));
      const clean = s => String(s || '').replace(/\\s+/g, ' ').trim();
      const visible = el => {{
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0
          && st.visibility !== 'hidden' && st.display !== 'none';
      }};
      const rectData = el => {{
        const r = el?.getBoundingClientRect?.();
        return r ? {{left:r.left, top:r.top, right:r.right, bottom:r.bottom,
          width:r.width, height:r.height}} : null;
      }};
      record('script_started', {{platform:'weibo', url:location.href,
        hidden:document.hidden, visibility:document.visibilityState,
        hasFocus:document.hasFocus()}});
      const wantedUser = textNorm(cfg.nickname || cfg.user_id);
      const wantedComment = textNorm(cfg.comment);
      const wantedTime = norm(cfg.comment_time);
      const timeKey = value => {{
        const text = String(value || '');
        const match = text.match(/(?:\\d{{2,4}}[-/年]\\s*)?\\d{{1,2}}[-/月]\\s*\\d{{1,2}}日?(?:\\s+\\d{{1,2}}:\\d{{2}})?/);
        return match ? match[0].replace(/\\D/g, '') : '';
      }};
      const targetTimeKey = timeKey(wantedTime);
      const findScroller = seed => {{
        // 微博评论固定优先使用 #scroller；评论会在此容器滚动后追加。
        const preferred = document.querySelector('#scroller');
        if (preferred && (preferred.scrollHeight > preferred.clientHeight + 20
            || /(auto|scroll|overlay)/.test(`${{getComputedStyle(preferred).overflowY}} ${{getComputedStyle(preferred).overflow}}`))) return preferred;
        let node = seed?.parentElement;
        while (node) {{
          const style = getComputedStyle(node);
          if (node.scrollHeight > node.clientHeight + 20
              && /(auto|scroll|overlay)/.test(`${{style.overflowY}} ${{style.overflow}}`)) return node;
          node = node.parentElement;
        }}
        const candidates = [...document.querySelectorAll(
          '[id*="scroller"],[class*="scroller"]'
        )].filter(el => el.scrollHeight > el.clientHeight + 20);
        if (candidates.length) return candidates.sort(
          (a, b) => (b.scrollHeight - b.clientHeight) -
            (a.scrollHeight - a.clientHeight)
        )[0];
        const root = document.scrollingElement;
        return root && root.scrollHeight > root.clientHeight + 20 ? root : null;
      }};
      const scrollerName = el => !el ? '' : (
        el === document.scrollingElement ? 'document.scrollingElement' :
        ((el.tagName || '').toLowerCase() + (el.id ? '#' + el.id : ''))
      );
      const scrollState = el => el ? {{
        name:scrollerName(el),
        scrollTop:el === document.scrollingElement ? window.scrollY : el.scrollTop,
        scrollHeight:el.scrollHeight,
        clientHeight:el === document.scrollingElement ? innerHeight : el.clientHeight,
        maxTop:Math.max(0, el.scrollHeight -
          (el === document.scrollingElement ? innerHeight : el.clientHeight))
      }} : null;
      const scrollOneStep = el => {{
        if (!el) return {{moved:false, before:null, after:null}};
        const before = scrollState(el);
        const step = Math.max(480, Math.floor(before.clientHeight * 0.8));
        const nextTop = Math.min(before.maxTop, before.scrollTop + step);
        if (el === document.scrollingElement) window.scrollTo(0, nextTop);
        else el.scrollTop = nextTop;
        el.dispatchEvent(new Event('scroll', {{bubbles:true}}));
        const after = scrollState(el);
        return {{moved:after.scrollTop > before.scrollTop + 1, before, after}};
      }};
      const collect = () => [...new Set(
        [...document.querySelectorAll('.wbpro-scroller-item')]
      )].filter(node => node.isConnected);
      const nodeHasValue = (node, value) => {{
        if (!node || !value) return false;
        const pool = [node, ...node.querySelectorAll('*')];
        return pool.some(el => [...(el.attributes || [])]
          .some(attr => String(attr.value || '').includes(value)));
      }};
      const scan = () => {{
        const nodes = collect();
        const identityNodes = nodes.filter(node => {{
          const text = textNorm(node.innerText || '');
          return wantedUser && (text.includes(wantedUser)
            || nodeHasValue(node, norm(cfg.user_id)));
        }});
        const idNodes = cfg.comment_id ? nodes.filter(node =>
          textNorm(node.innerText || '').includes(norm(cfg.comment_id))
          || nodeHasValue(node, norm(cfg.comment_id))
        ) : [];
        const contentNodes = wantedComment ? identityNodes.filter(node =>
          textNorm(node.innerText || '').includes(wantedComment)
        ) : [];
        const timeNodes = targetTimeKey ? identityNodes.filter(node =>
          timeKey(node.innerText || '') === targetTimeKey
        ) : [];
        const matchedNodes = idNodes.length ? idNodes
          : contentNodes.length ? contentNodes
          : (!wantedComment && (identityNodes.length === 1 || timeNodes.length === 1)
            ? (timeNodes.length === 1 ? timeNodes : identityNodes) : []);
        return {{nodes, matches:matchedNodes, identityCount:identityNodes.length,
          idCount:idNodes.length, contentCount:contentNodes.length,
          timeCount:timeNodes.length, nonTextComment:!wantedComment}};
      }};
      let scanned = scan();
      let nodes = scanned.nodes;
      let matches = scanned.matches;
      let comment = matches[0];
      let scroller = findScroller(nodes[0]);
      let scrollAttempts = 0;
      let stalledAtEnd = 0;
      if (!comment && scroller) {{
        for (let attempt = 1; attempt <= 30; attempt++) {{
          scrollAttempts = attempt;
          const before = scrollState(scroller);
          const movement = scrollOneStep(scroller);
          await sleep(650);
          scanned = scan();
          nodes = scanned.nodes;
          matches = scanned.matches;
          comment = matches[0];
          const after = scrollState(scroller);
          const grew = !!(before && after && after.scrollHeight > before.scrollHeight + 5);
          record('comment_scan_step', {{attempt, scroller:scrollerName(scroller),
            moved:movement.moved, beforeTop:before?.scrollTop,
            scrollTop:after?.scrollTop, maxTop:after?.maxTop,
            scrollHeight:after?.scrollHeight, nodeCount:nodes.length,
            matchCount:matches.length, contentGrew:grew}});
          if (comment) break;
          if (after && after.scrollTop >= after.maxTop - 2
              && !movement.moved && !grew) {{
            stalledAtEnd += 1;
            // 到底后再等一次，给微博的异步追加请求和虚拟列表重绘时间。
            await sleep(900);
            scanned = scan();
            nodes = scanned.nodes;
            matches = scanned.matches;
            comment = matches[0];
            if (comment) break;
          }} else {{
            stalledAtEnd = 0;
          }}
          if (stalledAtEnd >= 3) break;
        }}
      }}
      record('comment_candidates_collected', {{nodeCount:nodes.length,
        matchCount:matches.length, identityCount:scanned.identityCount || 0,
        idCount:scanned.idCount || 0, contentCount:scanned.contentCount || 0,
        timeCount:scanned.timeCount || 0, nonTextComment:!wantedComment,
        wantedUser:cfg.nickname || cfg.user_id,
        wantedComment:cfg.comment, scrollAttempts,
        scroller:scrollerName(scroller), scrollState:scrollState(scroller)}});
      if (!comment) return {{ok:false, stage:'comment_not_found',
        message:'微博作品中未找到目标评论，已完成评论区滚动扫描', matched:false,
        commentNodes:nodes.length, scrollAttempts,
        scrollState:scrollState(scroller), trace}};
      record('comment_matched', {{tag:comment.tagName,
        text:textNorm(comment.innerText || '').slice(0,300),
        beforeScroll:{{pageY:scrollY, rect:rectData(comment)}}}});
      comment.scrollIntoView({{block:'center', inline:'nearest'}});
      await sleep(450);
      record('comment_scroll_completed', {{after:{{pageY:scrollY,
        rect:rectData(comment)}}}});

      const findReplyInput = () => [...document.querySelectorAll(
        'textarea[placeholder="发布你的回复"]'
      )].find(visible);
      let input = findReplyInput();
      let selectionMethod = '';
      record('reply_controls_detected', {{replyInputAlreadyOpen:!!input,
        commentIconFound:!!comment.querySelector(
          '.wbpro-iconbed i[title="评论"]')}});
      if (input) {{
        selectionMethod = 'existing_weibo_reply_modal';
        record('reply_input_reused', {{reason:'existing_reply_modal'}});
      }} else {{
        const icon = comment.querySelector(
          '.wbpro-iconbed i[title="评论"]'
        )?.closest('.wbpro-iconbed');
        if (!icon) return {{ok:false, stage:'reply_button_not_found',
          message:'微博目标评论内未找到回复入口，未执行任何输入',
          matched:true, trace}};
        record('reply_button_click_started', {{text:'评论图标',
          title:'评论', tag:icon.tagName, rect:rectData(icon)}});
        icon.focus?.();
        icon.click();
        record('reply_button_click_completed', {{after:{{pageY:scrollY,
          rect:rectData(comment)}}}});
        await sleep(800);
        input = findReplyInput();
        selectionMethod = 'new_weibo_reply_modal_after_comment_click';
      }}
      record('post_reply_click_wait_completed', {{selectionMethod,
        pageY:scrollY, inputFound:!!input, inputRect:rectData(input)}});
      if (!input || !visible(input)) return {{ok:false,
        stage:'reply_input_not_found',
        message:'微博未找到专用回复输入框，未执行任何输入', matched:true,
        selectionMethod, trace}};
      input.focus();
      input.select?.();
      record('textarea_ready_for_cdp_input', {{
        insertionMethod:'Input.insertText',
        inputText:String(input.value || '').slice(0,300),
        placeholder:input.getAttribute('placeholder') || '',
        inputRect:rectData(input), pageY:scrollY}});
      return {{ok:true, stage:'input_ready',
        message:'微博专用回复输入框已定位，等待浏览器级输入', verified:false,
        matched:true, requiresCdpInput:true, replyButtonFound:true,
        selectionMethod, inputTag:input.tagName,
        inputPlaceholder:input.getAttribute('placeholder') || '', trace}};
    }})()"""


def _douyin_note_panel_script() -> str:
    """返回 note 页评论面板的异步定位脚本。

    Note 页的评论按钮会异步挂载，且不同页面版本可能只有图标没有文字。
    正式扫描前先复用采集端已验证的“倒序查找评论(N)叶子元素”流程，
    再用短时重试和可访问属性/类名作为兜底。
    """
    return r'''async () => {
      // 旧版采集器已经验证过的 Note 流程：倒序查找“评论(N)”叶子元素，
      // 直接触发 DOM 元素，不使用屏幕坐标，也不依赖元素当前位于窗口的哪个位置。
      let clicked = false;
      const panelStateFromCollectorFlow = () => {
        const lists = [...document.querySelectorAll(
          '[data-e2e="comment-list"],.comment-mainContent,.comment-container'
        )].filter(visible);
        const scroller = [...document.querySelectorAll(
          '.comment-mainContent,.comment-container,.route-scroll-container,'
          + '.note-scroller,[data-e2e="scroll-container"],'
          + '[class*="scroll-container"],[class*="comment-"]'
        )].find(el => visible(el) && Number(el.scrollHeight || 0)
          > Number(el.clientHeight || 0) + 20);
        // feed-comment-icon 只是作品操作栏的评论图标，不能当成评论列表。
        const nodes = [...document.querySelectorAll(
          '[data-e2e="comment-list"] [data-e2e="comment-item"],'
          + '[data-e2e="comment-item"],'
          + '.comment-mainContent [data-e2e*="comment"],'
          + '.comment-container [data-e2e*="comment"],'
          + '[class*="comment-item"],[class*="commentItem"],'
          + '[class*="CommentItem"],[class*="comment-content"]'
        )].filter(el => visible(el) && el.getAttribute('data-e2e') !== 'feed-comment-icon');
        return {list:!!lists.length, scroller:!!scroller, count:nodes.length};
      };
      const collectorCommentTab = () => [...document.querySelectorAll('span,div,button')]
        .reverse().find(el => {
          const text = norm(el.textContent || '');
          return /评论\s*\(?\d/.test(text)
            && text.length < 25 && el.children.length <= 2 && visible(el);
        });
      let primaryTab = collectorCommentTab();
      const primaryState = panelStateFromCollectorFlow();
      if (primaryState.list || primaryState.scroller || primaryState.count > 0) {
        return {ok:true, clicked:false, method:'already_open',
          commentNodeCount:primaryState.count, scrollerFound:primaryState.scroller,
          candidates:[], trace};
      }
      if (primaryTab) {
        primaryTab.focus?.();
        primaryTab.click();
        clicked = true;
        trace.push({step:'douyin_note_comment_panel_collector_click',
          method:'collector_comment_tab', tag:primaryTab.tagName,
          text:norm(primaryTab.textContent).slice(0,40),
          className:typeof primaryTab.className === 'string'
            ? primaryTab.className.slice(0,120) : ''});
        // 采集端点击后会等待评论面板异步挂载；这里保持相同的等待语义。
        for (let waitAttempt = 1; waitAttempt <= 25; waitAttempt++) {
          const state = panelStateFromCollectorFlow();
          if (state.list || state.scroller || state.count > 0) {
            return {ok:true, clicked:true, method:'collector_comment_tab',
              commentNodeCount:state.count, scrollerFound:state.scroller,
              candidates:[], trace};
          }
          await sleep(200);
        }
      }
      const candidatesOf = () => [...document.querySelectorAll(
        'button,[role="button"],[aria-label*="评论"],[title*="评论"],'
        + '[data-e2e*="comment"],[data-testid*="comment"],'
        + '[class*="comment"],[class*="Comment"],svg,span,div'
      )].filter(visible).map(el => {
        const text = norm(el.innerText || el.textContent);
        const aria = norm(el.getAttribute('aria-label'));
        const title = norm(el.getAttribute('title'));
        const data = norm(el.getAttribute('data-e2e') || el.getAttribute('data-testid'));
        const cls = norm(typeof el.className === 'string' ? el.className : '');
        const shortText = text.length <= 18 && el.children.length <= 3;
        let score = 0;
        if (/^(评论|评论区)(?:[（(]?\d+(?:万)?[）)]?)?$/.test(text) && shortText) score = 160;
        if (/评论|comment/i.test(`${aria} ${title} ${data}`)) score = Math.max(score, 180);
        if (/comment/i.test(cls) && /(icon|action|tab|button|operate)/i.test(cls)) score = Math.max(score, 150);
        if (el.tagName === 'SVG' && !/comment/i.test(`${aria} ${title} ${data} ${cls}`)) score = 0;
        return {el, text, aria, title, data, cls, score};
      }).filter(item => item.score > 0)
        .sort((a, b) => b.score - a.score);
      const commentNodes = () => [...document.querySelectorAll(
        '[data-e2e="comment-item"],[data-testid*="comment-item"],'
        + '[class*="comment-item"],[class*="commentItem"],'
        + '[class*="CommentItem"],[class*="comment-content"],'
        + '[data-comment-id],[data-commentid]'
      )].filter(el => visible(el) && el.getAttribute('data-e2e') !== 'feed-comment-icon');
      const commentScroller = () => [...document.querySelectorAll(
        '.comment-mainContent,.comment-container,.route-scroll-container,'
        + '.note-scroller,[data-e2e="scroll-container"],'
        + '[class*="scroll-container"],[class*="comment-"]'
      )].find(el => visible(el) && Number(el.scrollHeight || 0) > Number(el.clientHeight || 0) + 20);
      let last = [];
      for (let attempt = 1; attempt <= 12; attempt++) {
        // 旧采集流程已经点击过时，不再重复点击同一个按钮，只等待面板挂载。
        last = clicked ? [] : candidatesOf();
        trace.push({step:'douyin_note_comment_panel_probe', attempt,
          candidateCount:last.length,
          candidates:last.slice(0, 6).map(x => ({text:x.text.slice(0,40), aria:x.aria.slice(0,40),
            title:x.title.slice(0,40), data:x.data.slice(0,40), className:x.cls.slice(0,80), score:x.score}))});
        if (!clicked && last[0]) {
          const hit = last[0];
          hit.el.focus?.();
          hit.el.click();
          clicked = true;
          await sleep(700);
        } else {
          await sleep(500);
        }
        const scroller = commentScroller();
        const count = commentNodes().length;
        if (clicked && (scroller || count > 0)) {
          return {ok:true, clicked:true, commentNodeCount:count,
            scrollerFound:!!scroller, candidates:last.slice(0, 6), trace};
        }
      }
      return {ok:false, clicked, commentNodeCount:commentNodes().length,
        scrollerFound:!!commentScroller(), candidates:last.slice(0, 6), trace};
    }'''


def _fill_script(target: ReplyTarget, content: str) -> str:
    if _platform_key(target.platform) in ("bilibili", "bili"):
        return _fill_bilibili_script(target, content)
    if _platform_key(target.platform) in ("weibo", "wb"):
        return _fill_weibo_script(target, content)
    payload = _target_json(target, content)
    return f"""(async function() {{
      const cfg = {payload};
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const trace = [];
      const record = (step, data = {{}}) => trace.push(Object.assign({{step}}, data));
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      // 只按可见文字匹配；图片表情和 [赞] 等平台占位符不参与定位。
      const textNorm = s => norm(String(s || '')
        .replace(/\\[[^\\]\\r\\n]{{1,20}}\\]/g, '')
        .replace(/[\\u{{1F000}}-\\u{{1FAFF}}\\u{{2600}}-\\u{{27BF}}\\u{{FE0F}}]/gu, ''));
      const visible = el => {{
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
      }};
      const rectData = el => {{
        const r = el?.getBoundingClientRect?.();
        return r ? {{left:r.left, top:r.top, right:r.right, bottom:r.bottom,
          width:r.width, height:r.height}} : null;
      }};
      const scrollSnapshot = el => {{
        const ancestors = [];
        let node = el?.parentElement;
        while (node && ancestors.length < 8) {{
          const st = getComputedStyle(node);
          if (/(auto|scroll|overlay)/.test(`${{st.overflowY}} ${{st.overflow}}`)
              || node.scrollHeight > node.clientHeight + 1) {{
            ancestors.push({{tag:node.tagName, id:node.id || '',
              className:typeof node.className === 'string' ? node.className.slice(0,120) : '',
              scrollTop:node.scrollTop, scrollHeight:node.scrollHeight,
              clientHeight:node.clientHeight}});
          }}
          node = node.parentElement;
        }}
        return {{windowX:window.scrollX, windowY:window.scrollY,
          documentHeight:document.documentElement?.scrollHeight || 0,
          viewportHeight:window.innerHeight, rect:rectData(el), ancestors}};
      }};
      record('script_started', {{url:location.href, hidden:document.hidden,
        visibility:document.visibilityState, hasFocus:document.hasFocus()}});
      // 抖音图文笔记的评论区默认收在右侧，必须先打开“评论”面板，
      // 否则评论节点和回复按钮不会挂载到 DOM。
      const isDouyin = /(?:^|\.)douyin\.com$/i.test(location.hostname);
      const isDouyinNote = /(?:^|\/)note\//i.test(location.pathname);
      if (isDouyinNote) {{
        // note 页控件异步挂载，且部分版本只有图标没有“评论”文字。
        // 先用重试流程打开面板；失败时再保留旧定位器作为兜底。
        const notePanel = await ({_douyin_note_panel_script()})();
        record('douyin_note_comment_panel_retry', {{
          ok:!!notePanel.ok, clicked:!!notePanel.clicked,
          commentNodeCount:notePanel.commentNodeCount || 0,
          scrollerFound:!!notePanel.scrollerFound,
          attempts:(notePanel.trace || []).length,
          candidates:(notePanel.candidates || []).map(item => ({{
            text:item.text?.slice(0,40) || '', aria:item.aria?.slice(0,40) || '',
            title:item.title?.slice(0,40) || '', data:item.data?.slice(0,40) || '',
            className:item.cls?.slice(0,80) || '', score:item.score || 0
          }}))
        }});
        if (notePanel.ok) {{
          record('douyin_note_comment_panel_open_completed', {{
            method:'retry_comment_control',
            commentNodeCount:notePanel.commentNodeCount || 0,
            scrollerFound:!!notePanel.scrollerFound,
            pageY:window.scrollY
          }});
        }} else if (!notePanel.clicked) {{
        // 与采集端 dy_collect.fetch_comments 复用同一套首选定位：
        // 从后往前找“评论(N)”叶子元素，优先触发抖音笔记右侧面板。
        const collectorCommentTab = [...document.querySelectorAll('span,div,button')]
          .reverse().find(el => {{
            const text = norm(el.textContent || '');
            return /评论\\s*\\(?\\d/.test(text)
              && text.length < 25 && el.children.length <= 2 && visible(el);
          }});
        let panelOpenedByCollectorFlow = false;
        if (collectorCommentTab) {{
          record('douyin_note_comment_panel_open_started', {{
            method:'collector_comment_tab', text:norm(collectorCommentTab.textContent)
          }});
          collectorCommentTab.focus?.();
          collectorCommentTab.click();
          panelOpenedByCollectorFlow = true;
        }}
        const commentTabCandidates = [...document.querySelectorAll(
          'button,[role="button"],[aria-label*="评论"],[title*="评论"],'
          + '[data-e2e*="comment"],[class*="comment"],span,div'
        )].filter(visible).map(el => {{
          const text = norm(el.innerText || el.textContent || '');
          const aria = norm(el.getAttribute('aria-label') || '');
          const title = norm(el.getAttribute('title') || '');
          const data = norm(el.getAttribute('data-e2e') || '');
          const cls = norm(typeof el.className === 'string' ? el.className : '');
          const shortText = text.length <= 16 && el.children.length <= 3;
          const textHit = /^(评论|评论区)(?:[（(]?\d+(?:万)?[）)]?)?$/.test(text);
          const labeledHit = /评论|comment/i.test(`${{aria}} ${{title}} ${{data}}`);
          const iconHit = /comment/i.test(cls) && /(icon|action|tab|button)/i.test(cls);
          let score = 0;
          if (textHit && shortText) score = 70;
          if (labeledHit) score = Math.max(score, 100);
          if (iconHit) score = Math.max(score, 85);
          return {{el, text, aria, title, data, cls, score}};
        }}).filter(item => item.score > 0)
          .sort((a, b) => b.score - a.score);
        const commentTab = panelOpenedByCollectorFlow ? null : commentTabCandidates[0]?.el;
        record('douyin_note_comment_panel_candidates', {{count:commentTabCandidates.length,
          candidates:commentTabCandidates.slice(0, 8).map(item => ({{
            text:item.text.slice(0,40), aria:item.aria.slice(0,40),
            title:item.title.slice(0,40), data:item.data.slice(0,40),
            className:item.cls.slice(0,80), score:item.score
          }}))}});
        if (panelOpenedByCollectorFlow || commentTab) {{
          if (panelOpenedByCollectorFlow) await sleep(900);
          record('douyin_note_comment_panel_open_started', {{
            method:panelOpenedByCollectorFlow ? 'collector_comment_tab_completed' : 'fallback_comment_control',
            text:commentTab ? norm(commentTab.innerText) : '',
            aria:commentTab?.getAttribute('aria-label') || '',
            title:commentTab?.getAttribute('title') || '',
            className:commentTab && typeof commentTab.className === 'string' ? commentTab.className : ''
          }});
          if (commentTab) {{
            commentTab.focus?.();
            commentTab.click();
            await sleep(900);
          }}
          record('douyin_note_comment_panel_open_completed', {{
            commentNodeCount:document.querySelectorAll(
              '[data-e2e*="comment"],[class*="comment-item"],[class*="commentItem"],' +
              '[class*="CommentItem"],[class*="comment-content"]'
            ).length,
            pageY:window.scrollY
          }});
        }} else {{
          record('douyin_note_comment_panel_open_skipped', {{reason:'comment_tab_not_found'}});
        }}
        }}
      }}
      const selectors = [
        '[data-e2e="comment-item"]', '[data-e2e*="comment"]',
        '.comment-item', '[class*="comment-item"]',
        '[class*="commentItem"]', '[class*="CommentItem"]',
        '[data-testid*="comment"]'
      ];
      const commentRowSelector = [
        '[data-e2e="comment-item"]', '[data-testid*="comment-item"]',
        '.comment-item', '[class~="commentItem"]',
        '[class~="CommentItem"]'
      ].join(',');
      if (isDouyinNote) selectors.push(
        '[class*="comment-content"]', '[class*="CommentContent"]',
        '[data-comment-id]', '[data-commentid]'
      );
      const wantedComment = textNorm(cfg.comment);
      const wantedId = norm(cfg.comment_id);
      const wantedUser = textNorm(cfg.nickname || cfg.user_id);
      const wantedTime = norm(cfg.comment_time);
      const timeKey = value => {{
        const text = String(value || '');
        const match = text.match(/(?:\\d{{2,4}}[-/年]\\s*)?\\d{{1,2}}[-/月]\\s*\\d{{1,2}}日?(?:\\s+\\d{{1,2}}:\\d{{2}})?/);
        return match ? match[0].replace(/\\D/g, '') : '';
      }};
      const targetTimeKey = timeKey(wantedTime);
      const collectCommentNodes = () => [...new Set(
        selectors.flatMap(s => [...document.querySelectorAll(s)])
      )].filter(el => el.isConnected && el.getClientRects().length);
      const toCommentRow = node => node?.closest?.(commentRowSelector) || node;
      const uniqueCommentRows = nodes => [...new Set(
        nodes.map(toCommentRow).filter(Boolean)
      )];
      const nodeHasValue = (node, value) => {{
        if (!node || !value) return false;
        const pool = [node, ...node.querySelectorAll('*')];
        return pool.some(el => [...(el.attributes || [])]
          .some(attr => String(attr.value || '').includes(value)));
      }};
      // NOTE 页的 data-e2e="comment-item" 可能同时包住主评论和已经展开的
      // 楼中楼。回复前必须把匹配结果收缩到包含“昵称+原评论+本行回复按钮”
      // 的最小可见节点，不能直接对外层评论容器操作。
      const narrowDouyinComment = node => {{
        if (!isDouyinNote || !node) return node;
        const hasExactReply = el => [...el.querySelectorAll(
          '.LJU9cDNW,[data-e2e*="reply"],button,[role="button"]'
        )].some(button => visible(button)
          && (/^(回复|回应|答复)$/.test(norm(button.innerText || ''))
            || String(button.className || '').includes('LJU9cDNW')));
        const candidates = [node, ...node.querySelectorAll('*')]
          .filter(el => visible(el))
          .filter(el => {{
            const text = textNorm(el.innerText || '');
            return wantedUser && wantedComment
              && text.includes(wantedUser) && text.includes(wantedComment);
          }});
        const scoped = candidates.filter(hasExactReply);
        const pool = scoped.length ? scoped : candidates;
        return pool.sort((a, b) => {{
          const textDelta = textNorm(a.innerText || '').length
            - textNorm(b.innerText || '').length;
          if (textDelta) return textDelta;
          const ar = a.getBoundingClientRect();
          const br = b.getBoundingClientRect();
          return (ar.width * ar.height) - (br.width * br.height);
        }})[0] || node;
      }};
      // 抖音的楼中楼默认是折叠的。回复时会重新打开作品，不能假定
      // 采集阶段的展开状态仍然存在，因此定位前和滚动加载后都展开一次。
      // 不使用 scrollIntoView，避免点击展开入口时把整个 Note 页面滚到底部。
      const expandedReplyButtons = new WeakSet();
      const expandNestedReplies = () => {{
        if (!isDouyin) return {{clicked:0, labels:[], nodeCount:collectCommentNodes().length}};
        const expandRe = /(展开|查看|显示|更多)\\s*(?:更多\\s*)?\\d*\\s*(?:条)?\\s*(?:回复|评论)/;
        const isInViewport = el => {{
          const r = el.getBoundingClientRect();
          return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
        }};
        const shortText = el => String(el.innerText || el.textContent || '')
          .replace(/\\s+/g, ' ').trim();
        const candidates = [...new Set([
          ...document.querySelectorAll(
            'button.comment-reply-expand-btn,[class*="comment-reply-expand-btn"]'
          ),
          ...document.querySelectorAll('button,span,div')
        ])].filter(el => {{
          if (!el.isConnected || !visible(el) || !isInViewport(el) || expandedReplyButtons.has(el)) return false;
          const text = shortText(el);
          const exact = el.matches?.(
            'button.comment-reply-expand-btn,[class*="comment-reply-expand-btn"]'
          );
          const generic = text.length < 36 && expandRe.test(text);
          return exact || generic;
        }});
        const clicked = [];
        for (const button of candidates.slice(0, 12)) {{
          try {{
            expandedReplyButtons.add(button);
            button.focus?.();
            button.dispatchEvent(new MouseEvent('mousedown',
              {{bubbles:true, cancelable:true, view:window}}));
            button.dispatchEvent(new MouseEvent('mouseup',
              {{bubbles:true, cancelable:true, view:window}}));
            button.click?.();
            clicked.push(shortText(button).slice(0, 80) || button.className || 'reply-expand-button');
          }} catch (e) {{}}
        }}
        return {{clicked:clicked.length, labels:clicked,
          nodeCount:collectCommentNodes().length}};
      }};
      const findMatches = () => {{
        const nodes = collectCommentNodes();
        const identityNodes = nodes.filter(n => {{
          const text = textNorm(n.innerText || '');
          return wantedUser && (text.includes(wantedUser)
            || nodeHasValue(n, norm(cfg.user_id)));
        }});
        const idNodes = wantedId ? nodes.filter(n =>
          textNorm(n.innerText || '').includes(wantedId) || nodeHasValue(n, wantedId)
        ) : [];
        const contentNodes = wantedComment ? identityNodes.filter(n =>
          textNorm(n.innerText || '').includes(wantedComment)
        ) : [];
        const timeNodes = targetTimeKey ? identityNodes.filter(n =>
          timeKey(n.innerText || '') === targetTimeKey
        ) : [];
        const matchedNodes = idNodes.length ? idNodes
          : contentNodes.length ? contentNodes
          : (!wantedComment && (identityNodes.length === 1 || timeNodes.length === 1)
            ? (timeNodes.length === 1 ? timeNodes : identityNodes) : []);
        const matches = [...new Set(matchedNodes.map(node =>
          isDouyinNote ? narrowDouyinComment(node) : toCommentRow(node)
        ))].filter(Boolean)
          .sort((a, b) => textNorm(a.innerText).length - textNorm(b.innerText).length);
        return {{nodes, matches, identityCount:identityNodes.length,
          idCount:idNodes.length, contentCount:contentNodes.length,
          timeCount:timeNodes.length, nonTextComment:!wantedComment}};
      }};
      const expandAndWait = async (attempt, reason) => {{
        const result = expandNestedReplies();
        if (isDouyin) {{
          record('douyin_nested_reply_expand', Object.assign({{attempt, reason}}, result));
        }}
        if (result.clicked) {{
          await sleep(700);
        }}
        return result;
      }};
      const findCommentScroller = seed => {{
        let node = seed?.parentElement;
        while (node) {{
          const style = getComputedStyle(node);
          if (node.scrollHeight > node.clientHeight + 20
              && /(auto|scroll|overlay)/.test(`${{style.overflowY}} ${{style.overflow}}`)) return node;
          node = node.parentElement;
        }}
        // 首屏可能尚未挂载任何评论节点，此时不能只依赖 nodes[0]。
        // 从评论区自身及其滚动容器特征反查，覆盖“滚动后才加载”的页面。
        const hints = [...document.querySelectorAll(
          '[data-e2e*="comment"],[data-testid*="comment"],'
          + '[id*="comment" i],[class*="comment" i],'
          + '[aria-label*="评论"],[title*="评论"]'
        )].filter(el => el.isConnected);
        const candidates = [];
        const addCandidate = el => {{
          if (!el || !el.isConnected || candidates.includes(el)) return;
          const style = getComputedStyle(el);
          if (el.scrollHeight > el.clientHeight + 20
              && /(auto|scroll|overlay)/.test(`${{style.overflowY}} ${{style.overflow}}`))
            candidates.push(el);
        }};
        for (const hint of hints) {{
          let current = hint;
          for (let depth = 0; current && depth < 8; depth++, current = current.parentElement)
            addCandidate(current);
        }}
        addCandidate(document.scrollingElement);
        if (candidates.length) return candidates.sort((a, b) =>
          (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight)
        )[0];
        return document.scrollingElement || null;
      }};
      let scanned = findMatches();
      let nodes = scanned.nodes;
      let matches = scanned.matches;
      let comment = matches[0];
      if (!comment) {{
        await expandAndWait(0, 'before_comment_scan');
        scanned = findMatches();
        nodes = scanned.nodes;
        matches = scanned.matches;
        comment = matches[0];
      }}
      if (!comment) {{
        const scroller = findCommentScroller(nodes[0]);
        if (scroller) {{
          // 评论区是虚拟列表：滚动后页面高度会继续增长，固定 24 次会在
          // 尚未到达底部时提前结束。只有连续到达底部且没有移动、没有新
          // 内容时，才认为已经扫描完评论。
          let stalledAtEnd = 0;
          for (let attempt = 1; attempt <= 80; attempt++) {{
            const beforeHeight = scroller.scrollHeight;
            const beforeTop = scroller.scrollTop;
            const maxTop = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
            const step = Math.max(240, Math.floor(scroller.clientHeight * 0.72));
            const nextTop = Math.min(maxTop, beforeTop + step);
            if (nextTop > beforeTop + 1) scroller.scrollTop = nextTop;
            scroller.dispatchEvent(new Event('scroll', {{bubbles:true}}));
            await sleep(500);
            await expandAndWait(attempt, 'after_comment_scroll');
            scanned = findMatches();
            nodes = scanned.nodes;
            matches = scanned.matches;
            comment = matches[0];
            const afterTop = scroller.scrollTop;
            const afterHeight = scroller.scrollHeight;
            const moved = afterTop > beforeTop + 1;
            const contentGrew = afterHeight > beforeHeight + 5;
            record('comment_scan_step', {{attempt, beforeTop, scrollTop:scroller.scrollTop,
              maxTop, scrollHeight:scroller.scrollHeight, nodeCount:nodes.length,
              matchCount:matches.length, moved, contentGrew}});
            if (comment) break;
            const atEnd = afterTop >= Math.max(0, scroller.scrollHeight - scroller.clientHeight) - 2;
            if (atEnd && !moved && !contentGrew) {{
              stalledAtEnd += 1;
              await sleep(900);
              await expandAndWait(attempt, 'at_comment_scroll_end');
              scanned = findMatches();
              nodes = scanned.nodes;
              matches = scanned.matches;
              comment = matches[0];
              if (comment) break;
            }} else {{
              stalledAtEnd = 0;
            }}
            if (stalledAtEnd >= 3) break;
          }}
        }}
      }}
      record('comment_candidates_collected', {{nodeCount:nodes.length, matchCount:matches.length,
        identityCount:scanned.identityCount || 0, idCount:scanned.idCount || 0,
        contentCount:scanned.contentCount || 0, timeCount:scanned.timeCount || 0,
        nonTextComment:!wantedComment}});
      if (!comment) return {{ok:false, stage:'comment_not_found', message:'来源作品中未找到目标评论', trace}};
      record('comment_matched', {{
        matchCount:matches.length, tag:comment.tagName,
        text:textNorm(comment.innerText || '').slice(0,300), beforeScroll:scrollSnapshot(comment)
      }});
      try {{
        record('comment_scroll_started', {{before:scrollSnapshot(comment)}});
        comment.scrollIntoView({{block:'center', inline:'nearest'}});
        await sleep(450);
        record('comment_scroll_completed', {{after:scrollSnapshot(comment)}});
      }} catch (e) {{
        record('comment_scroll_failed', {{error:String(e)}});
        return {{ok:false, stage:'comment_scroll_failed', message:'定位评论时页面滚动失败', trace}};
      }}

      const inputSelector = '[contenteditable="true"], textarea, input, [role="textbox"]';
      const inViewport = el => {{
        const r = el.getBoundingClientRect();
        return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
      }};
      const visibleInputs = () => [...document.querySelectorAll(inputSelector)]
        .filter(el => visible(el) && inViewport(el));
      const inputMeta = el => norm([
        el.getAttribute('placeholder') || '',
        el.getAttribute('aria-label') || '',
        el.getAttribute('name') || '',
        el.getAttribute('id') || '',
        el.getAttribute('data-e2e') || '',
        typeof el.className === 'string' ? el.className : '',
      ].join(' '));
      const isSearchLike = el => {{
        const meta = inputMeta(el).toLowerCase();
        const type = String(el.getAttribute('type') || '').toLowerCase();
        return type === 'search' || /搜索|search/.test(meta);
      }};
      const inputScopeText = el => {{
        const parts = [inputMeta(el)];
        let scope = el;
        for (let depth = 0; scope && depth < 6; depth++, scope = scope.parentElement) {{
          parts.push(scope.innerText || scope.textContent || '');
        }}
        return norm(parts.join(' '));
      }};
      const isGenericDouyinInput = el => isDouyinNote
        && /留下你的精彩评论吧/.test(inputScopeText(el));
      const inputHasTargetMarker = el => {{
        const text = inputScopeText(el);
        return (wantedUser && (text.includes(wantedUser)
          || text.includes(`回复@${{wantedUser}}`)
          || text.includes(`回复${{wantedUser}}`)));
      }};
      const beforeInputs = new Set(visibleInputs());
      const existingReplyInputs = visibleInputs().filter(el => !isSearchLike(el)
        && !isGenericDouyinInput(el)
        && (comment.contains(el) || inputHasTargetMarker(el))
        && /回复|评论/.test(norm(
          (el.closest('.richtext-container,[data-e2e*="comment-input"],[class*="comment-input"]')
            || el.parentElement || el).innerText || '')));
      const isNestedCommentControl = el => {{
        let node = el?.parentElement;
        while (node && node !== comment) {{
          if (node.matches?.(commentRowSelector)) return true;
          node = node.parentElement;
        }}
        return false;
      }};
      const replyControlSelectors = [
        // 抖音真实回复入口：外层 div 承担 tabindex 和点击事件，
        // 内部 span 只是视觉文字，不能只依赖 span 冒泡。
        '.LJU9cDNW',
        // 小红书真实回复入口。
        '.reply.icon-container',
        '[data-e2e*="reply"]',
        'button,[role="button"],a,span'
      ].join(',');
      const buttons = [...new Set([...comment.querySelectorAll(replyControlSelectors)])]
        .filter(visible)
        .filter(el => !isNestedCommentControl(el));
      const isIconReplyButton = b => {{
        const className = typeof b.className === 'string' ? b.className : '';
        return /(^|\s)reply(\s|$)/i.test(className)
          && /(^|\s)icon-container(\s|$)/i.test(className);
      }};
      const normalizeReplyControl = button =>
        button?.closest?.('.LJU9cDNW,.reply.icon-container,[data-e2e*="reply"]')
          || button;
      const exactReplyButtons = [...new Set(buttons.filter(b =>
        /^(回复|回应|答复)$/.test(norm(b.innerText)) || isIconReplyButton(b)
      ).map(normalizeReplyControl))];
      const replyButton = exactReplyButtons[0]
        || buttons.find(b => /回复|回应|答复/.test(norm(b.innerText))
          || isIconReplyButton(b));
      record('reply_controls_detected', {{
        beforeInputCount:beforeInputs.size, existingReplyInputCount:existingReplyInputs.length,
        buttonCount:buttons.length, exactReplyButtonCount:exactReplyButtons.length,
        replyButtonFound:!!replyButton,
        replyButtonText:replyButton ? String(replyButton.innerText || '').trim() : '',
        replyButtonTag:replyButton ? replyButton.tagName : '',
        replyButtonClass:replyButton ? String(replyButton.className || '') : ''
      }});
      let inlineInput = existingReplyInputs.length === 1
        ? existingReplyInputs[0] : null;
      // 如果页面已经显示该目标评论的“回复中”状态，说明回复上下文已经
      // 由用户或上一轮流程打开。此时复用唯一的编辑器，不再次激活控件，
      // 避免把已打开的目标回复切回作品级评论框。
      if (!inlineInput && isDouyinNote && replyButton
          && /回复中/.test(norm(replyButton.innerText || ''))) {{
        const activeEditors = visibleInputs().filter(el => !isSearchLike(el));
        if (activeEditors.length === 1) {{
          inlineInput = activeEditors[0];
          record('reply_input_reused', {{reason:'target_reply_button_active'}});
        }}
      }}
      if (inlineInput) {{
        // 当前评论已经打开回复编辑器，直接复用，不重复点击“回复”。
        record('reply_input_reused', {{reason:'existing_inline_input'}});
      }} else if (!replyButton) {{
        inlineInput = [...comment.querySelectorAll(inputSelector)]
          .filter(visible).find(el => !isSearchLike(el));
        record('reply_button_missing', {{inlineInputFallback:!!inlineInput}});
        if (!inlineInput) return {{ok:false, stage:'reply_button_not_found',
          message:'目标评论内未找到回复按钮，未执行任何输入', trace}};
      }} else if (isDouyinNote) {{
        // Note 页的回复控件由 React 事件链处理，脚本 click 可能只改变
        // 外观而没有真正切换回复上下文。返回目标控件信息，由上层通过
        // 当前 DOM 盒模型进行浏览器动态点击；不依赖固定屏幕坐标。
        record('reply_button_dynamic_activation_required', {{
          tag:replyButton.tagName,
          className:String(replyButton.className || ''),
          text:String(replyButton.innerText || '').trim()
        }});
        return {{ok:false, stage:'reply_input_not_found',
          message:'目标回复控件已解析，准备语义激活', matched:true,
          inputCandidates:[], replyButtonFound:true,
          requiresDomReplyActivation:true, trace}};
      }} else {{
        record('reply_button_click_started', {{text:String(replyButton.innerText || '').trim()}});
        replyButton.focus?.();
        for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup']) {{
          const Ctor = type.startsWith('pointer') && window.PointerEvent
            ? window.PointerEvent : window.MouseEvent;
          replyButton.dispatchEvent(new Ctor(type,
            {{bubbles:true, cancelable:true, view:window, buttons:1}}));
        }}
        replyButton.click();
        record('reply_button_click_completed', {{after:scrollSnapshot(comment)}});
      }}
      await sleep(800);
      record('post_reply_click_wait_completed', {{after:scrollSnapshot(comment)}});

      const inputs = visibleInputs().filter(el => !isSearchLike(el));
      const markedInputs = inputs.filter(el => /回复|回应|答复|评论|输入/.test(inputMeta(el))
        || (wantedUser && inputMeta(el).includes(wantedUser)));
      const newInputs = inputs.filter(el => !beforeInputs.has(el));
      // 作品级编辑器也带有 comment-input class，不能仅凭 class 认定它
      // 属于目标评论。点击目标回复后，如果编辑器没有目标昵称标记，
      // 交给上层用 DOM 语义激活目标回复控件，不能直接输入。
      const commentScopedInputs = inputs.filter(el =>
        comment.contains(el) || inputHasTargetMarker(el));
      let input = null;
      let selectionMethod = '';
      if (inlineInput) {{
        input = inlineInput;
        selectionMethod = 'inline_comment_input';
      }} else if (newInputs.length === 1) {{
        input = newInputs[0];
        selectionMethod = 'new_after_reply_click';
      }} else if (markedInputs.length === 1) {{
        input = markedInputs[0];
        selectionMethod = 'reply_marker';
      }} else if (commentScopedInputs.length === 1) {{
        input = commentScopedInputs[0];
        selectionMethod = 'comment_scoped_input';
      }}
      record('reply_inputs_detected', {{
        inputCount:inputs.length, newInputCount:newInputs.length,
        markedInputCount:markedInputs.length, commentScopedInputCount:commentScopedInputs.length,
        inputCandidates:inputs.map(inputMeta), selectionMethod
      }});
      if (!input) return {{ok:false, stage:'reply_input_not_found',
        message:'点击回复后未挂载回复输入框，准备语义激活重试', matched:true,
        inputCandidates:inputs.map(inputMeta), newInputCount:newInputs.length,
        markedInputCount:markedInputs.length,
        requiresDomReplyActivation:!!replyButton, trace}};

      input.focus();
      record('reply_input_selected', {{
        selectionMethod, tag:input.tagName,
        placeholder:input.getAttribute('placeholder') || '',
        contentEditable:!!(input.isContentEditable || input.getAttribute('contenteditable') === 'true'),
        beforeText:String(input.innerText || input.value || input.textContent || '').slice(0,300),
        rect:rectData(input), scroll:scrollSnapshot(input)
      }});
      if (input.isContentEditable || input.getAttribute('contenteditable') === 'true') {{
        const range = document.createRange();
        range.selectNodeContents(input);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        record('contenteditable_ready_for_cdp_input', {{
          insertionMethod:'Input.insertText',
          execCommandUsed:false,
          syntheticBeforeInputDispatched:false,
          syntheticInputDispatched:false,
          syntheticChangeDispatched:false,
          selectionText:String(selection.toString() || '').slice(0,300)
        }});
        return {{ok:true, stage:'input_ready',
          message:'回复输入框已定位，等待浏览器级输入', verified:false,
          matched:true, requiresCdpInput:true, replyButtonFound:!!replyButton,
          selectionMethod, inputTag:input.tagName,
          inputPlaceholder:input.getAttribute('placeholder') || '', trace}};
      }} else {{
        const proto = Object.getPrototypeOf(input);
        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
        if (setter) setter.call(input, cfg.reply); else input.value = cfg.reply;
        input.dispatchEvent(new Event('input', {{bubbles:true}}));
        record('native_input_value_set', {{setterUsed:!!setter}});
      }}
      await sleep(0);
      record('post_input_microtask_check', {{
        inputConnected:document.contains(input), commentConnected:document.contains(comment),
        scroll:scrollSnapshot(comment)
      }});
      await sleep(100);
      record('post_input_settle_check', {{
        inputConnected:document.contains(input), commentConnected:document.contains(comment),
        scroll:scrollSnapshot(comment)
      }});
      const current = norm(input.innerText || input.value || input.textContent);
      const verified = current === norm(cfg.reply) || current.includes(norm(cfg.reply));
      record('reply_input_feedback_checked', {{
        verified, currentText:String(input.innerText || input.value || input.textContent || '').slice(0,300),
        after:scrollSnapshot(input)
      }});
      return {{ok:verified, stage:verified ? 'filled' : 'fill_unverified',
        message:verified ? '回复已填入输入框' : '输入框内容校验不一致',
        verified, matched:true, replyButtonFound:!!replyButton,
        selectionMethod, inputTag:input.tagName,
        inputPlaceholder:input.getAttribute('placeholder') || '', trace}};
    }})()"""


async def _click_reply_button_dynamically(session, session_id: str,
                                          target: ReplyTarget,
                                          trace: Optional[DebugTrace] = None) -> dict:
    """实时解析 DOM 回复控件，再按当前盒模型动态点击。

    抖音 Note 页的回复编辑器是全局 Draft.js 编辑器，按钮本身却在目标
    评论节点内。直接执行 ``element.click()`` 有时不会触发站点的可信事件链；
    因此先由页面脚本精确标记目标按钮，再通过 DOM.getBoxModel 读取本次
    点击所需的瞬时位置。位置只在当前点击前计算，不保存、不写死。
    """
    marker = "codex-reply-target-" + uuid.uuid4().hex
    marked = await session.eval(
        _reply_button_semantic_marker_script(target, marker), session_id,
        timeout=20.0,
    )
    if trace:
        trace.emit("reply_button_dynamic_target_resolved", result=marked)
    if not isinstance(marked, dict) or not marked.get("ok"):
        return marked if isinstance(marked, dict) else {
            "ok": False, "stage": "reply_button_not_found",
            "message": "未解析到目标评论的回复控件",
        }
    selector = f'[data-codex-reply-target="{marker}"]'
    try:
        await session.cmd("DOM.enable", session_id=session_id)
        document = await session.cmd(
            "DOM.getDocument", {"depth": -1, "pierce": True},
            session_id=session_id,
        )
        root_id = ((document or {}).get("root") or {}).get("nodeId")
        if not root_id:
            return {"ok": False, "stage": "dom_document_not_found",
                    "message": "未获取页面 DOM 根节点"}
        found = await session.cmd(
            "DOM.querySelector", {"nodeId": root_id, "selector": selector},
            session_id=session_id,
        )
        node_id = (found or {}).get("nodeId")
        if not node_id:
            return {"ok": False, "stage": "reply_button_dom_not_found",
                    "message": "目标回复控件已解析但未获取 DOM 节点"}
        described = await session.cmd(
            "DOM.describeNode", {"nodeId": node_id}, session_id=session_id,
        )
        backend_node_id = (((described or {}).get("node") or {})
                           .get("backendNodeId"))
        if not backend_node_id:
            return {"ok": False, "stage": "reply_button_backend_node_missing",
                    "message": "目标回复控件缺少可聚焦的 DOM 节点"}
        await session.cmd("Page.bringToFront", session_id=session_id)
        box = await session.cmd(
            "DOM.getBoxModel", {"nodeId": node_id}, session_id=session_id,
        )
        model = (box or {}).get("model") or {}
        quad = model.get("border") or model.get("content") or []
        if len(quad) < 8:
            return {"ok": False, "stage": "reply_button_box_missing",
                    "message": "目标回复控件缺少当前布局盒模型"}
        xs = quad[0::2]
        ys = quad[1::2]
        x = sum(xs) / len(xs)
        y = sum(ys) / len(ys)
        for event_type in ("mouseMoved", "mousePressed", "mouseReleased"):
            await session.cmd(
                "Input.dispatchMouseEvent",
                {"type": event_type, "x": x, "y": y,
                 "button": "left" if event_type != "mouseMoved" else "none",
                 "buttons": 1 if event_type == "mousePressed" else 0,
                 "clickCount": 1},
                session_id=session_id,
            )
        return {
            "ok": True,
            "stage": "reply_button_dynamic_clicked",
            "message": "已按目标 DOM 当前布局动态点击回复控件",
            "matchCount": marked.get("matchCount", 0),
            "buttonText": marked.get("buttonText", ""),
            "buttonClass": marked.get("buttonClass", ""),
            "clickMethod": "live_dom_box_model",
        }
    except Exception as exc:
        if trace:
            trace.exception("reply_button_dynamic_click_failed", exc,
                            marker=marker)
        return {"ok": False, "stage": "reply_button_dynamic_click_failed",
                "message": "动态点击目标回复控件失败", "error": str(exc)}
    finally:
        try:
            await session.eval(
                "document.querySelectorAll('[data-codex-reply-target]').forEach(" 
                "el => el.removeAttribute('data-codex-reply-target')); true",
                session_id, timeout=10.0,
            )
        except Exception:
            pass


def _reply_button_semantic_marker_script(target: ReplyTarget, marker: str) -> str:
    """解析目标评论并给其回复控件加一次性 DOM 标记。"""
    payload = _target_json(target, "")
    marker_json = json.dumps(marker, ensure_ascii=False)
    return f"""(function() {{
      const cfg = {payload};
      const marker = {marker_json};
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      const textNorm = s => norm(String(s || '')
        .replace(/\\[[^\\]\\r\\n]{{1,20}}\\]/g, '')
        .replace(/[\\u{{1F000}}-\\u{{1FAFF}}\\u{{2600}}-\\u{{27BF}}\\u{{FE0F}}]/gu, ''));
      const visible = el => {{
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0
          && st.visibility !== 'hidden' && st.display !== 'none';
      }};
      const wantedComment = textNorm(cfg.comment);
      const wantedId = norm(cfg.comment_id);
      const wantedUser = textNorm(cfg.nickname || cfg.user_id);
      const selectors = [
        '[data-e2e="comment-item"]', '[data-e2e*="comment"]',
        '.comment-item', '[class*="comment-item"]',
        '[class*="commentItem"]', '[class*="CommentItem"]',
        '[data-testid*="comment"]', '[class*="comment-content"]',
        '[class*="CommentContent"]', '[data-comment-id]', '[data-commentid]'
      ];
      const rowSelectors = [
        '[data-e2e="comment-item"]', '[data-testid*="comment-item"]',
        '.comment-item', '[class~="commentItem"]', '[class~="CommentItem"]'
      ].join(',');
      const nodes = () => [...new Set(selectors.flatMap(selector =>
        [...document.querySelectorAll(selector)]))]
        .filter(el => el.isConnected && visible(el));
      const hasValue = (node, value) => value && [node, ...node.querySelectorAll('*')]
        .some(el => [...(el.attributes || [])]
          .some(attr => String(attr.value || '').includes(value)));
      const toRow = node => node?.closest?.(rowSelectors) || node;
      const isNote = /(?:^|\\/)note\\//i.test(location.pathname);
      const hasReply = el => [...el.querySelectorAll(
        '.LJU9cDNW,.reply.icon-container,[data-e2e*="reply"],button,[role="button"]'
      )].some(button => visible(button)
        && (/^(回复|回应|答复)$/.test(norm(button.innerText || ''))
          || String(button.className || '').includes('LJU9cDNW')));
      const narrow = node => {{
        if (!isNote || !node || !wantedUser || !wantedComment) return node;
        const candidates = [node, ...node.querySelectorAll('*')]
          .filter(visible).filter(el => {{
            const text = textNorm(el.innerText || '');
            return text.includes(wantedUser) && text.includes(wantedComment);
          }});
        const scoped = candidates.filter(hasReply);
        const pool = scoped.length ? scoped : candidates;
        return pool.sort((a, b) => textNorm(a.innerText || '').length
          - textNorm(b.innerText || '').length)[0] || node;
      }};
      const matches = nodes().filter(node => {{
        const text = textNorm(node.innerText || '');
        const idHit = wantedId && (text.includes(wantedId) || hasValue(node, wantedId));
        return idHit || (wantedUser && wantedComment
          && text.includes(wantedUser) && text.includes(wantedComment));
      }}).map(node => isNote ? narrow(node) : toRow(node));
      const comments = [...new Set(matches)];
      const comment = comments.sort((a, b) => textNorm(a.innerText || '').length
        - textNorm(b.innerText || '').length)[0];
      if (!comment) return {{ok:false, stage:'comment_not_found', matchCount:0,
        message:'未解析到目标评论节点'}};
      const nested = el => {{
        let node = el?.parentElement;
        while (node && node !== comment) {{
          if (node.matches?.(rowSelectors)) return true;
          node = node.parentElement;
        }}
        return false;
      }};
      const iconReply = el => {{
        const cls = typeof el.className === 'string' ? el.className : '';
        return /(^|\\s)reply(\\s|$)/i.test(cls)
          && /(^|\\s)icon-container(\\s|$)/i.test(cls);
      }};
      const normalizeReply = el => el?.closest?.(
        '.LJU9cDNW,.reply.icon-container,[data-e2e*="reply"]') || el;
      const controls = [...new Set([...comment.querySelectorAll(
        '.LJU9cDNW,.reply.icon-container,[data-e2e*="reply"],button,[role="button"],a,span'
      )])].filter(visible).filter(el => !nested(el));
      const exact = [...new Set(controls.filter(el =>
        /^(回复|回应|答复)$/.test(norm(el.innerText || '')) || iconReply(el)
      ).map(normalizeReply))];
      const button = exact[0] || controls.find(el =>
        /回复|回应|答复/.test(norm(el.innerText || '')) || iconReply(el));
      if (!button) return {{ok:false, stage:'reply_button_not_found',
        matchCount:comments.length, message:'目标评论内没有回复控件'}};
      document.querySelectorAll('[data-codex-reply-target]').forEach(el =>
        el.removeAttribute('data-codex-reply-target'));
      button.setAttribute('data-codex-reply-target', marker);
      return {{ok:true, matchCount:comments.length, buttonText:norm(button.innerText),
        buttonClass:String(button.className || ''), buttonTag:button.tagName,
        marker}};
    }})()"""



def _reply_input_probe_script(target: ReplyTarget, content: str) -> str:
    """浏览器级点击后的输入框探测与聚焦脚本。

    该脚本不再点击回复按钮，只处理“按钮已通过 CDP 真实点击”后的编辑器挂载。
    抖音虚拟评论节点偶尔需要额外的渲染周期，因此这里短暂轮询，并复用与主脚本
    相同的可见性、搜索框排除和评论输入框优先级规则。
    """
    payload = _target_json(target, content)
    return f"""(async function() {{
      const cfg = {payload};
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      const visible = el => {{
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.visibility !== 'hidden'
          && st.display !== 'none' && Number(st.opacity || 1) > 0;
      }};
      const inViewport = el => {{
        const r = el.getBoundingClientRect();
        return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
      }};
      const rectData = el => {{
        const r = el?.getBoundingClientRect?.();
        return r ? {{left:r.left, top:r.top, right:r.right, bottom:r.bottom,
          width:r.width, height:r.height}} : null;
      }};
      const inputSelector = '[contenteditable="true"], textarea, input, [role="textbox"]';
      const inputMeta = el => norm([
        el.getAttribute('placeholder') || '',
        el.getAttribute('aria-label') || '',
        el.getAttribute('name') || '',
        el.getAttribute('id') || '',
        el.getAttribute('data-e2e') || '',
        typeof el.className === 'string' ? el.className : '',
      ].join(' '));
      const isSearchLike = el => {{
        const meta = inputMeta(el).toLowerCase();
        const type = String(el.getAttribute('type') || '').toLowerCase();
        return type === 'search' || /搜索|search/.test(meta);
      }};
      const trace = [];
      for (let attempt = 1; attempt <= 6; attempt++) {{
        const inputs = [...document.querySelectorAll(inputSelector)]
          .filter(el => visible(el) && inViewport(el) && !isSearchLike(el));
        const markedInputs = inputs.filter(el => /回复|回应|答复|评论|输入/.test(inputMeta(el)));
        const commentScopedInputs = inputs.filter(el =>
          el.closest('#comment-input-container,[data-e2e*="comment-input"],'
            + '[class*="comment-input"],[class*="DraftEditor"]'));
        let input = null;
        let selectionMethod = '';
        if (inputs.length === 1) {{
          input = inputs[0];
          selectionMethod = 'native_click_single_input';
        }} else if (markedInputs.length === 1) {{
          input = markedInputs[0];
          selectionMethod = 'native_click_reply_marker';
        }} else if (commentScopedInputs.length === 1) {{
          input = commentScopedInputs[0];
          selectionMethod = 'native_click_comment_scoped_input';
        }}
        trace.push({{step:'native_click_input_probe', attempt,
          inputCount:inputs.length, markedInputCount:markedInputs.length,
          commentScopedInputCount:commentScopedInputs.length,
          inputCandidates:inputs.map(inputMeta)}});
        if (input) {{
          input.focus();
          if (input.isContentEditable || input.getAttribute('contenteditable') === 'true') {{
            const range = document.createRange();
            range.selectNodeContents(input);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            return {{ok:true, stage:'input_ready', message:'回复输入框已定位，等待浏览器级输入',
              verified:false, matched:true, requiresCdpInput:true,
              replyButtonFound:true, selectionMethod, inputTag:input.tagName,
              inputPlaceholder:input.getAttribute('placeholder') || '', trace}};
          }}
          const proto = Object.getPrototypeOf(input);
          const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
          if (setter) setter.call(input, cfg.reply); else input.value = cfg.reply;
          input.dispatchEvent(new Event('input', {{bubbles:true}}));
          await sleep(80);
          const current = norm(input.value || input.innerText || input.textContent);
          const verified = current === norm(cfg.reply) || current.includes(norm(cfg.reply));
          return {{ok:verified, stage:verified ? 'filled' : 'fill_unverified',
            message:verified ? '回复已填入输入框' : '输入框内容校验不一致',
            verified, matched:true, replyButtonFound:true,
            selectionMethod, inputTag:input.tagName,
            inputPlaceholder:input.getAttribute('placeholder') || '', trace}};
        }}
        await sleep(180);
      }}
      return {{ok:false, stage:'reply_input_not_found',
        message:'浏览器级点击后仍未出现回复输入框', verified:false,
        matched:true, trace}};
    }})()"""


def _input_feedback_script(content: str, platform: str = "") -> str:
    """读取浏览器级输入后的真实 DOM 状态，区分输入成功和组件卸载。"""
    if _platform_key(platform) in ("bilibili", "bili"):
        return _input_feedback_bilibili_script(content)
    if _platform_key(platform) in ("weibo", "wb"):
        return _input_feedback_weibo_script(content)
    payload = json.dumps(content, ensure_ascii=False)
    return f"""(function() {{
      const wanted = {payload};
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      const visible = el => {{
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none'
          && st.visibility !== 'hidden';
      }};
      const inputs = [...document.querySelectorAll(
        '[contenteditable="true"], textarea, input'
      )].filter(visible);
      const input = inputs.find(el => norm(
        el.innerText || el.value || el.textContent || ''
      ).includes(norm(wanted)));
      const route = document.querySelector('.parent-route-container');
      const commentNodes = document.querySelectorAll(
        '[data-e2e="comment-item"],[data-e2e*="comment"],.comment-item,[class*="comment-item"],[class*="commentItem"]'
      ).length;
      const current = input ? String(
        input.innerText || input.value || input.textContent || ''
      ) : '';
      return {{
        ok:!!input && norm(current).includes(norm(wanted)),
        inputConnected:!!input && document.contains(input),
        commentConnected:commentNodes > 0,
        commentNodeCount:commentNodes,
        currentText:current.slice(0,300),
        bodyContainsReply:!!document.body && (document.body.innerText || '').includes(wanted),
        routeScrollTop:route ? route.scrollTop : null,
        routeScrollHeight:route ? route.scrollHeight : null,
        routeClientHeight:route ? route.clientHeight : null,
        inputRect:input ? (() => {{ const r=input.getBoundingClientRect();
          return {{left:r.left, top:r.top, right:r.right, bottom:r.bottom,
            width:r.width, height:r.height}}; }})() : null
      }};
    }})()"""


def _input_feedback_bilibili_script(content: str) -> str:
    payload = json.dumps(content, ensure_ascii=False)
    return f"""(function() {{
      const wanted = {payload};
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      const clean = s => String(s || '').replace(/\\s+/g, ' ').trim();
      const host = document.querySelector('bili-comments');
      const root = host?.shadowRoot;
      const threads = [...(root?.querySelectorAll(
        'bili-comment-thread-renderer'
      ) || [])];
      const find = () => {{
        for (const thread of threads) {{
          const box = thread.shadowRoot?.querySelector(
            '#reply-container bili-comment-box'
          );
          const editor = box?.shadowRoot?.querySelector(
            'bili-comment-rich-textarea'
          )?.shadowRoot?.querySelector('.brt-editor[contenteditable="true"]');
          if (editor && norm(editor.innerText || editor.textContent)
              .includes(norm(wanted))) return {{thread, box, editor}};
        }}
        return null;
      }};
      const found = find();
      const editor = found?.editor;
      const current = editor ? String(
        editor.innerText || editor.textContent || ''
      ) : '';
      const er = editor?.getBoundingClientRect?.();
      const tr = found?.thread?.getBoundingClientRect?.();
      const commentNodes = threads.length;
      return {{
        ok:!!editor && norm(current).includes(norm(wanted)),
        inputConnected:!!editor && editor.isConnected,
        commentConnected:!!found?.thread && found.thread.isConnected,
        commentNodeCount:commentNodes,
        currentText:current.slice(0,300),
        bodyContainsReply:false,
        pageScrollY:scrollY,
        commentRect:tr ? {{left:tr.left, top:tr.top, right:tr.right,
          bottom:tr.bottom, width:tr.width, height:tr.height}} : null,
        inputRect:er ? {{left:er.left, top:er.top, right:er.right,
          bottom:er.bottom, width:er.width, height:er.height}} : null,
        publishButton:(() => {{
          const button = found?.box?.shadowRoot?.querySelector('#pub button');
          return {{found:!!button, text:clean(button?.innerText),
            disabled:!!button?.disabled}};
        }})()
      }};
    }})()"""


def _input_feedback_weibo_script(content: str) -> str:
    payload = json.dumps(content, ensure_ascii=False)
    return f"""(function() {{
      const wanted = {payload};
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      const clean = s => String(s || '').replace(/\\s+/g, ' ').trim();
      const visible = el => {{
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none'
          && st.visibility !== 'hidden';
      }};
      const input = [...document.querySelectorAll(
        'textarea[placeholder="发布你的回复"]'
      )].find(visible);
      const current = input ? String(input.value || '') : '';
      const r = input?.getBoundingClientRect?.();
      const item = [...document.querySelectorAll('.wbpro-scroller-item')]
        .find(node => visible(node));
      const ir = item?.getBoundingClientRect?.();
      const button = input ? [...document.querySelectorAll('button')]
        .find(candidate => visible(candidate)
          && clean(candidate.innerText) === '回复') : null;
      return {{
        ok:!!input && norm(current).includes(norm(wanted)),
        inputConnected:!!input && input.isConnected,
        commentConnected:!!item && item.isConnected,
        commentNodeCount:document.querySelectorAll('.wbpro-scroller-item').length,
        currentText:current.slice(0,300),
        bodyContainsReply:!!document.body
          && (document.body.innerText || '').includes(wanted),
        pageScrollY:scrollY,
        commentRect:ir ? {{left:ir.left, top:ir.top, right:ir.right,
          bottom:ir.bottom, width:ir.width, height:ir.height}} : null,
        inputRect:r ? {{left:r.left, top:r.top, right:r.right,
          bottom:r.bottom, width:r.width, height:r.height}} : null,
        replyButton:{{found:!!button, text:clean(button?.innerText),
          disabled:!!button?.disabled}},
        modalOpen:!!input
      }};
    }})()"""


def _submit_script(content: str, platform: str = "") -> str:
    if _platform_key(platform) in ("bilibili", "bili"):
        return _submit_bilibili_script(content)
    if _platform_key(platform) in ("weibo", "wb"):
        return _submit_weibo_script(content)
    if _platform_key(platform) in ("douyin", "dy"):
        return _submit_douyin_script(content)
    if _platform_key(platform) in ("xhs", "xiaohongshu"):
        return _submit_xhs_script(content)
    return _submit_generic_script(content)


def _submit_douyin_script(content: str) -> str:
    """抖音发送按钮定位。

    抖音 Draft.js 编辑器的发送控件不在编辑器的两层父节点内，
    而是在作品评论编辑器容器中；必须先锁定容器，再扫描发送控件。
    """
    payload = json.dumps(content, ensure_ascii=False)
    return f"""(async function() {{
      const wanted = {payload};
      const trace = [];
      const record = (step, data = {{}}) => trace.push(Object.assign({{step}}, data));
      const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
      const clean = s => String(s || '').replace(/\\s+/g, '').trim();
      const visible = el => {{
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0
          && st.display !== 'none' && st.visibility !== 'hidden';
      }};
      const enabled = el => !el.disabled
        && el.getAttribute('aria-disabled') !== 'true';
      const input = [...document.querySelectorAll(
        '[contenteditable="true"], textarea, input'
      )].find(el => visible(el)
        && String(el.innerText || el.value || el.textContent || '').includes(wanted));
      record('submit_inputs_detected', {{count:document.querySelectorAll(
        '[contenteditable="true"], textarea, input').length, matchingInput:!!input,
        inputTag:input?.tagName || '', inputClass:String(input?.className || '')}});
      if (!input) return {{clicked:false, stage:'submit_input_not_found',
        message:'未找到已填入内容的抖音回复输入框', trace}};

      // 抖音发送按钮只有在输入内容后才进入可用状态，且不是 button：
      // #comment-input-container .commentInput-right-ct 内的第三个 span，
      // 内含红色 #FE2C55 上箭头 SVG。必须从当前编辑器向上锁定容器，
      // 不能全局按“回复”文字搜索，否则会点到评论行的回复入口。
      const editorRoot = () => input.closest(
        '#comment-input-container,.comment-input-container-focus,.comment-input-inner-container'
      );
      const candidateData = el => {{
        const r = el?.getBoundingClientRect?.();
        return {{
          tag:el?.tagName || '', text:String(el?.innerText || el?.textContent || '').trim().slice(0,100),
          className:String(el?.className || '').slice(0,160),
          aria:el?.getAttribute?.('aria-label') || '', title:el?.getAttribute?.('title') || '',
          rect:r ? {{left:r.left, top:r.top, right:r.right, bottom:r.bottom,
            width:r.width, height:r.height}} : null,
          redArrow:!!el?.querySelector?.('path[fill="#FE2C55"],path[fill="#fe2c55"]')
        }};
      }};
      const findSendIcon = () => {{
        const root = editorRoot();
        if (!root) return {{root:null, candidates:[], send:null}};
        const candidates = [...new Set([
          ...root.querySelectorAll('.commentInput-right-ct span'),
          ...root.querySelectorAll('.commentInput-right-ct svg')
        ])].map(el => el.closest?.('span') || el)
          .filter((el, index, all) => all.indexOf(el) === index && visible(el));
        const send = candidates.find(el =>
          [...el.querySelectorAll('path')].some(path =>
            String(path.getAttribute('fill') || '').toUpperCase() === '#FE2C55'
          )
        );
        return {{root, candidates, send}};
      }};
      let send = null;
      for (let attempt = 1; attempt <= 20; attempt++) {{
        const state = findSendIcon();
        send = state.send;
        record('submit_button_probe', {{attempt,
          strategy:'douyin_red_arrow_after_input',
          root:state.root ? {{id:state.root.id || '', className:String(state.root.className || '')}} : null,
          candidateCount:state.candidates.length,
          candidates:state.candidates.slice(0,8).map(candidateData),
          selected:send ? candidateData(send) : null}});
        if (send) break;
        await sleep(150);
      }}
      if (!send) return {{clicked:false, stage:'submit_button_not_found',
        message:'输入内容后未找到抖音红色上箭头发送按钮', trace}};
      record('submit_button_selected', Object.assign({{strategy:'douyin_red_arrow_after_input'}}, candidateData(send)));
      send.dispatchEvent(new MouseEvent('mousedown', {{bubbles:true, cancelable:true, view:window}}));
      send.dispatchEvent(new MouseEvent('mouseup', {{bubbles:true, cancelable:true, view:window}}));
      send.click();
      record('submit_button_clicked');
      return {{clicked:true, stage:'submit_clicked',
        buttonText:'红色上箭头发送按钮', sendElement:candidateData(send), trace}};
    }})()"""


def _submit_xhs_script(content: str) -> str:
    """小红书发送按钮定位。

    小红书的编辑器在 ``.input-box`` 内，发送按钮位于同级的
    ``.engage-bar``/``.right-btn-area``，不能只查输入框的父节点。
    """
    payload = json.dumps(content, ensure_ascii=False)
    return f"""(async function() {{
      const wanted = {payload};
      const trace = [];
      const record = (step, data = {{}}) => trace.push(Object.assign({{step}}, data));
      const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      const clean = s => String(s || '').replace(/\\s+/g, ' ').trim();
      const visible = el => {{
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0
          && st.display !== 'none' && st.visibility !== 'hidden';
      }};
      const enabled = el => !el.disabled
        && el.getAttribute('aria-disabled') !== 'true';
      const input = [...document.querySelectorAll(
        '#content-textarea[contenteditable="true"],'
        + '.content-input[contenteditable="true"],'
        + '[contenteditable="true"], textarea'
      )].find(el => visible(el)
        && norm(el.innerText || el.value || el.textContent).includes(norm(wanted)));
      record('submit_inputs_detected', {{count:document.querySelectorAll(
        '[contenteditable="true"], textarea').length, matchingInput:!!input,
        inputTag:input?.tagName || '', inputClass:String(input?.className || '')}});
      if (!input) return {{clicked:false, stage:'submit_input_not_found',
        message:'未找到已填入内容的小红书回复输入框', trace}};

      const roots = [];
      const addRoot = root => {{ if (root && !roots.includes(root)) roots.push(root); }};
      for (const selector of [
        '.engage-bar', '.engage-bar-container', '.interactions.engage-bar',
        '.input-box', 'form'
      ]) addRoot(input.closest(selector));
      for (let node = input; node && roots.length < 10; node = node.parentElement) {{
        if (node.querySelector?.('button,[role="button"],[class*="submit"]'))
          addRoot(node);
      }}
      if (!roots.length) addRoot(document.body);
      const candidates = () => [...new Set(roots.flatMap(root =>
        [...root.querySelectorAll(
          'button,[role="button"],[data-testid*="submit"],'
          + '[class*="submit"],[class*="publish"]'
        )]))];
      const meta = el => ({{
        tag:el.tagName, text:clean(el.innerText || el.textContent).slice(0,100),
        className:String(el.className || '').slice(0,160),
        disabled:!enabled(el), visible:visible(el)
      }});
      const select = all => all.filter(visible).filter(enabled).find(el =>
        /^(发送|发布|回复|提交)$/.test(norm(el.innerText || el.textContent))
      ) || all.filter(visible).filter(enabled).find(el =>
        /发送|发布|提交/.test(norm(el.innerText || el.textContent))
      );
      let send = null;
      let all = [];
      for (let attempt = 1; attempt <= 14; attempt++) {{
        all = candidates();
        send = select(all);
        record('submit_button_probe', {{attempt, rootCount:roots.length,
          candidateCount:all.length, candidates:all.slice(0,20).map(meta),
          selected:send ? meta(send) : null}});
        if (send) break;
        await sleep(150);
      }}
      if (!send) return {{clicked:false, stage:'submit_button_not_found',
        message:'小红书未找到明确的发送按钮', trace,
        roots:roots.map(root => ({{tag:root.tagName, id:root.id || '',
          className:String(root.className || '').slice(0,120)}}))}};
      record('submit_button_selected', {{text:clean(send.innerText),
        tag:send.tagName, className:String(send.className || ''),
        rootCount:roots.length}});
      send.click();
      record('submit_button_clicked');
      return {{clicked:true, stage:'submit_clicked',
        buttonText:String(send.innerText || ''), trace}};
    }})()"""


def _submit_generic_script(content: str) -> str:
    """兼容未单独适配平台的发送定位，保留完整候选日志。"""
    payload = json.dumps(content, ensure_ascii=False)
    return f"""(async function() {{
      const wanted = {payload};
      const trace = [];
      const record = (step, data = {{}}) => trace.push(Object.assign({{step}}, data));
      const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
      const visible = el => {{ const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
        return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden'; }};
      const enabled = el => !el.disabled
        && el.getAttribute('aria-disabled') !== 'true';
      const inputs = [...document.querySelectorAll('[contenteditable="true"], textarea, input')]
        .filter(visible);
      const input = inputs.find(el => String(el.innerText || el.value || el.textContent || '').includes(wanted));
      record('submit_inputs_detected', {{count:inputs.length, matchingInput:!!input}});
      const roots = [];
      const addRoot = root => {{ if (root && !roots.includes(root)) roots.push(root); }};
      if (input) {{
        for (let node=input; node && roots.length<10; node=node.parentElement) addRoot(node);
        addRoot(input.closest('form'));
      }}
      addRoot(document.body);
      const candidateSelector = 'button,[role="button"],[data-e2e*="send"],'
        + '[data-e2e*="publish"],[class*="send"],[class*="submit"],[class*="publish"]';
      const collect = () => [...new Set(roots.flatMap(root =>
        [...root.querySelectorAll(candidateSelector)]))];
      const candidate = el => ({{tag:el.tagName,
        text:String(el.innerText || el.textContent || '').replace(/\\s+/g,'').slice(0,100),
        className:String(el.className || '').slice(0,160), disabled:!enabled(el), visible:visible(el)}});
      const select = list => list.filter(visible).filter(enabled).find(b =>
        /^(发送|发布|回复|提交)$/.test(String(b.innerText||b.textContent||'').replace(/\\s+/g,''))
      ) || list.filter(visible).filter(enabled).find(b =>
        /发送|发布|提交/.test(String(b.innerText||b.textContent||'').replace(/\\s+/g,''))
      );
      let send = null;
      let all = [];
      for (let attempt=1; attempt<=12; attempt++) {{
        all = collect();
        send = select(all);
        record('submit_button_probe', {{attempt, rootCount:roots.length,
          candidateCount:all.length, candidates:all.slice(0,20).map(candidate),
          selected:send ? candidate(send) : null}});
        if (send) break;
        await sleep(150);
      }}
      if (!send) return {{clicked:false, stage:'submit_button_not_found', message:'未找到明确的发送按钮', trace}};
      record('submit_button_selected', {{text:String(send.innerText || '').trim(), tag:send.tagName}});
      send.click();
      record('submit_button_clicked');
      return {{clicked:true, stage:'submit_clicked', buttonText:String(send.innerText||''), trace}};
    }})()"""


def _submit_bilibili_script(content: str) -> str:
    payload = json.dumps(content, ensure_ascii=False)
    return f"""(async function() {{
      const wanted = {payload};
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      const clean = s => String(s || '').replace(/\\s+/g, ' ').trim();
      const trace = [];
      const record = (step, data = {{}}) => trace.push(Object.assign({{step}}, data));
      const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
      const visible = el => {{
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none'
          && st.visibility !== 'hidden';
      }};
      const enabled = el => !el.disabled
        && el.getAttribute('aria-disabled') !== 'true';
      const host = document.querySelector('bili-comments');
      const root = host?.shadowRoot;
      const findMatch = () => {{
        const threads = [...(root?.querySelectorAll(
          'bili-comment-thread-renderer'
        ) || [])];
        const candidates = [];
        for (const thread of threads) {{
          const box = thread.shadowRoot?.querySelector(
            '#reply-container bili-comment-box'
          );
          const boxRoot = box?.shadowRoot;
          const editor = boxRoot?.querySelector(
            'bili-comment-rich-textarea'
          )?.shadowRoot?.querySelector('.brt-editor[contenteditable="true"]');
          if (!editor || !norm(editor.innerText || editor.textContent)
                .includes(norm(wanted))) continue;
          const button = boxRoot?.querySelector(
            '#pub button,#pub [role="button"],#pub [tabindex="0"]'
          );
          candidates.push({{thread, box, editor, button}});
        }}
        return {{threads, candidates}};
      }};
      let state = findMatch();
      let match = state.candidates[0];
      let send = match?.button;
      for (let attempt = 1; attempt <= 14; attempt++) {{
        state = findMatch();
        match = state.candidates[0];
        send = match?.button;
        record('submit_button_probe', {{attempt, threadCount:state.threads.length,
          matchingInputCount:state.candidates.length,
          sendFound:!!send, sendVisible:!!send && visible(send),
          sendEnabled:!!send && enabled(send), sendText:clean(send?.innerText)}});
        if (send && visible(send) && enabled(send)) break;
        await sleep(150);
      }}
      record('submit_inputs_detected', {{threadCount:state.threads.length,
        matchingInputCount:state.candidates.length}});
      if (!send || !visible(send) || !enabled(send)) return {{clicked:false,
        stage:'submit_button_not_found',
        message:'B站未找到明确的发布按钮', trace,
        shadowDom:!!root, threadCount:state.threads.length}};
      record('submit_button_selected', {{text:clean(send.innerText),
        tag:send.tagName, platform:'bilibili'}});
      send.click();
      record('submit_button_clicked');
      return {{clicked:true, stage:'submit_clicked',
        buttonText:String(send.innerText || ''), trace}};
    }})()"""


def _submit_weibo_script(content: str) -> str:
    payload = json.dumps(content, ensure_ascii=False)
    return f"""(async function() {{
      const wanted = {payload};
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      const clean = s => String(s || '').replace(/\\s+/g, ' ').trim();
      const trace = [];
      const record = (step, data = {{}}) => trace.push(Object.assign({{step}}, data));
      const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
      const visible = el => {{
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none'
          && st.visibility !== 'hidden';
      }};
      const enabled = el => !el.disabled
        && el.getAttribute('aria-disabled') !== 'true';
      const input = [...document.querySelectorAll(
        'textarea[placeholder="发布你的回复"]'
      )].find(el => visible(el)
        && norm(el.value || '').includes(norm(wanted)));
      record('submit_inputs_detected', {{replyInputFound:!!input,
        inputText:String(input?.value || '').slice(0,300)}});
      if (!input) return {{clicked:false, stage:'submit_input_not_found',
        message:'未找到已填入内容的微博回复输入框', trace}};
      const scope = input.closest(
        '.woo-modal-wrap,.woo-modal-main,.wbpro-layer,.wbpro-f-layer'
      ) || document.body;
      const candidateButtons = () => [...scope.querySelectorAll(
        'button,[role="button"]'
      )];
      const meta = button => ({{tag:button.tagName,
        text:clean(button.innerText),
        className:String(button.className || '').slice(0,160),
        disabled:!enabled(button), visible:visible(button)}});
      let send = null;
      let all = [];
      for (let attempt = 1; attempt <= 14; attempt++) {{
        all = candidateButtons();
        send = all.find(button => visible(button) && enabled(button)
          && clean(button.innerText) === '回复');
        record('submit_button_probe', {{attempt, scopeTag:scope.tagName,
          scopeClass:String(scope.className || '').slice(0,120),
          candidateCount:all.length, candidates:all.slice(0,20).map(meta),
          selected:send ? meta(send) : null}});
        if (send) break;
        await sleep(150);
      }}
      if (!send) return {{clicked:false, stage:'submit_button_not_found',
        message:'微博未找到明确的回复按钮', trace}};
      record('submit_button_selected', {{text:clean(send.innerText),
        tag:send.tagName, platform:'weibo'}});
      send.click();
      record('submit_button_clicked');
      return {{clicked:true, stage:'submit_clicked',
        buttonText:String(send.innerText || ''), trace}};
    }})()"""


def _verify_script(content: str, platform: str = "") -> str:
    if _platform_key(platform) in ("bilibili", "bili"):
        return _verify_bilibili_script(content)
    if _platform_key(platform) in ("weibo", "wb"):
        return _verify_weibo_script(content)
    payload = json.dumps(content, ensure_ascii=False)
    return f"""(function() {{
      const wanted = {payload};
      const trace = [];
      const record = (step, data = {{}}) => trace.push(Object.assign({{step}}, data));
      const body = document.body ? document.body.innerText || '' : '';
      const input = [...document.querySelectorAll('[contenteditable="true"], textarea, input')]
        .find(el => String(el.innerText || el.value || el.textContent || '').includes(wanted));
      record('verify_state_read', {{bodyContains:body.includes(wanted), inputCleared:!input}});
      return {{ok:body.includes(wanted) && !input, bodyContains:body.includes(wanted), inputCleared:!input, trace}};
    }})()"""


def _verify_bilibili_script(content: str) -> str:
    payload = json.dumps(content, ensure_ascii=False)
    return f"""(function() {{
      const wanted = {payload};
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      const trace = [];
      const record = (step, data = {{}}) => trace.push(Object.assign({{step}}, data));
      const host = document.querySelector('bili-comments');
      const root = host?.shadowRoot;
      const walkShadow = (currentRoot, callback, depth = 0) => {{
        if (!currentRoot || depth > 16) return;
        let elements = [];
        try {{ elements = [...currentRoot.querySelectorAll('*')]; }} catch {{}}
        for (const element of elements) {{
          callback(element, currentRoot);
          if (element.shadowRoot) walkShadow(element.shadowRoot, callback, depth + 1);
        }}
      }};
      const threads = [];
      const replyNodes = [];
      const richTexts = [];
      walkShadow(root, element => {{
        const tag = String(element.tagName || '').toLowerCase();
        if (tag === 'bili-comment-thread-renderer') threads.push(element);
        if (tag === 'bili-comment-reply-renderer') replyNodes.push(element);
        if (tag === 'bili-rich-text') richTexts.push(element);
      }});
      const wantedNorm = norm(wanted);
      const postedMatches = [];
      for (const replyNode of replyNodes) {{
        walkShadow(replyNode.shadowRoot, element => {{
          if (String(element.tagName || '').toLowerCase() !== 'bili-rich-text') return;
          const text = norm(element.shadowRoot?.querySelector('#contents')?.innerText);
          if (text.includes(wantedNorm)) postedMatches.push(text);
        }});
      }}
      const posted = postedMatches.length > 0;
      const activeEditor = threads.some(thread => {{
        const editor = thread.shadowRoot?.querySelector(
          '#reply-container bili-comment-box'
        )?.shadowRoot?.querySelector('bili-comment-rich-textarea')
          ?.shadowRoot?.querySelector('.brt-editor[contenteditable="true"]');
        return editor && norm(editor.innerText || editor.textContent)
          .includes(norm(wanted));
      }});
      record('verify_state_read', {{posted, inputCleared:!activeEditor,
        commentNodeCount:threads.length, replyNodeCount:replyNodes.length,
        richTextCount:richTexts.length, postedMatchCount:postedMatches.length}});
      return {{ok:posted && !activeEditor, bodyContains:posted,
        inputCleared:!activeEditor, commentNodeCount:threads.length,
        replyNodeCount:replyNodes.length, postedMatchCount:postedMatches.length,
        trace}};
    }})()"""


def _verify_weibo_script(content: str) -> str:
    payload = json.dumps(content, ensure_ascii=False)
    return f"""(function() {{
      const wanted = {payload};
      const norm = s => String(s || '').replace(/\\s+/g, '').trim();
      const trace = [];
      const record = (step, data = {{}}) => trace.push(Object.assign({{step}}, data));
      const body = document.body ? document.body.innerText || '' : '';
      const posted = body.includes(wanted)
        || [...document.querySelectorAll('.wbpro-scroller-item')]
          .some(node => norm(node.innerText).includes(norm(wanted)));
      const input = [...document.querySelectorAll(
        'textarea[placeholder="发布你的回复"]'
      )].find(el => norm(el.value || '').includes(norm(wanted)));
      record('verify_state_read', {{posted, inputCleared:!input,
        modalOpen:!!input}});
      return {{ok:posted && !input, bodyContains:posted,
        inputCleared:!input, modalOpen:!!input, trace}};
    }})()"""
