# -*- coding: utf-8 -*-
"""小红书评论采集脚本的回归检查。"""

import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class XhsCommentCollectionTests(unittest.TestCase):
    def test_comment_script_excludes_author_badge_and_uses_content_node(self):
        path = os.path.join(ROOT, "src", "xhs_collect3.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()

        self.assertIn("const contentEl = n.querySelector", source)
        self.assertIn(".comment-mainContent", source)
        self.assertIn("const metadataLabels = new Set", source)
        self.assertIn("'作者'", source)
        self.assertIn("'作者回复'", source)
        self.assertIn("metadataLabels.has(s)", source)

    def test_xhs_collection_expands_nested_replies_before_scrolling(self):
        path = os.path.join(ROOT, "src", "xhs_collect3.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()

        self.assertIn("expand_replies_js", source)
        self.assertIn("展开|查看|显示|更多", source)
        self.assertIn("条\\s*(?:回复|评论)", source)
        self.assertIn("await c.eval(expand_replies_js, sid)", source)

    def test_other_platform_collectors_expand_nested_replies(self):
        for filename, marker in (
            ("weibo_chrome.py", "_EXPAND_REPLIES_JS"),
            ("bilibili_adapter.py", "_EXPAND_REPLIES_JS"),
        ):
            path = os.path.join(ROOT, "src", filename)
            with open(path, encoding="utf-8") as fh:
                source = fh.read()
            self.assertIn(marker, source)
            self.assertIn("expandedCount", source)
            self.assertIn("没有更多", source)
            self.assertIn("await c.eval(_EXPAND_REPLIES_JS, sid)", source)


if __name__ == "__main__":
    unittest.main()
