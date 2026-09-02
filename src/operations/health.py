# -*- coding: utf-8 -*-
"""平台/账号健康探针结果存储。探针本身由适配器提供。"""

from __future__ import annotations

import json
from datetime import datetime


class HealthStatus:
    OK = "ok"
    WARNING = "warning"
    HUMAN_REQUIRED = "human_required"
    LOGIN_REQUIRED = "login_required"
    FAILED = "failed"

    ALL = (OK, WARNING, HUMAN_REQUIRED, LOGIN_REQUIRED, FAILED)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class HealthStore:
    def __init__(self, conn, clock=None):
        self.conn = conn
        self.clock = clock or _now

    def record(self, platform: str, check_name: str, status: str, detail: str = "",
               account_id: int | None = None, run_id: str | None = None,
               metadata: dict | None = None) -> int:
        if status not in HealthStatus.ALL:
            raise ValueError(f"未知健康状态: {status}")
        cur = self.conn.execute(
            "INSERT INTO health_events "
            "(platform, account_id, check_name, status, detail, run_id, metadata, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (platform, account_id, check_name, status, detail, run_id,
             json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True), self.clock()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def latest(self, platform: str | None = None, account_id: int | None = None) -> list[dict]:
        clauses, params = [
            "h.id = (SELECT h2.id FROM health_events h2 "
            "WHERE h2.platform = h.platform AND h2.account_id IS h.account_id "
            "AND h2.check_name = h.check_name ORDER BY h2.observed_at DESC, h2.id DESC LIMIT 1)"
        ], []
        if platform:
            clauses.append("h.platform = ?")
            params.append(platform)
        if account_id is not None:
            clauses.append("h.account_id = ?")
            params.append(int(account_id))
        where = "WHERE " + " AND ".join(clauses)
        rows = self.conn.execute(
            f"SELECT h.* FROM health_events h {where} "
            "ORDER BY h.platform, h.check_name",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
