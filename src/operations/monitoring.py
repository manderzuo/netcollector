# -*- coding: utf-8 -*-
"""定时增量监控规则。只负责时间和规则，不负责启动浏览器。"""

from __future__ import annotations

from datetime import datetime, timedelta


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("T", " "))
    except ValueError:
        return None


def _minute(value: str | None) -> int | None:
    if not value:
        return None
    try:
        hour, minute = str(value).split(":", 1)
        hour, minute = int(hour), int(minute)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute
    except (TypeError, ValueError):
        return None


def in_work_window(now: datetime, work_start: str | None, work_end: str | None) -> bool:
    start, end = _minute(work_start), _minute(work_end)
    if start is None or end is None or start == end:
        return True
    current = now.hour * 60 + now.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end


class MonitoringRuleStore:
    def __init__(self, conn, clock=None):
        self.conn = conn
        self.clock = clock or _now

    def upsert(self, task_id: int, interval_seconds: int = 3600, enabled: bool = True,
               work_start: str | None = None, work_end: str | None = None,
               next_run_at: str | None = None) -> int:
        interval = max(60, int(interval_seconds))
        now = self.clock()
        next_run = next_run_at or (now + timedelta(seconds=interval)).isoformat(sep=" ")
        existing = self.conn.execute(
            "SELECT id FROM monitoring_rules WHERE task_id = ?", (int(task_id),)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE monitoring_rules SET enabled = ?, interval_seconds = ?, "
                "work_start = ?, work_end = ?, next_run_at = ?, no_more_observed = 0, "
                "updated_at = ? WHERE task_id = ?",
                (int(bool(enabled)), interval, work_start, work_end, next_run, now.isoformat(sep=" "), int(task_id)),
            )
            rule_id = int(existing[0])
        else:
            cur = self.conn.execute(
                "INSERT INTO monitoring_rules "
                "(task_id, enabled, interval_seconds, work_start, work_end, next_run_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (int(task_id), int(bool(enabled)), interval, work_start, work_end,
                 next_run, now.isoformat(sep=" "), now.isoformat(sep=" ")),
            )
            rule_id = int(cur.lastrowid)
        self.conn.execute(
            "UPDATE tasks SET execution_mode = 'monitoring', next_run_at = ? WHERE id = ?",
            (next_run, int(task_id)),
        )
        self.conn.commit()
        return rule_id

    def get_for_task(self, task_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM monitoring_rules WHERE task_id = ?", (int(task_id),)
        ).fetchone()
        return dict(row) if row else None

    def due(self, task_id: int, *, force: bool = False) -> bool:
        rule = self.get_for_task(task_id)
        if not rule or not int(rule["enabled"]):
            return False
        if int(rule["no_more_observed"] or 0) and not force:
            return False
        if force:
            return True
        now = self.clock()
        due_at = _parse(rule.get("next_run_at"))
        return due_at is None or due_at <= now

    def mark_run(self, task_id: int, run_id: str, *, no_more: bool = False) -> None:
        rule = self.get_for_task(task_id)
        if not rule:
            raise KeyError(f"监控规则不存在: {task_id}")
        now = self.clock()
        next_run = now + timedelta(seconds=max(60, int(rule["interval_seconds"] or 3600)))
        self.conn.execute(
            "UPDATE monitoring_rules SET last_run_at = ?, last_run_id = ?, "
            "no_more_observed = ?, next_run_at = ?, updated_at = ? WHERE task_id = ?",
            (now.isoformat(sep=" "), run_id, int(bool(no_more)), next_run.isoformat(sep=" "),
             now.isoformat(sep=" "), int(task_id)),
        )
        self.conn.execute(
            "UPDATE tasks SET next_run_at = ? WHERE id = ?",
            (next_run.isoformat(sep=" "), int(task_id)),
        )
        self.conn.commit()
