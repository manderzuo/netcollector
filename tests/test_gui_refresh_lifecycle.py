# -*- coding: utf-8 -*-
"""GUI 刷新生命周期回归测试。

这些测试不创建真实 Tk 窗口，只验证后台线程不会直接触碰 ``root.after``，
以及手动刷新不会继续创建短命状态线程。
"""
import os
import queue
import sys
import threading
import time
import unittest
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from gui import GuiApp  # noqa: E402


class _FakeRoot:
    def __init__(self):
        self.after_calls = []

    def after(self, delay, callback):
        self.after_calls.append((threading.current_thread(), delay, callback))
        return len(self.after_calls)


class TestGuiRefreshLifecycle(unittest.TestCase):
    @staticmethod
    def _app():
        app = GuiApp.__new__(GuiApp)
        app._closing = False
        app._ui_dispatch_queue = deque()
        app._ui_dispatch_queue_lock = threading.Lock()
        app._ui_dispatch_limit = 20
        app._ui_dispatch_batch_size = 20
        app._ui_dispatch_dropped = 0
        app._ui_dispatch_after = None
        app.root = _FakeRoot()
        return app

    def test_background_ui_call_never_calls_tk(self):
        app = self._app()
        called = []

        worker = threading.Thread(
            target=lambda: app._ui_call(lambda: called.append("done")),
            name="test-background-ui-call",
        )
        worker.start()
        worker.join()

        self.assertEqual(app.root.after_calls, [])
        app._drain_ui_dispatch_queue()
        self.assertEqual(called, ["done"])
        self.assertEqual(len(app.root.after_calls), 1)
        self.assertIs(app.root.after_calls[0][0], threading.current_thread())

    def test_manual_refresh_only_sets_request_event(self):
        app = self._app()
        app._refresh_requested = threading.Event()
        app.refresh_all()
        self.assertTrue(app._refresh_requested.is_set())
        self.assertFalse(hasattr(app, "_refresh_threads"))

    def test_ui_audit_is_queued_without_synchronous_file_write(self):
        app = GuiApp.__new__(GuiApp)
        app._audit_queue = queue.Queue(maxsize=2)
        app._audit_dropped = 0
        started = time.monotonic()
        app._queue_audit_record("界面点击", {"event": "ui_click", "x": 1})
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.05)
        self.assertEqual(app._audit_queue.qsize(), 1)
        self.assertEqual(app._audit_queue.get_nowait()[0], "界面点击")

    def test_page_switch_keeps_resident_views_mapped(self):
        path = os.path.join(ROOT, "src", "gui.py")
        with open(path, encoding="utf-8") as stream:
            source = stream.read()
        self.assertIn("后续 TAB 切换只 lift", source)
        self.assertIn("self.root.update_idletasks()", source)
        self.assertIn("lift_ms", source)
        self.assertNotIn("previous_view.place_forget()", source)


if __name__ == "__main__":
    unittest.main()
