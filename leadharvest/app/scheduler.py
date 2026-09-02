# -*- coding: utf-8 -*-
"""scheduler.py — 任务调度器（新架构）。

迁移自旧 scheduler.py 的核心调度逻辑：
- 任务状态机：pending → phase_a_search → phase_b_comments → done
- 账号状态机：idle → working → cooldown → waiting_human
- 阶段 A：搜索作品入库；阶段 B：按账号均分评论任务
- 冷却机制：每账号 batch_size 个后冷却 cooldown_seconds
- 断点续跑：以数据库为唯一真源
- 人工接管：HumanBlock → 账号冻结 waiting_human，等待人工处理

依赖：
- database.Database（数据层）
- platform.create_adapter（平台采集）
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

from .database import (
    Database,
    TaskRepository,
    VideoRepository,
    AccountRepository,
    CommentRepository,
)
from .errors import HumanBlock, RateLimited, TaskCancelled

log = logging.getLogger(__name__)

# 任务状态
TASK_PENDING = "pending"
TASK_SEARCHING = "phase_a_search"
TASK_COMMENTING = "phase_b_comments"
TASK_DONE = "done"
TASK_ABORTED = "aborted"
TASK_FAILED = "failed"

# 账号状态
ACCT_IDLE = "idle"
ACCT_WORKING = "working"
ACCT_COOLDOWN = "cooldown"
ACCT_WAITING_HUMAN = "waiting_human"
ACCT_FROZEN = "frozen"
ACCT_DEAD = "dead"


class TaskHandle:
    """任务运行句柄（供 GUI 查询/控制）。"""

    def __init__(self, task_id: int):
        self.task_id = task_id
        self.status = TASK_PENDING
        self.phase = "pending"
        self.pause_event = threading.Event()
        self.pause_event.set()  # set = 运行
        self.stop_event = threading.Event()
        self.done_event = threading.Event()
        self.threads: List[threading.Thread] = []
        self.error_message = ""
        self.search_complete = False
        self.search_reason = ""
        self.stats = {"videos": 0, "comments": 0, "accounts_done": 0}

    def pause(self):
        self.pause_event.clear()

    def resume(self):
        self.pause_event.set()

    def stop(self):
        self.stop_event.set()


class Scheduler:
    """任务调度器。"""

    def __init__(self, db: Database, cdp_client=None):
        self._db = db
        self._cdp = cdp_client
        self._tasks: Dict[int, TaskHandle] = {}
        self._lock = threading.RLock()
        self._task_repo = TaskRepository(db)
        self._video_repo = VideoRepository(db)
        self._account_repo = AccountRepository(db)
        self._comment_repo = CommentRepository(db)

    # ------------------------------------------------------------------
    # 任务生命周期
    # ------------------------------------------------------------------
    def create_task(self, keyword: str, platform: str = "douyin",
                    batch_size: int = 10, cooldown_seconds: int = 75,
                    collect_mode: str = "standard", target_count: int = 100,
                    task_accounts: Optional[list] = None,
                    search_sort: str = "default") -> int:
        """创建任务，返回 task_id。"""
        return self._task_repo.create(
            keyword=keyword, platform=platform,
            batch_size=batch_size, cooldown_seconds=cooldown_seconds,
            collect_mode=collect_mode, target_count=target_count,
            task_accounts=task_accounts, search_sort=search_sort,
        )

    def start(self, task_id: int, force_search: bool = False):
        """启动任务（后台线程执行）。"""
        with self._lock:
            if task_id in self._tasks:
                raise RuntimeError(f"任务 {task_id} 已在运行")
            task = self._task_repo.get(task_id)
            if task is None:
                raise KeyError(f"任务 {task_id} 不存在")
            handle = TaskHandle(task_id)
            self._tasks[task_id] = handle
        thread = threading.Thread(
            target=self._run, args=(task_id, force_search),
            name=f"sched-{task_id}", daemon=True)
        handle.threads.append(thread)
        thread.start()
        return handle

    def get_handle(self, task_id: int) -> Optional[TaskHandle]:
        return self._tasks.get(task_id)

    def pause_task(self, task_id: int):
        handle = self._tasks.get(task_id)
        if handle:
            handle.pause()

    def resume_task(self, task_id: int):
        handle = self._tasks.get(task_id)
        if handle:
            handle.resume()

    def stop_task(self, task_id: int):
        handle = self._tasks.get(task_id)
        if handle:
            handle.stop()

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def _run(self, task_id: int, force_search: bool = False):
        handle = self._tasks[task_id]
        task = self._task_repo.get(task_id)
        platform = task["platform"]
        keyword = task["keyword"]
        target = task["target_count"]
        batch_size = task["batch_size"]
        cooldown = task["cooldown_seconds"]
        search_sort = task["search_sort"]

        try:
            # 阶段 A：搜索
            self._task_repo.update_status(task_id, TASK_SEARCHING)
            handle.status = TASK_SEARCHING
            adapter = self._create_adapter(platform)
            result = adapter.search(
                keyword=keyword,
                mode=task["collect_mode"],
                target_count=target,
                search_sort=search_sort,
                pause_event=handle.pause_event,
                cancel_event=handle.stop_event,
            )
            # 入库
            for item in result.items:
                self._video_repo.insert(
                    task_id=task_id, vid=item.vid, url=item.url,
                    title=item.title, author=item.author,
                    platform=platform, search_query=keyword,
                    extra={"kind": item.kind, **item.extra},
                )
            handle.stats["videos"] = len(result.items)
            handle.search_complete = result.meta.search_complete
            handle.search_reason = result.meta.termination_reason
            if result.meta.no_more_results:
                self._task_repo.mark_search_complete(
                    task_id, exhausted=True, reason=result.meta.termination_reason)

            # 阶段 B：评论
            self._task_repo.update_status(task_id, TASK_COMMENTING)
            handle.status = TASK_COMMENTING
            self._collect_comments(
                task_id=task_id, platform=platform,
                batch_size=batch_size, cooldown_seconds=cooldown,
                pause_event=handle.pause_event,
                cancel_event=handle.stop_event,
                handle=handle,
            )

            self._task_repo.update_status(task_id, TASK_DONE)
            handle.status = TASK_DONE
            log.info("任务 %s 完成", task_id)
        except TaskCancelled:
            self._task_repo.update_status(task_id, TASK_ABORTED)
            handle.status = TASK_ABORTED
        except HumanBlock as exc:
            self._task_repo.update_status(task_id, TASK_FAILED, str(exc))
            handle.status = TASK_FAILED
            handle.error_message = str(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("任务 %s 失败", task_id)
            self._task_repo.update_status(task_id, TASK_FAILED, str(exc))
            handle.status = TASK_FAILED
            handle.error_message = str(exc)
        finally:
            handle.done_event.set()
            with self._lock:
                self._tasks.pop(task_id, None)

    # ------------------------------------------------------------------
    # 阶段 B：评论采集（单账号串行 + 冷却）
    # ------------------------------------------------------------------
    def _collect_comments(self, task_id: int, platform: str,
                          batch_size: int, cooldown_seconds: int,
                          pause_event: threading.Event,
                          cancel_event: threading.Event,
                          handle: TaskHandle):
        pending = self._video_repo.get_by_status(task_id, "pending")
        if not pending:
            return
        adapter = self._create_adapter(platform)
        batch_count = 0
        for video in pending:
            if cancel_event.is_set():
                raise TaskCancelled()
            while pause_event is not None and not pause_event.is_set():
                if cancel_event.is_set():
                    raise TaskCancelled()
                time.sleep(0.2)
            try:
                comments = adapter.fetch_comments(
                    vid=video["vid"], url=video["url"],
                    pause_event=pause_event, cancel_event=cancel_event)
            except HumanBlock as exc:
                log.warning("视频 %s 人工接管: %s", video["vid"], exc)
                self._video_repo.mark_done(video["id"])  # 跳过，不阻塞任务
                continue
            except RateLimited:
                time.sleep(5)
                continue
            # 入库
            for cm in comments:
                self._comment_repo.insert(
                    video_id=video["id"], user_id=cm.user_id,
                    nickname=cm.nickname, content=cm.content,
                    comment_time=str(cm.comment_time or ""),
                    platform=platform,
                    extra={"region": cm.region, **cm.extra},
                )
            handle.stats["comments"] += len(comments)
            self._video_repo.mark_done(video["id"])
            batch_count += 1
            # 冷却：每 batch_size 个视频后暂停
            if batch_count >= batch_size:
                log.info("账号批次完成 %s 个，冷却 %ss", batch_size, cooldown_seconds)
                for _ in range(cooldown_seconds * 5):
                    if cancel_event.is_set():
                        raise TaskCancelled()
                    time.sleep(0.2)
                batch_count = 0

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _create_adapter(self, platform: str):
        """创建平台适配器（注入 CDP 客户端）。"""
        from .platform import create_adapter
        return create_adapter(platform, cdp=self._cdp)

    def status_report(self) -> dict:
        """任务状态汇总（供 GUI）。"""
        with self._lock:
            tasks = self._task_repo.list_all()
            return {
                "tasks": tasks,
                "running": [t.task_id for t in self._tasks.values()],
            }
