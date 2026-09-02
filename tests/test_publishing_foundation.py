# -*- coding: utf-8 -*-
"""发布中心第一阶段基础回归测试。"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import db  # noqa: E402
from backend_protocol import COMMANDS, make_command  # noqa: E402
from publishing.browser import (  # noqa: E402
    PUBLISH_BUTTON_LABELS, _dispatch_file_events_script, _editor_probe_script, _editor_url,
    _fill_script, _mode_dom_click_script, _mode_probe_confirmed, _mode_probe_script,
    _publish_success_probe_script,
    _asset_type_from_path, _attach_publish_page, _safe_mode_click_script, _submit_publish,
    _xhs_publish_button_probe, _same_editor_page,
)
from publishing.account_content_reader import (  # noqa: E402
    PROFILE_ENTRY_URLS, normalize_profile_items, select_profile_target,
    _profile_url_reached, _reader_js,
)
from publishing.message_reader import (  # noqa: E402
    MESSAGE_ENTRY_URLS, MESSAGE_ROUTES, MESSAGE_TYPE_LABELS,
    _douyin_back_to_conversations_js, _douyin_conversation_targets_js, _douyin_detail_reader_js,
    _douyin_message_scroll_js, _label_rect_js, _message_reader_js, normalize_messages,
)
from publishing.service import PublishingService  # noqa: E402
from publishing.workspace import (  # noqa: E402
    ACCOUNT_CONTENT_PLATFORMS,
    PublishingWorkspaceService,
)
from time_utils import beijing_now  # noqa: E402
from ui2.state import PAGE_KEYS, Ui2State  # noqa: E402


class PublishingFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.init_db(
            os.path.join(self.temp.name, "publishing.db"), check_same_thread=False
        )
        self.service = PublishingService(self.conn)
        self.workspace = PublishingWorkspaceService(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_create_edit_and_filter_four_platform_variants(self):
        draft_id = self.service.create_draft(
            title="统一标题", body="统一正文",
            platforms=["douyin", "xhs", "bilibili", "weibo"],
        )
        result = self.service.list_drafts()
        self.assertEqual(result["total"], 1)
        self.assertEqual(
            {item["platform"] for item in result["items"][0]["variants"]},
            {"douyin", "xhs", "bilibili", "weibo"},
        )
        self.assertEqual(result["items"][0]["platforms"], [
            "douyin", "xhs", "bilibili", "weibo"
        ])
        self.assertEqual(result["items"][0]["platform_labels"], [
            "抖音", "小红书", "B站", "微博"
        ])
        self.assertEqual(result["platform_options"], [
            {"value": "douyin", "label": "抖音"},
            {"value": "xhs", "label": "小红书"},
            {"value": "bilibili", "label": "B站"},
            {"value": "weibo", "label": "微博"},
        ])

        self.service.update_variant(
            draft_id=draft_id, platform="xhs", title="小红书标题",
            body="小红书正文", topics=["话题一", "话题二"],
        )
        xhs = self.service.list_drafts(platform="xhs")["items"][0]
        self.assertEqual(xhs["variants"][0]["title"], "小红书标题")
        self.assertEqual(xhs["variants"][0]["topics"], ["话题一", "话题二"])
        self.assertEqual(xhs["source_content"], "统一正文")

    def test_status_transitions_and_safe_delete(self):
        draft_id = self.service.create_draft(title="标题", body="正文", platforms=["weibo"])
        self.service.change_status(draft_id, "review")
        self.assertEqual(self.service.list_drafts(status="review")["total"], 1)
        self.service.change_status(draft_id, "approved")
        self.assertEqual(self.service.list_drafts(status="approved")["total"], 1)
        self.service.change_status(draft_id, "queued")
        self.assertEqual(self.service.list_drafts(status="queued")["total"], 1)
        self.service.delete_draft(draft_id)
        self.assertEqual(self.service.list_drafts()["total"], 0)

    def test_invalid_platform_and_status_are_rejected(self):
        with self.assertRaises(ValueError):
            self.service.create_draft(title="标题", body="正文", platforms=["unknown"])
        with self.assertRaises(ValueError):
            self.service.list_drafts(status="unknown")

    def test_ui_and_protocol_expose_publish_center(self):
        self.assertIn("publish", PAGE_KEYS)
        self.assertIn("list_publish_drafts", COMMANDS)
        self.assertEqual(make_command("list_publish_drafts")["kind"], "command")
        state = Ui2State()
        state.apply_publishing(self.service.list_drafts())
        self.assertEqual(state.to_view_model()["publishing"]["total"], 0)

    def test_preview_variant_is_readable_without_submit_capability(self):
        draft_id = self.service.create_draft(
            title="预览标题", body="完整正文内容", platforms=["douyin"]
        )
        variant = self.service.get_variant(draft_id, "douyin")
        self.assertEqual(variant["title"], "预览标题")
        self.assertEqual(variant["body"], "完整正文内容")
        script = _fill_script(
            '{"platform":"douyin","title":"预览标题",'
            '"body":"完整正文内容","topics":[]}'
        )
        self.assertIn("filled_waiting_confirmation", script)
        self.assertIn("contentDocument", script)
        self.assertIn("titleRequired", script)
        self.assertIn("normalizeContent(String(data.body || ''))", script)
        self.assertIn("maxLengthOf", script)
        self.assertIn("titlePrefixAccepted", script)
        self.assertIn("mismatches", script)
        self.assertIn("verified", script)
        self.assertNotIn(".click(", script)
        self.assertNotIn("dispatchEvent(new MouseEvent", script)

    def test_fill_script_verifies_actual_content_for_all_platforms(self):
        for platform in ("douyin", "xhs", "bilibili", "weibo"):
            script = _fill_script(
                '{"platform":"%s","title":"编辑后的标题",'
                '"body":"编辑后的完整正文\\n第二行","topics":["平台话题"]}' % platform
            )
            self.assertIn("platform", script)
            self.assertIn("expected: {title: titleTarget, body: bodyTarget, topics: topicTarget}", script)
            self.assertIn("actual: {title: titleValue, body: actualBody, topics: actualTopics}", script)
            self.assertIn("titleMatched", script)
            self.assertIn("bodyMatched", script)
            self.assertIn("topicsMatched", script)
            self.assertIn("发布内容已填入并与本地平台版本核对一致", script)

    def test_image_editor_fill_probe_handles_shadow_dom_and_custom_textboxes(self):
        probe = _editor_probe_script()
        self.assertIn("shadowRoot", probe)
        self.assertIn("data-placeholder", probe)
        self.assertIn("body_candidates", probe)

        script = _fill_script(
            '{"platform":"douyin","title":"图文标题",'
            '"body":"图文正文","topics":[]}'
        )
        self.assertIn("const isEditable", script)
        self.assertIn("scrollIntoView", script)
        self.assertIn("role=\"textbox\"", script)
        self.assertIn("genericInputs", script)
        self.assertIn("titleFields", script)
        self.assertIn("填入后内容核对不一致", script)

    def test_image_upload_dispatches_events_inside_shadow_dom_and_iframe(self):
        script = _dispatch_file_events_script()
        self.assertIn("shadowRoot", script)
        self.assertIn("contentDocument", script)
        self.assertIn("composed: true", script)
        self.assertIn("input[type=file]", script)

    def test_image_mode_is_verified_and_has_a_second_activation_path(self):
        probe = _mode_probe_script("发布图文", "image")
        self.assertIn("aria-selected", probe)
        self.assertIn("imageInputs", probe)
        self.assertIn("oppositeTypeSignal", probe)
        self.assertIn("mp4|mov|mkv", probe)
        self.assertIn("confirmed", probe)
        fallback = _mode_dom_click_script(["发布图文"])
        self.assertIn("dom_mode_click", fallback)
        self.assertIn("selected.el.click", fallback)

    def test_mode_probe_does_not_treat_normal_candidates_as_mismatch(self):
        self.assertIsNone(_mode_probe_confirmed({
            "confirmed": False, "candidateCount": 4, "typeSignal": True,
            "oppositeTypeSignal": False,
        }, "image"))
        self.assertFalse(_mode_probe_confirmed({
            "confirmed": False, "candidateCount": 4, "oppositeTypeSignal": True,
        }, "image"))

    def test_platform_content_type_uses_correct_editor_and_safe_mode_probe(self):
        self.assertEqual(
            _editor_url("bilibili", "text"),
            "https://member.bilibili.com/platform/upload/text",
        )
        self.assertEqual(
            _editor_url("bilibili", "video"),
            "https://member.bilibili.com/platform/upload/video/frame",
        )
        script = _safe_mode_click_script(["发布图文", "新的创作"])
        self.assertIn("getBoundingClientRect", script)
        self.assertNotIn(".click(", script)
        self.assertIn("ownTextOf", script)
        self.assertIn("hasSpecificChild", script)
        fallback = _mode_dom_click_script(["发布图文"])
        self.assertIn("ownTextOf", fallback)
        self.assertIn("hasSpecificChild", fallback)

    def test_xhs_editor_entry_matches_asset_mode_and_reloads_mismatched_target(self):
        self.assertIn("target=image", _editor_url("xhs", "text", "image"))
        self.assertIn("target=video", _editor_url("xhs", "text", "video"))
        self.assertFalse(_same_editor_page(
            "https://creator.xiaohongshu.com/publish/publish?from=menu&target=video",
            "https://creator.xiaohongshu.com/publish/publish?from=menu&target=image",
        ))
        self.assertTrue(_same_editor_page(
            "https://creator.xiaohongshu.com/publish/publish?from=menu&target=image",
            "https://creator.xiaohongshu.com/publish/publish?from=menu&target=image",
        ))

    def test_real_publish_has_explicit_platform_button_labels(self):
        self.assertEqual(PUBLISH_BUTTON_LABELS["douyin"][:2], ["发布", "立即发布"])
        self.assertIn("发布作品", PUBLISH_BUTTON_LABELS["douyin"])
        self.assertIn("发布视频", PUBLISH_BUTTON_LABELS["douyin"])
        script = _safe_mode_click_script(PUBLISH_BUTTON_LABELS["douyin"])
        self.assertNotIn("Input", script)
        self.assertIn("getBoundingClientRect", script)
        self.assertIn("aria-disabled", script)

    def test_final_publish_probe_excludes_navigation_controls(self):
        script = _safe_mode_click_script(PUBLISH_BUTTON_LABELS["xhs"], final_submit=True)
        self.assertIn("finalSubmitOnly = true", script)
        self.assertIn("button,[role=\"button\"],input[type=\"submit\"]", script)
        self.assertIn("isNavigationLike", script)
        self.assertIn("navigationExcluded", script)

    def test_xhs_publish_probe_reads_closed_custom_element_button_box(self):
        class FakeSession:
            def __init__(self):
                self.calls = []

            async def cmd(self, method, params=None, session_id=None, timeout=None):
                self.calls.append((method, params, session_id, timeout))
                if method == "DOM.getFlattenedDocument":
                    return {"nodes": [
                        {"nodeId": 1, "nodeName": "XHS-PUBLISH-BTN",
                         "attributes": ["submit-text", "发布"],
                         "shadowRoots": [{"nodeId": 2}]},
                        {"nodeId": 2, "nodeName": "#document-fragment",
                         "parentId": None},
                        {"nodeId": 3, "nodeName": "DIV", "parentId": 2,
                         "attributes": ["class", "publish-page-publish-btn"]},
                        {"nodeId": 4, "nodeName": "BUTTON", "parentId": 3,
                         "backendNodeId": 44,
                         "attributes": ["class", "ce-btn bg-red",
                                        "aria-disabled", "false"]},
                        {"nodeId": 5, "nodeName": "#text", "parentId": 4,
                         "nodeValue": "发布"},
                    ]}
                if method == "DOM.getBoxModel":
                    return {"model": {"border": [100, 200, 220, 200,
                                                    220, 240, 100, 240]}}
                return {}

        session = FakeSession()
        result = asyncio.run(_xhs_publish_button_probe(session, "sid", ["发布"]))
        self.assertTrue(result["clicked"])
        self.assertEqual(result["label"], "发布")
        self.assertEqual(result["method"], "live_dom_flattened_box_model")
        self.assertEqual(result["x"], 160)
        self.assertEqual(result["y"], 220)
        self.assertIn("DOM.getFlattenedDocument", [call[0] for call in session.calls])
        self.assertIn("DOM.scrollIntoViewIfNeeded", [call[0] for call in session.calls])

    def test_publish_page_selection_skips_blank_page_before_responsive_page(self):
        class FakeSession:
            def __init__(self):
                self.attached = []
                self.timeouts = []

            async def cmd(self, method, params=None, session_id=None, timeout=None):
                self.timeouts.append((method, timeout))
                if method == "Target.getTargets":
                    return {"targetInfos": [
                        {"type": "page", "targetId": "blank", "url": ""},
                        {"type": "page", "targetId": "responsive", "url": "https://weibo.com/u/1"},
                    ]}
                if method == "Target.attachToTarget":
                    self.attached.append(params["targetId"])
                    return {"sessionId": f"sid-{params['targetId']}"}
                return {}

            async def eval(self, _script, session_id, timeout=None):
                self.timeouts.append(("Runtime.evaluate", timeout))
                target_id = session_id.removeprefix("sid-")
                if target_id == "responsive":
                    return {"url": "https://weibo.com/u/1", "title": "微博"}
                return {"url": "", "title": ""}

        session = FakeSession()
        sid, page = asyncio.run(_attach_publish_page(session, "https://weibo.com/"))
        self.assertEqual(sid, "sid-responsive")
        self.assertEqual(page["url"], "https://weibo.com/u/1")
        self.assertEqual(session.attached[0], "responsive")
        self.assertTrue(all(timeout is not None for _, timeout in session.timeouts))

    def test_real_publish_requires_a_new_success_prompt(self):
        script = _publish_success_probe_script("douyin")
        self.assertIn("发布成功", script)
        self.assertIn("success", script)
        self.assertIn("visibility", script)

    def test_asset_mode_prefers_the_real_file_suffix(self):
        self.assertEqual(_asset_type_from_path("clip.mp4", "image"), "video")
        self.assertEqual(_asset_type_from_path("cover.png", "video"), "image")
        self.assertEqual(_asset_type_from_path("clip-without-extension", "video"), "video")

    def test_real_publish_clicks_again_when_success_is_not_confirmed(self):
        class FakeSession:
            def __init__(self):
                self.clicks = []
                self.success_probe_count = 0

            async def eval(self, script, _session_id):
                if "发布成功" in script:
                    self.success_probe_count += 1
                    if self.success_probe_count == 1:
                        return {"success": False, "matches": []}
                    return {"success": True, "matches": ["发布成功"]}
                return {"clicked": True, "label": "发布", "x": 100, "y": 200}

            async def cmd(self, method, params=None, session_id=None):
                if method == "Input.dispatchMouseEvent" and params:
                    self.clicks.append(params["type"])
                return {}

        session = FakeSession()
        with patch("publishing.browser.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(_submit_publish(session, "session-1", "douyin"))
        self.assertTrue(result["success_confirmed"])
        self.assertTrue(result["clicked"])
        self.assertEqual(session.clicks, ["mouseMoved", "mousePressed", "mouseReleased"])

    def test_real_publish_retry_interval_starts_after_the_click(self):
        class FakeSession:
            def __init__(self):
                self.clicks = []

            async def eval(self, script, _session_id):
                if "发布成功" in script:
                    return {"success": False, "matches": []}
                return {"clicked": True, "label": "发布", "x": 100, "y": 200}

            async def cmd(self, method, params=None, session_id=None):
                if method == "Input.dispatchMouseEvent" and params:
                    self.clicks.append(params["type"])
                return {}

        session = FakeSession()
        sleep_mock = AsyncMock()
        success_mock = AsyncMock(side_effect=[
            {"success": False, "matches": []},
            {"success": True, "matches": ["发布成功"]},
        ])
        with patch("publishing.browser.asyncio.sleep", new=sleep_mock), \
             patch("publishing.browser._wait_publish_success", new=success_mock):
            result = asyncio.run(_submit_publish(session, "session-1", "douyin"))
        self.assertTrue(result["success_confirmed"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["click_count"], 2)
        self.assertEqual(len(session.clicks), 6)
        self.assertEqual(sleep_mock.await_args_list[0].args[0], 30.0)
        self.assertGreaterEqual(sleep_mock.await_args_list[1].args[0], 9.0)

    def test_xhs_non_retryable_publish_rejection_stops_duplicate_clicks(self):
        class FakeSession:
            def __init__(self):
                self.clicks = []
                self.listeners = {}

            def on(self, method, callback):
                self.listeners[method] = callback
                return lambda: self.listeners.pop(method, None)

            async def eval(self, script, _session_id):
                if "发布成功" in script:
                    return {"success": False, "matches": []}
                return {"clicked": True, "label": "发布", "x": 100, "y": 200}

            async def cmd(self, method, params=None, session_id=None, timeout=None):
                if method == "Input.dispatchMouseEvent" and params:
                    self.clicks.append(params["type"])
                    if params["type"] == "mouseReleased":
                        self.listeners["Network.responseReceived"]({
                            "requestId": "publish-1",
                            "response": {
                                "url": "https://edith.xiaohongshu.com/web_api/sns/v2/note",
                                "status": 200,
                            },
                        })
                return {}

            async def get_body(self, request_id, session_id, timeout=None):
                self.assert_request = (request_id, session_id)
                return '{"result":-9136,"success":false,"msg":"因违反社区规范禁止发笔记","need_retry":false}'

        session = FakeSession()
        with patch("publishing.browser.asyncio.sleep", new=AsyncMock()), \
             patch("publishing.browser._wait_publish_success", new=AsyncMock(
                 return_value={"success": False, "matches": []}
             )):
            result = asyncio.run(_submit_publish(session, "session-1", "xhs"))
        self.assertEqual(result["reason"], "platform_rejected")
        self.assertEqual(result["platform_error"], "因违反社区规范禁止发笔记")
        self.assertEqual(result["click_count"], 1)
        self.assertEqual(session.clicks, ["mouseMoved", "mousePressed", "mouseReleased"])

    def test_profile_items_are_normalized_and_deduplicated(self):
        items = normalize_profile_items("douyin", [
            {"url": "https://www.douyin.com/note/abc", "title": "笔记", "like_count": "3"},
            {"url": "https://www.douyin.com/note/abc", "title": "重复"},
            {"url": "https://www.douyin.com/video/xyz", "title": "视频", "comment_count": 8},
            {"url": "https://www.douyin.com/user/other", "title": "不是作品"},
        ])
        self.assertEqual([item["content_id"] for item in items], ["abc", "xyz"])
        self.assertEqual(items[0]["content_type"], "note")
        self.assertEqual(items[0]["extra"]["__account_content_origin"], "profile_sync")
        self.assertEqual(items[1]["comment_count"], 8)

    def test_kuaishou_account_contents_are_supported_without_enabling_publish_variants(self):
        account_id = db.upsert_account(
            self.conn, "快手账号", "kuaishou-window", "kuaishou"
        )
        self.assertIn("kuaishou", ACCOUNT_CONTENT_PLATFORMS)

        count = self.workspace.replace_profile_contents(
            account_id,
            "kuaishou",
            [{
                "content_id": "ks-1",
                "url": "https://www.kuaishou.com/short-video/ks-1",
                "title": "快手作品",
                "content_type": "video",
                "like_count": 12,
            }],
            profile_url="https://www.kuaishou.com/profile/ks-user",
        )
        self.assertEqual(count, 1)

        listed = self.workspace.list_account_contents(
            account_id, "kuaishou", page=1, page_size=30
        )
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["platform_label"], "快手")
        self.assertEqual(listed["items"][0]["content_id"], "ks-1")
        self.assertEqual(listed["items"][0]["platform_label"], "快手")

        # 快手目前只接入账号作品读取，不能因为修复同步而误把它加入
        # 尚未实现快手发布编辑器的发布平台下拉框。
        self.assertNotIn(
            {"value": "kuaishou", "label": "快手"},
            self.service.list_drafts()["platform_options"],
        )

    def test_profile_target_selection_ignores_bitbrowser_console(self):
        targets = [
            {
                "targetId": "console",
                "type": "page",
                "url": "https://console.bitbrowser.net/?id=window-1",
            },
            {
                "targetId": "editor",
                "type": "page",
                "url": "https://member.bilibili.com/platform/upload/text/new-edit",
            },
            {
                "targetId": "home",
                "type": "page",
                "url": "https://www.bilibili.com/",
            },
            {
                "targetId": "frame",
                "type": "iframe",
                "url": "https://www.bilibili.com/video/BV1abc",
            },
        ]
        selected = select_profile_target(
            targets, "bilibili", "https://www.bilibili.com/"
        )
        self.assertEqual(selected["targetId"], "home")
        self.assertIsNone(select_profile_target(
            [targets[0]], "bilibili", "https://www.bilibili.com/"
        ))

    def test_profile_navigation_must_reach_the_expected_user(self):
        self.assertTrue(_profile_url_reached(
            "https://space.bilibili.com/29299603?tab=video",
            "https://space.bilibili.com/29299603",
            "bilibili",
        ))
        self.assertTrue(_profile_url_reached(
            "https://space.bilibili.com/29299603/upload",
            "https://space.bilibili.com/29299603/upload",
            "bilibili",
        ))
        self.assertFalse(_profile_url_reached(
            "https://space.bilibili.com/29299603",
            "https://space.bilibili.com/29299603/upload",
            "bilibili",
        ))
        self.assertFalse(_profile_url_reached(
            "https://www.bilibili.com/",
            "https://space.bilibili.com/29299603",
            "bilibili",
        ))
        self.assertTrue(_profile_url_reached(
            "https://weibo.com/u/1627088615?tabtype=feed",
            "https://weibo.com/u/1627088615",
            "weibo",
        ))
        self.assertTrue(_profile_url_reached(
            "https://creator.xiaohongshu.com/new/note-manager?source=official",
            PROFILE_ENTRY_URLS["xhs"],
            "xhs",
        ))
        self.assertTrue(_profile_url_reached(
            "https://www.douyin.com/user/self",
            PROFILE_ENTRY_URLS["douyin"],
            "douyin",
        ))
        self.assertTrue(_profile_url_reached(
            "https://www.douyin.com/user/1234567890?from_tab=post",
            PROFILE_ENTRY_URLS["douyin"],
            "douyin",
        ))
        self.assertFalse(_profile_url_reached(
            "https://www.douyin.com/recommend",
            PROFILE_ENTRY_URLS["douyin"],
            "douyin",
        ))
        self.assertFalse(_profile_url_reached(
            "https://www.douyin.com/search/%E6%B4%97%E8%A1%A3",
            PROFILE_ENTRY_URLS["douyin"],
            "douyin",
        ))

    def test_account_content_entries_use_logged_in_account_pages(self):
        self.assertEqual(
            PROFILE_ENTRY_URLS["douyin"],
            "https://www.douyin.com/user/self?from_nav=1",
        )
        self.assertEqual(
            PROFILE_ENTRY_URLS["xhs"],
            "https://creator.xiaohongshu.com/new/note-manager?source=official",
        )

    def test_profile_target_selection_prefers_xhs_creator_manager(self):
        targets = [
            {
                "targetId": "public",
                "type": "page",
                "url": "https://www.xiaohongshu.com/explore",
            },
            {
                "targetId": "creator",
                "type": "page",
                "url": "https://creator.xiaohongshu.com/publish/publish",
            },
            {
                "targetId": "console",
                "type": "page",
                "url": "https://console.bitbrowser.net/?id=window-1",
            },
        ]
        selected = select_profile_target(
            targets, "xhs", PROFILE_ENTRY_URLS["xhs"]
        )
        self.assertEqual(selected["targetId"], "creator")

    def test_profile_reader_scopes_owner_content_and_supports_xhs_cards(self):
        douyin_script = _reader_js("douyin")
        self.assertIn('[data-e2e="user-post-list"]', douyin_script)
        self.assertIn("const root = ownerRoot", douyin_script)
        self.assertIn("profile_scope_found", douyin_script)
        self.assertIn("const safeRoots = platform === 'douyin'", douyin_script)
        self.assertIn("ownerRoot ? [ownerRoot] : []", douyin_script)
        self.assertNotIn("ownerRoot || document.createElement", douyin_script)
        xhs_script = _reader_js("xhs")
        self.assertIn(".note-card", xhs_script)
        self.assertIn("noteTarget", xhs_script)

    def test_preview_command_is_whitelisted(self):
        self.assertIn("preview_publish_draft", COMMANDS)
        self.assertIn("real_publish_draft", COMMANDS)
        self.assertEqual(make_command("preview_publish_draft")["kind"], "command")

    def test_asset_metadata_is_persisted_and_listed(self):
        draft_id = self.service.create_draft(
            title="素材内容", body="正文", platforms=["xhs"]
        )
        path = os.path.join(self.temp.name, "cover.png")
        with open(path, "wb") as stream:
            stream.write(b"not-a-real-image-for-metadata-test")
        ids = self.service.add_assets(
            draft_id, [{"path": path, "asset_type": "image"}]
        )
        self.assertEqual(len(ids), 1)
        row = self.service.list_drafts()["items"][0]
        self.assertEqual(row["asset_count"], 1)
        self.assertEqual(row["assets"][0]["path"], os.path.abspath(path))
        video_path = os.path.join(self.temp.name, "clip.mp4")
        with open(video_path, "wb") as stream:
            stream.write(b"not-a-real-video-for-metadata-test")
        self.service.add_assets(
            draft_id, [{"path": video_path, "asset_type": "image"}]
        )
        row = self.service.list_drafts()["items"][0]
        self.assertEqual(row["assets"][1]["asset_type"], "video")

    def test_workspace_generation_and_import_are_persistent(self):
        item = self.workspace.generate_local("互联网洗衣", "douyin", "选题#1")
        self.assertEqual(item["source_type"], "local_template")
        listed = self.workspace.list_generated()
        self.assertEqual(listed["total"], 1)
        self.assertIn("互联网洗衣", listed["items"][0]["body"])
        draft_id = self.service.create_draft(
            title=item["title"], body=item["body"], platforms=item["platforms"]
        )
        self.assertGreater(draft_id, 0)

    def test_workspace_account_content_and_message_filters(self):
        account_id = db.upsert_account(self.conn, "工作账号", "window-1", "douyin")
        now = "2026-08-29T10:00:00"
        with self.conn:
            self.conn.execute(
                "INSERT INTO account_contents "
                "(account_id, platform, content_id, content_type, url, title, "
                "comment_count, published_at, fetched_at, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, "douyin", "v-1", "video", "https://example.com/v-1",
                 "测试作品", 3, now, now, '{"__account_content_origin":"profile_sync"}'),
            )
            self.conn.execute(
                "INSERT INTO published_messages "
                "(account_id, platform, message_id, nickname, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (account_id, "douyin", "m-1", "访客", "想了解详情", now),
            )
        contents = self.workspace.list_account_contents(account_id, "douyin")
        self.assertEqual(contents["total"], 1)
        self.assertEqual(contents["items"][0]["content_type_label"], "视频")
        messages = self.workspace.list_messages("douyin", account_id, unread_only=True)
        self.assertEqual(messages["total"], 1)
        self.assertEqual(messages["unread"], 1)
        self.workspace.mark_message_read(messages["items"][0]["id"])
        self.assertEqual(self.workspace.list_messages("douyin", account_id, unread_only=True)["total"], 0)

    def test_full_message_normalizer_preserves_categories_and_deduplicates(self):
        raw = [
            {"message_id": "r-1", "message_type": "reply", "nickname": "访客",
             "content": "想了解详情", "quote_content": "原评论", "can_reply": True},
            {"message_id": "r-1", "message_type": "reply", "nickname": "访客",
             "content": "重复消息", "can_reply": True},
            {"message_id": "l-1", "message_type": "like", "nickname": "点赞用户",
             "content": "", "action": "赞了你的评论"},
            {"message_id": "s-1", "message_type": "system", "nickname": "平台",
             "content": "系统通知", "can_reply": False},
        ]
        items = normalize_messages("bilibili", 13, raw, category="消息中心")
        self.assertEqual(len(items), 3)
        self.assertEqual([item["message_type"] for item in items], ["reply", "like", "system"])
        self.assertEqual(items[0]["message_type_label"], "回复")
        self.assertEqual(items[0]["extra"]["quote_content"], "原评论")
        self.assertTrue(items[0]["can_reply"])
        self.assertFalse(items[2]["can_reply"])

    def test_full_message_entry_plan_covers_four_platform_categories(self):
        self.assertEqual(set(MESSAGE_ENTRY_URLS), {"douyin", "xhs", "bilibili", "weibo"})
        self.assertEqual([route["label"] for route in MESSAGE_ROUTES["douyin"]], ["消息", "私信"])
        self.assertTrue({"comment", "like", "follow", "private"}.issubset(
            {route["category"] for route in MESSAGE_ROUTES["xhs"]}
        ))
        self.assertTrue({"reply", "mention", "like", "system", "private"}.issubset(
            {route["category"] for route in MESSAGE_ROUTES["bilibili"]}
        ))
        self.assertTrue({"mention", "comment", "like", "private", "group"}.issubset(
            {route["category"] for route in MESSAGE_ROUTES["weibo"]}
        ))
        script = _message_reader_js("douyin", "interaction")
        self.assertIn('data-e2e="listDlgTest-container"', script)
        self.assertIn("getBoundingClientRect", script)
        self.assertNotIn(".click(", script)
        self.assertIn("conversationConversationItemwrapper", _douyin_conversation_targets_js())
        self.assertIn("r.bottom > innerHeight", _douyin_conversation_targets_js())
        self.assertIn("conversationConversationListwrapper", _douyin_message_scroll_js())
        self.assertIn("componentsRightPanelwrapper", _douyin_detail_reader_js())
        self.assertIn("messageMessageBoxmessageBox", _douyin_detail_reader_js())
        self.assertIn("StackLayoutStackTitleBartitleBar svg", _douyin_back_to_conversations_js())
        self.assertIn("rootRect.left", _douyin_back_to_conversations_js())
        self.assertNotIn("< 980", _douyin_back_to_conversations_js())
        self.assertIn("cursor === 'pointer'", _label_rect_js("消息"))

    def test_workspace_upsert_preserves_read_state_and_exposes_message_fields(self):
        account_id = db.upsert_account(self.conn, "消息账号", "window-message", "xhs")
        first = self.workspace.upsert_messages(account_id, "xhs", normalize_messages(
            "xhs", account_id, [{"message_id": "x-1", "message_type": "reply",
                                  "nickname": "小红薯", "content": "回复内容",
                                  "quote_content": "原评论", "source_url": "https://www.xiaohongshu.com/explore/a"}],
            category="comment",
        ))
        self.assertEqual(first["inserted"], 1)
        message = self.workspace.list_messages("xhs", account_id)["items"][0]
        self.assertEqual(message["message_type_label"], "回复")
        self.assertEqual(message["quote_content"], "原评论")
        self.assertEqual(message["source_url"], "https://www.xiaohongshu.com/explore/a")
        self.workspace.mark_message_read(message["id"])
        second = self.workspace.upsert_messages(account_id, "xhs", normalize_messages(
            "xhs", account_id, [{"message_id": "x-1", "message_type": "reply",
                                  "nickname": "小红薯", "content": "更新后的回复",
                                  "can_reply": True}], category="comment",
        ))
        self.assertEqual(second["updated"], 1)
        refreshed = self.workspace.list_messages("xhs", account_id)["items"][0]
        self.assertTrue(refreshed["is_read"])
        self.assertEqual(refreshed["content"], "更新后的回复")
        listed = self.workspace.list_messages("xhs", account_id)
        self.assertIn({"value": "private", "label": "私信"}, listed["message_type_options"])

    def test_ui_state_groups_messages_into_contextual_conversations(self):
        state = Ui2State()
        state.apply_published_messages({
            "items": [
                {"id": 1, "account_id": 7, "platform": "douyin", "platform_label": "抖音",
                 "account_name": "抖音账号", "user_id": "user-1", "nickname": "访客",
                 "content": "最新消息", "event_time": "2026-08-30T12:00:00", "is_read": False},
                {"id": 2, "account_id": 7, "platform": "douyin", "platform_label": "抖音",
                 "account_name": "抖音账号", "user_id": "user-1", "nickname": "访客",
                 "content": "历史消息", "event_time": "2026-08-29T12:00:00", "is_read": True},
                {"id": 3, "account_id": 7, "platform": "douyin", "platform_label": "抖音",
                 "account_name": "抖音账号", "user_id": "user-2", "nickname": "访客",
                 "content": "另一位同名用户", "event_time": "2026-08-30T11:00:00", "is_read": False},
                {"id": 4, "account_id": 7, "platform": "xhs", "platform_label": "小红书",
                 "account_name": "小红书账号", "user_id": "user-1", "nickname": "访客",
                 "content": "另一个平台", "event_time": "2026-08-30T10:00:00", "is_read": False},
                {"id": 5, "account_id": 7, "platform": "douyin", "platform_label": "抖音",
                 "account_name": "抖音账号", "user_id": "", "nickname": "无ID用户",
                 "content": "没有ID的消息", "event_time": "2026-08-30T09:00:00", "is_read": False},
                {"id": 6, "account_id": 7, "platform": "douyin", "platform_label": "抖音",
                 "account_name": "抖音账号", "user_id": "", "nickname": "无ID用户",
                 "content": "没有ID的历史消息", "event_time": "2026-08-29T09:00:00", "is_read": True},
            ],
            "total": 6,
            "unread": 4,
            "message_type_options": [],
        })
        groups = state.to_view_model()["publishing"]["message_groups"]
        self.assertEqual(len(groups), 4)
        self.assertEqual(groups[0]["display_id"], "user-1")
        self.assertEqual(groups[0]["message_count"], 2)
        self.assertEqual(groups[0]["unread_count"], 1)
        self.assertEqual(groups[0]["rows"][0]["content"], "最新消息")
        self.assertEqual(groups[1]["display_id"], "user-2")
        self.assertEqual(groups[2]["platform"], "xhs")
        self.assertEqual(groups[3]["display_id"], "未获取ID")
        self.assertEqual(groups[3]["message_count"], 2)

    def test_workspace_sync_reuses_collected_videos_and_comments(self):
        account_id = db.upsert_account(self.conn, "采集账号", "window-2", "douyin")
        task_id = db.create_task(self.conn, "测试关键词", platform="douyin", task_accounts=["采集账号"])
        video_id = db.insert_video(
            self.conn, task_id, "video-1", "https://example.com/video-1",
            title="已采集作品", author="作品作者", platform="douyin",
        )
        db.mark_videos_assigned(self.conn, task_id, "采集账号", ["video-1"])
        db.insert_comment(
            self.conn, video_id, "u-1", "访客", "想了解价格", "2026-08-29",
            platform="douyin",
        )
        now = "2026-08-29T10:00:00"
        with self.conn:
            self.conn.execute(
                "INSERT INTO account_contents "
                "(account_id, platform, content_id, content_type, title, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (account_id, "douyin", "stale-assigned-video", "video", "旧版错误缓存", now),
            )
        # 被分配给账号采集，不等于账号本人发布；不能展示到账号信息页。
        self.assertEqual(self.workspace.seed_account_contents(account_id, "douyin"), 0)
        self.assertEqual(
            self.workspace.list_account_contents(account_id, "douyin")["total"], 0
        )
        own_video_id = db.insert_video(
            self.conn, task_id, "video-2", "https://example.com/video-2",
            title="账号自己的作品", author="采集账号", platform="douyin",
        )
        db.insert_comment(
            self.conn, own_video_id, "u-2", "访客2", "想了解账号作品", "2026-08-29",
            platform="douyin",
        )
        self.assertEqual(self.workspace.seed_account_contents(account_id, "douyin"), 1)
        content = self.workspace.list_account_contents(account_id, "douyin")["items"][0]
        comments = self.workspace.list_content_comments(content["id"])
        self.assertEqual(comments["total"], 1)
        self.assertEqual(comments["items"][0]["content"], "想了解账号作品")

    def test_profile_sync_replaces_only_the_profile_cache(self):
        account_id = db.upsert_account(self.conn, "主页账号", "window-profile", "douyin")
        self.workspace.replace_profile_contents(account_id, "douyin", [{
            "content_id": "profile-1", "url": "https://www.douyin.com/video/profile-1",
            "title": "主页作品", "content_type": "video", "like_count": 12,
        }], profile_url="https://www.douyin.com/user/self")
        first = self.workspace.list_account_contents(account_id, "douyin")
        self.assertEqual(first["total"], 1)
        self.assertEqual(first["items"][0]["extra"]["__account_content_origin"], "profile_sync")
        self.workspace.replace_profile_contents(account_id, "douyin", [{
            "content_id": "profile-2", "url": "https://www.douyin.com/video/profile-2",
            "title": "最新主页作品", "content_type": "video",
        }])
        second = self.workspace.list_account_contents(account_id, "douyin")
        self.assertEqual(second["total"], 1)
        self.assertEqual(second["items"][0]["content_id"], "profile-2")

    def test_workspace_schedule_validates_platform_account(self):
        account_id = db.upsert_account(self.conn, "小红书账号", "window-x", "xhs")
        draft_id = self.service.create_draft(
            title="定时内容", body="正文", platforms=["xhs"]
        )
        future = (beijing_now() + timedelta(days=1)).replace(second=0)
        job_id = self.service.schedule_variant(
            draft_id=draft_id, platform="xhs", account_id=account_id,
            scheduled_at=future.strftime("%Y-%m-%d %H:%M"),
        )
        self.assertGreater(job_id, 0)
        self.assertEqual(self.service.list_drafts(status="queued")["total"], 1)
        stored = self.conn.execute(
            "SELECT scheduled_at, current_step FROM publish_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        self.assertEqual(stored["scheduled_at"], future.isoformat(timespec="seconds"))
        self.assertEqual(stored["current_step"], "scheduled")
        with self.assertRaisesRegex(ValueError, "晚于当前北京时间"):
            self.service.schedule_variant(
                draft_id=draft_id, platform="xhs", account_id=account_id,
                scheduled_at="2026-01-01 10:30",
            )
        with self.assertRaises(ValueError):
            self.service.schedule_variant(
                draft_id=draft_id, platform="douyin", account_id=account_id
            )


if __name__ == "__main__":
    unittest.main()
