# -*- coding: utf-8 -*-
"""小红书风控降频与限流退避的回归检查。

覆盖三类曾经导致「限流被当成验证码、进而冻结账号」的缺陷：
1. 限流文案漏检（旧正则 /操作频繁/ 匹配不到「操作过于频繁」）。
2. 采评论的滚动循环内没有任何风控探测，被限流后继续硬刷到升级。
3. 限流与验证码共用 HumanBlock，一次限流就冻结账号并要求人工点继续。
"""

import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import live_collector  # noqa: E402
import xhs_collect3  # noqa: E402
import db  # noqa: E402
from scheduler import Collector, Scheduler  # noqa: E402


async def _no_sleep(*args, **kwargs):
    """测试用：跳过所有随机等待，避免用例真实等待几十秒。"""
    return None


async def _async_true(*args, **kwargs):
    """测试用：把可中断等待替换成立即成功。"""
    return True


class _FakeCdp:
    """按顺序返回 eval 结果，并记录执行过的表达式与导航。"""

    def __init__(self, values=None):
        self.values = list(values or [])
        self.expressions = []
        self.navigations = []

    async def eval(self, expression, sid):
        self.expressions.append(expression)
        if self.values:
            return self.values.pop(0)
        return None

    async def navigate(self, url, sid, wait_load=True):
        self.navigations.append(url)
        return None


class _BulkCommentCollector(Collector):
    """产生一个大评论作品，用于验证调度器不逐条阻塞在扩展处理上。"""

    def search(self, *args, **kwargs):
        return [{"vid": "bulk-video", "url": "https://example.test/bulk"}]

    def fetch_comments(self, *args, **kwargs):
        return [
            {"user_id": f"u-{idx}", "nickname": f"用户{idx}", "content": f"评论{idx}"}
            for idx in range(120)
        ]

def _bare_ctx():
    """构造不带事件循环与线程的 _PlatformCtx，只测纯逻辑。"""
    return live_collector._PlatformCtx.__new__(live_collector._PlatformCtx)


class RateLimitClassificationTests(unittest.TestCase):
    def test_probe_matches_real_xhs_rate_limit_wording(self):
        path = os.path.join(ROOT, "src", "xhs_collect3.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        # 小红书实际弹的是「操作过于频繁，请稍后再试」这类写法，旧正则
        # /请求太频繁|操作频繁|一分钟后再试|访问频繁/ 全部匹配不到。
        for wording in (
            "请求(?:过于|太)?频繁",
            "操作(?:过于|太)?频繁",
            "访问(?:过于|太)?频繁",
            "操作过快",
        ):
            self.assertIn(wording, source)
        # 限流文案必须限制在可见弹窗/遮罩里判定，避免笔记正文误伤。
        self.assertIn("overlayText", source)
        self.assertIn("overlayNodes", source)

    def test_rate_limit_is_retryable_but_captcha_is_human(self):
        with self.assertRaises(xhs_collect3.RateLimited):
            xhs_collect3.raise_for_block("rate_limited")
        for reason in ("验证码", "登录页面", "登录弹窗", "bitbrowser拦截"):
            with self.assertRaises(xhs_collect3.HumanBlock):
                xhs_collect3.raise_for_block(reason)
        self.assertIsNone(xhs_collect3.raise_for_block(None))

    def test_rate_limited_is_not_a_human_block_subclass(self):
        # 两者必须互斥：否则限流又会被调度器当成需要人工验证。
        self.assertFalse(issubclass(xhs_collect3.RateLimited, xhs_collect3.HumanBlock))
        self.assertFalse(issubclass(xhs_collect3.HumanBlock, xhs_collect3.RateLimited))


class CommentScrollProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_during_comment_scroll_stops_immediately(self):
        # 调用顺序：进循环前探测(放行) → 读详情 → 循环内第 1 轮探测(命中限流)。
        cdp = _FakeCdp([None, {"title": "标题"}, "rate_limited"])
        with mock.patch.object(xhs_collect3, "sleep_random", _no_sleep):
            with self.assertRaises(xhs_collect3.RateLimited):
                await xhs_collect3.fetch_note(
                    cdp, "sid", {"id": "note-1", "xsec_token": "tk"})

        # 第 3 次调用就是循环内的探测。旧实现整段循环没有探测，
        # 会一路滚到 80 轮结束，限流根本不会被发现。
        self.assertEqual(len(cdp.expressions), 3)
        self.assertIn("overlayNodes", cdp.expressions[2])

    async def test_captcha_during_comment_scroll_freezes_for_human(self):
        cdp = _FakeCdp([None, {"title": "标题"}, "验证码"])
        with mock.patch.object(xhs_collect3, "sleep_random", _no_sleep):
            with self.assertRaises(xhs_collect3.HumanBlock):
                await xhs_collect3.fetch_note(
                    cdp, "sid", {"id": "note-1", "xsec_token": "tk"})

    async def test_comment_scroll_is_interruptible_by_cancel(self):
        cancel = threading.Event()
        cancel.set()
        cdp = _FakeCdp([None, {"title": "标题"}, {"desert": False, "comments": []}])
        with mock.patch.object(xhs_collect3, "sleep_random", _no_sleep):
            result = await xhs_collect3.fetch_note(
                cdp, "sid", {"id": "note-1", "xsec_token": "tk"},
                cancel_event=cancel)
        # 取消后不再滚动：只有「循环前探测 + 读详情 + 读评论」三次 eval。
        self.assertEqual(result["note_id"], "note-1")
        self.assertEqual(len(cdp.expressions), 3)


class XhsRateLimitRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_is_retried_before_freezing_account(self):
        ctx = _bare_ctx()
        ctx._wait_with_events = _async_true
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            if calls["n"] < 3:
                raise xhs_collect3.RateLimited()
            return {"comments": [{"text": "ok"}]}

        result = await ctx._xhs_retry(factory, None, None, "笔记详情")
        self.assertEqual(result, {"comments": [{"text": "ok"}]})
        # 旧实现第 1 次限流就抛 HumanBlock 冻结账号，这里必须重试到成功。
        self.assertEqual(calls["n"], 3)

    async def test_exhausted_retries_escalate_to_human_intervention(self):
        ctx = _bare_ctx()
        ctx._wait_with_events = _async_true
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            raise xhs_collect3.RateLimited()

        with self.assertRaises(live_collector.HumanInterventionRequired) as cm:
            await ctx._xhs_retry(factory, None, None, "笔记详情")
        self.assertEqual(calls["n"], live_collector.XHS_RATE_LIMIT_RETRIES)
        self.assertEqual(cm.exception.reason, "rate_limited")

    async def test_cancelled_backoff_wait_gives_up_without_human_freeze(self):
        ctx = _bare_ctx()
        cancel = threading.Event()
        cancel.set()

        async def factory():
            raise xhs_collect3.RateLimited()

        result = await ctx._xhs_retry(factory, None, cancel, "笔记详情")
        self.assertIs(result, live_collector._CANCELLED)

    async def test_captcha_is_not_retried_and_propagates(self):
        ctx = _bare_ctx()
        ctx._wait_with_events = _async_true
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            raise xhs_collect3.HumanBlock("验证码")

        with self.assertRaises(xhs_collect3.HumanBlock):
            await ctx._xhs_retry(factory, None, None, "笔记详情")
        # 验证码必须立刻交给人工，不能退避重试。
        self.assertEqual(calls["n"], 1)


class XhsPacingFloorTests(unittest.IsolatedAsyncioTestCase):
    def test_note_interval_floor_is_about_one_note_per_minute(self):
        low, high = live_collector.XHS_MIN_NOTE_INTERVAL
        self.assertGreaterEqual(low, 45.0)
        self.assertLess(low, high)
        # 由采集器负责笔记间隔，长休不要再每 5 篇触发一次；批次冷却由
        # scheduler 做剩余时间补足，避免两层冷却叠加。
        self.assertGreaterEqual(live_collector.XHS_BREAK_EVERY_NOTES, 10)
        break_low, break_high = live_collector.XHS_LONG_BREAK
        self.assertGreaterEqual(break_low, 60.0)
        self.assertLess(break_low, break_high)

    def test_xhs_batch_cooldown_only_fills_remaining_time(self):
        started = __import__("time").monotonic() - 120.0
        self.assertEqual(
            Scheduler._effective_batch_cooldown("xhs", 60, started), 0
        )
        self.assertEqual(
            Scheduler._effective_batch_cooldown("douyin", 60, started), 60
        )

    def test_xhs_fetch_timeout_has_room_for_the_slower_pacing(self):
        ctx = _bare_ctx()
        ctx.platform = "xhs"
        seen = {}

        def _run_sync(*args, **kwargs):
            seen.update(kwargs)
            return []

        ctx._run_sync = _run_sync
        ctx.fetch_comments("note-1")
        # 节奏下限 + 长休都在单次调用内部等待，超时太短会把正常采集
        # 取消掉并误判为采集失败。
        self.assertGreaterEqual(seen.get("timeout", 0), 600)

    def test_collector_pacing_uses_random_ranges_not_fixed_sleeps(self):
        path = os.path.join(ROOT, "src", "xhs_collect3.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        for name in ("NOTE_MIN_INTERVAL", "NOTE_SETTLE", "COMMENT_SCROLL_INTERVAL",
                     "SEARCH_SCROLL_INTERVAL", "COMMENT_BREAK", "SEARCH_BREAK"):
            self.assertIn(name, source)
        # 固定节奏是风控最容易建模的特征，详情页不能再用固定 4 秒等待。
        self.assertNotIn("await asyncio.sleep(4)", source)

    async def test_wait_with_events_does_not_consume_time_while_paused(self):
        ctx = _bare_ctx()
        pause = threading.Event()          # 未 set = 暂停中
        cancel = threading.Event()
        task = asyncio.ensure_future(ctx._wait_with_events(0.3, pause, cancel))
        await asyncio.sleep(0.5)
        # 暂停期间不能把等待时间走完，否则恢复后会立刻打开下一篇。
        self.assertFalse(task.done())
        pause.set()
        self.assertTrue(await task)

    async def test_wait_with_events_returns_false_when_cancelled(self):
        ctx = _bare_ctx()
        cancel = threading.Event()
        cancel.set()
        self.assertFalse(await ctx._wait_with_events(5.0, None, cancel))


class LargeCommentPipelineTests(unittest.TestCase):
    def test_comment_batch_can_commit_without_per_row_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "comments.db")
            conn = db.init_db(path)
            task_id = db.create_task(conn, "批量评论", target_count=1)
            video_id = db.insert_video(conn, task_id, "v-1", "u-1")
            first_id, inserted = db.insert_comment(
                conn, video_id, "u-1", content="同一条",
                commit=False, return_inserted=True,
            )
            duplicate_id, duplicate_inserted = db.insert_comment(
                conn, video_id, "u-1", content="同一条",
                commit=False, return_inserted=True,
            )
            self.assertTrue(inserted)
            self.assertFalse(duplicate_inserted)
            self.assertEqual(first_id, duplicate_id)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 1)
            conn.commit()
            conn.close()

    def test_large_comment_collection_queues_enrichment_after_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scheduler.db")
            scheduler = Scheduler(path, collector=_BulkCommentCollector())
            scheduler.add_account("bulk-account", "bulk-window", "douyin")
            task_id = scheduler.create_task(
                "批量评论", target_count=1, task_accounts=["bulk-account"],
                batch_size=1, cooldown_seconds=0,
            )
            try:
                with mock.patch.object(scheduler, "_queue_comment_enrichment") as enqueue:
                    scheduler.start(task_id)
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        if scheduler.status_report()["tasks"][task_id]["status"] == "done":
                            break
                        time.sleep(0.03)
                    self.assertEqual(
                        scheduler.status_report()["tasks"][task_id]["status"], "done"
                    )
                    enqueue.assert_called_once()
                    jobs = enqueue.call_args.args[0]
                    self.assertEqual(len(jobs), 120)
                    self.assertEqual(
                        scheduler.conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0],
                        120,
                    )
            finally:
                scheduler.shutdown(close_connections=True)


if __name__ == "__main__":
    unittest.main()
