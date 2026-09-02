# -*- coding: utf-8 -*-
"""定时增量监控执行器。

执行器只负责轮询监控规则并把到期任务交给 Scheduler；采集、浏览器和
任务状态机仍由 Scheduler 负责，避免出现第二套任务执行逻辑。
"""

from __future__ import annotations

import threading

from .monitoring import MonitoringRuleStore


class MonitoringRunner:
    """在后台线程中触发到期的定时增量任务。"""

    def __init__(self, scheduler, poll_seconds: float = 15.0, clock=None):
        self.scheduler = scheduler
        self.poll_seconds = max(0.2, float(poll_seconds))
        self.clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._lock = threading.RLock()

    def _due_task_ids(self) -> list[int]:
        """读取到期任务；共享连接访问遵守 Scheduler 的连接锁。"""
        due = []
        with self.scheduler._conn_lock:  # scheduler 的 SQLite 共享连接锁
            rows = self.scheduler.conn.execute(
                "SELECT id, status FROM tasks WHERE execution_mode = 'monitoring' "
                "AND status NOT IN ('phase_a_search', 'phase_b_comments', 'running') "
                "ORDER BY id"
            ).fetchall()
            rules = MonitoringRuleStore(self.scheduler.conn)
            for row in rows:
                task_id = int(row["id"])
                try:
                    if rules.due(task_id):
                        due.append(task_id)
                except (KeyError, TypeError, ValueError):
                    continue
        return due

    def run_once(self) -> list[int]:
        """触发一轮到期任务，返回实际提交给 Scheduler 的任务 ID。"""
        started = []
        for task_id in self._due_task_ids():
            try:
                self.scheduler.start(task_id)
                started.append(task_id)
                self.scheduler._emit_log(f"[monitor] 任务#{task_id} 到期，已触发增量采集")
            except Exception as exc:  # 监控不能让后台线程永久退出
                self.scheduler._emit_log(
                    f"[monitor] 任务#{task_id} 触发失败：{type(exc).__name__}: {exc}"
                )
        return started

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="monitor-runner", daemon=True
            )
            self._thread.start()
            return True

    def wake(self) -> None:
        """任务创建/修改后立即唤醒轮询，而不是等待完整轮询周期。"""
        self._wake.set()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # 防御共享数据库/第三方采集器异常
                try:
                    self.scheduler._emit_log(
                        f"[monitor] 轮询失败：{type(exc).__name__}: {exc}"
                    )
                except Exception:
                    pass
            self._wake.wait(self.poll_seconds)
            self._wake.clear()
