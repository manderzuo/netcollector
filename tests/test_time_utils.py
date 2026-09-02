# -*- coding: utf-8 -*-
"""计划时间的北京时间和过去时间校验。"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from time_utils import (  # noqa: E402
    BEIJING_TZ,
    format_beijing_minute,
    normalize_scheduled_at,
    parse_beijing_datetime,
)


class BeijingScheduleTimeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 2, 18, 0, 30, tzinfo=BEIJING_TZ)

    def test_naive_input_is_interpreted_as_beijing_and_normalized(self):
        result = normalize_scheduled_at("2026-09-02 18:05", now=self.now)
        self.assertEqual(result, "2026-09-02T18:05:00+08:00")
        self.assertEqual(format_beijing_minute(result), "2026-09-02 18:05")

    def test_offset_input_is_converted_to_beijing(self):
        parsed = parse_beijing_datetime("2026-09-02T10:05:00Z")
        self.assertEqual(parsed.isoformat(timespec="seconds"), "2026-09-02T18:05:00+08:00")

    def test_past_or_current_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "晚于当前北京时间"):
            normalize_scheduled_at("2026-09-02 17:59", now=self.now)
        with self.assertRaisesRegex(ValueError, "晚于当前北京时间"):
            normalize_scheduled_at("2026-09-02 18:00", now=self.now)

    def test_missing_seconds_and_invalid_values_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "时间格式无效"):
            parse_beijing_datetime("2026-09-02")
        with self.assertRaisesRegex(ValueError, "时间格式无效"):
            parse_beijing_datetime("2026-09-02 25:00")


if __name__ == "__main__":
    unittest.main()
