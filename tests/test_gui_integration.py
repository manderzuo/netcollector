# -*- coding: utf-8 -*-
"""主窗口集成测试（技术方案 Agent H / 9.9）。

验证：导航新增页、页面预加载与无重绘切换、共享连接、关闭释放。
用 ``root.withdraw()`` 隐藏窗口运行，避免干扰桌面。
"""
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import tkinter as tk

from leads.repository import LeadRepository  # noqa: E402


class TestGuiIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def _make_app(self):
        import gui

        tmp = tempfile.mkdtemp()
        app = gui.GuiApp(self.root, db_path=os.path.join(tmp, "gui_test.db"), demo=True)
        return app

    def test_nav_has_new_pages(self):
        app = self._make_app()
        self.assertIn("leads", app._nav_buttons)
        self.assertIn("interaction", app._nav_buttons)
        self.assertIn("settings", app._nav_buttons)
        # 关键词组改为从新建任务弹窗进入，不再占用左侧主导航；内部管理页仍预加载。
        self.assertNotIn("keywords", app._nav_buttons)
        self.assertIsNotNone(app.leads_page)
        self.assertIsNotNone(app.interaction_page)
        self.assertIsNotNone(app.settings_page)
        self.assertIsNotNone(app.keywords_page)
        # 页面启动时已挂入层叠容器，切换只 lift，不再反复卸载页面。
        self.assertEqual(app.leads_page.winfo_manager(), "place")
        self.assertEqual(app.interaction_page.winfo_manager(), "place")

    def test_switch_to_settings_page(self):
        app = self._make_app()
        app._show_page("settings")
        self.assertEqual(app._active_page, "settings")
        self.assertEqual(app.settings_page.winfo_manager(), "place")

    def test_switch_to_keywords_page(self):
        app = self._make_app()
        app._show_page("keywords")
        self.assertEqual(app._active_page, "keywords")
        self.assertEqual(app.keywords_page.winfo_manager(), "place")

    def test_keyword_group_choice_accepts_legacy_display_platform(self):
        import gui
        choices = gui.GuiApp._keyword_group_choices(
            [{"id": 7, "name": "洗衣组合", "platform": "全部平台", "version": 2}],
            "douyin",
        )
        self.assertEqual(choices[1][0], "7")

    def test_main_window_is_resizable(self):
        app = self._make_app()
        self.assertEqual(tuple(bool(v) for v in app.root.resizable()), (True, True))

    def test_switch_to_leads_page(self):
        app = self._make_app()
        before = app.leads_page._refresh_request
        app._show_page("leads")
        self.assertIsNotNone(app.leads_page)
        # 切换进入线索中心不自动查库，避免点击左侧 TAB 时卡顿。
        self.assertEqual(app.leads_page._refresh_request, before)
        interaction_before = app.interaction_page._refresh_request
        app._show_page("interaction")
        self.assertIsNotNone(app.interaction_page)
        self.assertEqual(app.interaction_page._refresh_request, interaction_before)

    def test_switched_pages_remain_mapped_for_instant_return(self):
        app = self._make_app()
        app._show_page("leads")
        self.assertEqual(app.leads_page.winfo_manager(), "place")
        app._show_page("tasks")
        # 页面始终驻留在层叠容器中，切换只改变前后层级。
        self.assertEqual(app.leads_page.winfo_manager(), "place")
        self.assertEqual(float(app.tab_task.place_info()["relwidth"]), 1.0)
        app._show_page("leads")
        self.assertEqual(app._active_page, "leads")
        self.assertEqual(app.leads_page.winfo_manager(), "place")

    def test_shared_connection(self):
        app = self._make_app()
        c1 = app._lead_conn()
        c2 = app._lead_conn()
        self.assertIs(c1, c2)
        self.assertIsNotNone(app._lead_db_conn)
        app._lead_db_conn.close()
        app._lead_db_conn = None

    def test_leads_export_writes_file(self):
        app = self._make_app()
        app._show_page("leads")
        # 造一条线索供导出
        conn = app._lead_conn()
        conn.execute("INSERT INTO tasks (keyword, platform) VALUES ('k', 'douyin')")
        conn.execute("INSERT INTO videos (task_id, platform, vid, url, title) "
                     "VALUES (1, 'douyin', 'v1', 'u1', 't1')")
        c = conn.execute(
            "INSERT INTO comments (video_id, platform, user_id, nickname, content, comment_time) "
            "VALUES (1, 'douyin', 'u-1', '小王', '想了解价格', '2026-08-20T10:00:00')"
        )
        conn.commit()
        from leads.service import LeadService
        LeadService(LeadRepository(conn)).ingest_comment(
            c.lastrowid, context={"self_declared": "河南郑州"})
        lead = conn.execute("SELECT id FROM leads").fetchone()
        target = os.path.join(tempfile.mkdtemp(), "exp")
        from leads.exporter import LeadExporter
        path = LeadExporter(LeadRepository(conn)).export([lead["id"]], target)
        self.assertTrue(os.path.exists(path))
        conn.close()
        app._lead_db_conn = None

    def test_add_lead_to_interaction_keeps_current_page(self):
        app = self._make_app()
        app._show_page("leads")
        conn = app._lead_conn()
        task_id = conn.execute(
            "INSERT INTO tasks (keyword, platform) VALUES ('k2', 'douyin')"
        ).lastrowid
        video_id = conn.execute(
            "INSERT INTO videos (task_id, platform, vid, url, title) "
            "VALUES (?, 'douyin', 'v2', 'https://example.com/v2', 't2')",
            (task_id,),
        ).lastrowid
        comment_id = conn.execute(
            "INSERT INTO comments (video_id, platform, user_id, nickname, content, comment_time) "
            "VALUES (?, 'douyin', 'u-2', '小李', '想了解价格怎么联系', '2026-08-20T10:00:00')",
            (video_id,),
        ).lastrowid
        from leads.service import LeadService
        LeadService(LeadRepository(conn)).ingest_comment(
            comment_id, context={"self_declared": "河南郑州"}
        )
        lead_id = conn.execute(
            "SELECT id FROM leads WHERE source_comment_id = ?", (comment_id,)
        ).fetchone()[0]

        app._on_leads_to_interaction([lead_id])
        deadline = time.time() + 3
        while getattr(app, "_interaction_add_inflight", False) and time.time() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.root.update()

        self.assertFalse(getattr(app, "_interaction_add_inflight", False))
        self.assertEqual(app._active_page, "leads")
        draft = conn.execute(
            "SELECT lead_id, status FROM interaction_drafts WHERE lead_id = ?",
            (lead_id,),
        ).fetchone()
        self.assertIsNotNone(draft)
        self.assertEqual(draft[0], lead_id)
        self.assertEqual(draft[1], "draft")


if __name__ == "__main__":
    unittest.main()
