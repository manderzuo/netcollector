# -*- coding: utf-8 -*-
"""BitBrowser 设置页诊断逻辑测试。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bitbrowser_inspector import BitBrowserInspector


class FakeBitBrowser:
    def health(self):
        return "the server is running good."

    def list_browsers(self, **_kwargs):
        return {
            "page": 0,
            "pageSize": 100,
            "list": [
                {"id": "window-1", "name": "抖音账号", "platform": "douyin"},
                {"browserId": "window-2", "remark": "小红书账号", "platform": "xhs"},
            ],
        }

    def ports(self):
        return {"data": {"window-1": "9222"}}

    def pids_all(self):
        return {"window-1": 12345}


class NestedFakeBitBrowser(FakeBitBrowser):
    def list_browsers(self, **_kwargs):
        return {"data": {"list": [{"id": "window-3", "name": "嵌套返回"}]}}

    def ports(self):
        return {"data": {"ports": {"window-3": 9333}}}

    def pids_all(self):
        return {"data": {"pids": {"window-3": 4567}}}


class TestBitBrowserInspector(unittest.TestCase):
    def test_reads_window_port_and_process_without_opening_window(self):
        result = BitBrowserInspector(FakeBitBrowser()).inspect()

        self.assertTrue(result["healthy"])
        self.assertEqual([item["name"] for item in result["checks"]],
                         ["本地服务", "窗口列表", "调试端口", "浏览器进程"])
        self.assertTrue(all(item["status"] == "正常" for item in result["checks"]))
        self.assertEqual(len(result["windows"]), 2)
        first, second = result["windows"]
        self.assertEqual(first["id"], "window-1")
        self.assertEqual(first["port"], "9222")
        self.assertEqual(first["pid"], 12345)
        self.assertTrue(first["opened"])
        self.assertEqual(second["id"], "window-2")
        self.assertFalse(second["opened"])

    def test_accepts_nested_bitbrowser_response_shape(self):
        result = BitBrowserInspector(NestedFakeBitBrowser()).inspect()
        self.assertEqual(result["windows"][0]["port"], "9333")
        self.assertEqual(result["windows"][0]["pid"], 4567)
        self.assertTrue(result["windows"][0]["opened"])


if __name__ == "__main__":
    unittest.main()
