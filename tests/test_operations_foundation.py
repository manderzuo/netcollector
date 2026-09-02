# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from db import init_db, create_task, delete_task, insert_comment, insert_video, upsert_account
from migrations import MigrationRunner
from operations.account_safety import AccountSafetyLimits, AccountSafetyStore
from operations.backup import backup_sqlite, validate_sqlite
from operations.browser_health import BrowserHealthChecker
from operations.collection_runs import CollectionRunStore
from operations.diagnostic_package import DiagnosticPackageExporter
from operations.browser_deep_diagnostic import DeepBrowserDiagnosticRunner
from operations.health import HealthStatus, HealthStore
from operations.keywords import KeywordGroupStore
from operations.monitoring import MonitoringRuleStore
from operations.monitor_runner import MonitoringRunner
from operations.platform_health import PlatformHealthChecker
from interactions.browser_reply import _fill_script
from interactions.reply_target import ReplyTarget
from leads.scoring import ConfigurableLeadScorer
from leads.repository import LeadRepository
from leads.service import LeadService
from leads.scoring_store import LeadScoreStore
from scheduler import FakeCollector, Scheduler
from ui.pages.settings_page import _health_check_text


class Clock:
    def __init__(self, value=None):
        self.value = value or datetime(2026, 8, 24, 10, 0, 0)

    def __call__(self):
        return self.value


class MultiKeywordCollector(FakeCollector):
    """让每个关键词生成独立视频，验证逐词搜索和统一分配。"""

    def __init__(self, video_count=2):
        super().__init__(video_count=video_count)
        self.search_calls = []

    def search(self, keyword, platform, mode="standard", target_count=100,
               window_id=None, search_sort="default"):
        self.search_calls.append((keyword, target_count))
        rows = super().search(keyword, platform, mode, target_count, window_id, search_sort)
        for row in rows:
            row["vid"] = f"{keyword}_{row['vid']}"
            row["url"] = f"https://example.test/{row['vid']}"
        return rows


class OperationsFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "test.db")
        self.conn = init_db(self.db_path)
        self.task_id = create_task(self.conn, "托班", "douyin")
        self.account_id = upsert_account(self.conn, "测试账号", "window-1", "douyin")
        self.clock = Clock()

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def test_v003_is_current_and_additive(self):
        self.assertTrue(MigrationRunner().is_current(self.conn))
        names = {
            row[0] for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue({
            "collection_runs", "collection_events", "monitoring_rules",
            "keyword_groups", "keyword_terms", "account_limits",
            "health_events", "backup_records",
        }.issubset(names))
        self.assertIsNotNone(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (self.task_id,)).fetchone())

    def test_collection_run_lifecycle_and_events(self):
        store = CollectionRunStore(self.conn, clock=lambda: self.clock().isoformat(timespec="seconds"))
        run_id = store.create(self.task_id, "douyin", self.account_id, metadata={"keyword": "托班"}, run_id="run-1")
        store.event(run_id, "search", "page_loaded", "搜索页已加载", {"page": 1})
        store.update(run_id, status="no_more", stop_reason="页面提示暂无更多", discovered_count=12,
                     new_count=10, duplicate_count=2)
        record = store.get(run_id)
        self.assertEqual(record["status"], "no_more")
        self.assertEqual(record["new_count"], 10)
        self.assertEqual(len(store.events(run_id)), 3)

    def test_delete_task_cleans_operational_dependencies(self):
        """右键删除任务不能被运行记录/监控外键拦截。"""
        video_id = insert_video(
            self.conn, self.task_id, "delete-video", "https://example.test/delete-video",
            platform="douyin", search_query="托班",
        )
        comment_id = insert_comment(
            self.conn, video_id, "delete-user", nickname="待删除用户", content="测试评论",
            platform="douyin",
        )
        lead_id = LeadService(LeadRepository(self.conn)).ingest_comment(comment_id)
        run_store = CollectionRunStore(
            self.conn, clock=lambda: self.clock().isoformat(timespec="seconds")
        )
        run_id = run_store.create(self.task_id, "douyin", self.account_id, run_id="delete-run")
        run_store.event(run_id, "search", "page_loaded", "搜索页已加载")
        MonitoringRuleStore(self.conn, clock=self.clock).upsert(
            self.task_id, interval_seconds=3600, enabled=True
        )

        self.assertTrue(delete_task(self.conn, self.task_id))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM tasks WHERE id = ?", (self.task_id,)).fetchone())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM videos WHERE task_id = ?", (self.task_id,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM collection_runs WHERE task_id = ?", (self.task_id,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM monitoring_rules WHERE task_id = ?", (self.task_id,)).fetchone()[0], 0)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM leads WHERE id = ?", (lead_id,)).fetchone())
        self.assertIsNone(self.conn.execute(
            "SELECT source_comment_id FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()[0])

    def test_keyword_group_expands_deduplicated_queries(self):
        store = KeywordGroupStore(self.conn, clock=lambda: self.clock().isoformat(timespec="seconds"))
        group_id = store.create_group("河南托班", "douyin")
        version = store.set_terms(group_id, {
            "core": ["托班", " 托班 "],
            "synonym": ["早教"],
            "region": ["郑州", "郑州"],
            "exclude": ["招聘"],
        })
        self.assertEqual(version, 2)
        queries = store.expand(group_id)
        self.assertEqual([item["query"] for item in queries], ["托班 郑州", "早教 郑州"])
        self.assertEqual(queries[0]["exclude_terms"], ["招聘"])

    def test_keyword_group_update_list_and_delete(self):
        store = KeywordGroupStore(self.conn, clock=lambda: self.clock().isoformat(timespec="seconds"))
        group_id = store.create_group("旧名称", "douyin")
        store.set_terms(group_id, {"core": ["早教"]})
        version = store.update_group(group_id, "新名称", "xhs")
        self.assertEqual(version, 3)
        self.assertEqual(store.list_groups()[0]["name"], "新名称")
        self.assertEqual(store.get(group_id)["platform"], "xhs")
        store.delete_group(group_id)
        self.assertEqual(store.list_groups(), [])

    def test_monitoring_rule_due_and_no_more_gate(self):
        store = MonitoringRuleStore(self.conn, clock=self.clock)
        store.upsert(self.task_id, interval_seconds=3600, next_run_at="2026-08-24 09:00:00")
        self.assertTrue(store.due(self.task_id))
        CollectionRunStore(self.conn, clock=lambda: self.clock().isoformat(timespec="seconds")).create(
            self.task_id, "douyin", self.account_id, run_id="run-1"
        )
        store.mark_run(self.task_id, "run-1", no_more=True)
        self.assertFalse(store.due(self.task_id))
        self.assertTrue(store.due(self.task_id, force=True))

    def test_account_safety_checks_hours_quota_and_delay(self):
        store = AccountSafetyStore(self.conn, clock=self.clock)
        store.save(self.account_id, AccountSafetyLimits(
            hourly_collect_limit=10, daily_reply_limit=2, min_delay_seconds=30,
            work_start="09:00", work_end="18:00", consecutive_failure_limit=3,
        ))
        self.assertTrue(store.check(self.account_id, "collect", hourly_collected=9).allowed)
        self.assertFalse(store.check(self.account_id, "collect", hourly_collected=10).allowed)
        recent = self.clock() - timedelta(seconds=10)
        self.assertFalse(store.check(self.account_id, "reply", daily_replied=0, last_action_at=recent).allowed)
        self.assertFalse(store.check(self.account_id, "reply", daily_replied=2).allowed)
        self.assertFalse(store.check(self.account_id, "collect", consecutive_failures=3).allowed)

    def test_scheduler_counts_only_recent_consecutive_failures(self):
        ids = []
        for i in range(4):
            video_id = insert_video(self.conn, self.task_id, f"failure-sequence-{i}",
                                    f"https://example.test/failure-sequence-{i}")
            ids.append(video_id)
        statuses = ["failed", "failed", "done", "failed"]
        for video_id, status in zip(ids, statuses):
            self.conn.execute(
                "UPDATE videos SET assigned_account = ?, status = ? WHERE id = ?",
                ("测试账号", status, video_id),
            )
        self.conn.commit()
        scheduler = Scheduler(self.db_path)
        try:
            self.assertEqual(scheduler._consecutive_failure_count(scheduler.conn, "测试账号"), 1)
        finally:
            scheduler.shutdown(close_connections=True)

    def test_health_store_returns_latest_per_check(self):
        store = HealthStore(self.conn, clock=lambda: self.clock().isoformat(timespec="seconds"))
        store.record("douyin", "comment_box", HealthStatus.WARNING, "等待加载", self.account_id)
        self.clock.value += timedelta(minutes=1)
        store.record("douyin", "comment_box", HealthStatus.OK, "正常", self.account_id)
        latest = store.latest(platform="douyin", account_id=self.account_id)
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["status"], HealthStatus.OK)

    def test_platform_health_check_records_five_platforms(self):
        result = PlatformHealthChecker(
            self.conn, clock=lambda: self.clock().isoformat(timespec="seconds")
        ).run()
        self.assertEqual([row["platform"] for row in result["rows"]],
                         ["douyin", "xhs", "weibo", "bilibili", "kuaishou"])
        self.assertEqual(result["rows"][0]["status"], HealthStatus.OK)
        self.assertEqual(result["rows"][1]["status"], HealthStatus.WARNING)
        events = self.conn.execute(
            "SELECT COUNT(*) FROM health_events WHERE check_name = '基础健康检查'"
        ).fetchone()[0]
        self.assertEqual(events, 5)

    def test_browser_health_without_bound_accounts_is_read_only(self):
        self.conn.execute("DELETE FROM accounts WHERE id = ?", (self.account_id,))
        self.conn.commit()
        result = BrowserHealthChecker(
            self.conn, None, clock=lambda: self.clock().isoformat(timespec="seconds")
        ).run()
        self.assertEqual(result["mode"], "browser_read_only")
        self.assertEqual(len(result["rows"]), 5)
        self.assertTrue(all(row["accounts"] == 0 for row in result["rows"]))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM health_events WHERE check_name = '真实浏览器诊断'"
            ).fetchone()[0],
            0,
        )

    def test_browser_health_records_failed_account_without_invalid_run_fk(self):
        class BrokenBitBrowser:
            @staticmethod
            def open_browser(_window_id, ignore_default_urls=True):
                return {}

        result = BrowserHealthChecker(
            self.conn, BrokenBitBrowser(), clock=lambda: self.clock().isoformat(timespec="seconds")
        ).run()
        self.assertEqual(result["rows"][0]["status"], HealthStatus.FAILED)
        event = self.conn.execute(
            "SELECT status, run_id, metadata FROM health_events "
            "WHERE check_name = '真实浏览器诊断' AND account_id = ?",
            (self.account_id,),
        ).fetchone()
        self.assertEqual(event["status"], HealthStatus.FAILED)
        self.assertIsNone(event["run_id"])
        self.assertIn("diagnostic_run_id", event["metadata"])

    def test_deep_browser_diagnostic_without_data_does_not_open_browser(self):
        class MustNotRun:
            def reply(self, *_args, **_kwargs):
                raise AssertionError("没有测试数据时不应调用浏览器回复")

        result = DeepBrowserDiagnosticRunner(
            self.conn, object(), clock=lambda: self.clock().isoformat(timespec="seconds"),
            reply_adapter=MustNotRun(),
        ).run()
        douyin = result["rows"][0]
        self.assertEqual(douyin["status"], HealthStatus.WARNING)
        self.assertIn("暂无测试数据", douyin["detail"])
        self.assertFalse(result["send_executed"])

    def test_deep_browser_diagnostic_reuses_reply_adapter_without_send(self):
        second_account_id = upsert_account(self.conn, "测试账号2", "window-2", "douyin")
        self.assertNotEqual(second_account_id, self.account_id)
        video_id = insert_video(
            self.conn, self.task_id, "video-deep-1", "https://www.douyin.com/video/1",
            title="测试作品", platform="douyin",
        )
        insert_comment(
            self.conn, video_id, "user-1", nickname="测试用户", content="想了解价格",
            comment_time="2026-08-24 10:00:00",
        )

        class FakeReply:
            def __init__(self):
                self.calls = []

            def reply(self, target, content, *, confirm=False):
                self.calls.append((target, content, confirm))
                from interactions.models import ReplyActionResult
                return ReplyActionResult(
                    ok=True, stage="filled_waiting_confirmation",
                    message="回复已填入，等待人工确认发送", target=target,
                )

        adapter = FakeReply()
        result = DeepBrowserDiagnosticRunner(
            self.conn, object(), clock=lambda: self.clock().isoformat(timespec="seconds"),
            reply_adapter=adapter,
        ).run()
        douyin = result["rows"][0]
        self.assertEqual(douyin["status"], HealthStatus.OK)
        self.assertEqual(douyin["accounts"], 2)
        self.assertEqual(len(adapter.calls), 2)
        self.assertTrue(all(call[2] is False for call in adapter.calls))
        self.assertEqual(len(douyin["account_details"]), 2)
        self.assertIn("填入测试内容", douyin["detail"])

    def test_deep_diagnostic_prefers_bilibili_root_comment(self):
        reply, reason = DeepBrowserDiagnosticRunner._pick_sample(
            [{"extra": '{"is_reply": true}', "content": "楼中回复"},
             {"extra": '{"is_reply": false}', "content": "一级评论"}],
            "bilibili", None,
        )
        self.assertEqual(reason, "")
        self.assertEqual(reply["content"], "一级评论")

        reply, reason = DeepBrowserDiagnosticRunner._pick_sample(
            [{"extra": '{"is_reply": true}', "content": "楼中回复"}],
            "bilibili", None,
        )
        self.assertIsNone(reply)
        self.assertIn("均为楼中回复", reason)

    def test_douyin_note_reply_script_opens_right_comment_panel(self):
        target = ReplyTarget(
            lead_id=0, draft_id=None, platform="douyin", account_id=1,
            account_name="测试账号", account_status="active", bb_window_id="window-1",
            video_id=1, video_platform_id="note-1",
            video_url="https://www.douyin.com/note/note-1", local_comment_id=1,
            platform_comment_id=None, platform_user_id="u1", nickname="用户",
            content="测试评论", comment_time="2026-08-24",
        )
        script = _fill_script(target, "测试回复")
        self.assertIn("isDouyinNote", script)
        self.assertIn("douyin_note_comment_panel_candidates", script)
        self.assertIn("aria-label*", script)
        self.assertIn("iconHit", script)
        self.assertIn("douyin_note_comment_panel_open_started", script)
        self.assertIn("douyin_note_comment_panel_retry", script)
        self.assertIn("collectorCommentTab", script)
        self.assertIn("collector_comment_tab", script)
        self.assertIn("panelStateFromCollectorFlow", script)
        self.assertIn("feed-comment-icon", script)
        self.assertIn("comment-list", script)
        self.assertNotIn("clean(commentTab.innerText)", script)

    def test_health_detail_labels_are_chinese(self):
        self.assertEqual(_health_check_text("reply_filled", True), "回复框填充：是")
        self.assertEqual(_health_check_text("scroll", "locked"), "滚轮响应：滚轮被拦截")

    def test_browser_health_treats_xhs_home_as_valid_page(self):
        self.assertTrue(BrowserHealthChecker._is_platform_home(
            "https://www.xiaohongshu.com/explore", "xhs"))
        self.assertFalse(BrowserHealthChecker._is_platform_home(
            "https://www.xiaohongshu.com/search_result?keyword=洗衣", "xhs"))

    def test_diagnostic_package_redacts_sensitive_values(self):
        store = HealthStore(self.conn, clock=lambda: self.clock().isoformat(timespec="seconds"))
        store.record(
            "douyin", "真实浏览器诊断", HealthStatus.WARNING,
            "测试账号在 window-1 页面发现 token=secret-value",
            account_id=self.account_id,
            metadata={"token": "secret-value", "websocket": "ws://127.0.0.1:1234/devtools/browser/x",
                      "checks": {"scroll": "ok"}},
        )
        destination = os.path.join(self.tempdir.name, "packages")
        screenshot = os.path.join(self.tempdir.name, "page.png")
        from PIL import Image
        Image.new("RGB", (80, 60), (220, 220, 220)).save(screenshot)
        result = DiagnosticPackageExporter(self.db_path).export(
            destination_dir=destination,
            health_result={"mode": "browser_read_only", "screenshot_paths": [screenshot]},
        )
        self.assertTrue(os.path.isfile(result["path"]))
        self.assertEqual(result["screenshot_count"], 1)
        with __import__("zipfile").ZipFile(result["path"]) as bundle:
            names = set(bundle.namelist())
            self.assertTrue({"README.txt", "summary.json", "health_events.json", "health_events.csv"}.issubset(names))
            self.assertIn("screenshots/page_01.png", names)
            content = bundle.read("health_events.json").decode("utf-8")
        self.assertNotIn("secret-value", content)
        self.assertNotIn("window-1", content)
        self.assertNotIn("测试账号", content)
        self.assertIn("账号#", content)

    def test_backup_is_verified_and_recorded(self):
        destination = os.path.join(self.tempdir.name, "backups")
        result = backup_sqlite(self.db_path, destination, record_conn=self.conn)
        self.assertTrue(result.verified)
        self.assertTrue(os.path.isfile(result.path))
        self.assertEqual(validate_sqlite(result.path), (True, "ok"))
        row = self.conn.execute("SELECT verified, sha256 FROM backup_records").fetchone()
        self.assertEqual(row["verified"], 1)
        self.assertEqual(row["sha256"], result.sha256)

    def test_scheduler_persists_run_for_completed_task(self):
        scheduler = Scheduler(self.db_path, collector=FakeCollector(video_count=2))
        account_id = scheduler.add_account("采集账号", "window-2", "douyin")
        task_id = scheduler.create_task(
            "托班", "douyin", batch_size=10, cooldown_seconds=0,
            target_count=2, task_accounts=["采集账号"],
        )
        scheduler.start(task_id)
        self.assertTrue(scheduler.wait_for_task(task_id, timeout=5))
        rows = self.conn.execute(
            "SELECT status, account_id, new_count FROM collection_runs WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        report = scheduler.status_report()
        scheduler.shutdown(close_connections=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["account_id"], account_id)
        self.assertEqual(rows[0]["new_count"], 2)
        self.assertEqual(report["tasks"][task_id]["latest_run"]["status"], "completed")

    def test_configurable_lead_score_is_explainable_and_historic(self):
        scorer = ConfigurableLeadScorer({
            "version": "custom-v2",
            "purchase_keywords": {"报价": 60},
            "negative_keywords": {"招聘": -40},
        })
        result = scorer.score("想咨询报价和微信怎么联系", region="河南", repeat_count=2)
        self.assertEqual(result.rule_version, "custom-v2")
        self.assertGreaterEqual(result.score, 70)
        self.assertTrue(any("购买/咨询命中「报价」" in reason for reason in result.reasons))
        lead_id = self.conn.execute(
            "INSERT INTO leads(platform, dedupe_key, first_seen_at, last_seen_at, created_at, updated_at) "
            "VALUES ('douyin', 'score-test', '2026-08-24 10:00:00', '2026-08-24 10:00:00', '2026-08-24 10:00:00', '2026-08-24 10:00:00')"
        ).lastrowid
        self.conn.commit()
        store = LeadScoreStore(self.conn, clock=lambda: "2026-08-24 10:01:00")
        store.save_rule("默认评分", result.rule_version, {"version": result.rule_version})
        store.record(lead_id, result)
        history = store.history(lead_id)
        self.assertEqual(history[0]["score"], result.score)
        self.assertIn("reasons", history[0])

    def test_scheduler_creates_monitoring_rule_for_monitoring_task(self):
        scheduler = Scheduler(self.db_path, collector=FakeCollector(video_count=1))
        task_id = scheduler.create_task(
            "早教", "douyin", target_count=1, execution_mode="monitoring"
        )
        task = scheduler.get_task(task_id)
        rule = self.conn.execute(
            "SELECT enabled, interval_seconds FROM monitoring_rules WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        scheduler.shutdown(close_connections=True)
        self.assertEqual(task["execution_mode"], "monitoring")
        self.assertIsNotNone(rule)
        self.assertEqual(rule["enabled"], 1)

    def test_scheduler_persists_keyword_group_reference(self):
        group_id = KeywordGroupStore(self.conn, clock=lambda: self.clock().isoformat(timespec="seconds")).create_group(
            "托班组合", "douyin"
        )
        scheduler = Scheduler(self.db_path, collector=FakeCollector(video_count=0))
        task_id = scheduler.create_task(
            "托班", platform="douyin", target_count=1,
            keyword_group_id=group_id,
        )
        self.assertEqual(scheduler.get_task(task_id)["keyword_group_id"], group_id)
        scheduler.shutdown(close_connections=True)

    def test_keyword_group_searches_each_keyword_then_fairly_distributes(self):
        group_store = KeywordGroupStore(self.conn, clock=lambda: self.clock().isoformat(timespec="seconds"))
        group_id = group_store.create_group("洗衣预制词", "douyin")
        group_store.set_terms(group_id, {"core": ["京东洗衣、互联网洗衣、干洗店"]})
        collector = MultiKeywordCollector(video_count=2)
        scheduler = Scheduler(self.db_path, collector=collector)
        scheduler.add_account("词组账号A", "window-keyword-a", "douyin")
        scheduler.add_account("词组账号B", "window-keyword-b", "douyin")
        task_id = scheduler.create_task(
            "洗衣预制词", platform="douyin", target_count=2,
            task_accounts=["词组账号A", "词组账号B"], keyword_group_id=group_id,
        )
        scheduler.start(task_id)
        self.assertTrue(scheduler.wait_for_task(task_id, timeout=5))
        query_rows = self.conn.execute(
            "SELECT query, target_count, status FROM task_search_queries "
            "WHERE task_id = ? ORDER BY query_order", (task_id,)
        ).fetchall()
        video_rows = self.conn.execute(
            "SELECT search_query, status, assigned_account FROM videos "
            "WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        report = scheduler.status_report()["tasks"][task_id]
        scheduler.shutdown(close_connections=True)
        self.assertEqual([item[0] for item in collector.search_calls], ["京东洗衣", "互联网洗衣", "干洗店"])
        self.assertEqual([item[1] for item in collector.search_calls], [2, 2, 2])
        self.assertEqual([(row[0], row[1], row[2]) for row in query_rows], [
            ("京东洗衣", 2, "completed"),
            ("互联网洗衣", 2, "completed"),
            ("干洗店", 2, "completed"),
        ])
        self.assertEqual(len(video_rows), 6)
        self.assertTrue(all(row[1] == "done" for row in video_rows))
        self.assertEqual({row[2] for row in video_rows}, {"词组账号A", "词组账号B"})
        self.assertEqual(report["keyword_count"], 3)
        self.assertEqual(report["effective_target_count"], 6)

    def test_keyword_group_pause_does_not_advance_to_next_keyword(self):
        group_store = KeywordGroupStore(self.conn, clock=lambda: self.clock().isoformat(timespec="seconds"))
        group_id = group_store.create_group("暂停词组", "douyin")
        group_store.set_terms(group_id, {"core": ["关键词甲、关键词乙"]})

        class PauseAfterFirstSearch(MultiKeywordCollector):
            def __init__(self):
                super().__init__(video_count=2)
                self.scheduler = None
                self.task_id = None

            def search(self, keyword, platform, mode="standard", target_count=100,
                       window_id=None, search_sort="default"):
                self.search_calls.append((keyword, target_count))
                if keyword == "关键词甲":
                    self.scheduler.pause(self.task_id)
                return [dict(v, vid=f"{keyword}_{v['vid']}",
                             url=f"https://example.test/{keyword}/{v['vid']}")
                        for v in self._videos[:target_count]]

        collector = PauseAfterFirstSearch()
        scheduler = Scheduler(self.db_path, collector=collector)
        collector.scheduler = scheduler
        scheduler.add_account("暂停账号", "window-pause", "douyin")
        task_id = scheduler.create_task(
            "暂停词组", platform="douyin", target_count=2,
            task_accounts=["暂停账号"], keyword_group_id=group_id,
        )
        collector.task_id = task_id
        scheduler.start(task_id)
        query_rows = self.conn.execute(
            "SELECT query, status FROM task_search_queries WHERE task_id = ? ORDER BY query_order",
            (task_id,),
        ).fetchall()
        scheduler.shutdown(close_connections=True)
        self.assertEqual([item[0] for item in collector.search_calls], ["关键词甲"])
        self.assertEqual([(row[0], row[1]) for row in query_rows], [
            ("关键词甲", "incomplete"), ("关键词乙", "pending"),
        ])

    def test_keyword_group_phase_b_restart_does_not_search_again(self):
        group_store = KeywordGroupStore(self.conn, clock=lambda: self.clock().isoformat(timespec="seconds"))
        group_id = group_store.create_group("断点词组", "douyin")
        group_store.set_terms(group_id, {"core": ["断点关键词"]})
        collector = MultiKeywordCollector(video_count=1)
        scheduler = Scheduler(self.db_path, collector=collector)
        scheduler.add_account("断点账号", "window-resume", "douyin")
        task_id = scheduler.create_task(
            "断点词组", platform="douyin", target_count=1,
            task_accounts=["断点账号"], keyword_group_id=group_id,
        )
        task = scheduler.get_task(task_id)
        rows = scheduler._ensure_task_search_queries(task, 1)
        video_id = insert_video(
            self.conn, task_id, "fake_vid_001", "https://example.test/resume-video-1",
            search_query="断点关键词",
        )
        self.conn.execute(
            "UPDATE task_search_queries SET status = 'in_progress' WHERE task_id = ?",
            (task_id,),
        )
        self.conn.execute(
            "UPDATE tasks SET status = 'phase_b_comments', search_phase_complete = 0 WHERE id = ?",
            (task_id,),
        )
        self.conn.commit()
        scheduler.start(task_id)
        self.assertTrue(scheduler.wait_for_task(task_id, timeout=5))
        query = self.conn.execute(
            "SELECT status FROM task_search_queries WHERE task_id = ?", (task_id,)
        ).fetchone()
        task_after = self.conn.execute(
            "SELECT status, search_phase_complete FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        video = self.conn.execute(
            "SELECT status FROM videos WHERE id = ?", (video_id,)
        ).fetchone()
        scheduler.shutdown(close_connections=True)
        self.assertEqual(collector.search_calls, [])
        self.assertEqual(query["status"], "completed")
        self.assertEqual(task_after["search_phase_complete"], 1)
        self.assertEqual(video["status"], "done")

    def test_scheduler_configures_monitoring_and_latest_sort(self):
        scheduler = Scheduler(self.db_path, collector=FakeCollector(video_count=0))
        task_id = scheduler.create_task("早教", platform="douyin", target_count=1)
        rule = scheduler.configure_monitoring(
            task_id, interval_seconds=2 * 3600 + 15 * 60,
            enabled=True, search_sort="latest",
        )
        self.assertEqual(rule["interval_seconds"], 8100)
        self.assertEqual(scheduler.get_task(task_id)["search_sort"], "latest")
        scheduler.configure_monitoring(task_id, interval_seconds=3600, enabled=False,
                                       search_sort="latest")
        disabled = scheduler.get_task(task_id)
        self.assertEqual(disabled["execution_mode"], "once")
        self.assertEqual(
            self.conn.execute(
                "SELECT enabled FROM monitoring_rules WHERE task_id = ?", (task_id,)
            ).fetchone()[0], 0
        )
        scheduler.shutdown(close_connections=True)

    def test_monitor_runner_triggers_due_task_only_once_while_running(self):
        scheduler = Scheduler(self.db_path, collector=FakeCollector(video_count=1))
        scheduler.add_account("监控账号", "window-monitor", "douyin")
        task_id = scheduler.create_task(
            "早教", "douyin", target_count=1, execution_mode="monitoring",
            task_accounts=["监控账号"]
        )
        self.conn.execute(
            "UPDATE monitoring_rules SET next_run_at = ? WHERE task_id = ?",
            ("2026-08-24 09:00:00", task_id),
        )
        self.conn.commit()
        runner = MonitoringRunner(scheduler, poll_seconds=1)
        self.assertEqual(runner.run_once(), [task_id])
        # start() 已经把任务置为 phase_a_search/phase_b_comments，
        # 同一轮不能再次创建搜索任务。
        self.assertEqual(runner.run_once(), [])
        scheduler.stop_task(task_id)
        scheduler.shutdown(close_connections=True)

    def test_monitoring_no_more_gate_blocks_automatic_retry_but_allows_force(self):
        store = MonitoringRuleStore(self.conn, clock=self.clock)
        store.upsert(
            self.task_id, interval_seconds=3600, enabled=True,
            next_run_at="2026-08-24 09:00:00",
        )
        self.assertTrue(store.due(self.task_id))
        CollectionRunStore(self.conn, clock=lambda: self.clock().isoformat(timespec="seconds")).create(
            self.task_id, "douyin", self.account_id, run_id="simulated-run"
        )
        store.mark_run(self.task_id, "simulated-run", no_more=True)
        self.assertFalse(store.due(self.task_id))
        # “暂无更多”只拦截后台自动轮询；用户明确继续时仍允许重开搜索闸门。
        self.assertTrue(store.due(self.task_id, force=True))

    def test_monitoring_task_closes_second_run_after_historical_target(self):
        scheduler = Scheduler(self.db_path, collector=FakeCollector(video_count=1))
        scheduler.add_account("监控账号2", "window-monitor-2", "douyin")
        task_id = scheduler.create_task(
            "早教", "douyin", target_count=1, execution_mode="monitoring",
            task_accounts=["监控账号2"], cooldown_seconds=0,
        )
        scheduler.start(task_id)
        self.assertTrue(scheduler.wait_for_task(task_id, timeout=5))
        # 第二轮仍可搜索，尽管历史完成数已经达到任务目标；重复内容应闭合运行记录。
        self.conn.execute(
            "UPDATE monitoring_rules SET next_run_at = ? WHERE task_id = ?",
            ("2026-08-24 09:00:00", task_id),
        )
        self.conn.commit()
        runner = MonitoringRunner(scheduler)
        self.assertEqual(runner.run_once(), [task_id])
        rows = self.conn.execute(
            "SELECT status FROM collection_runs WHERE task_id = ? ORDER BY started_at, rowid",
            (task_id,),
        ).fetchall()
        scheduler.shutdown(close_connections=True)
        self.assertEqual(len(rows), 2)
        self.assertIn(rows[-1]["status"], {"completed", "no_more"})


if __name__ == "__main__":
    unittest.main()
