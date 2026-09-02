# -*- coding: utf-8 -*-
import os
import tempfile
import unittest

from src.operation_log import OperationLog


class OperationLogTests(unittest.TestCase):
    def test_append_reload_and_export(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "operation_events.jsonl")
            store = OperationLog(path)
            store.append("任务已开始")
            store.append("浏览器连接成功")

            reloaded = OperationLog(path)
            records = reloaded.recent()
            self.assertEqual([r["message"] for r in records], ["任务已开始", "浏览器连接成功"])

            target = os.path.join(folder, "导出日志.txt")
            exported = reloaded.export_text(target)
            self.assertEqual(exported, os.path.abspath(target))
            with open(target, encoding="utf-8") as stream:
                text = stream.read()
            self.assertIn("多平台采集工作台后台操作日志", text)
            self.assertIn("任务已开始", text)
            self.assertIn("浏览器连接成功", text)

    def test_memory_limit_keeps_recent_records(self):
        with tempfile.TemporaryDirectory() as folder:
            store = OperationLog(os.path.join(folder, "events.jsonl"), memory_limit=100)
            for index in range(105):
                store.append(f"事件 {index}")
            records = store.recent(200)
            self.assertEqual(len(records), 100)
            self.assertEqual(records[0]["message"], "事件 5")

    def test_structured_details_and_hourly_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "events.jsonl")
            store = OperationLog(path)
            record = store.append(
                "点击回复按钮",
                source="cdp",
                event="native_click",
                action="mouse_click",
                outcome="success",
                duration_ms=12.34,
                run_id="run-1",
                details={"x": 120, "y": 240, "selector": "button.send"},
            )
            self.assertEqual(record["event"], "native_click")
            self.assertEqual(record["details"]["x"], 120)
            self.assertIn("native_click", OperationLog.format_record(record))

            snapshot = store.save_hourly_snapshot(folder)
            self.assertTrue(os.path.exists(snapshot["jsonl"]))
            self.assertTrue(os.path.exists(snapshot["text"]))
            self.assertEqual(snapshot["count"], 1)
            with open(snapshot["jsonl"], encoding="utf-8") as stream:
                self.assertIn("mouse_click", stream.read())


if __name__ == "__main__":
    unittest.main()
