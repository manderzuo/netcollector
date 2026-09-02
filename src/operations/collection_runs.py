# -*- coding: utf-8 -*-
"""采集运行和事件时间线持久化。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional


RUN_STATUSES = {"running", "paused", "completed", "no_more", "failed", "cancelled"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


class CollectionRunStore:
    """以已有 SQLite 连接为依赖，不持有 GUI 或调度器对象。"""

    def __init__(self, conn, clock=None):
        self.conn = conn
        self.clock = clock or _now

    def create(self, task_id: int, platform: str, account_id: Optional[int] = None,
               execution_mode: str = "manual", metadata: Optional[dict] = None,
               run_id: Optional[str] = None) -> str:
        rid = run_id or uuid.uuid4().hex
        started = self.clock()
        self.conn.execute(
            "INSERT INTO collection_runs "
            "(run_id, task_id, platform, account_id, execution_mode, status, started_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?, ?)",
            (rid, int(task_id), str(platform), account_id, str(execution_mode), started, _json(metadata)),
        )
        self.conn.commit()
        self.event(rid, "lifecycle", "run_started", "采集运行开始", metadata or {})
        return rid

    def update(self, run_id: str, *, status: Optional[str] = None,
               account_id: Optional[int] = None,
               stop_reason: Optional[str] = None, discovered_count: Optional[int] = None,
               new_count: Optional[int] = None, duplicate_count: Optional[int] = None,
               failed_count: Optional[int] = None, metadata: Optional[dict] = None) -> None:
        if status is not None and status not in RUN_STATUSES:
            raise ValueError(f"未知采集运行状态: {status}")
        fields, values = [], []
        for name, value in (
            ("status", status), ("account_id", account_id), ("stop_reason", stop_reason),
            ("discovered_count", discovered_count), ("new_count", new_count),
            ("duplicate_count", duplicate_count), ("failed_count", failed_count),
        ):
            if value is not None:
                fields.append(f"{name} = ?")
                values.append(value)
        if metadata is not None:
            fields.append("metadata = ?")
            values.append(_json(metadata))
        if status in {"completed", "no_more", "failed", "cancelled"}:
            fields.append("finished_at = ?")
            values.append(self.clock())
        if not fields:
            return
        values.append(run_id)
        cur = self.conn.execute(
            f"UPDATE collection_runs SET {', '.join(fields)} WHERE run_id = ?", values
        )
        if cur.rowcount != 1:
            raise KeyError(f"采集运行不存在: {run_id}")
        self.conn.commit()
        if status:
            self.event(run_id, "lifecycle", f"run_{status}", stop_reason or status, {})

    def event(self, run_id: str, stage: str, event_type: str, message: str = "",
              payload: Optional[dict] = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO collection_events "
            "(run_id, stage, event_type, message, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, str(stage), str(event_type), message, _json(payload), self.clock()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get(self, run_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM collection_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def events(self, run_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM collection_events WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def list_for_task(self, task_id: int, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        rows = self.conn.execute(
            "SELECT * FROM collection_runs WHERE task_id = ? "
            "ORDER BY started_at DESC LIMIT ?", (int(task_id), limit)
        ).fetchall()
        return [dict(row) for row in rows]
