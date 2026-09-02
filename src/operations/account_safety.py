# -*- coding: utf-8 -*-
"""账号限额和工作时段检查；只返回判定，不执行任何浏览器操作。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def _minute(value: str | None) -> int | None:
    if not value:
        return None
    try:
        hour, minute = map(int, str(value).split(":", 1))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
    except (TypeError, ValueError):
        pass
    return None


def _in_window(now: datetime, start: str | None, end: str | None) -> bool:
    begin, finish = _minute(start), _minute(end)
    if begin is None or finish is None or begin == finish:
        return True
    current = now.hour * 60 + now.minute
    return begin <= current < finish if begin < finish else current >= begin or current < finish


@dataclass(frozen=True)
class AccountSafetyLimits:
    hourly_collect_limit: int = 0
    daily_reply_limit: int = 0
    min_delay_seconds: int = 0
    max_delay_seconds: int = 0
    work_start: str | None = None
    work_end: str | None = None
    consecutive_failure_limit: int = 3
    enabled: bool = True

    @classmethod
    def from_row(cls, row) -> "AccountSafetyLimits":
        if not row:
            return cls()
        return cls(
            hourly_collect_limit=max(0, int(row.get("hourly_collect_limit") or 0)),
            daily_reply_limit=max(0, int(row.get("daily_reply_limit") or 0)),
            min_delay_seconds=max(0, int(row.get("min_delay_seconds") or 0)),
            max_delay_seconds=max(0, int(row.get("max_delay_seconds") or 0)),
            work_start=row.get("work_start"), work_end=row.get("work_end"),
            consecutive_failure_limit=max(0, int(row.get("consecutive_failure_limit") or 0)),
            enabled=bool(int(row.get("enabled", 1))),
        )


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    reason: str = ""


class AccountSafetyStore:
    def __init__(self, conn, clock=None):
        self.conn = conn
        self.clock = clock or datetime.now

    def save(self, account_id: int, limits: AccountSafetyLimits) -> None:
        now = self.clock().isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO account_limits "
            "(account_id, hourly_collect_limit, daily_reply_limit, min_delay_seconds, "
            "max_delay_seconds, work_start, work_end, consecutive_failure_limit, enabled, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET hourly_collect_limit=excluded.hourly_collect_limit, "
            "daily_reply_limit=excluded.daily_reply_limit, min_delay_seconds=excluded.min_delay_seconds, "
            "max_delay_seconds=excluded.max_delay_seconds, work_start=excluded.work_start, "
            "work_end=excluded.work_end, consecutive_failure_limit=excluded.consecutive_failure_limit, "
            "enabled=excluded.enabled, updated_at=excluded.updated_at",
            (int(account_id), limits.hourly_collect_limit, limits.daily_reply_limit,
             limits.min_delay_seconds, limits.max_delay_seconds, limits.work_start,
             limits.work_end, limits.consecutive_failure_limit, int(limits.enabled), now),
        )
        self.conn.commit()

    def get(self, account_id: int) -> AccountSafetyLimits:
        row = self.conn.execute(
            "SELECT * FROM account_limits WHERE account_id = ?", (int(account_id),)
        ).fetchone()
        return AccountSafetyLimits.from_row(dict(row) if row else None)

    def check(self, account_id: int, operation: str, *, now: datetime | None = None,
              hourly_collected: int = 0, daily_replied: int = 0,
              last_action_at: datetime | None = None,
              consecutive_failures: int = 0) -> SafetyDecision:
        limits = self.get(account_id)
        if not limits.enabled:
            return SafetyDecision(True, "账号安全限制未启用")
        current = now or self.clock()
        if not _in_window(current, limits.work_start, limits.work_end):
            return SafetyDecision(False, "当前不在账号工作时段")
        if operation == "collect" and limits.hourly_collect_limit and hourly_collected >= limits.hourly_collect_limit:
            return SafetyDecision(False, "已达到每小时采集上限")
        if operation == "reply" and limits.daily_reply_limit and daily_replied >= limits.daily_reply_limit:
            return SafetyDecision(False, "已达到每日回复上限")
        if last_action_at is not None and limits.min_delay_seconds:
            elapsed = (current - last_action_at).total_seconds()
            if elapsed < limits.min_delay_seconds:
                return SafetyDecision(False, f"操作间隔不足，还需等待 {int(limits.min_delay_seconds - elapsed)} 秒")
        if limits.consecutive_failure_limit and consecutive_failures >= limits.consecutive_failure_limit:
            return SafetyDecision(False, "连续失败达到暂停阈值")
        return SafetyDecision(True, "允许操作")
