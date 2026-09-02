# -*- coding: utf-8 -*-
"""互动中心 Presenter 单元测试（技术方案 Agent F / 9.7）。

覆盖：草稿列表、提交审核、批准、拒绝、勿扰。全部走 Presenter 层，
不依赖 Tk 控件。批量批准必须逐条返回成功/失败原因（9.7 红线）。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import db
from leads.models import DraftStatus, LeadStatus, Pool
from leads.repository import LeadRepository
from leads.service import LeadService
from interactions.repository import InteractionRepository
from interactions.service import InteractionService
from ui.pages.interaction_page import InteractionPagePresenter


class TestInteractionPagePresenter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.init_db(os.path.join(self.tmp, "platform.db"),
                               check_same_thread=False)
        self.conn.execute("INSERT INTO tasks (keyword, platform) VALUES ('k', 'douyin')")
        self.conn.execute("INSERT INTO videos (task_id, platform, vid, url, title) "
                          "VALUES (1, 'douyin', 'v1', 'u1', 't1')")
        self.conn.commit()
        self.lead_repo = LeadRepository(self.conn)
        self.lead_service = LeadService(self.lead_repo)
        self.inter_repo = InteractionRepository(self.conn)
        self.service = InteractionService(self.inter_repo, self.lead_repo)
        self.presenter = InteractionPagePresenter(self.service)

    def tearDown(self):
        self.conn.close()

    def _make_henan_lead(self):
        c = self.conn.execute(
            "INSERT INTO comments (video_id, platform, user_id, nickname, content, comment_time) "
            "VALUES (1, 'douyin', 'u-1', '小王', '想了解价格怎么联系', '2026-08-20T10:00:00')"
        )
        self.lead_service.ingest_comment(c.lastrowid, context={"self_declared": "河南郑州"})
        lead = self.conn.execute("SELECT * FROM leads").fetchone()
        return lead["id"]

    def test_list_drafts_empty(self):
        data = self.presenter.list_drafts("pending_review")
        self.assertEqual(data["total"], 0)

    def test_interaction_center_exposes_four_operational_tabs(self):
        self.assertEqual(
            [(key, label) for key, label, _enabled in self.presenter.TABS],
            [("draft", "待生成"), ("queued", "待发送"),
             ("sent", "已回复"), ("failed", "失败")],
        )

    def test_create_and_submit_draft(self):
        lid = self._make_henan_lead()
        draft_id = self.service.create_draft(lid, template_id="greeting")
        self.presenter.submit(draft_id)
        draft = self.inter_repo.get_draft(draft_id)
        self.assertEqual(draft["status"], DraftStatus.PENDING_REVIEW)

    def test_approve_draft(self):
        lid = self._make_henan_lead()
        draft_id = self.service.create_draft(lid, template_id="greeting")
        self.presenter.submit(draft_id)
        self.presenter.approve(draft_id, "您好小王！请联系我们。")
        draft = self.inter_repo.get_draft(draft_id)
        # 互动中心批准后直接进入待发送池，账号在待发送页签中选择。
        self.assertEqual(draft["status"], DraftStatus.QUEUED)
        self.assertEqual(draft["reviewed_by"], "ui-user")

    def test_reply_accounts_are_available_for_queue(self):
        self.conn.execute(
            "INSERT INTO accounts (name, bb_window_id, platform, status) "
            "VALUES ('抖音测试账号', 'bb-1', 'douyin', 'idle')"
        )
        self.conn.commit()
        accounts = self.presenter.list_reply_accounts()
        self.assertEqual(accounts[0]["name"], "抖音测试账号")

    def test_reply_account_must_match_source_platform(self):
        account_id = self.conn.execute(
            "INSERT INTO accounts (name, bb_window_id, platform, status) "
            "VALUES ('小红书测试账号', 'xhs-1', 'xhs', 'idle')"
        ).lastrowid
        self.conn.commit()
        lead_id = self._make_henan_lead()
        draft_id = self.service.create_draft(lead_id, template_id="greeting")
        self.presenter.enter_send_page(draft_id, "待发送内容")
        with self.assertRaisesRegex(ValueError, "回复账号平台不匹配"):
            self.presenter.assign_reply_account(draft_id, account_id)

    def test_reject_draft(self):
        lid = self._make_henan_lead()
        draft_id = self.service.create_draft(lid, template_id="greeting")
        self.presenter.submit(draft_id)
        self.presenter.reject(draft_id, "需修改措辞")
        draft = self.inter_repo.get_draft(draft_id)
        self.assertEqual(draft["status"], DraftStatus.REJECTED)

    def test_suppress_lead(self):
        lid = self._make_henan_lead()
        self.presenter.suppress(lid, "用户拒绝")
        lead = self.conn.execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone()
        self.assertEqual(lead["status"], LeadStatus.SUPPRESSED)
        self.assertTrue(self.inter_repo.is_suppressed(lead["dedupe_key"]))

    def test_to_card_shape(self):
        lid = self._make_henan_lead()
        draft_id = self.service.create_draft(lid, template_id="greeting")
        self.presenter.submit(draft_id)
        data = self.presenter.list_drafts("pending_review")
        card = self.presenter.to_card(data["items"][0])
        for key in ("draft_id", "lead_id", "nickname", "platform", "region",
                    "comment_time", "intent", "pool", "content", "status",
                    "original_comment", "failure_reason"):
            self.assertIn(key, card)
        self.assertEqual(card["lead_id"], lid)
        self.assertNotEqual(card["intent"], "unknown")
        self.assertEqual(card["platform"], "抖音")
        self.assertEqual(card["comment_time"], "2026-08-20T10:00:00")
        self.assertEqual(card["original_comment"], "想了解价格怎么联系")
        self.assertIn("可信度", card["region"])

    def test_templates_can_render_for_selected_lead(self):
        lid = self._make_henan_lead()
        templates = self.presenter.list_templates()
        self.assertIn("greeting", templates)
        text = self.presenter.render_template("greeting", lid)
        self.assertIn("小王", text)

    def test_submit_with_edited_content(self):
        lid = self._make_henan_lead()
        draft_id = self.service.create_draft(lid, template_id="greeting")
        self.presenter.submit_with_content(draft_id, "您好小王！这是修改后的回复。")
        draft = self.inter_repo.get_draft(draft_id)
        self.assertEqual(draft["status"], DraftStatus.PENDING_REVIEW)
        self.assertEqual(draft["content"], "您好小王！这是修改后的回复。")

    def test_enter_send_page_and_delete(self):
        lid = self._make_henan_lead()
        draft_id = self.service.create_draft(lid, template_id="greeting")
        self.presenter.enter_send_page(draft_id, "修改后的完整回复内容")
        self.assertEqual(
            self.inter_repo.get_draft(draft_id)["status"], DraftStatus.QUEUED
        )
        self.assertEqual(
            self.inter_repo.get_draft(draft_id)["content"], "修改后的完整回复内容"
        )
        self.presenter.delete(draft_id)
        self.assertEqual(
            self.inter_repo.get_draft(draft_id)["status"], DraftStatus.CANCELLED
        )

    def test_failed_items_can_return_to_draft_or_be_deleted(self):
        lid = self._make_henan_lead()
        retry_id = self.service.create_draft(lid, template_id="greeting")
        self.presenter.enter_send_page(retry_id, "失败后保留的完整回复")
        self.presenter.mark_reply_failed(retry_id, "微博评论尚未加载", account_id=None)
        failed_data = self.presenter.list_drafts("failed")
        failed_card = self.presenter.to_card(failed_data["items"][0])
        self.assertEqual(failed_card["original_comment"], "想了解价格怎么联系")
        self.assertEqual(failed_card["failure_reason"], "微博评论尚未加载")
        self.presenter.return_failed_to_draft(retry_id)
        retry = self.inter_repo.get_draft(retry_id)
        self.assertEqual(retry["status"], DraftStatus.DRAFT)
        self.assertEqual(retry["content"], "失败后保留的完整回复")
        self.assertIsNone(retry["reply_account_id"])

        delete_id = self.service.create_draft(lid, template_id="greeting")
        self.presenter.enter_send_page(delete_id, "准备删除的失败回复")
        self.presenter.mark_reply_failed(delete_id, "测试失败", account_id=None)
        self.presenter.delete(delete_id)
        self.assertEqual(
            self.inter_repo.get_draft(delete_id)["status"], DraftStatus.CANCELLED
        )

    def test_sent_filter_supports_platform_and_account(self):
        account_id = self.conn.execute(
            "INSERT INTO accounts (name, bb_window_id, platform, status) "
            "VALUES ('抖音测试账号', 'bb-1', 'douyin', 'idle')"
        ).lastrowid
        self.conn.commit()
        lid = self._make_henan_lead()
        draft_id = self.service.create_draft(lid, template_id="greeting")
        self.presenter.enter_send_page(draft_id, "已回复内容")
        self.presenter.assign_reply_account(draft_id, account_id)
        self.inter_repo.update_draft_status(draft_id, DraftStatus.SENDING)
        self.inter_repo.update_draft_status(draft_id, DraftStatus.SENT)
        data = self.presenter.list_drafts(
            "sent", platform="douyin", account_id=account_id
        )
        self.assertEqual(data["total"], 1)
        card = self.presenter.to_card(data["items"][0])
        self.assertEqual(card["reply_account_name"], "抖音测试账号")


if __name__ == "__main__":
    unittest.main()
