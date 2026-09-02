# -*- coding: utf-8 -*-
"""新中心回归测试：覆盖真实评论字段、增量分类、勿扰和 UI 查询映射。"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import db
from interactions.repository import InteractionRepository
from interactions.service import InteractionService
from interactions.models import ReplyActionResult
from leads.assignment import AssignmentService
from leads.models import DraftStatus, LeadStatus, Pool
from leads.region import RegionClassifier
from leads.repository import LeadRepository
from leads.service import LeadService
from scheduler import Scheduler
from ui.pages.leads_page import LeadPagePresenter


class NewCenterRegressionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "platform.db")
        self.conn = db.init_db(self.path, check_same_thread=False)
        self.conn.execute("INSERT INTO tasks (keyword, platform) VALUES ('k', 'douyin')")
        self.conn.execute(
            "INSERT INTO videos (task_id, platform, vid, url, title) "
            "VALUES (1, 'douyin', 'v1', 'u1', 't1')"
        )
        self.conn.commit()
        self.lead_repo = LeadRepository(self.conn)
        self.lead_service = LeadService(self.lead_repo)

    def tearDown(self):
        self.conn.close()

    def add_comment(self, *, user_id="u-1", content="想了解价格怎么联系",
                    comment_time="2026-08-20T10:00:00", extra=None):
        cur = self.conn.execute(
            "INSERT INTO comments "
            "(video_id, platform, user_id, nickname, content, comment_time, extra) "
            "VALUES (1, 'douyin', ?, '小王', ?, ?, ?)",
            (user_id, content, comment_time,
             json.dumps(extra, ensure_ascii=False) if extra is not None else None),
        )
        self.conn.commit()
        return cur.lastrowid


class TestNewCenterRegressions(NewCenterRegressionBase):
    def test_comment_extra_is_used_for_profile_and_region(self):
        cid = self.add_comment(
            content="想了解价格",
            extra={"homepage": "https://example.com/u/1", "region": "河南省"},
        )
        lead_id = self.lead_service.ingest_comment(cid)
        lead = self.conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        self.assertEqual(lead["profile_url"], "https://example.com/u/1")
        self.assertEqual(lead["region_province"], "河南省")
        self.assertEqual(lead["pool"], Pool.HENAN)

    def test_repeated_user_refreshes_classification_without_downgrading_time(self):
        first = self.add_comment(
            content="普通内容", comment_time="2026-08-20T10:00:00",
        )
        lead_id = self.lead_service.ingest_comment(
            first, context={"self_declared": "广东省"}
        )
        second = self.add_comment(
            content="想了解价格怎么联系", comment_time="2026-08-21T10:00:00",
        )
        self.lead_service.ingest_comment(
            second, context={"self_declared": "河南郑州"}
        )
        lead = self.conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        self.assertEqual(lead["pool"], Pool.HENAN)
        self.assertEqual(lead["intent_level"], "high")
        self.assertEqual(lead["last_interaction_at"], "2026-08-21T10:00:00")

    def test_region_text_and_city_are_recognized(self):
        classifier = RegionClassifier()
        self.assertEqual(classifier.classify(text="我是河南的").province, "河南省")
        self.assertEqual(classifier.classify(profile_region="郑州").province, "河南省")

    def test_platform_filter_maps_ui_label_to_database_value(self):
        presenter = LeadPagePresenter(self.lead_service)
        presenter.set_filter("platform", "抖音")
        self.assertEqual(presenter._build_query(1).platform, "douyin")

    def test_suppression_blocks_new_drafts_and_cancels_existing(self):
        cid = self.add_comment()
        lead_id = self.lead_service.ingest_comment(
            cid, context={"self_declared": "河南郑州"}
        )
        interaction_repo = InteractionRepository(self.conn)
        service = InteractionService(interaction_repo, self.lead_repo)
        draft_id = service.create_draft(lead_id)
        service.add_suppression(lead_id, "用户拒绝")
        draft = interaction_repo.get_draft(draft_id)
        self.assertEqual(draft["status"], DraftStatus.CANCELLED)
        with self.assertRaises(ValueError):
            service.create_draft(lead_id)
        service.remove_suppression(lead_id)
        self.assertEqual(
            self.conn.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()[0],
            "qualified",
        )
        self.assertIsInstance(service.create_draft(lead_id), int)

    def test_manual_selection_bypasses_all_import_qualification_limits(self):
        cid = self.add_comment(content="普通内容", user_id="u-manual")
        lead_id = self.lead_service.ingest_comment(
            cid, context={"self_declared": "广东省"}
        )
        lead = self.lead_repo.get(lead_id)
        self.assertNotEqual(lead["region_province"], "河南省")

        repo = InteractionRepository(self.conn)
        service = InteractionService(repo, self.lead_repo)
        draft_id = service.create_draft(
            lead_id,
            generation_mode="manual-selection",
            manual_override=True,
        )

        draft = repo.get_draft(draft_id)
        self.assertEqual(draft["generation_mode"], "manual-selection")
        snapshot = json.loads(draft["policy_snapshot"])
        self.assertEqual(snapshot["policy_version"], "manual-selection-override-v1")
        self.assertEqual(snapshot["reasons"], ["人工选择放行：跳过自动资格限制"])

    def test_lead_is_hidden_after_entering_interaction_center(self):
        """加入互动中心后线索池不应再次展示同一条线索。"""
        cid = self.add_comment(content="普通内容", user_id="u-hidden")
        lead_id = self.lead_service.ingest_comment(cid)
        interaction_repo = InteractionRepository(self.conn)
        service = InteractionService(interaction_repo, self.lead_repo)
        draft_id = service.create_draft(lead_id, generation_mode="manual-selection")

        from leads.repository import LeadQuery

        hidden = self.lead_repo.list_leads(LeadQuery(page=1, page_size=50))
        self.assertEqual(hidden.total, 0)

        # 取消后恢复在线索池，便于用户重新处理。
        service.delete_draft(draft_id)
        visible = self.lead_repo.list_leads(LeadQuery(page=1, page_size=50))
        self.assertEqual(visible.total, 1)

    def test_queued_draft_can_return_to_generation(self):
        cid = self.add_comment(content="待重新录入", user_id="u-queued-return")
        lead_id = self.lead_service.ingest_comment(cid)
        repo = InteractionRepository(self.conn)
        service = InteractionService(repo, self.lead_repo)
        draft_id = service.create_draft(lead_id, generation_mode="manual-selection")
        service.move_draft_to_send(draft_id, "旧的待发送内容")
        account_id = self.conn.execute(
            "INSERT INTO accounts (name, bb_window_id, platform, created_at) "
            "VALUES ('返回测试账号', 'return-window', 'douyin', datetime('now'))"
        ).lastrowid
        service.assign_reply_account(draft_id, account_id)

        service.return_queued_to_draft(draft_id)
        draft = repo.get_draft(draft_id)
        self.assertEqual(draft["status"], DraftStatus.DRAFT)
        self.assertIsNone(draft["reply_account_id"])
        self.assertEqual(draft["content"], "旧的待发送内容")

    def test_event_idempotency_returns_existing_event(self):
        cid = self.add_comment()
        lead_id = self.lead_service.ingest_comment(
            cid, context={"self_declared": "河南郑州"}
        )
        repo = InteractionRepository(self.conn)
        first = repo.log_event(lead_id, "send_failed", idempotency_key="same-key")
        second = repo.log_event(lead_id, "send_failed", idempotency_key="same-key")
        self.assertEqual(first, second)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM interaction_events WHERE idempotency_key = 'same-key'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_scheduler_comment_hook_creates_lead(self):
        cid = self.add_comment(extra={"region": "河南省"})
        scheduler = Scheduler(self.path)
        try:
            scheduler._ingest_comment_to_lead(scheduler.conn, cid)
            lead = scheduler.conn.execute(
                "SELECT region_province, pool FROM leads WHERE source_comment_id = ?",
                (cid,),
            ).fetchone()
            self.assertEqual(lead["region_province"], "河南省")
            self.assertEqual(lead["pool"], Pool.HENAN)
        finally:
            scheduler.shutdown(close_connections=True)

    def test_unassign_returns_assigned_lead_to_qualified(self):
        cid = self.add_comment(extra={"region": "河南省"})
        lead_id = self.lead_service.ingest_comment(cid)
        owner_id = self.conn.execute(
            "INSERT INTO owners (name, enabled, created_at) "
            "VALUES ('运营', 1, datetime('now'))"
        ).lastrowid
        self.conn.commit()
        assignments = AssignmentService(self.lead_repo)
        self.assertEqual(
            assignments.assign([lead_id], owner_id, "首轮分配").success_count, 1
        )
        self.assertEqual(
            self.conn.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()[0],
            LeadStatus.ASSIGNED,
        )
        self.assertEqual(
            assignments.unassign([lead_id], "重新分配").success_count, 1
        )
        lead = self.conn.execute(
            "SELECT status, owner_id FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        self.assertEqual(lead["status"], LeadStatus.QUALIFIED)
        self.assertIsNone(lead["owner_id"])

    def test_approved_draft_keeps_source_comment_and_browser_reply_requires_confirmation(self):
        cid = self.add_comment(
            content="想了解价格怎么联系",
            extra={"region": "河南省", "platform_comment_id": "platform-cid-1"},
        )
        lead_id = self.lead_service.ingest_comment(cid)
        account_id = self.conn.execute(
            "INSERT INTO accounts (name, bb_window_id, platform, created_at) "
            "VALUES ('抖音测试账号', 'bb-window-1', 'douyin', datetime('now'))"
        ).lastrowid
        self.conn.execute(
            "UPDATE videos SET assigned_account = '抖音测试账号' WHERE id = 1"
        )
        self.conn.commit()

        class FakeReplyAdapter:
            def __init__(self):
                self.calls = []

            def reply(self, target, content, *, confirm=False):
                self.calls.append((target, content, confirm))
                return ReplyActionResult(
                    ok=True,
                    stage="sent" if confirm else "filled",
                    message="ok",
                    verified=True,
                    target=target,
                )

        adapter = FakeReplyAdapter()
        service = InteractionService(
            InteractionRepository(self.conn), self.lead_repo,
            reply_adapter=adapter,
            config={
                "lead_rules": {"minimum_intent_for_draft": "medium"},
                "interaction": {"browser_reply_enabled": False},
            },
        )
        draft_id = service.create_draft(lead_id)
        draft = self.conn.execute(
            "SELECT source_comment_id, source_video_id, platform_comment_id, "
            "source_video_url, source_nickname, source_content, reply_account_id "
            "FROM interaction_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        self.assertEqual(draft["source_comment_id"], cid)
        self.assertEqual(draft["source_video_id"], 1)
        self.assertEqual(draft["platform_comment_id"], "platform-cid-1")
        self.assertEqual(draft["source_video_url"], "u1")
        self.assertEqual(draft["reply_account_id"], account_id)

        service.submit_for_review(draft_id)
        service.review(draft_id, "approved", "tester", "审核后的回复")
        service.stage_for_send(draft_id, account_id)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM interaction_drafts WHERE id = ?", (draft_id,)
            ).fetchone()[0],
            DraftStatus.QUEUED,
        )
        filled = service.simulate_browser_reply(draft_id, account_id)
        self.assertTrue(filled.ok)
        self.assertFalse(adapter.calls[-1][2])

        pending = service.send_browser_reply(draft_id)
        self.assertFalse(pending.ok)
        self.assertEqual(pending.stage, "confirmation_required")
        self.assertEqual(len(adapter.calls), 1)

        with self.assertRaisesRegex(ValueError, "真实发送开关未开启"):
            service.send_browser_reply(draft_id, confirm=True)
        self.assertEqual(len(adapter.calls), 1)

        sent = service.send_browser_reply(
            draft_id, confirm=True, real_send_enabled=True,
        )
        self.assertTrue(sent.ok)
        self.assertTrue(adapter.calls[-1][2])
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM interaction_drafts WHERE id = ?", (draft_id,)
            ).fetchone()[0],
            DraftStatus.SENT,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM leads WHERE id = ?", (lead_id,)
            ).fetchone()[0],
            LeadStatus.CONTACTED,
        )

    def test_manual_selected_draft_can_record_direct_real_send(self):
        """人工选定后直接进入待发送，成功发送必须落库为已联系。"""
        cid = self.add_comment(
            content="人工选定的回复目标",
            extra={"region": "广东省", "platform_comment_id": "manual-cid-1"},
        )
        lead_id = self.lead_service.ingest_comment(cid)
        account_id = self.conn.execute(
            "INSERT INTO accounts (name, bb_window_id, platform, created_at) "
            "VALUES ('抖音人工发送测试账号', 'bb-window-manual-1', 'douyin', datetime('now'))"
        ).lastrowid
        self.conn.execute(
            "UPDATE videos SET assigned_account = '抖音人工发送测试账号' WHERE id = 1"
        )
        self.conn.commit()

        class FakeReplyAdapter:
            def reply(self, target, content, *, confirm=False):
                return ReplyActionResult(
                    ok=True, stage="sent", message="ok", verified=True, target=target,
                )

        service = InteractionService(
            InteractionRepository(self.conn), self.lead_repo,
            reply_adapter=FakeReplyAdapter(),
            config={"interaction": {"browser_reply_enabled": True}},
        )
        draft_id = service.create_draft(
            lead_id, generation_mode="manual-selection", manual_override=True,
        )
        service.move_draft_to_send(draft_id, "人工审核后的回复")
        sent = service.send_browser_reply(
            draft_id, account_id, confirm=True, real_send_enabled=True,
        )

        self.assertTrue(sent.ok)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM interaction_drafts WHERE id = ?", (draft_id,)
            ).fetchone()[0],
            DraftStatus.SENT,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM leads WHERE id = ?", (lead_id,)
            ).fetchone()[0],
            LeadStatus.CONTACTED,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM interaction_events "
                "WHERE draft_id = ? AND event_type = ?", 
                (draft_id, "send_success"),
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
