# -*- coding: utf-8 -*-
"""快手适配器的离线回归测试。

测试只覆盖接口响应解析、去重和页面脚本契约，不打开浏览器、不登录、
不点击评论或发布控件。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import kuaishou_collect  # noqa: E402
from kuaishou_comment_fields import normalize_kuaishou_dom_fields  # noqa: E402
import live_collector  # noqa: E402
from publishing.account_content_reader import (  # noqa: E402
    CONTENT_PATTERNS,
    PROFILE_ENTRY_URLS,
    normalize_profile_items,
)


class KuaishouCollectTests(unittest.TestCase):
    def test_dom_comment_fields_split_nickname_time_and_content(self):
        nickname, content, comment_time = normalize_kuaishou_dom_fields(
            "幸福毛孩生活馆 2周前",
            "说半天地地址了",
            "2周前",
        )
        self.assertEqual(nickname, "幸福毛孩生活馆")
        self.assertEqual(content, "说半天地地址了")
        self.assertEqual(comment_time, "2周前")

    def test_dom_comment_fields_reject_ui_metadata_only_rows(self):
        nickname, content, comment_time = normalize_kuaishou_dom_fields(
            "匿名用户",
            "2月前",
            "",
        )
        self.assertEqual(nickname, "匿名用户")
        self.assertEqual(content, "")
        self.assertEqual(comment_time, "2月前")

    def test_dom_comment_fields_keep_non_text_placeholder(self):
        self.assertEqual(
            normalize_kuaishou_dom_fields("用户A 3天前", "[表情]", "3天前"),
            ("用户A", "[表情]", "3天前"),
        )

    def test_search_navigates_from_recommendation_before_block_probe(self):
        """推荐页的预加载文案不能阻止任务进入真实搜索页。"""

        class FakeSession:
            def __init__(self):
                self.navigated = False
                self.navigation_urls = []
                self.probe_before_navigation = False

            async def cmd(self, *_args, **_kwargs):
                return {}

            def on(self, *_args, **_kwargs):
                return lambda: None

            async def navigate(self, url, *_args, **_kwargs):
                self.navigated = True
                self.navigation_urls.append(url)

            async def get_body(self, *_args, **_kwargs):
                return "{}"

            async def eval(self, script, *_args, **_kwargs):
                if script == kuaishou_collect._blocked_probe_js():
                    if not self.navigated:
                        self.probe_before_navigation = True
                        # 模拟推荐页里存在会触发旧逻辑的宽泛验证文案。
                        return {"challenge": True, "login": False}
                    return {"challenge": False, "login": False}
                if script == "location.href":
                    return (
                        self.navigation_urls[-1]
                        if self.navigation_urls
                        else "https://www.kuaishou.com/new-reco"
                    )
                if script == kuaishou_collect._SEARCH_DOM_JS:
                    return {
                        "items": [{
                            "vid": "photo-1",
                            "url": "https://www.kuaishou.com/short-video/photo-1",
                            "title": "搜索结果",
                            "author": "作者",
                            "kind": "video",
                        }],
                    }
                if script == kuaishou_collect._SEARCH_STATE_JS:
                    return {
                        "visible": 1, "top": 0, "height": 1000,
                        "viewport": 800, "atBottom": False,
                        "endText": False, "endMessage": "",
                    }
                return {}

        session = FakeSession()
        result = asyncio.run(kuaishou_collect.search_videos(
            session, "sid", "郑州幼儿园", target_count=1
        ))

        self.assertFalse(session.probe_before_navigation)
        self.assertEqual(len(session.navigation_urls), 1)
        self.assertTrue(session.navigation_urls[0].startswith(
            kuaishou_collect.SEARCH_URL_PREFIX
        ))
        self.assertEqual(len(result), 1)

    def test_search_reports_navigation_failure_instead_of_fake_captcha(self):
        class FakeSession:
            async def cmd(self, *_args, **_kwargs):
                return {}

            def on(self, *_args, **_kwargs):
                return lambda: None

            async def navigate(self, *_args, **_kwargs):
                return None

            async def eval(self, script, *_args, **_kwargs):
                if script == "location.href":
                    return "https://www.kuaishou.com/new-reco"
                if script == kuaishou_collect._blocked_probe_js():
                    return {"challenge": False, "login": False}
                return {}

            async def get_body(self, *_args, **_kwargs):
                return "{}"

        with self.assertRaisesRegex(RuntimeError, "搜索页导航未生效"):
            asyncio.run(kuaishou_collect.search_videos(
                FakeSession(), "sid", "郑州幼儿园", target_count=1
            ))

    def test_search_payload_extracts_nested_photo_and_deduplicates(self):
        payload = {
            "searchResult": [
                {
                    "id": "feed-wrapper-id",
                    "photo": {
                        "id": "photo-1",
                        "caption": "第一条作品",
                        "user": {"id": "user-1", "name": "作者一"},
                        "playUrl": "https://cdn.example/1.mp4",
                    },
                },
                {
                    "photo": {
                        "id": "photo-1",
                        "caption": "重复作品",
                        "user": {"id": "user-1", "name": "作者一"},
                    },
                },
                {
                    "photoId": "photo-2",
                    "title": "第二条作品",
                    "author": {"id": "user-2", "name": "作者二"},
                    "coverUrl": "https://cdn.example/2.jpg",
                },
            ]
        }

        items = kuaishou_collect.parse_search_payload(payload)

        self.assertEqual([item["vid"] for item in items], ["photo-1", "photo-2"])
        self.assertEqual(items[0]["title"], "第一条作品")
        self.assertEqual(items[0]["author"], "作者一")
        self.assertEqual(items[0]["url"], "https://www.kuaishou.com/short-video/photo-1")

    def test_comment_payload_keeps_root_and_nested_comments(self):
        payload = {
            "rootCommentsV2": [
                {
                    "commentId": "root-1",
                    "content": "一级评论",
                    "author": {"id": "user-1", "name": "评论者一"},
                    "subCommentCount": 1,
                }
            ],
            "subCommentsV2": [
                {
                    "commentId": "sub-1",
                    "content": "楼中楼回复",
                    "rootCommentId": "root-1",
                    "author": {"id": "user-2", "name": "评论者二"},
                }
            ],
        }

        items = kuaishou_collect.parse_comment_payload(payload)

        self.assertEqual({item["cid"] for item in items}, {"root-1", "sub-1"})
        nested = next(item for item in items if item["cid"] == "sub-1")
        self.assertEqual(nested["parent_id"], "root-1")
        self.assertEqual(nested["nickname"], "评论者二")

    def test_comment_payload_accepts_single_comment_info_and_route_variants(self):
        payload = {
            "commentInfo": {
                "id": "single-1",
                "commentText": "单条接口评论",
                "userInfo": {"userId": "u-1", "userName": "用户一"},
                "createTime": "2026-09-01 11:43",
            },
            "commentFeeds": [{
                "cid": "feed-1",
                "text": "评论流内容",
                "user": {"id": "u-2", "name": "用户二"},
            }],
        }

        items = kuaishou_collect.parse_comment_payload(payload)

        self.assertEqual({item["cid"] for item in items}, {"single-1", "feed-1"})
        self.assertEqual(
            next(item for item in items if item["cid"] == "single-1")["text"],
            "单条接口评论",
        )
        self.assertTrue(kuaishou_collect._is_comment_response_url(
            "https://www.kuaishou.com/rest/v/photo/comment/list?photoId=1"
        ))
        self.assertTrue(kuaishou_collect._is_comment_response_url(
            "https://api.kuaishou.com/rest/v/comment/sublist?rootCommentId=1"
        ))
        self.assertFalse(kuaishou_collect._is_comment_response_url(
            "https://example.com/rest/v/photo/comment/list"
        ))

    def test_fetch_comments_waits_for_async_response_and_uses_dom_fallback(self):
        class FakeSession:
            def __init__(self, *, with_response):
                self.callback = None
                self.with_response = with_response
                self.body_calls = []

            async def cmd(self, *_args, **_kwargs):
                return {}

            def on(self, method, callback):
                if method == "Network.responseReceived":
                    self.callback = callback
                return lambda: None

            async def navigate(self, *_args, **_kwargs):
                if self.with_response and self.callback:
                    self.callback({
                        "response": {
                            "url": "https://www.kuaishou.com/rest/v/photo/comment/list"
                        },
                        "requestId": "comment-1",
                    })

            async def get_body(self, request_id, *_args, **_kwargs):
                self.body_calls.append(request_id)
                return (
                    '{"rootComments":[{"commentId":"net-1",'
                    '"content":"网络评论","author":{"id":"u-1","name":"网络用户"}}]}'
                )

            async def eval(self, script, *_args, **_kwargs):
                if script == kuaishou_collect._blocked_probe_js():
                    return {"challenge": False, "login": False}
                if script == kuaishou_collect._OPEN_COMMENT_JS:
                    return {"ok": True, "text": "评论"}
                if script == kuaishou_collect._EXPAND_SUBCOMMENTS_JS:
                    return {"clicked": 0}
                if script == kuaishou_collect._COMMENT_SCROLL_JS:
                    return {"found": True, "bottom": True, "endText": True}
                if script == kuaishou_collect._COMMENT_DOM_JS:
                    return {"panelFound": True, "rowCount": 0, "comments": []}
                return {}

        async def no_wait(_seconds):
            return None

        with patch.object(kuaishou_collect, "_COMMENT_INITIAL_WAIT_SECONDS", 0.01), \
                patch.object(kuaishou_collect, "_COMMENT_SETTLE_SECONDS", 0.0), \
                patch.object(kuaishou_collect.asyncio, "sleep", new=no_wait):
            network = FakeSession(with_response=True)
            network_items = asyncio.run(kuaishou_collect.fetch_comments(
                network, "sid", "https://www.kuaishou.com/short-video/photo-1",
                quiet=0, max_work=20,
            ))

            dom = FakeSession(with_response=False)
            dom.eval = lambda script, *_args, **_kwargs: None  # replaced below

            async def dom_eval(script, *_args, **_kwargs):
                if script == kuaishou_collect._blocked_probe_js():
                    return {"challenge": False, "login": False}
                if script == kuaishou_collect._OPEN_COMMENT_JS:
                    return {"ok": True, "text": "评论"}
                if script == kuaishou_collect._EXPAND_SUBCOMMENTS_JS:
                    return {"clicked": 0}
                if script == kuaishou_collect._COMMENT_SCROLL_JS:
                    return {"found": True, "bottom": True, "endText": True}
                if script == kuaishou_collect._COMMENT_DOM_JS:
                    return {"panelFound": True, "rowCount": 1, "comments": [{
                        "cid": "dom-1", "text": "页面渲染评论",
                        "user_id": "u-2", "nickname": "页面用户",
                        "create_time_str": "刚刚", "parent_id": "",
                    }]}
                return {}

            dom.eval = dom_eval
            dom_items = asyncio.run(kuaishou_collect.fetch_comments(
                dom, "sid", "https://www.kuaishou.com/short-video/photo-2",
                quiet=0, max_work=20,
            ))

            missing = FakeSession(with_response=False)

            async def missing_eval(script, *_args, **_kwargs):
                if script == kuaishou_collect._blocked_probe_js():
                    return {"challenge": False, "login": False}
                if script == kuaishou_collect._OPEN_COMMENT_JS:
                    return {"ok": False, "reason": "comment_button_not_found"}
                return {}

            missing.eval = missing_eval
            with self.assertRaisesRegex(RuntimeError, "评论区入口未定位到"):
                asyncio.run(kuaishou_collect.fetch_comments(
                    missing, "sid", "https://www.kuaishou.com/short-video/photo-3",
                    quiet=0, max_work=20,
                ))

        self.assertEqual([item["cid"] for item in network_items], ["net-1"])
        self.assertEqual(network.body_calls, ["comment-1"])
        self.assertEqual([item["cid"] for item in dom_items], ["dom-1"])

    def test_page_scripts_cover_search_scroll_comments_and_nested_replies(self):
        import inspect
        self.assertIn("/rest/v/search/feed", inspect.getsource(kuaishou_collect.search_videos))
        source = inspect.getsource(kuaishou_collect.fetch_comments)
        self.assertIn("/rest/v/photo/comment/", source)
        self.assertIn("comment/list", inspect.getsource(kuaishou_collect))
        self.assertIn(".comment-list", kuaishou_collect._COMMENT_SCROLL_JS)
        self.assertIn(".comment-root", kuaishou_collect._EXPAND_SUBCOMMENTS_JS)
        self.assertIn("span.expand", kuaishou_collect._EXPAND_SUBCOMMENTS_JS)
        self.assertIn("already_open", kuaishou_collect._OPEN_COMMENT_JS)
        self.assertIn("diagnostic", kuaishou_collect._OPEN_COMMENT_JS)
        self.assertNotIn(".comment-mainContent,',", kuaishou_collect._COMMENT_DOM_JS)
        self.assertIn("/search/video?searchKey=", kuaishou_collect.SEARCH_URL_PREFIX)

    def test_collector_and_account_metadata_register_kuaishou(self):
        self.assertEqual(live_collector.PLATFORM_ALIAS["ks"], "kuaishou")
        self.assertEqual(live_collector.LiveCollector._WINDOW_FILES["kuaishou"], "kuaishou_window.txt")
        self.assertIn("kuaishou", PROFILE_ENTRY_URLS)
        self.assertIn("kuaishou", CONTENT_PATTERNS)
        rows = normalize_profile_items(
            "kuaishou",
            [
                {
                    "url": "https://www.kuaishou.com/short-video/photo-9",
                    "title": "快手作品",
                    "like_count": "1.2万",
                },
                {
                    "url": "https://www.kuaishou.com/short-video/photo-9",
                    "title": "重复",
                },
            ],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content_id"], "photo-9")
        self.assertEqual(rows[0]["content_type"], "video")


if __name__ == "__main__":
    unittest.main()
