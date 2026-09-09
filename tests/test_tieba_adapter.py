# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

from src.tieba_adapter import (
    TiebaApiClient,
    TiebaApiCollector,
    TiebaApiError,
    parse_post_list,
    parse_thread_list,
)


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class TiebaAdapterTests(unittest.TestCase):
    def test_thread_list_is_filtered_and_deduplicated(self):
        rows = parse_thread_list({"data": {"thread_list": [
            {"thread_id": "1", "title": "郑州早教", "abstract": "课程咨询",
             "author": {"id": "u1", "name": "小王"}},
            {"thread_id": "1", "title": "重复帖子"},
            {"thread_id": "2", "title": "无关内容"},
        ]}}, "早教")
        self.assertEqual([item["vid"] for item in rows], ["1"])
        self.assertEqual(rows[0]["extra"]["platform_user_id"], "u1")
        self.assertEqual(rows[0]["url"], "https://tieba.baidu.com/p/1")

    def test_posts_keep_nested_replies_and_skip_empty(self):
        rows = parse_post_list({"post_list": [
            {"post_id": "p1", "author": {"portrait": "u1", "name": "甲"},
             "content": [{"text": "主楼内容"}], "sub_post_list": [
                 {"post_id": "p2", "author": {"uid": "u2", "nickname": "乙"},
                  "content": "楼中楼回复"},
             ]},
            {"post_id": "empty", "content": ""},
        ]}, "t1")
        self.assertEqual([row["cid"] for row in rows], ["p1", "p2"])
        self.assertEqual(rows[1]["parent_id"], "p1")
        self.assertTrue(rows[1]["extra"]["is_reply"])

    def test_client_rejects_non_official_endpoint(self):
        with self.assertRaises(ValueError):
            TiebaApiClient("token", base_url="https://example.com")

    def test_client_uses_token_only_for_official_request(self):
        captured = {}

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["auth"] = request.headers.get("Authorization")
            captured["timeout"] = timeout
            return _Response({"data": {"thread_list": []}})

        with patch("src.tieba_adapter.urllib.request.urlopen", fake_open):
            result = TiebaApiClient("secret-token").list_threads()
        self.assertEqual(result["data"]["thread_list"], [])
        self.assertEqual(captured["auth"], "secret-token")
        self.assertEqual(captured["url"].split("?")[0], "https://tieba.baidu.com/c/f/frs/page_claw")

    def test_missing_token_is_explicit(self):
        with self.assertRaisesRegex(TiebaApiError, "TB_TOKEN"):
            TiebaApiClient("").list_threads()

    def test_collector_search_maps_hot_sort(self):
        collector = TiebaApiCollector("token")
        with patch.object(collector, "_client") as make_client:
            make_client.return_value.list_threads.return_value = {"data": {"thread_list": [
                {"thread_id": "1", "title": "洗鞋店", "abstract": "价格"},
            ]}}
            result = collector.search("洗鞋", search_sort="hot", target_count=10)
        make_client.return_value.list_threads.assert_called_once_with(sort_type=3)
        self.assertEqual(len(result), 1)
        self.assertTrue(result.no_more_results)

    def test_collector_fetch_comments_paginates_and_deduplicates(self):
        collector = TiebaApiCollector("token")
        with patch.object(collector, "_client") as make_client:
            make_client.return_value.thread_page.side_effect = [
                {"page": {"has_more": 1}, "post_list": [
                    {"id": "p1", "content": "第一页"},
                ]},
                {"page": {"has_more": 0}, "post_list": [
                    {"id": "p1", "content": "重复楼层"},
                    {"id": "p2", "content": "第二页"},
                ]},
            ]
            rows = collector.fetch_comments("thread-1")
        self.assertEqual([row["cid"] for row in rows], ["p1", "p2"])
        self.assertEqual(make_client.return_value.thread_page.call_count, 2)


if __name__ == "__main__":
    unittest.main()
