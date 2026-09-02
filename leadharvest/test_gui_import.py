# -*- coding: utf-8 -*-
"""test_gui_import.py — GUI 模块导入与调度器集成验证。

不打开真实窗口，验证：
1. app.ui.app 模块可正常导入
2. 调度器 + 数据库可创建任务并完成（mock 适配器）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 1. GUI 模块导入
try:
    from app.ui.app import LeadHarvestApp
    from app.ui.theme import PLATFORM_CN, TASK_STATUS_CN
    print("[1] GUI 模块导入 OK")
    print(f"    平台映射: {PLATFORM_CN}")
    print(f"    状态映射: {list(TASK_STATUS_CN.keys())[:3]}...")
except Exception as exc:
    print(f"[1] GUI 导入失败: {exc}")
    raise

# 2. 调度器集成（mock）
from app.database import Database
from app.scheduler import Scheduler
from app.platform.base import VideoItem, CommentItem, SearchResult, SearchMeta


class MockAdapter:
    platform = "douyin"

    def __init__(self, cdp=None):
        pass

    def search(self, keyword, mode="standard", target_count=100, window_id="",
               search_sort="default", pause_event=None, cancel_event=None):
        items = [VideoItem(vid=f"v{i}", url=f"https://x/{i}",
                           title=f"T{i}", keyword=keyword) for i in range(3)]
        return SearchResult(items=items, meta=SearchMeta(search_complete=True))

    def fetch_comments(self, vid, url="", window_id="",
                       pause_event=None, cancel_event=None):
        return [CommentItem(user_id=f"u{vid}_{i}", nickname=f"N{vid}",
                            content=f"C{vid}-{i}") for i in range(2)]


db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "test_gui.db")
try:
    if os.path.exists(db_path):
        os.remove(db_path)
except PermissionError:
    pass
db = Database(db_path)
db.connect()
sched = Scheduler(db)
sched._create_adapter = lambda platform: MockAdapter()

task_id = sched.create_task(keyword="测试", platform="douyin", target_count=3)
handle = sched.start(task_id)
handle.done_event.wait(timeout=30)
print(f"[2] 调度器集成: 任务 {task_id} 状态={handle.status}")
assert handle.status == "done", f"任务未完成: {handle.status}"

# 3. status_report 供 GUI 使用
report = sched.status_report()
print(f"[3] status_report: {len(report['tasks'])} 个任务")
assert len(report["tasks"]) >= 1

print("\n[PASS] GUI 导入 + 调度器集成验证通过!")


# 4. 验证 GUI 构建逻辑（无窗口：直接构造 Tk 根会弹窗，跳过实际显示）
# 只验证 LeadHarvestApp 的辅助逻辑不依赖旧代码
print("[4] GUI 类可实例化验证: 等待真实窗口环境（跳过）")
print("\n全部验证完成")
