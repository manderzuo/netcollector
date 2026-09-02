# -*- coding: utf-8 -*-
"""独立 QA 与风险审计测试（技术方案 Agent I / 9.10）。

覆盖：旧库迁移/回滚、空库首启、地域/时效/意向边界、非河南与勿扰硬拦截、
分页性能、多线程读写、日志泄露检查。
"""
import io
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import db
from leads.dedupe import build_dedupe_key
from leads.freshness import FreshnessClassifier
from leads.intent import IntentScorer
from leads.models import DraftStatus, FreshnessBucket, IntentLevel, Pool
from leads.region import RegionClassifier
from leads.repository import LeadQuery, LeadRepository
from leads.service import LeadService
from interactions.policy import ReplyEligibilityPolicy
from interactions.repository import InteractionRepository


# =====================================================================
# 迁移与数据库
# =====================================================================
class TestMigrationQA(unittest.TestCase):
    def test_empty_db_first_start(self):
        """空数据库首次启动（9.10）。"""
        tmp = tempfile.mkdtemp()
        conn = db.init_db(os.path.join(tmp, "a.db"), check_same_thread=False)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("leads", tables)
        self.assertIn("interaction_drafts", tables)
        conn.close()

    def test_old_db_upgrade_keeps_data(self):
        """v1.0.10 旧结构升级：旧表数据保留，新表创建。"""
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "old.db")
        # 只建旧表（模拟旧库）
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL, platform TEXT NOT NULL DEFAULT 'douyin',
                status TEXT NOT NULL DEFAULT 'pending');
            CREATE TABLE videos (id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL, platform TEXT NOT NULL DEFAULT 'douyin',
                vid TEXT NOT NULL, url TEXT NOT NULL, title TEXT, status TEXT NOT NULL DEFAULT 'pending',
                FOREIGN KEY (task_id) REFERENCES tasks(id), UNIQUE (task_id, platform, vid));
            CREATE TABLE comments (id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id INTEGER NOT NULL, platform TEXT NOT NULL DEFAULT 'douyin',
                user_id TEXT, nickname TEXT, content TEXT, comment_time TEXT,
                FOREIGN KEY (video_id) REFERENCES videos(id));
        """)
        conn.execute("INSERT INTO tasks (keyword, platform) VALUES ('旧任务', 'douyin')")
        conn.commit()
        conn.close()

        conn2 = db.init_db(path, check_same_thread=False)
        n = conn2.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
        self.assertEqual(n, 1)  # 旧数据保留
        tables = {r[0] for r in conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("leads", tables)
        conn2.close()

    def test_repeated_migration_idempotent(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "r.db")
        conn = db.init_db(path, check_same_thread=False)
        conn.close()
        conn = db.init_db(path, check_same_thread=False)  # 再次
        conn.close()
        conn = db.init_db(path, check_same_thread=False)  # 三次
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("leads", tables)
        conn.close()

    def test_fk_enforcement(self):
        """外键完整性：不存在的评论引用被拒绝。"""
        tmp = tempfile.mkdtemp()
        conn = db.init_db(os.path.join(tmp, "f.db"), check_same_thread=False)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO leads (platform, dedupe_key, first_seen_at, last_seen_at, "
                "created_at, updated_at, source_comment_id) "
                "VALUES ('douyin', 'k-x', '2026-01-01', '2026-01-01', '2026-01-01', "
                "'2026-01-01', 99999)")
            conn.commit()
        conn.close()


# =====================================================================
# 分类引擎边界（9.10）
# =====================================================================
class TestClassifierEdges(unittest.TestCase):
    def test_region_aliases(self):
        r = RegionClassifier()
        for alias in ("河南", "河南省", "豫", "河南郑州"):
            res = r.classify(self_declared=alias)
            self.assertEqual(res.province, "河南省")
        self.assertEqual(RegionClassifier().classify(self_declared="广东省").province, "广东省")

    def test_region_conflict_prefers_manual(self):
        r = RegionClassifier()
        res = r.classify(profile_region="广东省", manual_province="河南省")
        self.assertEqual(res.province, "河南省")
        self.assertEqual(res.source, "manual")

    def test_unknown_not_henan(self):
        r = RegionClassifier()
        res = r.classify(text="随便聊聊")
        self.assertNotEqual(res.province, "河南省")
        self.assertEqual(r.pool_for(res), Pool.UNKNOWN_REGION)

    def test_freshness_future_time(self):
        """未来时间不得判为高价值（9.3 红线）。"""
        from datetime import datetime, timedelta
        fut = (datetime.now() + timedelta(days=3)).isoformat()
        f = FreshnessClassifier()
        res = f.classify(fut)
        self.assertEqual(res.bucket, FreshnessBucket.UNKNOWN)

    def test_freshness_boundaries(self):
        f = FreshnessClassifier(hot_days=3, active_days=14, cooling_days=30)
        from datetime import datetime, timedelta
        now = datetime.now()
        cases = [
            ((now - timedelta(days=1)).isoformat(), FreshnessBucket.HOT),
            ((now - timedelta(days=10)).isoformat(), FreshnessBucket.ACTIVE),
            ((now - timedelta(days=20)).isoformat(), FreshnessBucket.COOLING),
            ((now - timedelta(days=40)).isoformat(), FreshnessBucket.EXPIRED),
        ]
        for ts, expect in cases:
            self.assertEqual(f.classify(ts).bucket, expect, ts)

        # 小红书常见的展示时间：月-日后直接拼接地区，没有年份。
        xhs = FreshnessClassifier(
            hot_days=3,
            active_days=14,
            cooling_days=30,
            now=now,
        )
        self.assertEqual(
            xhs.classify(now.strftime("%m-%d") + "河南").bucket,
            FreshnessBucket.HOT,
        )
        self.assertEqual(
            xhs.classify((now - timedelta(days=40)).strftime("%m-%d") + "河南").bucket,
            FreshnessBucket.EXPIRED,
        )

        # 无年份日期若落在当前日期之后，应按上一自然年解析，避免产生未来时间。
        year_start = datetime(now.year, 1, 2)
        self.assertEqual(
            FreshnessClassifier(now=year_start).classify("12-31河南").bucket,
            FreshnessBucket.HOT,
        )
        self.assertEqual(
            FreshnessClassifier(now=year_start).classify(
                f"{now.year - 1}-12-31河南"
            ).bucket,
            FreshnessBucket.HOT,
        )

        from comment_time import normalize_xhs_comment_time
        self.assertEqual(
            normalize_xhs_comment_time("07-18河南", now=datetime(2027, 7, 20)),
            ("2027-07-18", "河南"),
        )
        self.assertEqual(
            normalize_xhs_comment_time("12-25河南", now=datetime(2026, 8, 29)),
            ("2025-12-25", "河南"),
        )
        self.assertEqual(
            normalize_xhs_comment_time("2025年07月18日河南", now=now),
            ("2025-07-18", "河南"),
        )

    def test_freshness_empty(self):
        f = FreshnessClassifier()
        self.assertEqual(f.classify(None).bucket, FreshnessBucket.UNKNOWN)
        self.assertEqual(f.classify("not-a-date").bucket, FreshnessBucket.UNKNOWN)

    def test_intent_negative(self):
        s = IntentScorer()
        res = s.score("不需要谢谢，随便问问")
        self.assertEqual(res.level, IntentLevel.LOW)
        self.assertTrue(res.negatives)

    def test_dedupe_not_nickname(self):
        k1 = build_dedupe_key(platform="douyin", platform_user_id="u1")
        k2 = build_dedupe_key(platform="douyin", platform_user_id="u2")
        self.assertNotEqual(k1.key, k2.key)
        # 不同昵称但同用户 → 同 key
        k3 = build_dedupe_key(platform="douyin", platform_user_id="u1")
        self.assertEqual(k1.key, k3.key)


# =====================================================================
# 硬拦截（9.10 / 6.5）
# =====================================================================
class TestHardBlocks(unittest.TestCase):
    def setUp(self):
        self.policy = ReplyEligibilityPolicy(henan_confidence_threshold=65,
                                             feature_enabled=True,
                                             real_sender_enabled=True)
        self.lead = {
            "region_province": "河南省", "region_confidence": 80,
            "freshness_bucket": "hot", "intent_level": "high",
            "status": "new", "platform": "douyin", "pool": Pool.HENAN,
        }

    def test_non_henan_blocked(self):
        lead = dict(self.lead, region_province="广东省")
        res = self.policy.evaluate(lead, {"draft_status": "approved"})
        self.assertFalse(res.allowed)

    def test_manual_selection_bypasses_region_and_intent_blocks(self):
        lead = dict(
            self.lead,
            region_province="广东省",
            region_confidence=0,
            intent_level="low",
        )
        res = self.policy.evaluate(
            lead,
            {"draft_status": "approved", "manual_selection": True},
        )
        self.assertTrue(res.allowed)

    def test_unknown_region_blocked(self):
        lead = dict(self.lead, region_province=None, region_confidence=0)
        res = self.policy.evaluate(lead, {"draft_status": "approved"})
        self.assertFalse(res.allowed)

    def test_low_confidence_blocked(self):
        lead = dict(self.lead, region_confidence=40)
        res = self.policy.evaluate(lead, {"draft_status": "approved"})
        self.assertFalse(res.allowed)

    def test_expired_is_manual_controlled(self):
        lead = dict(self.lead, freshness_bucket="expired")
        res = self.policy.evaluate(lead, {"draft_status": "approved"})
        self.assertTrue(res.allowed)

        # 草稿创建资格同样不再受时效字段拦截。
        draft_res = self.policy.can_create_draft(lead)
        self.assertTrue(draft_res.allowed)

    def test_bilibili_defaults_to_manual_review(self):
        lead = {
            "platform": "bilibili", "region_province": None,
            "region_confidence": 0, "pool": Pool.UNKNOWN_REGION,
            "freshness_bucket": "unknown", "intent_level": "low",
            "status": "new",
        }
        self.assertTrue(self.policy.can_create_draft(lead).allowed)
        self.assertTrue(
            self.policy.evaluate(
                lead,
                {"draft_status": "approved"},
            ).allowed
        )

    def test_suppression_blocked(self):
        res = self.policy.evaluate(self.lead, {"draft_status": "approved",
                                               "in_suppression": True})
        self.assertFalse(res.allowed)

    def test_sender_disabled_by_default(self):
        """默认（无 real_sender_enabled）时即使全过也拒绝（MVP 红线）。"""
        p = ReplyEligibilityPolicy(henan_confidence_threshold=65,
                                   feature_enabled=True)
        res = p.evaluate(self.lead, {"draft_status": "approved"})
        self.assertFalse(res.allowed)
        self.assertTrue(any("未启用真实发送" in r for r in res.reasons))


# =====================================================================
# 性能 / 并发（9.10 / 13）
# =====================================================================
class TestPerformanceAndConcurrency(unittest.TestCase):
    def test_page_performance_10k(self):
        """1 万条线索分页响应 < 500ms（普通 SSD 目标）。"""
        tmp = tempfile.mkdtemp()
        conn = db.init_db(os.path.join(tmp, "p.db"), check_same_thread=False)
        conn.execute("INSERT INTO tasks (keyword, platform) VALUES ('k', 'douyin')")
        conn.execute("INSERT INTO videos (task_id, platform, vid, url, title) "
                     "VALUES (1, 'douyin', 'v1', 'u1', 't1')")
        # 批量插入 1 万条线索（直接 SQL 造数，跳过评论链路）
        now = "2026-08-20T10:00:00"
        rows = [(f"douyin", f"u-{i}", f"用户{i}", f"k-{i}", now, now,
                 "河南省", "profile", 80, "hot", "high", 5, "[]", Pool.HENAN,
                 "new", now, now) for i in range(10000)]
        conn.executemany(
            """INSERT INTO leads (platform, platform_user_id, nickname, dedupe_key,
                 first_seen_at, last_seen_at, region_province, region_source,
                 region_confidence, freshness_bucket, intent_level, intent_score,
                 intent_reasons, pool, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        conn.commit()
        repo = LeadRepository(conn)
        t0 = time.monotonic()
        page = repo.list_leads(LeadQuery(pool=Pool.HENAN, page=1, page_size=50))
        dt = (time.monotonic() - t0) * 1000
        conn.close()
        self.assertEqual(page.total, 10000)
        self.assertEqual(len(page.items), 50)
        # 500ms 目标（CI 机器可能慢，放宽到 2000ms 但记录实际值）
        print(f"    1 万条分页耗时: {dt:.0f}ms")
        self.assertLess(dt, 2000)

    def test_threaded_read_write(self):
        """WAL 下多线程并发读写不崩溃（13）。"""
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "t.db")
        conn = db.init_db(path, check_same_thread=False)
        conn.execute("INSERT INTO tasks (keyword, platform) VALUES ('k', 'douyin')")
        conn.execute("INSERT INTO videos (task_id, platform, vid, url, title) "
                     "VALUES (1, 'douyin', 'v1', 'u1', 't1')")
        conn.commit()

        errors = []
        stop = threading.Event()

        def writer():
            try:
                c = db.init_db(path, check_same_thread=False)
                for i in range(200):
                    c.execute("INSERT INTO comments (video_id, platform, user_id, nickname, content) "
                              "VALUES (1, 'douyin', ?, ?, ?)", (f"w-{i}", f"u{i}", "c"))
                    c.commit()
                c.close()
            except Exception as e:
                errors.append(("writer", e))

        def reader():
            try:
                c = db.init_db(path, check_same_thread=False)
                for _ in range(200):
                    c.execute("SELECT COUNT(*) FROM leads").fetchone()
                c.close()
            except Exception as e:
                errors.append(("reader", e))

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        conn.close()
        self.assertEqual(errors, [])


# =====================================================================
# 日志泄露检查（9.10）
# =====================================================================
class TestLogLeak(unittest.TestCase):
    def test_service_does_not_log_sensitive(self):
        """Service 层日志不包含完整评论内容 / 账号信息。"""
        tmp = tempfile.mkdtemp()
        conn = db.init_db(os.path.join(tmp, "l.db"), check_same_thread=False)
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            from leads.pipeline import batch_ingest_comments
            svc = LeadService(LeadRepository(conn))
            batch_ingest_comments(svc, [999999], batch_size=10)  # 触发失败日志
            out = buf.getvalue()
            self.assertNotIn("cookie", out.lower())
            self.assertNotIn("window_id", out.lower())
        finally:
            root.removeHandler(handler)
        conn.close()


if __name__ == "__main__":
    unittest.main()
