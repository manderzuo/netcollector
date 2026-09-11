# -*- coding: utf-8 -*-
"""四个平台浏览器回复定位脚本的契约回归测试。"""

import asyncio
import os
import sys
import unittest
from dataclasses import replace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from interactions.browser_reply import (  # noqa: E402
    _bring_target_page_to_front,
    _fill_script,
    _input_feedback_script,
    _reply_button_semantic_marker_script,
    _reply_input_probe_script,
    _probe_target_script,
    _submit_script,
    _verify_script,
)
from interactions.reply_target import ReplyTarget  # noqa: E402


def _target(platform: str) -> ReplyTarget:
    return ReplyTarget(
        lead_id=1,
        draft_id=2,
        platform=platform,
        account_id=3,
        account_name="测试账号",
        account_status="idle",
        bb_window_id="window-1",
        video_id=4,
        video_platform_id="platform-video-1",
        video_url="https://example.com/video/1",
        local_comment_id=5,
        platform_comment_id=None,
        platform_user_id="user-1",
        nickname="测试用户",
        content="这是一条原评论",
        comment_time="2026-08-22",
    )


class TestBrowserReplyPlatformScripts(unittest.TestCase):
    def test_bilibili_uses_shadow_dom_reply_editor(self):
        target = _target("bilibili")
        fill = _fill_script(target, "测试回复")
        probe = _probe_target_script(target)
        feedback = _input_feedback_script("测试回复", "bilibili")
        submit = _submit_script("测试回复", "bilibili")
        verify = _verify_script("测试回复", "bilibili")

        self.assertIn("bili-comments", probe)
        self.assertIn("bili-comment-thread-renderer", probe)
        self.assertIn("comment_scan_step", fill)
        self.assertIn("document.scrollingElement", fill)
        self.assertIn("window.scrollTo", fill)
        self.assertIn("#reply-container bili-comment-box", fill)
        self.assertIn('.brt-editor[contenteditable="true"]', fill)
        self.assertIn("Input.insertText", fill)
        self.assertIn("bili-comment-rich-textarea", feedback)
        self.assertIn("#pub button", submit)
        self.assertIn("findMatch", submit)
        self.assertIn("submit_button_probe", submit)
        self.assertIn("sendEnabled", submit)
        self.assertIn("bili-rich-text", verify)
        self.assertIn("walkShadow", verify)
        self.assertIn("bili-comment-reply-renderer", verify)
        self.assertIn("postedMatchCount", verify)
        self.assertIn("nonTextComment", probe)
        self.assertIn("comment_time", fill)
        self.assertIn("identityCount", fill)

    def test_hidden_target_page_is_activated_before_reply(self):
        class FakeSession:
            def __init__(self):
                self.commands = []

            async def cmd(self, method, params=None, session_id=None):
                self.commands.append((method, session_id))
                return {}

            async def eval(self, _script, _session_id):
                return {
                    "url": "https://example.com/video/1",
                    "hidden": False,
                    "visibility": "visible",
                    "hasFocus": True,
                }

        session = FakeSession()
        state = asyncio.run(_bring_target_page_to_front(
            session, "session-1", "https://example.com/video/1", timeout=0.2,
        ))
        self.assertEqual(session.commands[0], ("Page.bringToFront", "session-1"))
        self.assertFalse(state["hidden"])

    def test_weibo_uses_comment_icon_and_dedicated_reply_modal(self):
        target = _target("weibo")
        fill = _fill_script(target, "测试回复")
        probe = _probe_target_script(target)
        feedback = _input_feedback_script("测试回复", "weibo")
        submit = _submit_script("测试回复", "weibo")
        verify = _verify_script("测试回复", "weibo")

        self.assertIn(".wbpro-scroller-item", probe)
        self.assertIn("#scroller", probe)
        self.assertIn("scrollAction", probe)
        self.assertIn('.wbpro-scroller-item', fill)
        self.assertIn("comment_scan_step", fill)
        self.assertIn("scrollTop", fill)
        self.assertIn("scrollHeight", fill)
        self.assertIn("document.scrollingElement", fill)
        self.assertIn("window.scrollTo", fill)
        self.assertIn('i[title="评论"]', fill)
        self.assertIn('textarea[placeholder="发布你的回复"]', fill)
        self.assertIn("Input.insertText", fill)
        self.assertIn('textarea[placeholder="发布你的回复"]', feedback)
        self.assertIn("clean(button.innerText) === '回复'", submit)
        self.assertIn(".woo-modal-wrap", submit)
        self.assertIn("submit_button_probe", submit)
        self.assertIn(".wbpro-scroller-item", verify)
        self.assertIn("nodeHasValue", fill)
        self.assertIn("nonTextComment", fill)
        self.assertIn("timeCount", fill)

    def test_existing_platforms_keep_generic_reply_path(self):
        for platform in ("douyin", "xhs"):
            fill = _fill_script(_target(platform), "测试回复")
            self.assertIn("comment-item", fill)
            self.assertIn("textNorm", fill)
            self.assertIn("[赞]", fill)
            self.assertIn("comment_scan_step", fill)
            self.assertIn("scrollTop", fill)
            self.assertNotIn("bili-comments", fill)
            self.assertNotIn("发布你的回复", fill)

        # 小红书当前评论行使用无文字的 .reply.icon-container 图标入口。
        self.assertIn(".reply.icon-container", fill)
        self.assertIn("isIconReplyButton", fill)
        self.assertIn("commentRowSelector", fill)
        self.assertIn("isNestedCommentControl", fill)
        self.assertIn("document.scrollingElement || null", fill)
        self.assertIn("requiresDomReplyActivation", fill)
        self.assertIn("reply_button_dynamic_activation_required", fill)
        self.assertIn("nodeHasValue", fill)
        self.assertIn("isGenericDouyinInput", fill)
        self.assertIn("inputScopeText", fill)
        self.assertIn("inputHasTargetMarker", fill)
        self.assertIn("nonTextComment", fill)
        self.assertIn("timeCount", fill)
        marker = _reply_button_semantic_marker_script(_target("douyin"), "test-marker")
        self.assertIn("data-codex-reply-target", marker)
        self.assertIn("reply_button_not_found", marker)
        self.assertNotIn("replyClick", marker)
        probe = _reply_input_probe_script(_target("douyin"), "测试回复")
        self.assertIn("native_click_input_probe", probe)
        self.assertIn("native_click_single_input", probe)
        self.assertIn("requiresCdpInput", probe)

        # 抖音 Note 的楼中楼默认折叠，回复定位必须先展开再扫描。
        note_target = replace(
            _target("douyin"),
            video_url="https://www.douyin.com/note/7593340177108857082",
        )
        note_fill = _fill_script(note_target, "测试回复")
        self.assertIn("expandNestedReplies", note_fill)
        self.assertIn("comment-reply-expand-btn", note_fill)
        self.assertIn("douyin_nested_reply_expand", note_fill)
        self.assertIn("expandClickLimit = 24", note_fill)
        self.assertIn("expandStallLimit = 3", note_fill)
        self.assertIn("expandDisabled", note_fill)

        # 普通抖音视频的楼中楼同样默认折叠，不能只在 Note 页面展开。
        video_target = replace(
            _target("douyin"),
            video_url="https://www.douyin.com/video/7678981663795268902",
        )
        video_fill = _fill_script(video_target, "测试回复")
        self.assertIn("const isDouyin =", video_fill)
        self.assertIn("if (!isDouyin)", video_fill)
        self.assertIn("douyin_nested_reply_expand", video_fill)

    def test_non_text_comment_target_is_valid_with_user_and_time(self):
        target = replace(_target("douyin"), content="", platform_comment_id=None)
        target.validate_for_browser()
        invalid = replace(target, nickname=None, platform_user_id=None, comment_time=None)
        with self.assertRaises(ValueError):
            invalid.validate_for_browser()

    def test_douyin_and_xhs_have_platform_specific_submit_scope(self):
        douyin_submit = _submit_script("测试回复", "douyin")
        xhs_submit = _submit_script("测试回复", "xhs")

        self.assertIn("#comment-input-container", douyin_submit)
        self.assertIn(".comment-input-container-focus", douyin_submit)
        self.assertIn(".comment-input-inner-container", douyin_submit)
        self.assertIn(".commentInput-right-ct", douyin_submit)
        self.assertIn("#FE2C55", douyin_submit)
        self.assertIn("douyin_red_arrow_after_input", douyin_submit)
        self.assertNotIn("/发送|发布|提交/.test", douyin_submit)
        self.assertIn("submit_button_probe", douyin_submit)
        self.assertIn("未找到已填入内容的抖音回复输入框", douyin_submit)

        self.assertIn("#content-textarea", xhs_submit)
        self.assertIn(".engage-bar", xhs_submit)
        self.assertIn(".input-box", xhs_submit)
        self.assertIn("submit_button_probe", xhs_submit)
        self.assertIn("未找到已填入内容的小红书回复输入框", xhs_submit)

    def test_submit_dispatch_is_platform_specific(self):
        scripts = {
            "douyin": _submit_script("测试回复", "douyin"),
            "xhs": _submit_script("测试回复", "xhs"),
            "weibo": _submit_script("测试回复", "weibo"),
            "bilibili": _submit_script("测试回复", "bilibili"),
        }
        self.assertIn("输入内容后未找到抖音红色上箭头发送按钮", scripts["douyin"])
        self.assertIn("小红书未找到明确的发送按钮", scripts["xhs"])
        self.assertIn("微博未找到明确的回复按钮", scripts["weibo"])
        self.assertIn("B站未找到明确的发布按钮", scripts["bilibili"])


if __name__ == "__main__":
    unittest.main()
