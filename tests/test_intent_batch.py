# -*- coding: utf-8 -*-
import os
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from db import create_task, init_db, insert_comment, insert_video
from intent_batch import IntentBatchProcessor, _ProgressReporter
from llm_api import is_llm_eligible_comment


class IntentBatchTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "intent.db")
        self.conn = init_db(self.db_path)
        self.task_id = create_task(self.conn, "测试关键词", "douyin")
        self.video_id = insert_video(
            self.conn, self.task_id, "video-1", "https://example.test/video-1",
            platform="douyin",
        )

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def test_llm_result_mapping_requires_all_numbered_items(self):
        ids = [11, 12]
        mapped = IntentBatchProcessor._map_llm_results(
            [{"编号": 1, "意向": "高"}, {"编号": 2, "意向": "低"}], ids
        )
        self.assertEqual(mapped[11]["level"], "high")
        self.assertEqual(mapped[12]["score"], 1)
        self.assertIsNone(IntentBatchProcessor._map_llm_results(
            [{"编号": 1, "意向": "高"}], ids
        ))

    def test_local_fallback_updates_comment_and_lead(self):
        comment_id = insert_comment(
            self.conn, self.video_id, "user-1", "测试用户", "多少钱，怎么联系？",
            platform="douyin",
        )
        processor = IntentBatchProcessor(self.db_path)
        processor._apply_local([comment_id], source="local")
        comment = self.conn.execute(
            "SELECT intent_label, intent_score, extra FROM comments WHERE id = ?",
            (comment_id,),
        ).fetchone()
        self.assertEqual(comment["intent_label"], "high")
        self.assertEqual(comment["intent_score"], 5)
        lead = self.conn.execute(
            "SELECT intent_level, intent_score, rule_version FROM leads WHERE source_comment_id = ?",
            (comment_id,),
        ).fetchone()
        self.assertEqual(lead["intent_level"], "high")
        self.assertEqual(lead["intent_score"], 5)
        self.assertEqual(lead["rule_version"], "intent-rules-v1")

    def test_non_text_comment_filter(self):
        self.assertFalse(is_llm_eligible_comment({"content": None}))
        self.assertFalse(is_llm_eligible_comment({"content": "[图片]"}))
        self.assertFalse(is_llm_eligible_comment({"content": "😀👍🏻"}))
        self.assertFalse(is_llm_eligible_comment({"content": "１２３ 456"}))
        self.assertTrue(is_llm_eligible_comment({"content": "多少钱？😀"}))

    def test_on_comment_filters_before_queueing(self):
        processor = IntentBatchProcessor(self.db_path)
        with patch.object(processor, "_apply_local") as apply_local:
            processor.on_comment(123, comment={"content": "[图片]"})
        apply_local.assert_called_once_with([123], source="filtered_non_text")
        self.assertEqual(processor._pending, set())

    def test_batch_filters_invalid_comments_before_mapping(self):
        ids = [
            insert_comment(self.conn, self.video_id, "u-text", "用户1", "多少钱？"),
            insert_comment(self.conn, self.video_id, "u-empty", "用户2", ""),
            insert_comment(self.conn, self.video_id, "u-image", "用户3", "[图片]"),
            insert_comment(self.conn, self.video_id, "u-emoji", "用户4", "😀👍"),
            insert_comment(self.conn, self.video_id, "u-number", "用户5", "123456"),
            insert_comment(self.conn, self.video_id, "u-text2", "用户6", "怎么购买？"),
        ]
        api_result = {
            "healthy": True,
            "detail": "已完成 2 条评论的批量意向分析",
            "items": [
                {"编号": 1, "意向": "高"},
                {"编号": 2, "意向": "中"},
            ],
            "returned_count": 2,
        }
        processor = IntentBatchProcessor(self.db_path)
        with patch("intent_batch.LLMApiClient.analyze_comments_batch",
                   return_value=api_result) as analyze:
            processor._run_batch(ids, self.task_id)

        sent_samples = analyze.call_args.args[1]
        self.assertEqual([item["content"] for item in sent_samples], ["多少钱？", "怎么购买？"])
        rows = self.conn.execute(
            "SELECT id, intent_label, extra FROM comments WHERE id IN (?, ?, ?, ?, ?, ?) ORDER BY id",
            ids,
        ).fetchall()
        self.assertEqual([row[1] for row in rows], ["high", "low", "low", "low", "low", "medium"])
        self.assertEqual(json.loads(rows[1][2])["intent_source"], "filtered_non_text")

    def test_progress_reporter_deduplicates_zero_and_throttles_stream(self):
        clock = [0.0]
        events = []
        reporter = _ProgressReporter(
            lambda done, total, phase: events.append((done, total, phase)),
            min_step=100,
            min_interval=1.0,
            clock=lambda: clock[0],
        )

        self.assertTrue(reporter(0, 500, "接收分析结果"))
        for _ in range(20):
            self.assertFalse(reporter(0, 500, "接收分析结果"))
        self.assertEqual(len(events), 1)

        clock[0] = 0.5
        self.assertFalse(reporter(20, 500, "接收分析结果"))
        clock[0] = 1.1
        self.assertTrue(reporter(20, 500, "接收分析结果"))
        self.assertEqual(events[-1][0], 20)
        self.assertFalse(reporter(20, 500, "接收分析结果"))

    def test_batch_scheduler_limits_default_llm_concurrency(self):
        processor = IntentBatchProcessor(self.db_path, batch_size=2)
        processor._pending.update(range(1, 5))
        with patch("intent_batch.threading.Thread") as thread_cls:
            processor._schedule_ready_batches()

        self.assertEqual(processor.max_concurrent_batches, 1)
        self.assertEqual(thread_cls.call_count, 1)
        self.assertEqual(len(processor._inflight), 2)
        self.assertEqual(len(processor._pending - processor._inflight), 2)
        processor.close()


if __name__ == "__main__":
    unittest.main()
