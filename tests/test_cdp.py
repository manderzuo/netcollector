# -*- coding: utf-8 -*-
import asyncio
import threading
import time
import unittest

from src.cdp import CdpCancelledError, CdpSession


class _SilentWebSocket:
    async def send(self, _message):
        return None


class CdpCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_event_interrupts_inflight_command_quickly(self):
        session = CdpSession("ws://test", timeout=30.0)
        session.ws = _SilentWebSocket()
        stop_event = threading.Event()
        session.set_cancel_event(stop_event)

        task = asyncio.create_task(session.cmd("Runtime.evaluate"))
        await asyncio.sleep(0.15)
        started = time.monotonic()
        stop_event.set()

        with self.assertRaises(CdpCancelledError):
            await task
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(session._pending, {})


if __name__ == "__main__":
    unittest.main()
