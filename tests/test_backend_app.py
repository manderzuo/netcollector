# -*- coding: utf-8 -*-
"""2.0 独立后台启动入口回归测试。"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from backend_app import build_runtime, start_service  # noqa: E402
from backend_client import BackendClient  # noqa: E402


class TestBackendApp(unittest.TestCase):
    def test_demo_runtime_uses_existing_scheduler_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_runtime(os.path.join(directory, "data.db"), demo=True)
            try:
                report = runtime.scheduler.status_report()
                self.assertEqual(len(report["accounts"]), 3)
                self.assertTrue(all(
                    row.get("name", "").startswith("演示账号")
                    for row in report["accounts"].values()
                ))
            finally:
                runtime.close()

    def test_start_service_writes_endpoint_and_accepts_status(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, service = start_service(
                os.path.join(directory, "data.db"),
                endpoint_path=os.path.join(directory, "backend_endpoint.json"),
                demo=True,
            )
            client = BackendClient(service.endpoint, timeout=3)
            try:
                client.connect()
                client.request("auth_login", {"username": "admin", "password": "abc123"})
                report = client.request("status")
                self.assertEqual(report["totals"]["accounts"], 3)
                self.assertTrue(os.path.exists(os.path.join(directory, "backend_endpoint.json")))
            finally:
                client.close()
                service.stop()
                runtime.close()
            self.assertFalse(os.path.exists(os.path.join(directory, "backend_endpoint.json")))


if __name__ == "__main__":
    unittest.main()
