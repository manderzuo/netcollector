# -*- coding: utf-8 -*-
"""导出层有效数据过滤回归测试。"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from export_report import export_unified, filter_valid_items  # noqa: E402


class TestExportReport(unittest.TestCase):
    def test_invalid_title_and_empty_comment_are_not_exported(self):
        items = [
            {"kind": "douyin", "pid": "ok", "title": "有效作品",
             "url": "https://example.test/ok", "comments": [
                 {"text": "有效评论", "uid": "u1"},
                 {"text": "   ", "uid": "u2"},
             ]},
            {"kind": "douyin", "pid": "bad-title", "title": " ",
             "url": "https://example.test/bad", "comments": [
                 {"text": "不应导出", "uid": "u3"},
             ]},
        ]
        cleaned = filter_valid_items(items)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(len(cleaned[0]["comments"]), 1)
        with tempfile.TemporaryDirectory() as directory:
            result = export_unified(items, directory, "task-1")
            self.assertEqual(result["n_videos"], 1)
            self.assertEqual(result["n_comments"], 1)
            with open(result["path"], encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1][12], "有效评论")


if __name__ == "__main__":
    unittest.main()
