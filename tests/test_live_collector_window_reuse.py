import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import live_collector


class _FakeCtx:
    def __init__(self, platform, ws_url=None):
        self.platform = platform
        self.ws_url = ws_url
        self.pause_event = None
        self.cancel_event = None

    def navigate_home(self):
        pass

    def close(self):
        pass


class _FakeBitBrowser:
    def __init__(self):
        self.open_calls = []

    def open_browser(self, window_id, **kwargs):
        self.open_calls.append((window_id, kwargs))
        return {"ws": "ws://fresh"}


class _ReconnectCtx(_FakeCtx):
    def fetch_comments(self, *args, **kwargs):
        if self.ws_url == "ws://cached":
            raise RuntimeError("ConnectionClosedError: no close frame received or sent")
        return [{"content": "reconnected"}]


class LiveCollectorWindowReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(live_collector, "DATA", self.tmp.name)
        self.data_patch.start()

    def tearDown(self):
        self.data_patch.stop()
        self.tmp.cleanup()

    def test_cached_ws_is_used_when_window_id_matches(self):
        with open(os.path.join(self.tmp.name, "dy_window.txt"), "w", encoding="utf-8") as f:
            f.write("window-1\nws://cached\n")
        bb = _FakeBitBrowser()
        with patch.object(live_collector, "_PlatformCtx", _FakeCtx):
            collector = live_collector.LiveCollector(platforms=("douyin",), bb=bb)
            ctx = collector._ctx("douyin", "window-1")
        self.assertEqual(ctx.ws_url, "ws://cached")
        self.assertEqual(bb.open_calls, [])

    def test_mismatched_cached_ws_does_not_bind_to_another_window(self):
        with open(os.path.join(self.tmp.name, "dy_window.txt"), "w", encoding="utf-8") as f:
            f.write("window-other\nws://cached\n")
        bb = _FakeBitBrowser()
        with patch.object(live_collector, "_PlatformCtx", _FakeCtx):
            collector = live_collector.LiveCollector(platforms=("douyin",), bb=bb)
            ctx = collector._ctx("douyin", "window-1")
        self.assertEqual(ctx.ws_url, "ws://fresh")
        self.assertEqual(len(bb.open_calls), 1)
        self.assertEqual(bb.open_calls[0][0], "window-1")

    def test_closed_cdp_connection_refreshes_window_and_retries_once(self):
        with open(os.path.join(self.tmp.name, "dy_window.txt"), "w", encoding="utf-8") as f:
            f.write("window-1\nws://cached\n")
        bb = _FakeBitBrowser()
        created = []

        def make_ctx(platform, ws_url=None):
            ctx = _ReconnectCtx(platform, ws_url)
            created.append(ctx)
            return ctx

        with patch.object(live_collector, "_PlatformCtx", side_effect=make_ctx):
            collector = live_collector.LiveCollector(platforms=("douyin",), bb=bb)
            result = collector.fetch_comments(
                "video-1", "account", platform="douyin", window_id="window-1"
            )

        self.assertEqual(result[0]["content"], "reconnected")
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0].ws_url, "ws://cached")
        self.assertEqual(created[1].ws_url, "ws://fresh")
        self.assertEqual(len(bb.open_calls), 1)


if __name__ == "__main__":
    unittest.main()
