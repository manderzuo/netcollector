# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

from src.update_checker import (
    add_cache_buster,
    compare_versions,
    fetch_manifest,
    is_update_available,
    normalize_manifest,
)


def _manifest(**overrides):
    value = {
        "version": "2.1",
        "build_id": "20260903-2.1-update-check",
        "download_url": "https://example.com/collector.zip",
        "sha256": "A" * 64,
        "notes": "修复更新链路",
    }
    value.update(overrides)
    return value


class UpdateCheckerTests(unittest.TestCase):
    def test_compare_versions_pads_missing_components(self):
        self.assertEqual(compare_versions("2.1", "2.1.0"), 0)
        self.assertEqual(compare_versions("2.1.1", "2.1"), 1)
        self.assertEqual(compare_versions("1.9", "2.0"), -1)

    def test_same_version_different_build_is_available(self):
        manifest = normalize_manifest(_manifest())
        self.assertEqual(is_update_available("2.1", "old-build", manifest), (True, "发现同版本修复包"))
        self.assertEqual(is_update_available("2.1", "", manifest), (True, "发现同版本修复包"))
        self.assertEqual(is_update_available("2.1", manifest["build_id"], manifest), (False, "已是最新版本"))

    def test_newer_version_is_available_and_older_remote_is_not(self):
        self.assertEqual(is_update_available("2.0", "", _manifest(version="2.1")), (True, "发现新版本"))
        self.assertEqual(is_update_available("2.2", "", _manifest(version="2.1")), (False, "本地版本较新"))

    def test_manifest_validation_rejects_bad_hash(self):
        with self.assertRaises(Exception):
            normalize_manifest(_manifest(sha256="bad"))

    def test_fetch_manifest_uses_cache_buster_and_utf8_json(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(_manifest(), ensure_ascii=False).encode("utf-8")

        with patch("src.update_checker.urlopen", return_value=Response()) as mocked:
            result = fetch_manifest("https://example.com/latest.json?channel=stable", timeout=3)
        self.assertEqual(result["build_id"], "20260903-2.1-update-check")
        request_url = mocked.call_args.args[0].full_url
        self.assertIn("channel=stable", request_url)
        self.assertIn("_client_check=", request_url)


if __name__ == "__main__":
    unittest.main()
