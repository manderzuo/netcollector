"""账号昵称读取的无浏览器回归测试。"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from account_reader import (  # noqa: E402
    _clean_nickname,
    _normalise_expected_uid,
    looks_like_account_key,
    READERS,
    read_bilibili_profile,
    read_douyin,
    read_kuaishou_profile,
    read_weibo_profile,
    read_xhs_profile,
)


class TestAccountReader(unittest.TestCase):
    def test_clean_nickname_rejects_numeric_id_and_ui_labels(self):
        self.assertEqual(_clean_nickname("1234567899"), "")
        self.assertEqual(
            _clean_nickname("1234567899", allow_numeric=True), "1234567899"
        )
        self.assertEqual(_clean_nickname("  @真实昵称  "), "真实昵称")
        self.assertEqual(_clean_nickname("登录"), "")

    def test_account_key_detection_covers_alphanumeric_window_names(self):
        self.assertTrue(looks_like_account_key("1234567899"))
        self.assertTrue(looks_like_account_key("12345678a"))
        self.assertTrue(looks_like_account_key("60b5ad980000000001000a58"))
        self.assertTrue(looks_like_account_key("抖音号：dyu431gedsv7"))
        self.assertFalse(looks_like_account_key("我的抖音账号"))

    def test_expected_uid_only_accepts_platform_identity_formats(self):
        self.assertEqual(
            _normalise_expected_uid("weibo", "1627088615"), "1627088615"
        )
        self.assertEqual(
            _normalise_expected_uid("xhs", "60b5ad980000000001000a58"),
            "60b5ad980000000001000a58",
        )
        self.assertEqual(_normalise_expected_uid("kuaishou", "快手01"), "")
        self.assertEqual(_normalise_expected_uid("douyin", "12345678a"), "")

    def test_douyin_reader_script_uses_semantic_selectors(self):
        # 不连接真实浏览器，只验证定位策略没有退回固定窗口坐标。
        self.assertTrue(callable(read_douyin))
        source = Path(ROOT, "src", "account_reader.py").read_text(encoding="utf-8")
        self.assertIn('[data-e2e*="user-name"]', source)
        self.assertNotIn("r.left>=280 && r.left<=560", source)
        self.assertNotIn("r.top>=75 && r.top<=140", source)

    def test_platform_readers_use_the_annotated_account_entry_points(self):
        source = Path(ROOT, "src", "account_reader.py").read_text(encoding="utf-8")
        self.assertIs(READERS["xhs"], read_xhs_profile)
        self.assertIs(READERS["weibo"], read_weibo_profile)
        self.assertIs(READERS["bilibili"], read_bilibili_profile)
        self.assertIs(READERS["kuaishou"], read_kuaishou_profile)
        self.assertIn("creator.xiaohongshu.com/publish/publish?source=official", source)
        self.assertIn("'.user-info'", source)
        self.assertIn('[class*="user-info"]', source)
        self.assertIn('"https://weibo.com/"', source)
        self.assertIn('"https://www.bilibili.com/"', source)
        self.assertIn('"https://www.kuaishou.com/new-reco"', source)
        self.assertIn("Input.dispatchMouseEvent", source)
        self.assertIn("左下角固定账号入口", source)
        self.assertIn("头像菜单", source)
        self.assertIn("expected_uid=expected_uid", source)
        self.assertIn("profile-scope", source)
        self.assertIn("bottom-left-panel", source)
        self.assertIn("recommendationAncestor", source)
        self.assertIn("top-account-entry", source)
        self.assertNotIn("const chosen = top[0] || links[0]", source)
        self.assertIn("未找到微博中间个人资料区域", source)
        # 微博不能再从整页 a/span/div/p 文本中取第一个候选，否则会抓到
        # 推荐卡片或其他用户昵称。
        self.assertNotIn("document.querySelectorAll('a,span,div,p')", source)


if __name__ == "__main__":
    unittest.main()
