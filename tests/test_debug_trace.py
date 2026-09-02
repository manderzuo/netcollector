# -*- coding: utf-8 -*-
"""结构化调试追踪日志回归测试。"""

import json
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, os.path.join(ROOT, "src"))

from debug_trace import DebugTrace, register_trace_sink, unregister_trace_sink
from interactions.browser_reply import _same_url


class TestDebugTrace(unittest.TestCase):
    def test_writes_ordered_jsonl_with_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trace.jsonl")
            trace = DebugTrace("unit_test", log_path=path, run_id="run-test-001")
            first = trace.emit("started", target={"nickname": "小王"})
            second = trace.emit("feedback", values=[1, "二"])

            with open(path, "r", encoding="utf-8") as stream:
                rows = [json.loads(line) for line in stream if line.strip()]

            self.assertEqual(first["run_id"], "run-test-001")
            self.assertEqual(second["sequence"], 2)
            self.assertEqual([row["event"] for row in rows], ["started", "feedback"])
            self.assertEqual([row["sequence"] for row in rows], [1, 2])
            self.assertEqual(rows[0]["fields"]["target"]["nickname"], "小王")

    def test_exception_keeps_error_feedback_in_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trace.jsonl")
            trace = DebugTrace("unit_test", log_path=path)
            try:
                raise RuntimeError("用于重现的异常")
            except RuntimeError as exc:
                trace.exception("operation_failed", exc, operation="demo")

            with open(path, "r", encoding="utf-8") as stream:
                row = json.loads(stream.readline())
            self.assertEqual(row["event"], "operation_failed")
            self.assertEqual(row["fields"]["exception_type"], "RuntimeError")
            self.assertIn("用于重现的异常", row["fields"]["traceback"])

    def test_same_url_accepts_platform_query_reencoding(self):
        encoded = (
            "https://www.xiaohongshu.com/explore/note-1?xsec_token=abc%3D"
            "&xsec_source=pc_search"
        )
        normalized = (
            "https://www.xiaohongshu.com/explore/note-1?xsec_token=abc="
            "&xsec_source=pc_search"
        )
        self.assertTrue(_same_url(encoded, normalized))

    def test_trace_sink_receives_structured_event_without_breaking_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trace.jsonl")
            received = []

            def sink(record):
                received.append(record)

            register_trace_sink(sink)
            try:
                trace = DebugTrace("unit_test_sink", log_path=path)
                trace.emit("button_click", x=88, y=99)
            finally:
                unregister_trace_sink(sink)

            self.assertEqual(received[0]["event"], "button_click")
            self.assertEqual(received[0]["fields"]["x"], 88)


if __name__ == "__main__":
    unittest.main()
