# -*- coding: utf-8 -*-
"""发布中心的浏览器预览填充适配器。

这个适配器默认负责：打开选定账号的目标创作页、确认当前页面、填入标题和
完整正文，并返回可审计的填充结果。只有后端收到界面二次确认后，才会进入
real_publish，使用当前可见 DOM 的实时盒模型坐标点击明确的最终发布按钮。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit


PUBLISH_EDITOR_URLS = {
    "douyin": "https://creator.douyin.com/creator-micro/content/upload",
    "xhs": "https://creator.xiaohongshu.com/publish/publish",
    "bilibili": "https://member.bilibili.com/platform/upload/video/frame",
    "weibo": "https://weibo.com/",
}

PUBLISH_BUTTON_LABELS = {
    "douyin": ["发布", "立即发布", "发布作品", "发布视频", "发布图文", "发布笔记"],
    "xhs": ["发布", "立即发布", "发布笔记", "发布图文", "发布视频"],
    "bilibili": ["立即投稿", "发布", "立即发布", "发布动态"],
    "weibo": ["发布", "发送", "发布微博", "发送微博"],
}

PUBLISH_COOLDOWN_SECONDS = 30.0
PUBLISH_RETRY_INTERVAL_SECONDS = 10.0
PUBLISH_MAX_ATTEMPTS = 10
# 发布页经常同时存在导航页、消息页和创作页。单个失去响应的标签不能
# 占住整个发布流程；探测单页时使用短超时，随后换下一个标签。
PUBLISH_TARGET_PROBE_TIMEOUT_SECONDS = 5.0
PUBLISH_TARGETS_TIMEOUT_SECONDS = 8.0

VIDEO_SUFFIXES = frozenset({
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".mpeg", ".mpg",
})
IMAGE_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif", ".heic", ".heif",
})


def _asset_type_from_path(path: str, declared_type: str = "") -> str:
    """优先按本地文件后缀识别素材，避免历史错误元数据把视频当成图片。"""
    suffix = os.path.splitext(str(path or "").strip())[1].lower()
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    declared = str(declared_type or "").strip().lower()
    return declared if declared in {"image", "video"} else "image"


def _editor_url(platform: str, content_type: str, asset_type: str = "") -> str:
    """按平台和内容类型选择入口，避免 B 站文字内容误进视频投稿页。"""
    if platform == "bilibili" and str(content_type or "").strip().lower() in {
        "text", "article", "dynamic_text"
    }:
        return "https://member.bilibili.com/platform/upload/text"
    if platform == "xhs":
        # 小红书同一路径通过 target 决定初始编辑器。已有页面可能停留在
        # target=video；图文草稿必须显式进入 target=image，不能只比较路径。
        target = {"image": "image", "video": "video"}.get(
            str(asset_type or "").strip().lower()
        )
        if target:
            return f"https://creator.xiaohongshu.com/publish/publish?from=menu&target={target}"
    return PUBLISH_EDITOR_URLS[platform]


class PublishingBrowserError(RuntimeError):
    """发布中心浏览器预览填充失败。"""


def _cdp_session_class():
    """兼容包内启动和 src 顶层脚本启动两种导入方式。"""
    try:
        from ..cdp import CdpSession
    except ImportError:  # pragma: no cover - 顶层脚本入口
        from cdp import CdpSession  # type: ignore
    return CdpSession


def _same_host(left: str, right: str) -> bool:
    return bool(urlsplit(str(left or "")).netloc) and (
        urlsplit(str(left or "")).netloc.lower()
        == urlsplit(str(right or "")).netloc.lower()
    )


def _same_editor_page(left: str, right: str) -> bool:
    """主机和创作路径都匹配，避免停在平台首页时误填。"""
    if not _same_host(left, right):
        return False
    target_path = urlsplit(str(right or "")).path.rstrip("/")
    current_path = urlsplit(str(left or "")).path.rstrip("/")
    if not target_path:
        return False
    # B 站文字投稿会从 /text 跳转到 /text/new-article，二者都属于同一编辑器。
    if target_path == "/platform/upload/text":
        same_path = current_path == target_path or current_path.startswith(target_path + "/")
    else:
        same_path = current_path == target_path
    if not same_path:
        return False
    # 只有调用方明确指定 target 时才校验查询参数，兼容原有平台入口。
    expected_target = parse_qs(urlsplit(str(right or "")).query).get("target", [""])[0]
    if expected_target:
        current_target = parse_qs(urlsplit(str(left or "")).query).get("target", [""])[0]
        return current_target == expected_target
    return True


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise PublishingBrowserError("发布预览不能在当前 asyncio 事件循环中同步执行")


class PublishingBrowserAdapter:
    """通过 BitBrowser/CDP 完成发布内容的安全预览填充。"""

    def __init__(self, bitbrowser_client, *, settle_seconds: float = 2.0,
                 on_log: Callable[[str], None] | None = None):
        self._bb = bitbrowser_client
        self._settle_seconds = max(0.5, float(settle_seconds))
        self._on_log = on_log

    def preview_fill(self, *, account: Mapping[str, Any], platform: str,
                     title: str, body: str,
                     topics: list[str] | None = None,
                     content_type: str = "text",
                     assets: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        platform = str(platform or "").strip().lower()
        if platform not in PUBLISH_EDITOR_URLS:
            raise PublishingBrowserError(f"不支持的平台：{platform}")
        title = str(title or "").strip()
        body = str(body or "").strip()
        if not title and not body:
            raise PublishingBrowserError("发布标题和正文不能同时为空")
        window_id = str(account.get("bb_window_id") or "").strip()
        if not window_id:
            raise PublishingBrowserError("账号尚未绑定 BitBrowser 窗口")
        if window_id in {"chrome", "chrome-bilibili"}:
            raise PublishingBrowserError("外部 Chrome 会话暂不支持自动填入")
        return _run(self._preview_fill_async(
            window_id=window_id,
            platform=platform,
            content_type=str(content_type or "text").strip().lower(),
            title=title,
            body=body,
            topics=list(topics or []),
            assets=list(assets or []),
            submit=False,
        ))

    def real_publish(self, *, account: Mapping[str, Any], platform: str,
                     title: str, body: str,
                     topics: list[str] | None = None,
                     content_type: str = "text",
                     assets: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        """填充并点击平台最终发布按钮。

        这个入口只由后端的显式二次确认命令调用；页面定位仍使用当前
        DOM 的可见按钮和实时坐标，不使用固定屏幕坐标或模糊文本点击。
        """
        platform = str(platform or "").strip().lower()
        if platform not in PUBLISH_EDITOR_URLS:
            raise PublishingBrowserError(f"不支持的平台：{platform}")
        title = str(title or "").strip()
        body = str(body or "").strip()
        if not title and not body:
            raise PublishingBrowserError("发布标题和正文不能同时为空")
        window_id = str(account.get("bb_window_id") or "").strip()
        if not window_id:
            raise PublishingBrowserError("账号尚未绑定 BitBrowser 窗口")
        if window_id in {"chrome", "chrome-bilibili"}:
            raise PublishingBrowserError("外部 Chrome 会话暂不支持自动发布")
        return _run(self._preview_fill_async(
            window_id=window_id,
            platform=platform,
            content_type=str(content_type or "text").strip().lower(),
            title=title,
            body=body,
            topics=list(topics or []),
            assets=list(assets or []),
            submit=True,
        ))

    async def _preview_fill_async(self, *, window_id: str, platform: str,
                                  title: str, body: str,
                                  topics: list[str], content_type: str,
                                  assets: list[Mapping[str, Any]],
                                  submit: bool = False) -> dict[str, Any]:
        asset_kinds = {
            _asset_type_from_path(
                str(item.get("path") or ""), str(item.get("asset_type") or "")
            )
            for item in assets
            if isinstance(item, Mapping) and str(item.get("path") or "").strip()
        }
        editor_asset_type = next(iter(asset_kinds), "") if len(asset_kinds) == 1 else ""
        editor_url = _editor_url(platform, content_type, editor_asset_type)
        opened = self._bb.open_browser(
            window_id,
            ignore_default_urls=True,
            new_page_url=editor_url,
        )
        if not isinstance(opened, dict):
            raise PublishingBrowserError("BitBrowser 未返回浏览器连接信息")
        ws_url = str(opened.get("ws") or opened.get("webSocketDebuggerUrl") or "").strip()
        if not ws_url:
            raise PublishingBrowserError("BitBrowser 未返回 CDP 连接地址")

        session = _cdp_session_class()(ws_url, timeout=35.0)
        stage = "连接 CDP"
        try:
            await session.connect()
            stage = "选择响应正常的发布页面"
            sid, initial_page = await _attach_publish_page(session, editor_url)
            # 打开时指定 URL 仍可能被窗口已有页面覆盖；这里再次显式导航，
            # 确保预览填入不会落到导航页或上一次遗留页面。
            if not _same_editor_page(initial_page.get("url", ""), editor_url):
                stage = "导航到发布页面"
                await session.navigate(editor_url, sid, wait_load=False)
            try:
                # 置顶只是辅助动作，某些旧页面会卡住该 CDP 命令，不能阻塞发布。
                await session.cmd(
                    "Page.bringToFront", session_id=sid,
                    timeout=PUBLISH_TARGET_PROBE_TIMEOUT_SECONDS,
                )
            except Exception:
                # 某些 BitBrowser 内核不开放 bringToFront，不影响页面填入。
                pass
            await asyncio.sleep(self._settle_seconds)
            stage = "等待发布页面就绪"
            await _wait_for_document_ready(session, sid, timeout=10.0)
            page = await session.eval("""
                ({url: location.href, title: document.title,
                  readyState: document.readyState,
                  visible: document.visibilityState === 'visible'})
            """, sid, timeout=PUBLISH_TARGET_PROBE_TIMEOUT_SECONDS)
            if not isinstance(page, dict) or not _same_host(page.get("url"), editor_url):
                return {
                    "ok": False,
                    "stage": "target_page_mismatch",
                    "message": "当前页面不是目标平台创作页，未执行任何输入",
                    "page": page if isinstance(page, dict) else {},
                    "target_url": editor_url,
                }

            preparation = await _prepare_editor(
                session, sid, platform, content_type, assets,
                settle_seconds=self._settle_seconds,
            )
            mode_click = preparation.get("mode_click") or {}
            if preparation.get("mode_labels") and (
                preparation.get("mode_confirmed") is False
                or (
                    not mode_click.get("clicked")
                    and preparation.get("mode_confirmed") is not True
                )
            ):
                mode_message = "未能切换到目标内容模式"
                if not mode_click.get("clicked"):
                    mode_message = f"未能切换到{preparation['mode_labels'][0]}模式"
                mode_probe = preparation.get("mode_probe_after") or {}
                mode_detail = (
                    f"模式确认={preparation.get('mode_confirmed')}、"
                    f"目标输入={mode_probe.get('desiredInputCount', 0)}、"
                    f"相反输入={mode_probe.get('oppositeInputCount', 0)}、"
                    f"候选={mode_probe.get('candidateCount', 0)}、"
                    f"激活={mode_probe.get('activeCount', 0)}、"
                    f"备用切换={bool((preparation.get('mode_fallback') or {}).get('clicked'))}"
                )
                return {
                    "ok": False,
                    "stage": "content_mode_not_selected",
                    "message": f"{mode_message}，未执行素材上传和内容填充（{mode_detail}）",
                    "page": page,
                    "preparation": preparation,
                    "send_clicked": False,
                    "submit_executed": False,
                }

            payload = json.dumps({
                "platform": platform,
                "title": title,
                "body": body,
                "topics": topics,
            }, ensure_ascii=False).replace("</", "<\\/")
            stage = "填充发布内容"
            result = await session.eval(_fill_script(payload), sid, timeout=45.0)
            if not isinstance(result, dict):
                return {
                    "ok": False,
                    "stage": "fill_unverified",
                    "message": "页面未返回可验证的填充结果",
                    "page": page,
                    "send_clicked": False,
                    "submit_executed": False,
                }
            result["page"] = page
            result["target_url"] = editor_url
            result["preparation"] = preparation
            # 适配器的职责止于填入，明确记录没有执行提交动作。
            result["send_clicked"] = False
            result["submit_executed"] = False
            if submit:
                if not result.get("ok"):
                    candidates = result.get("candidates") or {}
                    filled = result.get("filled") or {}
                    after = preparation.get("editor_after") or {}
                    mode_click = preparation.get("mode_click") or {}
                    result["message"] = (
                        "编辑器填充未确认，未点击发布按钮"
                        f"（标题候选{candidates.get('title', 0)}、正文候选{candidates.get('body', 0)}；"
                        f"标题已填{bool(filled.get('title'))}、正文已填{bool(filled.get('body'))}；"
                        f"图文模式{bool(mode_click.get('clicked'))}、素材上传{preparation.get('uploaded', 0)}、"
                        f"上传事件{preparation.get('file_events_dispatched', 0)}；"
                        f"模式确认{preparation.get('mode_confirmed')}、"
                        f"备用切换{bool((preparation.get('mode_fallback') or {}).get('clicked'))}；"
                        f"上传后探测标题{after.get('title', 0)}、正文{after.get('body', 0)}）"
                    )
                    return result
                submission = await _submit_publish(
                    session, sid, platform, on_log=self._on_log,
                )
                result["submission"] = submission
                # 只有出现本次点击之后的新成功提示，才把草稿标记为已发布。
                # 鼠标事件本身只能证明“尝试点击”，不能证明平台接受了发布。
                result["submit_executed"] = bool(submission.get("clicked"))
                result["send_clicked"] = bool(submission.get("success_confirmed"))
                if result["send_clicked"]:
                    if submission.get("stage") == "publish_success_network_confirmed":
                        result["stage"] = "publish_success_network_confirmed"
                        result["message"] = "已点击发布按钮并检测到平台接口成功响应"
                    else:
                        result["stage"] = "publish_success_confirmed"
                        result["message"] = "已点击发布按钮并检测到平台成功提示"
                elif submission.get("reason") == "platform_rejected":
                    result["stage"] = "platform_rejected"
                    result["message"] = (
                        f"平台拒绝发布：{submission.get('platform_error') or '平台未提供具体原因'}"
                    )
                elif submission.get("clicked"):
                    result["stage"] = "publish_success_unconfirmed"
                    result["message"] = (
                        "已完成发布按钮重试，但未检测到平台成功提示，未标记为已发布"
                    )
                else:
                    result["stage"] = "submit_button_notfound"
                    reason = str(submission.get("reason") or "")
                    reason_text = {
                        "no_publish_label_match": "页面没有匹配到发布按钮文字或语义属性",
                        "matched_but_disabled_or_unavailable": "匹配到发布候选，但按钮仍处于禁用或不可用状态",
                        "invalid_box_model_rect": "匹配到发布候选，但实时位置无效",
                    }.get(reason, "页面未返回可点击的发布按钮")
                    result["message"] = f"未找到明确的发布按钮，未执行发布：{reason_text}"
            return result
        except asyncio.TimeoutError as exc:
            raise PublishingBrowserError(
                f"{platform} 发布流程在“{stage}”阶段超时，已跳过无响应页面；请检查浏览器是否仍停留在目标发布页"
            ) from exc
        finally:
            await session.close()


async def _attach_publish_page(session, editor_url: str) -> tuple[str, dict[str, Any]]:
    """多页面窗口中优先选择创作页，避免把内容填入导航页。"""
    targets = await session.cmd(
        "Target.getTargets", timeout=PUBLISH_TARGETS_TIMEOUT_SECONDS,
    )
    pages = [item for item in targets.get("targetInfos", [])
             if item.get("type") == "page"]
    if not pages:
        return await session.attach_page(), {"url": ""}

    target_host = urlsplit(editor_url).netloc.lower()
    ordered = sorted(
        pages,
        key=lambda item: (
            not _same_editor_page(str(item.get("url") or ""), editor_url),
            not bool(str(item.get("url") or "").strip()),
            urlsplit(str(item.get("url") or "")).netloc.lower() != target_host,
        ),
    )
    fallback: tuple[str, dict[str, Any]] | None = None
    for page in ordered:
        try:
            attached = await session.cmd(
                "Target.attachToTarget",
                {"targetId": page["targetId"], "flatten": True},
                timeout=PUBLISH_TARGET_PROBE_TIMEOUT_SECONDS,
            )
            sid = str(attached["sessionId"])
            await session.cmd(
                "Page.enable", session_id=sid,
                timeout=PUBLISH_TARGET_PROBE_TIMEOUT_SECONDS,
            )
            await session.cmd(
                "Runtime.enable", session_id=sid,
                timeout=PUBLISH_TARGET_PROBE_TIMEOUT_SECONDS,
            )
            current = await session.eval(
                "({url: location.href, title: document.title})", sid,
                timeout=PUBLISH_TARGET_PROBE_TIMEOUT_SECONDS,
            )
            current = current if isinstance(current, dict) else {"url": page.get("url", "")}
            if fallback is None:
                fallback = (sid, current)
            if _same_editor_page(current.get("url", ""), editor_url):
                return sid, current
        except Exception:
            continue
    if fallback is not None:
        return fallback
    return await session.attach_page(), {"url": ""}


def _publish_log(on_log: Callable[[str], None] | None, message: str) -> None:
    if not on_log:
        return
    try:
        on_log(message)
    except Exception:
        # 日志回调不能影响发布流程本身。
        pass


async def _submit_publish(session, session_id: str, platform: str, *,
                          on_log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """冷却后按平台候选标签重新解析发布按钮，并等待成功提示。"""
    labels = PUBLISH_BUTTON_LABELS.get(platform, ["发布"])
    _publish_log(on_log, "发布内容已填充，进入平台检测冷却，等待30秒")
    await asyncio.sleep(PUBLISH_COOLDOWN_SECONDS)

    # 小红书的最终按钮位于闭合自定义组件中，点击后是否真正提交只能从
    # 发布接口响应确认。监听仅覆盖发布接口，不抓取或保存其他网络内容；
    # 没有事件订阅能力的测试会话/兼容会话继续使用原有成功提示逻辑。
    publish_responses: dict[str, dict[str, Any]] = {}
    remove_publish_listener: Callable[[], Any] | None = None
    publish_url_markers = {
        "xhs": ("edith.xiaohongshu.com/web_api/sns/v2/note",),
    }.get(str(platform or "").strip().lower(), ())
    if publish_url_markers and callable(getattr(session, "on", None)):
        try:
            await session.cmd(
                "Network.enable", {}, session_id=session_id,
                timeout=PUBLISH_TARGET_PROBE_TIMEOUT_SECONDS,
            )

            def _on_publish_response(params: Mapping[str, Any]) -> None:
                response = params.get("response") or {}
                url = str(response.get("url") or "")
                if not url or not any(marker in url for marker in publish_url_markers):
                    return
                request_id = str(params.get("requestId") or "")
                if request_id:
                    publish_responses[request_id] = {
                        "request_id": request_id,
                        "url": url,
                        "status": response.get("status"),
                        "mime_type": response.get("mimeType"),
                    }

            remove_publish_listener = session.on(
                "Network.responseReceived", _on_publish_response,
            )
        except Exception as exc:
            _publish_log(on_log, f"发布接口监听未启用，继续使用页面提示检测：{str(exc)[:160]}")
            remove_publish_listener = None

    async def _read_network_feedback() -> dict[str, Any]:
        if not publish_responses:
            return {}
        for request_id, metadata in list(publish_responses.items()):
            try:
                if callable(getattr(session, "get_body", None)):
                    raw_body = await session.get_body(
                        request_id, session_id,
                        timeout=PUBLISH_TARGET_PROBE_TIMEOUT_SECONDS,
                    )
                else:
                    response_body = await session.cmd(
                        "Network.getResponseBody", {"requestId": request_id},
                        session_id=session_id,
                        timeout=PUBLISH_TARGET_PROBE_TIMEOUT_SECONDS,
                    )
                    raw_body = (response_body or {}).get("body", "")
                payload = json.loads(str(raw_body or ""))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if "success" not in payload and "need_retry" not in payload:
                continue
            retryable = payload.get("need_retry")
            return {
                **metadata,
                "available": True,
                "success": bool(payload.get("success")),
                "retryable": retryable if isinstance(retryable, bool) else None,
                "message": str(payload.get("msg") or payload.get("message") or "").strip(),
                "result": payload.get("result"),
            }
        return {}

    def _cleanup_publish_listener() -> None:
        nonlocal remove_publish_listener
        if remove_publish_listener is None:
            return
        try:
            remove_publish_listener()
        except Exception:
            pass
        remove_publish_listener = None

    baseline = await session.eval(_publish_success_probe_script(platform), session_id)
    baseline_matches = set((baseline or {}).get("matches") or []) if isinstance(baseline, dict) else set()
    last: dict[str, Any] = {
        "clicked": False,
        "click_count": 0,
        "success_confirmed": False,
        "label": "",
        "method": "fresh_box_model_click",
        "cooldown_seconds": PUBLISH_COOLDOWN_SECONDS,
        "retry_interval_seconds": PUBLISH_RETRY_INTERVAL_SECONDS,
        "attempts": 0,
    }
    clicked_any = False
    for attempt in range(1, PUBLISH_MAX_ATTEMPTS + 1):
        _publish_log(on_log, f"发布尝试 {attempt}/{PUBLISH_MAX_ATTEMPTS}：重新解析当前发布按钮")
        point: dict[str, Any] = {}
        retry_delay = PUBLISH_RETRY_INTERVAL_SECONDS
        # 输入事件到 React/Vue 状态和按钮 enabled 状态之间可能有一小段延迟。
        # 每次真正点击前都重新解析实时 DOM，不复用旧坐标。
        for probe_attempt in range(1, 13):
            value: Any
            # 小红书最终“发布”按钮位于 xhs-publish-btn 自定义组件内部，
            # 普通 document.querySelector 看不到这个闭合组件树；优先通过
            # CDP 展开 DOM 获取按钮的实时盒模型，失败时再走通用候选探测。
            if platform == "xhs":
                value = await _xhs_publish_button_probe(session, session_id, labels)
                if not isinstance(value, dict) or not value.get("clicked"):
                    value = await session.eval(
                        _safe_mode_click_script(labels, final_submit=True), session_id,
                    )
            else:
                value = await session.eval(
                    _safe_mode_click_script(labels, final_submit=True), session_id,
                )
            point = value if isinstance(value, dict) else {
                "clicked": False, "reason": "invalid_probe_result",
            }
            if point.get("clicked"):
                point["probe_attempt"] = probe_attempt
                break
            if probe_attempt < 12:
                await asyncio.sleep(0.25)
        last = {
            **last,
            **point,
            "clicked": clicked_any,
            "click_count": int(last.get("click_count") or 0),
            "attempts": attempt,
            "cooldown_seconds": PUBLISH_COOLDOWN_SECONDS,
            "retry_interval_seconds": PUBLISH_RETRY_INTERVAL_SECONDS,
        }
        if point.get("clicked"):
            x, y = float(point.get("x") or 0), float(point.get("y") or 0)
            if x > 0 and y > 0:
                click_started = asyncio.get_running_loop().time()
                # 先移动到实时按钮中心，再按下/释放。部分平台的自定义
                # Web Component 只在完整鼠标轨迹进入后才触发最终按钮事件。
                for event_type, button, buttons in (
                    ("mouseMoved", "none", 0),
                    ("mousePressed", "left", 1),
                    ("mouseReleased", "left", 0),
                ):
                    await session.cmd(
                        "Input.dispatchMouseEvent",
                        {"type": event_type, "x": x, "y": y,
                         "button": button, "buttons": buttons, "clickCount": 1},
                        session_id=session_id,
                    )
                last.update({
                    "clicked": True, "x": x, "y": y,
                    "click_count": int(last.get("click_count") or 0) + 1,
                    "label": str(point.get("label") or ""),
                    "method": str(point.get("method") or "fresh_box_model_click"),
                })
                clicked_any = True
                _publish_log(
                    on_log,
                    f"发布尝试 {attempt}/{PUBLISH_MAX_ATTEMPTS}：已定位并点击“{last['label'] or '发布按钮'}”"
                    f"（实时坐标 {x:.0f},{y:.0f}，解析方式 {last.get('method') or '实时盒模型'}，"
                    f"候选 {last.get('candidateCount', 0)}），等待成功提示",
                )
                confirmation = await _wait_publish_success(
                    session, session_id, platform, baseline_matches,
                )
                last["confirmation"] = confirmation
                if confirmation.get("success"):
                    last.update({"success_confirmed": True, "stage": "publish_success_confirmed"})
                    _publish_log(on_log, f"发布尝试 {attempt}/{PUBLISH_MAX_ATTEMPTS}：检测到平台成功提示")
                    _cleanup_publish_listener()
                    return last
                network_feedback = await _read_network_feedback()
                if network_feedback:
                    last["network_feedback"] = network_feedback
                    if network_feedback.get("success"):
                        last.update({
                            "success_confirmed": True,
                            "stage": "publish_success_network_confirmed",
                        })
                        _publish_log(
                            on_log,
                            f"发布尝试 {attempt}/{PUBLISH_MAX_ATTEMPTS}：检测到平台接口成功响应"
                            f"（{network_feedback.get('message') or '无附加说明'}）",
                        )
                        _cleanup_publish_listener()
                        return last
                    if network_feedback.get("retryable") is False:
                        last.update({
                            "reason": "platform_rejected",
                            "platform_error": network_feedback.get("message") or "平台拒绝发布",
                            "stage": "platform_rejected",
                        })
                        _publish_log(
                            on_log,
                            "平台已明确拒绝本次发布，停止重复点击："
                            f"{last['platform_error']}",
                        )
                        _cleanup_publish_listener()
                        return last
                last["reason"] = "success_prompt_not_detected"
                _publish_log(
                    on_log,
                    f"发布尝试 {attempt}/{PUBLISH_MAX_ATTEMPTS}：未检测到新的成功提示，准备10秒后重试",
                )
                retry_delay = max(
                    0.0,
                    PUBLISH_RETRY_INTERVAL_SECONDS
                    - (asyncio.get_running_loop().time() - click_started),
                )
            else:
                last.update({"clicked": False, "reason": "invalid_box_model_point"})
        if attempt < PUBLISH_MAX_ATTEMPTS:
            # 重试间隔从上一次真实鼠标点击开始计算，保证“每 10 秒点击一次”。
            await asyncio.sleep(retry_delay)
    last.update({
        "clicked": clicked_any,
        "success_confirmed": False,
        "stage": "publish_success_unconfirmed",
        "max_attempts": PUBLISH_MAX_ATTEMPTS,
    })
    _publish_log(
        on_log,
        f"发布重试结束：连续{PUBLISH_MAX_ATTEMPTS}次未检测到平台成功提示，保留未确认状态",
    )
    _cleanup_publish_listener()
    return last


async def _xhs_publish_button_probe(session, session_id: str,
                                    labels: list[str]) -> dict[str, Any]:
    """读取小红书自定义发布组件内部的最终按钮盒模型。

    小红书创作页使用 ``xhs-publish-btn`` 自定义元素。它的实际“发布”
    按钮在闭合组件树中，页面 JS 的 ``querySelectorAll('button')`` 只能
    看到宿主元素，无法看到内部 ``button.ce-btn.bg-red``。CDP 的
    ``DOM.getFlattenedDocument(pierce=true)`` 可以读取这棵展开树；每次
    尝试都重新取得 nodeId 和 box model，因此窗口移动、缩放、页面滚动
    后仍使用当前坐标，不保存固定位置。
    """
    try:
        await session.cmd(
            "DOM.enable", session_id=session_id,
            timeout=PUBLISH_TARGET_PROBE_TIMEOUT_SECONDS,
        )
        flattened = await session.cmd(
            "DOM.getFlattenedDocument", {"depth": -1, "pierce": True},
            session_id=session_id,
            timeout=PUBLISH_TARGET_PROBE_TIMEOUT_SECONDS,
        )
        nodes = list((flattened or {}).get("nodes") or [])
        if not nodes:
            return {
                "clicked": False,
                "reason": "xhs_publish_dom_empty",
                "candidateCount": 0,
                "method": "live_dom_flattened_box_model",
            }

        by_id = {node.get("nodeId"): node for node in nodes if node.get("nodeId")}
        parent_links = {
            node.get("nodeId"): node.get("parentId")
            for node in nodes if node.get("nodeId")
        }
        children: dict[Any, list[dict[str, Any]]] = {}
        for node in nodes:
            parent_id = node.get("parentId")
            if parent_id:
                children.setdefault(parent_id, []).append(node)
            # getFlattenedDocument 把闭合 Shadow DOM 挂在宿主元素的
            # shadowRoots 字段里，而不是给文档片段补 parentId。
            for shadow_root in node.get("shadowRoots") or []:
                shadow_id = shadow_root.get("nodeId")
                if shadow_id:
                    parent_links[shadow_id] = node.get("nodeId")
                    children.setdefault(node.get("nodeId"), []).append(
                        by_id.get(shadow_id, shadow_root)
                    )

        def attrs(node: Mapping[str, Any]) -> dict[str, str]:
            values = list(node.get("attributes") or [])
            return {
                str(values[index]): str(values[index + 1])
                for index in range(0, len(values) - 1, 2)
            }

        def ancestor_chain(node: Mapping[str, Any]) -> list[dict[str, Any]]:
            chain: list[dict[str, Any]] = []
            current = node
            seen: set[Any] = set()
            while current and current.get("nodeId") not in seen:
                seen.add(current.get("nodeId"))
                chain.append(current)
                current = by_id.get(parent_links.get(current.get("nodeId")))
            return chain

        def descendant_text(node_id: Any, limit: int = 160) -> str:
            values: list[str] = []
            pending = list(children.get(node_id, []))
            while pending and len(" ".join(values)) < limit:
                current = pending.pop(0)
                if current.get("nodeName") == "#text":
                    values.append(str(current.get("nodeValue") or ""))
                pending.extend(children.get(current.get("nodeId"), []))
            return " ".join(values).replace("\u00a0", " ").strip()[:limit]

        hosts = [
            node for node in nodes
            if str(node.get("nodeName") or "").upper() == "XHS-PUBLISH-BTN"
        ]
        candidates: list[tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]] = []
        expected_labels = {str(label or "发布").strip() for label in labels} | {"发布"}
        for host in hosts:
            host_attrs = attrs(host)
            host_children = list(children.get(host.get("nodeId"), []))
            pending = host_children[:]
            while pending:
                node = pending.pop(0)
                pending.extend(children.get(node.get("nodeId"), []))
                if str(node.get("nodeName") or "").upper() != "BUTTON":
                    continue
                node_attrs = attrs(node)
                class_name = node_attrs.get("class", "")
                text = descendant_text(node.get("nodeId"))
                is_red_publish = "bg-red" in class_name
                is_expected_label = text in expected_labels
                if is_red_publish or is_expected_label:
                    candidates.append((node, node_attrs, ancestor_chain(node)))

        if not candidates:
            return {
                "clicked": False,
                "reason": "xhs_publish_button_not_in_flattened_dom",
                "candidateCount": 0,
                "hostCount": len(hosts),
                "method": "live_dom_flattened_box_model",
            }

        enabled: list[tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]] = []
        for node, node_attrs, chain in candidates:
            disabled = (
                node_attrs.get("disabled") is not None
                or node_attrs.get("aria-disabled") == "true"
                or node_attrs.get("aria-busy") == "true"
            )
            if not disabled:
                enabled.append((node, node_attrs, chain))
        if not enabled:
            return {
                "clicked": False,
                "reason": "matched_but_disabled_or_unavailable",
                "candidateCount": len(candidates),
                "hostCount": len(hosts),
                "method": "live_dom_flattened_box_model",
            }

        node, node_attrs, chain = enabled[0]
        node_id = node.get("nodeId")
        backend_node_id = node.get("backendNodeId")
        try:
            if node_id:
                await session.cmd(
                    "DOM.scrollIntoViewIfNeeded", {"nodeId": node_id},
                    session_id=session_id,
                    timeout=PUBLISH_TARGET_PROBE_TIMEOUT_SECONDS,
                )
        except Exception:
            # 某些内核不支持对闭合组件内部节点滚动；盒模型仍可能已经
            # 在可视区域内，后续 getBoxModel 继续尝试。
            pass
        box_params: dict[str, Any] = {}
        if node_id:
            box_params["nodeId"] = node_id
        elif backend_node_id:
            box_params["backendNodeId"] = backend_node_id
        else:
            return {
                "clicked": False,
                "reason": "xhs_publish_backend_node_missing",
                "candidateCount": len(candidates),
                "method": "live_dom_flattened_box_model",
            }
        box = await session.cmd(
            "DOM.getBoxModel", box_params, session_id=session_id,
            timeout=PUBLISH_TARGET_PROBE_TIMEOUT_SECONDS,
        )
        model = (box or {}).get("model") or {}
        quad = model.get("border") or model.get("content") or []
        if len(quad) < 8:
            return {
                "clicked": False,
                "reason": "invalid_box_model_rect",
                "candidateCount": len(candidates),
                "method": "live_dom_flattened_box_model",
            }
        xs = [float(value) for value in quad[0::2]]
        ys = [float(value) for value in quad[1::2]]
        text = descendant_text(node_id)
        label = text or node_attrs.get("aria-label") or "发布"
        return {
            "clicked": True,
            "label": label[:80],
            "matchedBy": " ".join([
                str(node.get("nodeName") or ""),
                node_attrs.get("class", ""),
                node_attrs.get("aria-label", ""),
            ]).strip(),
            "x": sum(xs) / len(xs),
            "y": sum(ys) / len(ys),
            "candidateCount": len(candidates),
            "hostCount": len(hosts),
            "method": "live_dom_flattened_box_model",
        }
    except Exception as exc:
        return {
            "clicked": False,
            "reason": "xhs_publish_dom_probe_error",
            "message": str(exc)[:300],
            "method": "live_dom_flattened_box_model",
        }


async def _wait_publish_success(session, session_id: str, platform: str,
                                baseline_matches: set[str], *, timeout: float = 3.0) -> dict[str, Any]:
    """点击后短暂轮询平台成功提示，避免仅凭鼠标事件误记为已发布。"""
    deadline = asyncio.get_running_loop().time() + max(0.5, float(timeout))
    latest: dict[str, Any] = {"success": False, "matches": []}
    while asyncio.get_running_loop().time() < deadline:
        value = await session.eval(_publish_success_probe_script(platform), session_id)
        if isinstance(value, dict):
            latest = value
            matches = set(value.get("matches") or [])
            if bool(value.get("success")) and (not baseline_matches or matches - baseline_matches):
                return {**value, "success": True}
        await asyncio.sleep(0.5)
    return latest


def _publish_success_probe_script(platform: str) -> str:
    """读取当前页面可见的成功提示，不点击、不改变页面状态。"""
    tokens = {
        "douyin": ["发布成功", "作品发布成功", "提交成功", "审核中"],
        "xhs": ["发布成功", "笔记发布成功", "提交成功", "审核中"],
        "bilibili": ["投稿成功", "发布成功", "提交成功", "审核中"],
        "weibo": ["发布成功", "发送成功", "微博发布成功", "提交成功"],
    }.get(str(platform or "").strip().lower(), ["发布成功", "提交成功"])
    payload = json.dumps(tokens, ensure_ascii=False)
    return f"""
(() => {{
  const tokens = {payload};
  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const visible = (el) => {{
    if (!el) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && r.right > 0 && r.bottom > 0 &&
      r.left < innerWidth && r.top < innerHeight && s.display !== 'none' &&
      s.visibility !== 'hidden' && Number(s.opacity || 1) > 0;
  }};
  const matches = Array.from(document.querySelectorAll('body *'))
    .filter(visible)
    .map((el) => clean(el.innerText || el.textContent || ''))
    .filter((text) => text && text.length <= 120 && tokens.some((token) => text.includes(token)))
    .filter((text, index, all) => all.indexOf(text) === index)
    .slice(0, 8);
  return {{success: matches.length > 0, matches, url: location.href}};
}})()
"""


async def _prepare_editor(session, session_id: str, platform: str,
                          content_type: str, assets: list[Mapping[str, Any]],
                          *, settle_seconds: float) -> dict[str, Any]:
    """仅做内容类型选择和素材预填；不触碰发布/发送控件。"""
    probe = await session.eval(_editor_probe_script(), session_id)
    probe = probe if isinstance(probe, dict) else {}
    asset_items = [item for item in assets
                   if isinstance(item, Mapping) and str(item.get("path") or "").strip()]
    paths = [str(item.get("path") or "").strip() for item in asset_items]
    asset_types = [
        _asset_type_from_path(str(item.get("path") or ""), str(item.get("asset_type") or ""))
        for item in asset_items
    ]
    labels: list[str] = []
    if platform in {"douyin", "xhs"} and paths:
        kinds = set(asset_types)
        if len(kinds) > 1:
            raise PublishingBrowserError("图片和视频不能混合发布，请分开创建发布内容")
        is_image = next(iter(kinds), "image") == "image"
        if platform == "douyin":
            labels = ["发布图文"] if is_image else ["发布视频"]
        else:
            labels = ["上传图文"] if is_image else ["上传视频"]
    elif platform == "bilibili" and content_type in {"text", "article", "dynamic_text"}:
        labels = ["新的创作"]
    if not paths and not labels:
        return {"mode_changed": False, "uploaded": 0, "editor_before": probe}
    mode_click = {"clicked": False, "label": ""}
    mode_probe_before: dict[str, Any] = {}
    mode_probe_after: dict[str, Any] = {}
    mode_fallback: dict[str, Any] = {}
    mode_confirmed: bool | None = None
    expected_mode_type = ""
    if platform in {"douyin", "xhs"} and labels:
        expected_mode_type = "image" if next(iter(set(asset_types)), "image") == "image" else "video"
        mode_probe_before = await session.eval(
            _mode_probe_script(labels[0], expected_mode_type), session_id
        )
        mode_probe_before = mode_probe_before if isinstance(mode_probe_before, dict) else {}
    if labels:
        mode_click = await _safe_mode_click(session, session_id, labels)
        await asyncio.sleep(min(1.5, max(0.5, settle_seconds)))
        if platform in {"douyin", "xhs"}:
            mode_probe_after = await _wait_for_mode_probe(
                session, session_id, labels[0], expected_mode_type, timeout=8.0,
            )
            mode_confirmed = _mode_probe_confirmed(mode_probe_after, expected_mode_type)
            # 仅当坐标事件发出但模式状态仍明确不匹配时触发备用 DOM 点击。
            # 若页面没有可读的 active/accept 信号，则不武断判定失败，继续走原流程。
            if mode_click.get("clicked") and mode_confirmed is False:
                mode_fallback = await session.eval(
                    _mode_dom_click_script(labels), session_id
                )
                mode_fallback = mode_fallback if isinstance(mode_fallback, dict) else {}
                await asyncio.sleep(min(1.5, max(0.5, settle_seconds)))
                mode_probe_after = await _wait_for_mode_probe(
                    session, session_id, labels[0], expected_mode_type, timeout=8.0,
                )
                mode_confirmed = _mode_probe_confirmed(mode_probe_after, expected_mode_type)
        elif platform == "bilibili":
            await _wait_for_probe(session, session_id, require_editor=True, timeout=8.0)

    uploaded = 0
    file_events_dispatched = 0
    # 模式明确不匹配时直接返回诊断结果，不能先把图片塞进仍处于视频
    # 模式的文件节点，否则页面状态会被污染，下一次测试也会继续误导。
    if platform in {"douyin", "xhs"} and mode_confirmed is not False:
        uploaded = await _set_file_inputs(session, session_id, paths)
        if uploaded:
            dispatched = await session.eval(_dispatch_file_events_script(), session_id)
            if isinstance(dispatched, dict):
                file_events_dispatched = int(dispatched.get("dispatched") or 0)
            await _wait_for_probe(session, session_id, require_editor=True, timeout=12.0)
    after = await session.eval(_editor_probe_script(), session_id)
    return {
        "mode_changed": bool(labels),
        "mode_labels": labels,
        "mode_click": mode_click,
        "mode_fallback": mode_fallback,
        "mode_confirmed": mode_confirmed,
        "mode_probe_before": mode_probe_before,
        "mode_probe_after": mode_probe_after,
        "asset_types": asset_types,
        "uploaded": uploaded,
        "file_events_dispatched": file_events_dispatched,
        "editor_before": probe,
        "editor_after": after if isinstance(after, dict) else {},
    }


async def _wait_for_document_ready(session, session_id: str, *, timeout: float) -> dict[str, Any]:
    timeout = max(0.5, float(timeout))
    deadline = asyncio.get_running_loop().time() + timeout
    probe_timeout = min(PUBLISH_TARGET_PROBE_TIMEOUT_SECONDS, timeout)
    latest: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        try:
            value = await session.eval(
                "({readyState: document.readyState, url: location.href})", session_id,
                timeout=probe_timeout,
            )
        except asyncio.TimeoutError:
            # 页面渲染器无响应时不要再使用 CDP 默认的 35 秒超时；到总时限
            # 后由上层报告具体阶段，方便区分页面卡住和页面内容错误。
            await asyncio.sleep(0.25)
            continue
        latest = value if isinstance(value, dict) else latest
        if latest.get("readyState") == "complete":
            break
        await asyncio.sleep(0.5)
    return latest


async def _wait_for_probe(session, session_id: str, *, require_file: bool = False,
                          require_editor: bool = False, timeout: float) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + max(0.5, float(timeout))
    latest: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        value = await session.eval(_editor_probe_script(), session_id)
        latest = value if isinstance(value, dict) else latest
        has_editor = bool(latest.get("title") or latest.get("body"))
        has_file = bool(latest.get("file_inputs"))
        if (require_editor and has_editor) or (require_file and has_file) or (
            not require_editor and not require_file and (has_editor or has_file)
        ):
            break
        await asyncio.sleep(0.5)
    return latest


def _mode_probe_confirmed(probe: Mapping[str, Any], expected_type: str) -> bool | None:
    """把模式探测结果分成确认、明确不匹配和无法判断三种状态。"""
    if not isinstance(probe, Mapping):
        return None
    if bool(probe.get("confirmed")):
        return True
    # 页面上存在“发布图文”文字、文件节点或普通候选项，并不代表仍停留在
    # 视频模式。很多前端会同时挂载两个入口，且 active/accept 属性在异步
    # 渲染完成前为空；旧逻辑把这些普通候选误当成“明确不匹配”，导致图片
    # 还没上传就被主动中止。只有探测到明确的相反类型输入时才拦截。
    if bool(probe.get("oppositeTypeSignal")):
        return False
    return None


async def _wait_for_mode_probe(session, session_id: str, label: str,
                               expected_type: str, *, timeout: float) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + max(0.5, float(timeout))
    latest: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        value = await session.eval(
            _mode_probe_script(label, expected_type), session_id
        )
        latest = value if isinstance(value, dict) else latest
        status = _mode_probe_confirmed(latest, expected_type)
        if status is True:
            break
        await asyncio.sleep(0.5)
    return latest


async def _set_file_inputs(session, session_id: str, paths: list[str]) -> int:
    """通过 CDP 文件节点设置素材；只接收本地已存在路径，不点击上传/发布。"""
    import os

    existing = [path for path in paths if os.path.isfile(path)]
    if not existing:
        return 0
    document = await session.cmd(
        "DOM.getDocument", {"depth": -1, "pierce": True}, session_id=session_id
    )
    root_id = ((document or {}).get("root") or {}).get("nodeId")
    if not root_id:
        return 0
    found = await session.cmd(
        "DOM.querySelectorAll",
        {"nodeId": root_id, "selector": "input[type=file]"},
        session_id=session_id,
    )
    node_ids = list((found or {}).get("nodeIds") or [])
    if not node_ids:
        return 0
    file_info = await session.eval(
        "Array.from(document.querySelectorAll('input[type=file]')).map(e => "
        "({accept: String(e.accept || '').toLowerCase()}))",
        session_id,
    )
    extensions = {os.path.splitext(path)[1].lower() for path in existing}
    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".heif"}
    want_image = bool(extensions) and extensions.issubset(image_exts)
    preferred = "image" if want_image else "video"
    selected_index = next(
        (index for index, info in enumerate(file_info or [])
         if preferred in str((info or {}).get("accept") or "")),
        0,
    )
    selected_index = min(selected_index, len(node_ids) - 1)
    await session.cmd(
        "DOM.setFileInputFiles",
        {"nodeId": node_ids[selected_index], "files": existing},
        session_id=session_id,
    )
    return len(existing)


def _dispatch_file_events_script() -> str:
    return """
(() => {
  const roots = [];
  const addRoot = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    let elements = [];
    try { elements = Array.from(root.querySelectorAll('*')); } catch (error) { return; }
    for (const el of elements) {
      if (el.shadowRoot) addRoot(el.shadowRoot);
      if (el.tagName === 'IFRAME') {
        try { if (el.contentDocument) addRoot(el.contentDocument); } catch (error) {}
      }
    }
  };
  addRoot(document);
  const inputs = roots.flatMap((root) => Array.from(root.querySelectorAll('input[type=file]')))
    .filter((el, index, all) => all.indexOf(el) === index);
  inputs.filter((el) => el.files && el.files.length).forEach((el) => {
    el.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
    el.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
  });
  return {dispatched: inputs.filter((el) => el.files && el.files.length).length};
})()
"""


def _editor_probe_script() -> str:
    return """
(() => {
  const rendered = (el) => {
    if (!el) return false;
    const view = el.ownerDocument.defaultView || window;
    const s = view.getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' &&
      Number(s.opacity || 1) > 0 && r.width > 4 && r.height > 4;
  };
  const roots = [];
  const addRoot = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    let elements = [];
    try { elements = Array.from(root.querySelectorAll('*')); } catch (error) { return; }
    for (const el of elements) {
      if (el.shadowRoot) addRoot(el.shadowRoot);
    }
  };
  addRoot(document);
  for (const frame of Array.from(document.querySelectorAll('iframe'))) {
    try { if (frame.contentDocument) addRoot(frame.contentDocument); } catch (error) {}
  }
  const fields = roots.flatMap((root) => Array.from(root.querySelectorAll(
    'input:not([type=file]), textarea, [contenteditable="true"], [role="textbox"], [data-placeholder]'
  ))).filter(rendered).filter((el) => {
    const editable = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' ||
      el.isContentEditable || el.getAttribute('contenteditable') === 'true' ||
      el.getAttribute('role') === 'textbox' || el.hasAttribute('data-placeholder');
    if (!editable) return false;
    const t = [el.placeholder, el.getAttribute('aria-label'),
      el.getAttribute('data-placeholder'), el.name, el.id, el.className]
      .join(' ').toLowerCase();
    return !t.includes('搜索') && !t.includes('search');
  });
  const describe = (el) => {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName, type: el.type || '',
      placeholder: el.getAttribute('placeholder') || '',
      ariaLabel: el.getAttribute('aria-label') || '',
      dataPlaceholder: el.getAttribute('data-placeholder') || '',
      role: el.getAttribute('role') || '',
      className: String(el.className || '').slice(0, 120),
      x: Math.round(r.left), y: Math.round(r.top),
      width: Math.round(r.width), height: Math.round(r.height)
    };
  };
  const titleFields = fields.filter((el) => /标题|title|作品标题/.test(
    [el.placeholder, el.getAttribute('aria-label'), el.getAttribute('data-placeholder'),
      el.name, el.id, el.className].join(' ').toLowerCase()
  ));
  const bodyFields = fields.filter((el) => !titleFields.includes(el) && (
    el.tagName === 'TEXTAREA' || el.isContentEditable ||
    el.getAttribute('role') === 'textbox' || el.hasAttribute('data-placeholder')
  ));
  return {
    title: titleFields.length,
    body: bodyFields.length,
    file_inputs: roots.reduce((total, root) => total + root.querySelectorAll('input[type=file]').length, 0),
    title_candidates: titleFields.slice(0, 8).map(describe),
    body_candidates: bodyFields.slice(0, 8).map(describe)
  };
})()
"""


async def _safe_mode_click(session, session_id: str, labels: list[str]) -> dict[str, Any]:
    point = await session.eval(_safe_mode_click_script(labels), session_id)
    if not isinstance(point, dict) or not point.get("clicked"):
        return point if isinstance(point, dict) else {"clicked": False, "label": ""}
    x, y = float(point.get("x") or 0), float(point.get("y") or 0)
    if x <= 0 or y <= 0:
        return {"clicked": False, "label": point.get("label", "")}
    for event_type in ("mousePressed", "mouseReleased"):
        await session.cmd(
            "Input.dispatchMouseEvent",
            {"type": event_type, "x": x, "y": y, "button": "left", "clickCount": 1},
            session_id=session_id,
        )
    return {"clicked": True, "label": point.get("label", ""), "x": x, "y": y,
            "method": "fresh_box_model_click"}


def _safe_mode_click_script(labels: list[str], *, final_submit: bool = False) -> str:
    payload = json.dumps([str(label) for label in labels], ensure_ascii=False)
    final_submit_literal = "true" if final_submit else "false"
    return f"""
(() => {{
  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const labels = {payload}.map(clean);
  const finalSubmitOnly = {final_submit_literal};
  const rendered = (el) => {{
    const view = el.ownerDocument.defaultView || window;
    const r = el.getBoundingClientRect(), s = view.getComputedStyle(el);
    return r.width > 4 && r.height > 4 && r.right > 0 && r.bottom > 0 &&
      s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) > 0;
  }};
  const inViewport = (el) => {{
    const view = el.ownerDocument.defaultView || window;
    const r = el.getBoundingClientRect();
    return r.right > 0 && r.bottom > 0 && r.left < view.innerWidth && r.top < view.innerHeight;
  }};
  const roots = [{{root: document, offsetX: 0, offsetY: 0}}, ...Array.from(document.querySelectorAll('iframe')).flatMap((frame) => {{
    try {{
      const r = frame.getBoundingClientRect();
      return frame.contentDocument ? [{{root: frame.contentDocument, offsetX: r.left, offsetY: r.top}}] : [];
    }} catch (error) {{ return []; }}
  }})];
  const candidates = [];
  // 最终发布只允许真正的按钮控件参与候选，不能把顶部导航链接或普通文本
  // “发布笔记”误判成提交动作。模式切换仍使用原来的宽松候选范围。
  const selector = finalSubmitOnly
    ? 'button,[role="button"],input[type="submit"],input[type="button"]'
    : 'button,[role="button"],input[type="submit"],input[value],a,div,span,[aria-label],[title]';
  const scan = (root, offsetX, offsetY) => {{
    for (const el of Array.from(root.querySelectorAll(selector))) {{
      candidates.push({{el, offsetX, offsetY}});
      if (el.shadowRoot) scan(el.shadowRoot, offsetX, offsetY);
    }}
  }};
  for (const root of roots) scan(root.root, root.offsetX, root.offsetY);
  const textOf = (el) => clean([
    el.innerText, el.textContent, el.value,
    el.getAttribute('aria-label'), el.getAttribute('title'),
  ].filter(Boolean).join(' '));
  const ownTextOf = (el) => clean(Array.from(el.childNodes || [])
    .filter((node) => node.nodeType === 3)
    .map((node) => node.nodeValue || '').join(' '));
  const directAttrTextOf = (el) => clean([
    el.getAttribute('aria-label'), el.getAttribute('title'),
    el.getAttribute('data-e2e'), el.getAttribute('data-testid'),
  ].filter(Boolean).join(' '));
  const attrsOf = (el) => clean([
    el.tagName, el.id, el.className, el.getAttribute('data-e2e'),
    el.getAttribute('data-testid'), el.getAttribute('aria-label'), el.getAttribute('title'),
  ].filter(Boolean).join(' '));
  const isInteractive = (el) => el.tagName === 'BUTTON' ||
    ['button', 'tab', 'radio', 'link'].includes(el.getAttribute('role')) ||
    el.hasAttribute('tabindex');
  const isNavigationLike = (el) => {{
    if (!finalSubmitOnly) return false;
    if (el.closest && el.closest('header,nav,[role="navigation"],[role="tablist"],aside')) return true;
    let current = el.parentElement;
    for (let depth = 0; current && depth < 4; depth += 1, current = current.parentElement) {{
      const attrs = clean([
        current.tagName, current.id, current.className,
        current.getAttribute('data-e2e'), current.getAttribute('data-testid')
      ].filter(Boolean).join(' ')).toLowerCase();
      if (/(^|[-_ ])(header|nav|menu|sidebar|topbar|tab)([-_ ]|$)/.test(attrs)) return true;
    }}
    return false;
  }};
  const directMatchesLabel = (el) => {{
    const ownText = ownTextOf(el), attrText = directAttrTextOf(el);
    return labels.some((label) => ownText === label || attrText === label ||
      (isInteractive(el) && textOf(el) === label));
  }};
  const hasSpecificChild = (el) => Array.from(el.querySelectorAll('*'))
    .some((child) => rendered(child) && directMatchesLabel(child));
  const matchesLabel = (el) => {{
    return directMatchesLabel(el) && (!hasSpecificChild(el) || isInteractive(el));
  }};
  const matched = candidates.filter((item) => rendered(item.el) && matchesLabel(item.el));
  const navigationExcluded = matched.filter((item) => isNavigationLike(item.el)).length;
  const usableMatched = finalSubmitOnly
    ? matched.filter((item) => !isNavigationLike(item.el))
    : matched;
  const enabled = usableMatched.filter((item) => !item.el.disabled &&
    item.el.getAttribute('aria-disabled') !== 'true');
  const ranked = enabled.map((item) => {{
    const el = item.el;
    const text = textOf(el);
    const attrs = attrsOf(el);
    const r = el.getBoundingClientRect();
    const exact = labels.some((label) => text === label);
    const semantic = /publish|submit|post|send|发布|投稿|发送/i.test(attrs);
    const bottom = r.top >= (el.ownerDocument.defaultView || window).innerHeight * 0.55;
    const navigation = isNavigationLike(el);
    const score = (exact ? 100 : 65) + (semantic ? 45 : 0) +
      (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button' ? 25 : 0) +
      (bottom ? (finalSubmitOnly ? 55 : 25) : 0) +
      (finalSubmitOnly && navigation ? -180 : 0);
    return {{...item, text, attrs, r, score, navigation}};
  }}).sort((left, right) => right.score - left.score || right.r.top - left.r.top);
  const brief = (items) => items.slice(0, 8).map((item) => {{
    const r = item.el.getBoundingClientRect();
    return {{text: textOf(item.el).slice(0, 80), tag: item.el.tagName,
      disabled: !!item.el.disabled || item.el.getAttribute('aria-disabled') === 'true',
      inViewport: inViewport(item.el), x: Math.round(r.left), y: Math.round(r.top)}};
  }});
  if (!ranked.length) return {{clicked: false, label: '', method: 'fresh_box_model_click',
    reason: matched.length ? 'matched_but_disabled_or_unavailable' : 'no_publish_label_match',
    candidateCount: matched.length, navigationExcluded, candidates: brief(matched)}};
  const selected = ranked[0];
  try {{ selected.el.scrollIntoView({{block: 'center', inline: 'center'}}); }} catch (_) {{}}
  const r = selected.el.getBoundingClientRect();
  if (r.width <= 4 || r.height <= 4) return {{clicked: false, label: selected.text,
    method: 'fresh_box_model_click', reason: 'invalid_box_model_rect', navigationExcluded,
    candidates: brief(ranked)}};
  return {{clicked: true, label: selected.text, matchedBy: selected.attrs,
    wasInViewport: inViewport(selected.el),
    x: selected.offsetX + r.left + r.width / 2, y: selected.offsetY + r.top + r.height / 2,
    candidateCount: matched.length, navigationExcluded, method: 'fresh_box_model_click'}};
}})()
"""


def _mode_probe_script(label: str, expected_type: str) -> str:
    """检查内容模式是否真的切换，而不是只确认鼠标事件发出。"""
    label_json = json.dumps(str(label or ""), ensure_ascii=False)
    expected_json = json.dumps(str(expected_type or ""), ensure_ascii=False)
    return f"""
(() => {{
  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const label = clean({label_json});
  const expected = clean({expected_json}).toLowerCase();
  const roots = [];
  const addRoot = (root) => {{
    if (!root || roots.includes(root)) return;
    roots.push(root);
    let elements = [];
    try {{ elements = Array.from(root.querySelectorAll('*')); }} catch (error) {{ return; }}
    for (const el of elements) {{
      if (el.shadowRoot) addRoot(el.shadowRoot);
      if (el.tagName === 'IFRAME') {{
        try {{ if (el.contentDocument) addRoot(el.contentDocument); }} catch (error) {{}}
      }}
    }}
  }};
  addRoot(document);
  const rendered = (el) => {{
    if (!el) return false;
    const view = el.ownerDocument.defaultView || window;
    const r = el.getBoundingClientRect(), s = view.getComputedStyle(el);
    return r.width > 4 && r.height > 4 && r.right > 0 && r.bottom > 0 &&
      s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) > 0;
  }};
  const textOf = (el) => clean([
    el.innerText, el.textContent, el.value, el.getAttribute('aria-label'),
    el.getAttribute('title')
  ].filter(Boolean).join(' '));
  const attrsOf = (el) => clean([
    el.tagName, el.id, el.className, el.getAttribute('data-e2e'),
    el.getAttribute('data-testid'), el.getAttribute('aria-label'), el.getAttribute('title')
  ].filter(Boolean).join(' '));
  const matches = (el) => {{
    if (!rendered(el)) return false;
    const text = textOf(el), attrs = attrsOf(el);
    return text === label || text.endsWith(label) || text.includes(label) ||
      attrs.includes(label);
  }};
  const active = (el) => {{
    let current = el;
    for (let depth = 0; current && depth < 3; depth += 1, current = current.parentElement) {{
      const cls = String(current.className || '').toLowerCase();
      if (current.getAttribute('aria-selected') === 'true' ||
          current.getAttribute('aria-checked') === 'true' ||
          current.getAttribute('data-active') === 'true' ||
          /(^|[-_ ])(active|selected|current|checked|on)([-_ ]|$)/.test(cls)) return true;
    }}
    return false;
  }};
  const candidates = roots.flatMap((root) => Array.from(root.querySelectorAll(
    'button,[role="button"],[role="tab"],input[type="radio"],a,div,span'
  ))).filter((el, index, all) => all.indexOf(el) === index).filter(matches);
  const accepts = roots.flatMap((root) => Array.from(root.querySelectorAll('input[type=file]')))
    .filter((el, index, all) => all.indexOf(el) === index)
    .map((el) => clean(el.accept).toLowerCase()).filter(Boolean);
  // 小红书的视频 input 使用扩展名白名单（如 .mp4,.mov），不包含
  // "video" 字样；按 MIME 和常见后缀同时识别，避免已经在目标上传页时
  // 因找不到“上传视频”入口而被误判为模式未切换。
  const imageInputs = accepts.filter((value) =>
    value.includes('image') || /\.(png|jpe?g|webp|gif|bmp|tiff?|avif|heic|heif)/i.test(value)
  ).length;
  const videoInputs = accepts.filter((value) =>
    value.includes('video') || /\.(mp4|mov|mkv|avi|webm|flv|wmv|m4v|mpeg|mpg|ts|rmvb?)/i.test(value)
  ).length;
  const activeCount = candidates.filter(active).length;
  const desiredInputCount = expected === 'image' ? imageInputs :
    expected === 'video' ? videoInputs : 0;
  const oppositeInputCount = expected === 'image' ? videoInputs :
    expected === 'video' ? imageInputs : 0;
  const typeSignal = accepts.length > 0 || activeCount > 0;
  const typeMatched = desiredInputCount > 0;
  // 只有“相反类型有明确文件输入、目标类型没有文件输入”才是可阻断的
  // 证据。accept="image/*,video/*" 这种通用输入不应被判为错误模式。
  const oppositeTypeSignal = oppositeInputCount > 0 && desiredInputCount === 0;
  return {{
    label, expected, candidateCount: candidates.length, activeCount,
    active: activeCount > 0, accepts, imageInputs, videoInputs,
    desiredInputCount, oppositeInputCount, typeSignal, oppositeTypeSignal,
    confirmed: activeCount > 0 || typeMatched,
    candidates: candidates.slice(0, 8).map((el) => ({{
      text: textOf(el).slice(0, 80), tag: el.tagName,
      className: String(el.className || '').slice(0, 120), active: active(el)
    }}))
  }};
}})()
"""


def _mode_dom_click_script(labels: list[str]) -> str:
    """模式坐标点击未生效时的第二次 DOM 触发，只用于模式入口。"""
    payload = json.dumps([str(label) for label in labels], ensure_ascii=False)
    return f"""
(() => {{
  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const labels = {payload}.map(clean);
  const roots = [];
  const addRoot = (root) => {{
    if (!root || roots.includes(root)) return;
    roots.push(root);
    let elements = [];
    try {{ elements = Array.from(root.querySelectorAll('*')); }} catch (error) {{ return; }}
    for (const el of elements) {{
      if (el.shadowRoot) addRoot(el.shadowRoot);
      if (el.tagName === 'IFRAME') {{
        try {{ if (el.contentDocument) addRoot(el.contentDocument); }} catch (error) {{}}
      }}
    }}
  }};
  addRoot(document);
  const rendered = (el) => {{
    if (!el) return false;
    const view = el.ownerDocument.defaultView || window;
    const r = el.getBoundingClientRect(), s = view.getComputedStyle(el);
    return r.width > 4 && r.height > 4 && r.right > 0 && r.bottom > 0 &&
      s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) > 0;
  }};
  const textOf = (el) => clean([
    el.innerText, el.textContent, el.value, el.getAttribute('aria-label'), el.getAttribute('title')
  ].filter(Boolean).join(' '));
  const ownTextOf = (el) => clean(Array.from(el.childNodes || [])
    .filter((node) => node.nodeType === 3)
    .map((node) => node.nodeValue || '').join(' '));
  const directAttrTextOf = (el) => clean([
    el.getAttribute('aria-label'), el.getAttribute('title'),
    el.getAttribute('data-e2e'), el.getAttribute('data-testid'),
  ].filter(Boolean).join(' '));
  const attrsOf = (el) => clean([
    el.tagName, el.id, el.className, el.getAttribute('data-e2e'),
    el.getAttribute('data-testid'), el.getAttribute('aria-label'), el.getAttribute('title')
  ].filter(Boolean).join(' '));
  const isInteractive = (el) => el.tagName === 'BUTTON' ||
    ['button', 'tab', 'radio', 'link'].includes(el.getAttribute('role')) ||
    el.hasAttribute('tabindex');
  const directMatchesLabel = (el) => {{
    const ownText = ownTextOf(el), attrText = directAttrTextOf(el);
    return labels.some((label) => ownText === label || attrText === label ||
      (isInteractive(el) && textOf(el) === label));
  }};
  const hasSpecificChild = (el) => Array.from(el.querySelectorAll('*'))
    .some((child) => rendered(child) && directMatchesLabel(child));
  const candidates = roots.flatMap((root) => Array.from(root.querySelectorAll(
    'button,[role="button"],[role="tab"],input[type="radio"],a,div,span'
  ))).filter((el, index, all) => all.indexOf(el) === index).filter((el) => {{
    if (!rendered(el)) return false;
    return directMatchesLabel(el) && (!hasSpecificChild(el) || isInteractive(el));
  }});
  const ranked = candidates.map((el) => {{
    const text = textOf(el), attrs = attrsOf(el);
    const exact = labels.some((label) => text === label);
    const semantic = /publish|upload|post|发布|上传|图文|视频/i.test(attrs);
    const button = el.tagName === 'BUTTON' || /button|tab/.test(el.getAttribute('role') || '');
    return {{el, text, attrs, score: (exact ? 100 : 60) + (semantic ? 35 : 0) + (button ? 25 : 0)}};
  }}).sort((left, right) => right.score - left.score);
  if (!ranked.length) return {{clicked: false, method: 'dom_mode_click', reason: 'mode_label_not_found'}};
  const selected = ranked[0];
  try {{
    if (typeof selected.el.click !== 'function') return {{clicked: false, method: 'dom_mode_click', reason: 'element_not_clickable'}};
    selected.el.click();
  }} catch (error) {{
    return {{clicked: false, method: 'dom_mode_click', reason: String(error && error.message || error)}};
  }}
  return {{clicked: true, label: selected.text, matchedBy: selected.attrs, method: 'dom_mode_click'}};
}})()
"""


def _fill_script(payload_json: str) -> str:
    """生成只填充编辑器的页面脚本，不包含任何提交按钮选择器。"""
    return f"""
(() => {{
  const data = {payload_json};
  const platform = String(data.platform || '');
  const rendered = (el) => {{
    if (!el) return false;
    const view = el.ownerDocument.defaultView || window;
    const s = view.getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' &&
      Number(s.opacity || 1) > 0 && r.width > 4 && r.height > 4;
  }};
   const clean = (v) => String(v == null ? '' : v).replace(/\\s+/g, ' ').trim();
   // 回读编辑器时保留换行，只统一浏览器可能产生的 CRLF、不可见空格和行尾空格。
   // 发布前必须核对实际输入内容，而不是只做前缀或片段包含判断。
   const normalizeContent = (v) => String(v == null ? '' : v)
     .replace(/\\u00a0/g, ' ').replace(/\\r\\n?/g, '\\n')
     .split('\\n').map((line) => line.replace(/[ \\t]+/g, ' ').replace(/[ \\t]+$/, ''))
     .join('\\n').trim();
  const fieldText = (el) => clean([
    el.getAttribute('placeholder'), el.getAttribute('aria-label'),
    el.getAttribute('data-placeholder'), el.getAttribute('role'),
    el.name, el.id, el.className, el.getAttribute('data-e2e'),
    el.getAttribute('data-testid')
  ].join(' '));
  const isSearchLike = (el) => {{
    const text = fieldText(el).toLowerCase();
    return el.type === 'search' || text.includes('搜索') || text.includes('search');
  }};
  const unique = (items) => Array.from(new Set(items.filter(Boolean)));
  const roots = [];
  const addRoot = (root) => {{
    if (!root || roots.includes(root)) return;
    roots.push(root);
    let elements = [];
    try {{ elements = Array.from(root.querySelectorAll('*')); }} catch (error) {{ return; }}
    for (const el of elements) if (el.shadowRoot) addRoot(el.shadowRoot);
  }};
  addRoot(document);
  for (const frame of Array.from(document.querySelectorAll('iframe'))) {{
    try {{ if (frame.contentDocument) addRoot(frame.contentDocument); }} catch (error) {{}}
  }};
  const query = (selectors) => unique(roots.flatMap((root) => selectors.flatMap((selector) =>
    Array.from(root.querySelectorAll(selector))
  ))).filter(rendered).filter((el) => !isSearchLike(el));
  const isEditable = (el) => el && (
    el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable ||
    el.getAttribute('contenteditable') === 'true' ||
    el.getAttribute('role') === 'textbox' || el.hasAttribute('data-placeholder')
  );
  const titleSelectors = {{
    douyin: ['input[placeholder*="标题"]', 'input[placeholder*="作品标题"]',
      'input[placeholder*="添加标题"]', 'input[aria-label*="标题"]',
      'input[placeholder*="作品"]', 'input[aria-label*="作品"]',
      '[data-placeholder*="标题"]', 'input[name*="title"]',
      '[data-e2e*="title"]', '[data-testid*="title"]', 'input[class*="title"]'],
    xhs: ['input[placeholder*="标题"]', 'input[placeholder*="填写标题"]'],
    bilibili: ['input[placeholder*="标题"]', 'input[placeholder*="视频标题"]', 'textarea[placeholder*="标题"]'],
    weibo: []
  }};
  const bodySelectors = {{
    douyin: ['textarea[placeholder*="描述"]', 'textarea[placeholder*="正文"]',
      'textarea[placeholder*="文案"]', '[data-placeholder*="描述"]',
      '[aria-label*="描述"]', '[contenteditable="true"]', '[role="textbox"]'],
    xhs: ['textarea[placeholder*="正文"]', '[contenteditable="true"]'],
    bilibili: ['textarea[placeholder*="简介"]', 'textarea[placeholder*="描述"]', '[contenteditable="true"]'],
    weibo: ['textarea[placeholder*="分享"]', 'textarea[placeholder*="发布"]', 'textarea', '[contenteditable="true"]']
  }};
  const setValue = (el, value) => {{
    if (!el) return false;
    const rawText = String(value || '');
    const maxLength = Number(el.maxLength || el.getAttribute('maxlength') || 0);
    // 平台标题输入框可能主动限制长度；先按控件真实限制写入，避免页面
    // 截断后再被校验逻辑误判成“没有填入”。正文仍按原文写入。
    const text = maxLength > 0 ? rawText.slice(0, maxLength) : rawText;
    try {{ el.scrollIntoView({{block: 'center', inline: 'nearest'}}); }} catch (_) {{}}
    el.focus();
    if (el.isContentEditable || el.getAttribute('contenteditable') === 'true') {{
      el.innerHTML = '';
      el.textContent = text;
    }} else if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {{
      const view = el.ownerDocument.defaultView || window;
      const proto = el.tagName === 'TEXTAREA'
        ? view.HTMLTextAreaElement.prototype : view.HTMLInputElement.prototype;
      const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
      if (descriptor && descriptor.set) descriptor.set.call(el, text);
      else el.value = text;
    }} else if (el.getAttribute('role') === 'textbox' || el.hasAttribute('data-placeholder')) {{
      // 某些创作页使用非 contenteditable 的自定义 textbox。
      // 先写入文本并派发输入事件，避免把 HTMLInputElement 的 setter
      // 错用到 DIV/自定义元素上导致整段填充中断。
      el.textContent = text;
    }} else {{
      return false;
    }}
    const view = el.ownerDocument.defaultView || window;
    el.dispatchEvent(new view.InputEvent('input', {{bubbles: true, inputType: 'insertText', data: text.slice(0, 80)}}));
    el.dispatchEvent(new view.Event('change', {{bubbles: true}}));
    return true;
  }};
  const genericInputs = query([
    'input:not([type="file"]):not([type="search"]):not([type="password"])'
  ]).filter(isEditable);
  const markedTitleFields = genericInputs.filter((el) =>
    /标题|title/i.test(fieldText(el))
  );
  const titleFields = unique([
    ...query(titleSelectors[platform] || []),
    ...markedTitleFields,
    ...(markedTitleFields.length === 0 && genericInputs.length === 1 ? genericInputs : [])
  ]).filter(isEditable);
  const genericTextboxes = query([
    'textarea', '[contenteditable="true"]', '[role="textbox"]', '[data-placeholder]'
  ]).filter(isEditable);
  const bodyFields = unique([
    ...query(bodySelectors[platform] || []), ...genericTextboxes
  ]).filter(isEditable)
    .filter((el) => !/标题|title/i.test(fieldText(el)));
  // 只处理创作编辑器邻域内的可见字段；不使用“发布/发送/提交”文本定位，
  // 也不触发任何鼠标点击，避免误触最终提交按钮。
  const titleField = titleFields[0] || null;
  const bodyField = bodyFields.find((el) => el !== titleField) || null;
  const filled = {{title: false, body: false, topics: false}};
  if (data.title && titleField) filled.title = setValue(titleField, data.title);
  if (data.body && bodyField) filled.body = setValue(bodyField, data.body);
  const topicText = Array.isArray(data.topics) ? data.topics.filter(Boolean).join(' ') : '';
  const topicFields = query([
    'input[placeholder*="话题"]', 'input[placeholder*="标签"]',
    'textarea[placeholder*="话题"]'
  ]);
  if (topicText && topicFields[0]) filled.topics = setValue(topicFields[0], topicText);
   const read = (el) => el ? normalizeContent(
    el.isContentEditable || el.getAttribute('contenteditable') === 'true'
      ? (el.innerText || el.textContent)
      : (el.value !== undefined ? el.value : (el.innerText || el.textContent))
  ) : '';
  const maxLengthOf = (el) => Number(el?.maxLength || el?.getAttribute?.('maxlength') || 0);
   const titleTarget = normalizeContent(String(data.title || ''));
   const titleValue = read(titleField);
   const titleLimit = maxLengthOf(titleField);
   // 平台输入框可能有真实 maxlength（抖音图文标题当前为 20 字）。
   // setValue 会按该限制写入；核对时也必须使用同一份页面可接受值，
   // 否则本地标题较长时会把已经成功填入的内容误判为失败，进而不会进入发布按钮流程。
   const titleComparable = titleLimit > 0
     ? titleTarget.slice(0, titleLimit) : titleTarget;
   const bodyTarget = normalizeContent(String(data.body || ''));
   const topicTarget = normalizeContent(topicText);
  const titlePrefixAccepted = Boolean(
    filled.title && titleLimit > 0 && titleValue && titleTarget.startsWith(titleValue) &&
    titleValue.length >= Math.min(titleLimit, 8)
  );
  const describe = (el) => {{
    const r = el.getBoundingClientRect();
    return {{tag: el.tagName, type: el.type || '',
      placeholder: el.getAttribute('placeholder') || '',
      ariaLabel: el.getAttribute('aria-label') || '',
      dataPlaceholder: el.getAttribute('data-placeholder') || '',
      role: el.getAttribute('role') || '',
      maxLength: maxLengthOf(el),
      className: String(el.className || '').slice(0, 120),
      x: Math.round(r.left), y: Math.round(r.top),
      width: Math.round(r.width), height: Math.round(r.height)}};
  }};
   const actualBody = read(bodyField);
   const actualTopics = topicFields[0] ? read(topicFields[0]) : '';
   const titleMatched = platform === 'weibo' || !titleTarget || (
     filled.title && (titleValue === titleComparable || titlePrefixAccepted)
   );
   const bodyMatched = !bodyTarget || (filled.body && actualBody === bodyTarget);
   // 没有独立话题控件的平台把话题视为“不适用”；一旦页面提供话题控件，
   // 则必须核对实际值，避免只填正文却误以为整条平台版本已准备好。
   const topicsChecked = Boolean(topicTarget && topicFields.length);
   const topicsMatched = !topicsChecked || (filled.topics && actualTopics === topicTarget);
   const mismatches = [];
   if (!titleMatched) mismatches.push('title');
   if (!bodyMatched) mismatches.push('body');
   if (!topicsMatched) mismatches.push('topics');
   const feedback = {{
     title: titleValue, body: actualBody, topics: actualTopics,
     filled,
    titleRequired: platform !== 'weibo',
    // DOM 读取会把换行、连续空格统一成单个空格；比较时对目标文本
    // 使用同一套规范化规则，避免“实际已经填入”却被误判为未确认。
     titleMatched,
     bodyMatched,
     topicsMatched,
     topicsChecked,
     expected: {{title: titleTarget, body: bodyTarget, topics: topicTarget}},
     actual: {{title: titleValue, body: actualBody, topics: actualTopics}},
     mismatches,
     verified: mismatches.length === 0,
    candidates: {{title: titleFields.length, body: bodyFields.length, topics: topicFields.length}},
    titleFields: titleFields.slice(0, 8).map(describe),
    bodyFields: bodyFields.slice(0, 8).map(describe),
    platform,
    clickCount: 0
  }};
   feedback.ok = Boolean(feedback.verified && feedback.titleMatched && feedback.bodyMatched && feedback.topicsMatched);
   feedback.stage = feedback.ok ? 'filled_waiting_confirmation' : 'fill_unverified';
   feedback.message = feedback.ok ? '发布内容已填入并与本地平台版本核对一致，未执行发送' :
     (feedback.candidates.title === 0 && feedback.candidates.body === 0
       ? '当前页面需要先完成素材选择，尚未显示发布编辑器'
       : ('填入后内容核对不一致：' + (mismatches.length ? mismatches.join('、') : '编辑器未返回可核对内容')));
  return feedback;
}})()
"""


__all__ = ["PUBLISH_EDITOR_URLS", "PUBLISH_BUTTON_LABELS", "PublishingBrowserAdapter", "PublishingBrowserError"]
