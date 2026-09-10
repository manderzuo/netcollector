import os
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import db
from scheduler import Collector, HumanInterventionRequired, Scheduler
from operations.keywords import KeywordGroupStore


class PartialSearchResult(list):
    """模拟搜索阶段达到安全轮次但未确认终止条件。"""

    search_complete = False
    reached_target = False
    no_more_results = False
    rounds = 3


class SlowCollector(Collector):
    def __init__(self, count=4, delay=.08):
        self.count = count
        self.delay = delay

    def search(self, keyword, platform, mode="standard", target_count=100,
               window_id=None, pause_event=None, cancel_event=None):
        return [{"vid": f"{keyword}-{i}", "url": f"u-{i}", "title": "t", "author": "a"}
                for i in range(self.count)]

    def fetch_comments(self, vid, account, url="", platform=None, window_id=None,
                       pause_event=None, cancel_event=None):
        end = time.monotonic() + self.delay
        while time.monotonic() < end:
            if cancel_event is not None and cancel_event.is_set():
                return []
            time.sleep(.01)
        return [{"user_id": vid, "nickname": account, "content": "c"}]


class RefillCollector(SlowCollector):
    def __init__(self):
        super().__init__(count=0, delay=.01)
        self.round = 0

    def search(self, keyword, platform, mode="standard", target_count=100,
               window_id=None, pause_event=None, cancel_event=None):
        start = self.round * 2
        self.round += 1
        return [{"vid": f"v-{i}", "url": f"u-{i}"} for i in range(start, start + 2)]


class CancellableSearch(SlowCollector):
    def search(self, keyword, platform, mode="standard", target_count=100,
               window_id=None, pause_event=None, cancel_event=None):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if cancel_event is not None and cancel_event.is_set():
                return []
            while pause_event is not None and not pause_event.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    return []
                time.sleep(.01)
            time.sleep(.01)
        return super().search(keyword, platform, mode, target_count, window_id)


class UnconfirmedPartialCollector(SlowCollector):
    def __init__(self):
        super().__init__(count=2, delay=.01)
        self.detail_calls = 0

    def search(self, keyword, platform, mode="standard", target_count=100,
               window_id=None, pause_event=None, cancel_event=None):
        return PartialSearchResult([
            {"vid": "partial-1", "url": "u-1"},
            {"vid": "partial-2", "url": "u-2"},
        ])

    def fetch_comments(self, *args, **kwargs):
        self.detail_calls += 1
        return super().fetch_comments(*args, **kwargs)


class HumanOnSearchCollector(SlowCollector):
    def __init__(self):
        super().__init__(count=2, delay=.01)
        self.search_calls = 0

    def search(self, *args, **kwargs):
        self.search_calls += 1
        if self.search_calls == 1:
            raise HumanInterventionRequired("captcha", "测试验证")
        return super().search(*args, **kwargs)[:1]


class HumanOnSecondKeywordCollector(SlowCollector):
    class Result(list):
        search_complete = True
        reached_target = True
        no_more_results = False
        rounds = 1

    def __init__(self):
        super().__init__(count=1, delay=.01)
        self.search_calls = []

    def search(self, keyword, *args, **kwargs):
        self.search_calls.append(keyword)
        if len(self.search_calls) == 2:
            raise HumanInterventionRequired("captcha", "第二个关键词触发验证")
        return self.Result([{"vid": f"{keyword}-1", "url": f"u-{keyword}-1"}])


class ExplicitExhaustedCollector(SlowCollector):
    class Result(list):
        search_complete = True
        reached_target = False
        no_more_results = True
        rounds = 1

    def __init__(self):
        super().__init__(count=1, delay=.01)
        self.search_calls = 0

    def search(self, *args, **kwargs):
        self.search_calls += 1
        return self.Result([{"vid": "only-one", "url": "u"}])


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def wait_status(self, sched, task_id, statuses, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            state = sched.status_report()["tasks"][task_id]["status"]
            if state in statuses:
                return state
            time.sleep(.03)
        self.fail(f"任务 {task_id} 未进入 {statuses}")

    def test_process_alive_treats_invalid_windows_handle_as_dead(self):
        # 强制结束 GUI 后，Windows 可能把 os.kill(pid, 0) 报成 SystemError，
        # 旧租约清理必须把它视为进程已结束，不能阻断 Scheduler 启动。
        with patch("scheduler.os.kill", side_effect=SystemError("WinError 6")):
            self.assertFalse(Scheduler._process_alive(12345))

    def test_legacy_foreign_key_and_null_comment_dedup(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
        PRAGMA foreign_keys=OFF;
        CREATE TABLE tasks(id INTEGER PRIMARY KEY);
        CREATE TABLE videos(id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL,
          platform TEXT DEFAULT 'douyin', vid TEXT UNIQUE, url TEXT NOT NULL,
          title TEXT, author TEXT, extra TEXT, status TEXT DEFAULT 'pending',
          assigned_account TEXT, collected_at TEXT,
          FOREIGN KEY(task_id) REFERENCES tasks(id));
        CREATE TABLE comments(id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL,
          platform TEXT DEFAULT 'douyin', user_id TEXT, nickname TEXT, content TEXT,
          comment_time TEXT, extra TEXT, intent_score INTEGER DEFAULT 0,
          intent_label TEXT, reply_suggestion TEXT,
          UNIQUE(video_id,user_id,content), FOREIGN KEY(video_id) REFERENCES videos(id));
        """)
        conn.close()
        conn = db.init_db(self.db_path)
        self.assertEqual(conn.execute("PRAGMA foreign_key_list('comments')").fetchone()[2], "videos")
        conn.execute("INSERT INTO tasks(id) VALUES(1)")
        conn.execute("INSERT INTO videos(id,task_id,platform,vid,url) VALUES(1,1,'douyin','v','u')")
        conn.commit()
        db.insert_comment(conn, 1, None, content=None)
        db.insert_comment(conn, 1, None, content=None)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 1)

        extra = {}
        db.insert_comment(conn, 1, "xhs-u", nickname="小红书用户",
                          content="评论", comment_time="07-18河南",
                          extra=extra, platform="xhs")
        row = conn.execute(
            "SELECT comment_time, extra FROM comments WHERE user_id = 'xhs-u'"
        ).fetchone()
        self.assertEqual(row["comment_time"], f"{datetime.now().year}-07-18")
        self.assertEqual(json.loads(row["extra"])["region"], "河南")

        db.insert_comment(conn, 1, "xhs-old", nickname="旧评论",
                          content="旧评论", comment_time="2025年07月18日河南",
                          platform="xhs")
        old = conn.execute(
            "SELECT comment_time, extra FROM comments WHERE user_id = 'xhs-old'"
        ).fetchone()
        self.assertEqual(old["comment_time"], "2025-07-18")
        self.assertEqual(json.loads(old["extra"])["region"], "河南")
        conn.close()

    def test_cross_platform_same_name_isolated(self):
        conn = db.init_db(self.db_path)
        a = db.upsert_account(conn, "同名", "dy-window", "douyin")
        b = db.upsert_account(conn, "同名", "xhs-window", "xhs")
        self.assertNotEqual(a, b)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 2)
        conn.close()

    def test_account_nickname_is_separate_from_task_binding_key(self):
        conn = db.init_db(self.db_path)
        account_id = db.upsert_account(conn, "1234567899", "window-1", "douyin")
        db.update_account_nickname(conn, account_id, "真实平台昵称")
        row = conn.execute(
            "SELECT name, nickname, bb_window_id FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        self.assertEqual(row["name"], "1234567899")
        self.assertEqual(row["nickname"], "真实平台昵称")
        self.assertEqual(row["bb_window_id"], "window-1")
        # 重复保存账号/窗口不能清空已经读取到的昵称。
        self.assertEqual(
            db.upsert_account(conn, "1234567899", "window-1", "douyin"), account_id
        )
        self.assertEqual(
            conn.execute("SELECT nickname FROM accounts WHERE id = ?", (account_id,)).fetchone()[0],
            "真实平台昵称",
        )
        conn.close()

    def test_distinct_accounts_run_in_parallel(self):
        sched = Scheduler(self.db_path, bb=None, collector=SlowCollector(5, .08))
        sched.add_account("a", "wa", "douyin")
        sched.add_account("b", "wb", "douyin")
        ta = sched.create_task("A", target_count=5, task_accounts=["a"])
        tb = sched.create_task("B", target_count=5, task_accounts=["b"])
        threading.Thread(target=sched.start, args=(ta,), daemon=True).start()
        threading.Thread(target=sched.start, args=(tb,), daemon=True).start()
        self.assertEqual(self.wait_status(sched, ta, {"done"}), "done")
        self.assertEqual(self.wait_status(sched, tb, {"done"}), "done")
        sched.shutdown(close_connections=True)

    def test_shared_account_waits_then_auto_resumes(self):
        sched = Scheduler(self.db_path, bb=None, collector=SlowCollector(4, .1))
        sched.add_account("shared", "w", "douyin")
        ta = sched.create_task("A", target_count=4, task_accounts=["shared"])
        tb = sched.create_task("B", target_count=4, task_accounts=["shared"])
        threading.Thread(target=sched.start, args=(ta,), daemon=True).start()
        time.sleep(.04)
        threading.Thread(target=sched.start, args=(tb,), daemon=True).start()
        self.assertEqual(self.wait_status(sched, tb, {"waiting_account", "done"}), "waiting_account")
        self.assertEqual(self.wait_status(sched, ta, {"done"}), "done")
        self.assertEqual(self.wait_status(sched, tb, {"done"}), "done")
        sched.shutdown(close_connections=True)

    def test_incomplete_task_refills(self):
        collector = RefillCollector()
        sched = Scheduler(self.db_path, bb=None, collector=collector)
        sched.add_account("a", "w", "douyin")
        task = sched.create_task("K", target_count=4, task_accounts=["a"])
        sched.start(task)
        self.assertEqual(self.wait_status(sched, task, {"done"}), "done")
        self.assertGreaterEqual(collector.round, 2)
        sched.shutdown(close_connections=True)

    def test_unconfirmed_search_never_enters_detail_phase(self):
        collector = UnconfirmedPartialCollector()
        sched = Scheduler(self.db_path, bb=None, collector=collector)
        sched.add_account("a", "w", "douyin")
        task = sched.create_task("K", target_count=1000, task_accounts=["a"])
        sched.start(task)
        self.assertEqual(self.wait_status(sched, task, {"incomplete"}), "incomplete")
        report = sched.status_report()["tasks"][task]
        self.assertEqual(report["videos_total"], 0)
        self.assertEqual(collector.detail_calls, 0)
        sched.shutdown(close_connections=True)

    def test_human_search_pauses_and_resumes_same_account(self):
        collector = HumanOnSearchCollector()
        sched = Scheduler(self.db_path, bb=None, collector=collector)
        aid_a = sched.add_account("a", "wa", "douyin")
        sched.add_account("b", "wb", "douyin")
        task = sched.create_task("K", target_count=1, task_accounts=["a", "b"])
        sched.start(task)
        self.assertEqual(self.wait_status(sched, task, {"paused"}), "paused")
        self.assertEqual(sched.status_report()["accounts"]["douyin:" + str(aid_a)]["status"], "waiting_human")
        self.assertTrue(sched.resolve_human(aid_a))
        sched.resume(task)
        sched.start(task)
        self.assertEqual(self.wait_status(sched, task, {"done"}), "done")
        rows = sched.conn.execute(
            "SELECT DISTINCT assigned_account FROM videos WHERE task_id = ?", (task,)
        ).fetchall()
        self.assertEqual([row[0] for row in rows], ["a"])
        sched.shutdown(close_connections=True)

    def test_reconcile_clears_persisted_human_freeze_after_restart(self):
        sched = Scheduler(self.db_path, bb=object(), collector=SlowCollector(0))
        account_id = sched.add_account("a", "window-a", "douyin")
        db.update_account_status(
            sched.ctl, account_id, "waiting_human",
            wait_reason="captcha", wait_since="2026-08-28T16:00:00",
        )
        healthy = {
            "status": "ok",
            "detail": "当前页面未发现验证码或登录拦截",
            "checks": {
                "domain_match": True,
                "captcha": False,
                "login_required": False,
            },
        }
        with (patch("operations.browser_health.BrowserHealthChecker") as checker_cls,
              patch.object(sched, "_restart_waiting_tasks") as restart_waiting):
            checker_cls.return_value.check_current_account.return_value = healthy
            result = sched.reconcile_human_waiting()

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["cleared"], 1)
        row = sched.ctl.execute(
            "SELECT status, wait_reason, wait_since FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        self.assertEqual(row["status"], "idle")
        self.assertIsNone(row["wait_reason"])
        self.assertIsNone(row["wait_since"])
        restart_waiting.assert_not_called()
        sched.shutdown(close_connections=True)

    def test_startup_reconciles_orphaned_working_account(self):
        """旧版本/异常退出留下的 working，重启后必须回到 idle。"""
        sched = Scheduler(self.db_path, bb=None, collector=SlowCollector(0))
        account_id = sched.add_account("残留账号", "window-stale", "douyin")
        # 模拟旧版本只写 status、没有运行租约的数据库残留。
        db.update_account_status(sched.conn, account_id, "working")
        sched.shutdown(close_connections=True)

        restarted = Scheduler(self.db_path, bb=None, collector=SlowCollector(0))
        row = restarted.conn.execute(
            "SELECT status, runtime_owner, runtime_heartbeat, cd_until "
            "FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        self.assertEqual(row["status"], "idle")
        self.assertIsNone(row["runtime_owner"])
        self.assertIsNone(row["runtime_heartbeat"])
        self.assertIsNone(row["cd_until"])
        restarted.shutdown(close_connections=True)

    def test_pause_releases_worker_account_and_keeps_video_checkpoint(self):
        """用户暂停会退出 worker，账号释放，未完成作品保留给后续继续。"""
        sched = Scheduler(self.db_path, bb=None, collector=SlowCollector(8, .4))
        account_id = sched.add_account("暂停释放账号", "window-pause-release", "douyin")
        task_id = sched.create_task(
            "暂停释放", target_count=8, task_accounts=["暂停释放账号"]
        )
        starter = threading.Thread(target=sched.start, args=(task_id,), daemon=True)
        starter.start()

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            row = sched.conn.execute(
                "SELECT status FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if row and row["status"] == "working":
                break
            time.sleep(.02)
        else:
            self.fail("账号未进入 working")

        sched.pause(task_id)
        starter.join(timeout=3)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            row = sched.conn.execute(
                "SELECT status, runtime_owner FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            task_row = sched.conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row and row["status"] == "idle" and task_row and task_row["status"] == "paused":
                break
            time.sleep(.03)
        else:
            self.fail("暂停后账号未释放或任务未保持 paused")
        self.assertIsNone(row["runtime_owner"])
        pending = sched.conn.execute(
            "SELECT COUNT(*) FROM videos WHERE task_id = ? AND status IN ('assigned','collecting')",
            (task_id,),
        ).fetchone()[0]
        self.assertGreater(pending, 0)
        sched.shutdown(close_connections=True)

    def test_human_pause_resumes_unfinished_keyword_search_before_comments(self):
        collector = HumanOnSecondKeywordCollector()
        sched = Scheduler(self.db_path, bb=None, collector=collector)
        aid = sched.add_account("a", "wa", "douyin")
        group_store = KeywordGroupStore(sched.conn)
        group_id = group_store.create_group("恢复测试词组", "douyin")
        group_store.set_terms(group_id, {"core": ["词一、词二"]})
        task = sched.create_task(
            "恢复测试词组", platform="douyin", target_count=1,
            task_accounts=["a"], keyword_group_id=group_id,
        )
        sched.start(task)
        self.assertEqual(self.wait_status(sched, task, {"paused"}), "paused")
        self.assertEqual(collector.search_calls, ["词一", "词二"])
        self.assertEqual(sched.status_report()["tasks"][task]["videos_total"], 1)

        self.assertTrue(sched.resolve_human(aid))
        sched.resume(task)
        sched.start(task)
        self.assertEqual(self.wait_status(sched, task, {"done"}), "done")
        self.assertEqual(collector.search_calls, ["词一", "词二", "词二"])
        self.assertEqual(sched.status_report()["tasks"][task]["videos_total"], 2)
        sched.shutdown(close_connections=True)

    def test_explicit_no_more_is_persisted_and_not_refilled(self):
        collector = ExplicitExhaustedCollector()
        sched = Scheduler(self.db_path, bb=None, collector=collector)
        sched.add_account("a", "w", "douyin")
        task = sched.create_task("K", target_count=2, task_accounts=["a"])
        sched.start(task)
        self.assertEqual(self.wait_status(sched, task, {"incomplete"}), "incomplete")
        row = sched.conn.execute(
            "SELECT search_exhausted, search_stop_reason FROM tasks WHERE id = ?", (task,)
        ).fetchone()
        self.assertEqual(row["search_exhausted"], 1)
        self.assertIn("没有更多", row["search_stop_reason"])
        sched.start(task)
        time.sleep(.2)
        self.assertEqual(collector.search_calls, 1)
        sched.shutdown(close_connections=True)

    def test_manual_continue_reopens_exhausted_search(self):
        collector = ExplicitExhaustedCollector()
        sched = Scheduler(self.db_path, bb=None, collector=collector)
        sched.add_account("a", "w", "douyin")
        task = sched.create_task("K", target_count=2, task_accounts=["a"])
        sched.start(task)
        self.assertEqual(self.wait_status(sched, task, {"incomplete"}), "incomplete")
        sched.start(task, force_search=True)
        self.assertEqual(self.wait_status(sched, task, {"incomplete"}), "incomplete")
        self.assertEqual(collector.search_calls, 2)
        row = sched.conn.execute(
            "SELECT search_exhausted FROM tasks WHERE id = ?", (task,)
        ).fetchone()
        self.assertEqual(row["search_exhausted"], 1)
        sched.shutdown(close_connections=True)

    def test_configured_cooldown_really_waits(self):
        sched = Scheduler(self.db_path, bb=None, collector=SlowCollector(2, .01))
        sched.add_account("a", "w", "douyin")
        task = sched.create_task("K", target_count=2, task_accounts=["a"],
                                 batch_size=1, cooldown_seconds=1)
        started = time.monotonic()
        sched.start(task)
        self.assertEqual(self.wait_status(sched, task, {"done"}), "done")
        self.assertGreaterEqual(time.monotonic() - started, .9)
        sched.shutdown(close_connections=True)

    def test_stop_interrupts_search_phase(self):
        sched = Scheduler(self.db_path, bb=None, collector=CancellableSearch())
        account_id = sched.add_account("a", "w", "douyin")
        task = sched.create_task("K", target_count=2, task_accounts=["a"])
        thread = threading.Thread(target=sched.start, args=(task,), daemon=True)
        thread.start()
        time.sleep(.1)
        started = time.monotonic()
        sched.stop_task(task)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(sched.status_report()["tasks"][task]["status"], "stopped")
        self.assertEqual(
            sched.conn.execute(
                "SELECT status FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()[0],
            "idle",
        )
        sched.shutdown(close_connections=True)


if __name__ == "__main__":
    unittest.main()
