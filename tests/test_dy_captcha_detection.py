import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import dy_collect


class _FakeCdp:
    def __init__(self, values):
        self.values = list(values)
        self.commands = []
        self.expressions = []

    async def eval(self, expression, sid):
        self.expressions.append(expression)
        return self.values.pop(0)

    async def cmd(self, method, params=None, session_id=None):
        self.commands.append((method, params, session_id))
        return {}


class ScrollLockedCaptchaTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_check_is_scoped_to_visible_overlay(self):
        cdp = _FakeCdp([None, None])
        reason = await dy_collect.probe_blocked(cdp, "sid-note")
        self.assertIsNone(reason)
        expression = cdp.expressions[0]
        self.assertIn("overlayNodes", expression)
        self.assertIn("positioned", expression)
        self.assertIn("hasRateLimitText", expression)
        self.assertIn(".test(overlayText)", expression)
        self.assertNotIn(".test(whole)", expression)

    async def test_overlay_and_immobile_wheel_is_human_block(self):
        cdp = _FakeCdp([
            {"before": 120, "max": 2200, "x": 600, "y": 400},
            120,
            120,
        ])
        reason = await dy_collect.probe_scroll_locked(cdp, "sid-1")
        self.assertEqual(reason, "验证码（页面灰屏且滚轮失效）")
        self.assertEqual(cdp.commands[0][0], "Input.dispatchMouseEvent")
        self.assertEqual(cdp.commands[0][2], "sid-1")
        self.assertEqual([item[1]["deltaY"] for item in cdp.commands], [-520, 520])

    async def test_no_covering_overlay_does_not_send_wheel(self):
        cdp = _FakeCdp([None])
        reason = await dy_collect.probe_scroll_locked(cdp, "sid-1")
        self.assertIsNone(reason)
        self.assertEqual(cdp.commands, [])

    async def test_successful_wheel_is_restored_and_not_blocked(self):
        cdp = _FakeCdp([
            {"before": 100, "max": 2200, "x": 600, "y": 400},
            0,
            620,
            None,
        ])
        reason = await dy_collect.probe_scroll_locked(cdp, "sid-1")
        self.assertIsNone(reason)
        self.assertIn("scrollTop = 100.0", cdp.expressions[-1])

    async def test_note_comment_panel_is_used_as_scroll_context(self):
        cdp = _FakeCdp([
            {"before": 700, "max": 2400, "x": 980, "y": 360, "kind": "comment"},
            180,
            1180,
            None,
        ])
        reason = await dy_collect.probe_scroll_locked(cdp, "sid-note")
        self.assertIsNone(reason)
        self.assertIn("comment-mainContent", cdp.expressions[-1])


if __name__ == "__main__":
    unittest.main()
