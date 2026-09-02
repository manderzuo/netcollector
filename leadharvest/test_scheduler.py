# -*- coding: utf-8 -*-
"""test_scheduler.py — 调度器核心逻辑测试（mock 适配器）。

验证：任务创建 → 阶段A入库 → 阶段B评论 → 冷却 → done。
不依赖真实浏览器。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import Database  # noqa: E402
from app.scheduler import Scheduler  # noqa: E402
from app.platform.base import (  # noqa: E402
    VideoItem, CommentItem, SearchResult, SearchMeta,
)


class MockAdapter:
    """假适配器：返回确定性数据。"""

    platform = "douyin"

    def __init__(self, cdp=None):
        pass

    def search(self, keyword, mode="standard", target_count=100, window_id="",
               search_sort="default", pause_event=None, cancel_event=None):
        items = [VideoItem(vid=f"v{i}", url=f"https://x/{i}", title=f"T{i}",
                           keyword=keyword) for i in range(5)]
        return SearchResult(items=items, meta=SearchMeta(search_complete=True,
                                                         reached_target=True))

    def fetch_comments(self, vid, url="", window_id="",
                       pause_event=None, cancel_event=None):
        return [CommentItem(user_id=f"u{vid}_{i}", nickname=f"用户{vid}",
                            content=f"评论内容-{vid}-{i}") for i in range(3)]


def main():
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "test_scheduler.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    db = Database(db_path)
    db.connect()

    sched = Scheduler(db)
    # 注入 mock 适配器
    sched._create_adapter = lambda platform: MockAdapter()

    # 1. 创建任务
    task_id = sched.create_task(
        keyword="测试", platform="douyin", target_count=5,
        batch_size=3, cooldown_seconds=1)
    print(f"[1] 任务创建: id={task_id}")

    # 2. 启动并等待完成
    handle = sched.start(task_id)
    handle.done_event.wait(timeout=30)
    print(f"[2] 任务状态: {handle.status}")
    print(f"    stats: {handle.stats}")

    # 3. 验证数据库
    task = sched._task_repo.get(task_id)
    print(f"[3] DB 任务: status={task['status']}")
    videos = sched._video_repo.get_by_status(task_id, "done")
    print(f"    DB 已完成视频: {len(videos)}")
    total_comments = 0
    for v in videos:
        total_comments += len(sched._comment_repo.list_by_video(v["id"]))
    print(f"    DB 总评论: {total_comments}")

    # 4. 断言
    assert handle.status == "done", f"任务未完成: {handle.status}"
    assert len(videos) == 5, f"视频数错误: {len(videos)}"
    assert total_comments == 15, f"评论数错误: {total_comments}"
    print("\n[PASS] 调度器核心逻辑验证通过!")

    # 清理
    db_path2 = db_path
    if os.path.exists(db_path2):
        os.remove(db_path2)


if __name__ == "__main__":
    main()
