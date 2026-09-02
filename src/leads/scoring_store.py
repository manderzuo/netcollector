# -*- coding: utf-8 -*-
"""线索评分规则和历史记录存取。"""

from __future__ import annotations

import json
from datetime import datetime


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


class LeadScoreStore:
    def __init__(self, conn, clock=None):
        self.conn = conn
        self.clock = clock or _now

    def save_rule(self, name: str, version: str, config: dict, *, platform=None,
                  enabled=True, applies_to_new=True) -> int:
        now = self.clock()
        encoded = json.dumps(config or {}, ensure_ascii=False, sort_keys=True)
        row = self.conn.execute(
            "SELECT id FROM lead_score_rules WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE lead_score_rules SET platform=?, config=?, enabled=?, applies_to_new=?, updated_at=? WHERE id=?",
                (platform, encoded, int(enabled), int(applies_to_new), now, row[0]),
            )
            rule_id = int(row[0])
        else:
            cur = self.conn.execute(
                "INSERT INTO lead_score_rules(name,platform,version,config,enabled,applies_to_new,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (name, platform, version, encoded, int(enabled), int(applies_to_new), now, now),
            )
            rule_id = int(cur.lastrowid)
        self.conn.commit()
        return rule_id

    def record(self, lead_id: int, result, *, source="rule") -> int:
        cur = self.conn.execute(
            "INSERT INTO lead_score_history(lead_id,score,level,reasons,rule_version,source,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (int(lead_id), int(result.score), result.level,
             json.dumps(result.reasons, ensure_ascii=False), result.rule_version,
             source, self.clock()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def history(self, lead_id: int, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM lead_score_history WHERE lead_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(lead_id), max(1, int(limit))),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["reasons"] = json.loads(item.get("reasons") or "[]")
            except (TypeError, ValueError):
                item["reasons"] = []
            out.append(item)
        return out

