# -*- coding: utf-8 -*-
"""贴吧 API 评论/回复适配器。

贴吧不需要 BitBrowser 窗口。互动中心仍复用已有的审核、确认和发送状态机，
这里只把最终动作转换为官方 ``addPost`` API 调用。
"""

from __future__ import annotations

from collections.abc import Callable

from .models import ReplyActionResult
from .reply_target import ReplyTarget, ReplyTargetError


class TiebaReplyAdapter:
    """通过贴吧官方 API 发布主帖评论或回复某条楼层。"""

    def __init__(self, client_factory: Callable):
        self._client_factory = client_factory

    def reply(self, target: ReplyTarget, content: str,
              *, confirm: bool = False) -> ReplyActionResult:
        if not isinstance(target, ReplyTarget):
            raise ReplyTargetError("贴吧回复目标格式错误")
        if str(target.platform or "").strip().lower() not in {"tieba", "贴吧"}:
            raise ReplyTargetError("贴吧回复适配器收到非贴吧目标")
        text = str(content or "").strip()
        if not text:
            raise ReplyTargetError("回复内容不能为空")
        thread_id = str(target.video_platform_id or "").strip()
        if not thread_id:
            raise ReplyTargetError("贴吧帖子 ID 不能为空")
        # 填充阶段只做目标校验，不产生网络写操作；发送阶段由互动中心的
        # 人工确认和真实发送开关共同保护。
        if not confirm:
            return ReplyActionResult(
                ok=True,
                stage="tieba_target_ready",
                message="已定位贴吧帖子，确认后发送",
                verified=False,
                details={
                    "thread_id": thread_id,
                    "post_id": str(target.platform_comment_id or ""),
                },
                target=target,
            )
        post_id = str(target.platform_comment_id or "").strip() or None
        client = self._client_factory()
        result = client.add_post(
            text,
            thread_id=thread_id,
            post_id=post_id,
        )
        return ReplyActionResult(
            ok=True,
            stage="tieba_api_sent",
            message="贴吧评论已发送" if not post_id else "贴吧回复已发送",
            verified=True,
            details={
                "thread_id": thread_id,
                "post_id": post_id or "",
                "response": result if isinstance(result, dict) else {},
            },
            target=target,
        )


class RoutingReplyAdapter:
    """按目标平台选择贴吧 API 或 BitBrowser 回复适配器。"""

    def __init__(self, browser_adapter=None, tieba_adapter=None):
        self._browser = browser_adapter
        self._tieba = tieba_adapter

    def reply(self, target: ReplyTarget, content: str,
              *, confirm: bool = False) -> ReplyActionResult:
        platform = str(getattr(target, "platform", "") or "").strip().lower()
        if platform in {"tieba", "贴吧"}:
            if self._tieba is None:
                raise ReplyTargetError("贴吧 API 回复适配器未配置")
            return self._tieba.reply(target, content, confirm=confirm)
        if self._browser is None:
            raise ReplyTargetError("浏览器回复适配器未配置")
        return self._browser.reply(target, content, confirm=confirm)


__all__ = ["TiebaReplyAdapter", "RoutingReplyAdapter"]
