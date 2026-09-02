# -*- coding: utf-8 -*-
"""线索中心 Presenter 单元测试（技术方案 Agent E / 9.6）。

Presenter 层纯逻辑、不依赖 Tk，可直接单测。覆盖：筛选→查询、行组装、
详情加载。业务判断不得写入控件回调——这里验证 Presenter 承担了判断。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import db
from leads.repository import LeadRepository
from leads.service import LeadService
from ui.pages.leads_page import LeadPagePresenter


class BaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.init_db(os.path.join(self.tmp, "platform.db"),
                               check_same_thread=False)
        self.conn.execute("INSERT INTO tasks (keyword, platform) VALUES ('k', 'douyin')")
        self.conn.execute("INSERT INTO videos (task_id, platform, vid, url, title) "
                          "VALUES (1, 'douyin', 'v1', 'u1', 't1')")
        self.conn.commit()
        self.repo = LeadRepository(self.conn)
        self.service = LeadService(self.repo)
        self.presenter = LeadPagePresenter(self.service, page_size=10)

    def tearDown(self):
        self.conn.close()

    def _add_comment(self, uid, nickname, content, region=None):
        c = self.conn.execute(
            "INSERT INTO comments (video_id, platform, user_id, nickname, content, comment_time) "
            "VALUES (1, 'douyin', ?, ?, ?, '2026-08-20T10:00:00')",
            (uid, nickname, content),
        )
        ctx = {"self_declared": region} if region else {}
        self.service.ingest_comment(c.lastrowid, context=ctx)
        return c.lastrowid


class TestLeadPagePresenter(BaseTestCase):
    def test_load_page_returns_items(self):
        self._add_comment("u-1", "小王", "想了解价格", "河南郑州")
        page = self.presenter.load_page(1)
        self.assertEqual(page.total, 1)
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].platform, "douyin")

    def test_filter_pool(self):
        self._add_comment("u-1", "小王", "想了解价格", "河南郑州")
        self._add_comment("u-2", "老李", "不错", "广东省广州")
        self.presenter.set_filter("pool", "henan")
        page = self.presenter.load_page(1)
        self.assertEqual(page.total, 1)
        self.assertEqual(page.items[0].platform_user_id, "u-1")

    def test_filter_intent(self):
        self._add_comment("u-1", "小王", "想了解价格怎么联系", "河南郑州")  # high
        self._add_comment("u-2", "老李", "随便看看", "河南郑州")  # low
        self.presenter.set_filter("intent", "high")
        page = self.presenter.load_page(1)
        self.assertEqual(page.total, 1)

    def test_filter_task_uses_selected_task_comment(self):
        self.conn.execute(
            "INSERT INTO tasks (keyword, platform) VALUES ('k2', 'douyin')"
        )
        self.conn.execute(
            "INSERT INTO videos (task_id, platform, vid, url, title) "
            "VALUES (2, 'douyin', 'v2', 'u2', 't2')"
        )
        self.conn.commit()
        c1 = self.conn.execute(
            "INSERT INTO comments (video_id, platform, user_id, nickname, content, comment_time) "
            "VALUES (1, 'douyin', 'u-1', '小王', '任务一评论', '2026-08-20T10:00:00')"
        ).lastrowid
        c2 = self.conn.execute(
            "INSERT INTO comments (video_id, platform, user_id, nickname, content, comment_time) "
            "VALUES (2, 'douyin', 'u-2', '小李', '任务二评论', '2026-08-21T10:00:00')"
        ).lastrowid
        self.service.ingest_comment(c1, context={"task_id": 1, "video_id": 1})
        self.service.ingest_comment(c2, context={"task_id": 2, "video_id": 2})

        options = self.presenter.task_options()
        self.assertIn("1", options)
        self.assertIn("2", options)
        self.presenter.set_filter("task", "2")
        page = self.presenter.load_page(1)
        self.assertEqual(page.total, 1)
        self.assertEqual(page.items[0].summary_text, "任务二评论")
        self.assertEqual(page.items[0].source_url, "u2")

    def test_keyword_search(self):
        self._add_comment("u-1", "小王", "想了解价格", "河南郑州")
        self._add_comment("u-2", "老李", "不错", "河南省开封")
        self.presenter.set_keyword("小王")
        page = self.presenter.load_page(1)
        self.assertEqual(page.total, 1)

    def test_clear_filters(self):
        self._add_comment("u-1", "小王", "想了解价格", "河南郑州")
        self.presenter.set_filter("pool", "henan")
        self.presenter.clear_filters()
        page = self.presenter.load_page(1)
        self.assertEqual(page.total, 1)

    def test_to_row_shape(self):
        self._add_comment("u-1", "小王", "想了解价格", "河南郑州")
        row = self.presenter.to_row(self.presenter.load_page(1).items[0])
        for key in ("_identity", "id", "platform", "user", "summary", "region",
                    "intent", "owner", "status"):
            self.assertIn(key, row)
        self.assertEqual(str(row["id"]), row["_identity"])
        self.assertEqual(row["platform"], "抖音")
        self.assertIn("comment", row)
        self.assertIn("comment_time", row)
        self.assertEqual(row["comment_time"], "2026-08-20 10:00:00")
        self.assertIn("source_url", row)
        self.assertEqual(row["source_url"], "点击查看")
        self.assertEqual(row["source_url_value"], "u1")

    def test_load_detail(self):
        self._add_comment("u-1", "小王", "想了解价格", "河南郑州")
        lead_id = self.presenter.load_page(1).items[0].id
        detail = self.presenter.load_detail(lead_id)
        self.assertIn("lead", detail)
        self.assertIn("evidence", detail)
        self.assertEqual(detail["lead"]["id"], lead_id)


if __name__ == "__main__":
    unittest.main()
