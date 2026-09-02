# -*- coding: utf-8 -*-
"""线索仓库（技术方案 Agent A：数据访问层）。

所有查询使用参数化 SQL，禁止字符串拼接用户输入（7.5）。Repository 只做
数据存取，不做业务判定；业务判定在 service / 领域规则层完成。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .models import (
    BatchResult,
    DraftStatus,
    LeadStatus,
    LeadSummary,
    Page,
    Pool,
    is_valid_lead_transition,
)


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _to_json(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _from_json(value):
    if not value:
        return [] if value is None else value
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


@dataclass
class LeadQuery:
    """线索分页查询条件（7.5）。只允许白名单字段参与 WHERE。"""

    platform: Optional[str] = None
    task_id: Optional[int] = None
    province: Optional[str] = None
    pool: Optional[str] = None
    freshness_bucket: Optional[str] = None
    intent_level: Optional[str] = None
    owner_id: Optional[int] = None
    data_owner_user_id: Optional[int] = None
    status: Optional[str] = None
    keyword: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    page: int = 1
    page_size: int = 50
    sort_by: str = "updated_at"
    sort_order: str = "desc"

    # 允许排序的字段白名单
    SORTABLE = {
        "updated_at", "first_seen_at", "last_interaction_at",
        "intent_score", "region_confidence", "last_seen_at", "comment_time",
    }


class LeadRepository:
    """leads / lead_evidence / lead_assignments / lead_audit_log 的存取。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # ------------------------------------------------------------------
    # leads 增改查
    # ------------------------------------------------------------------
    def upsert_lead(self, lead: dict) -> tuple[int, bool]:
        """插入或更新线索。返回 (lead_id, created_new)。

        按 dedupe_key 幂等：已存在则更新基本资料字段（不覆盖 region/intent/pool/
        status 等业务判定，那些由 service 经 update_fields 显式处理），返回
        (id, False)；不存在则整行插入，返回 (id, True)。
        """
        now = _now_iso()
        existing = self.get_lead_id_by_dedupe(lead["dedupe_key"])
        if existing is not None:
            current = self.get(existing) or {}
            last_interaction_at = self._latest_timestamp(
                current.get("last_interaction_at"),
                lead.get("last_interaction_at"),
            )
            self._conn.execute(
                """UPDATE leads SET
                     platform_user_id = COALESCE(?, platform_user_id),
                     profile_url      = COALESCE(?, profile_url),
                     nickname         = COALESCE(?, nickname),
                     last_seen_at     = ?,
                     last_interaction_at = ?,
                     note             = COALESCE(?, note),
                     updated_at       = ?
                   WHERE id = ?""",
                (
                    lead.get("platform_user_id"),
                    lead.get("profile_url"),
                    lead.get("nickname"),
                    lead.get("last_seen_at", now),
                    last_interaction_at,
                    lead.get("note"),
                    now,
                    existing,
                ),
            )
            self._conn.commit()
            return existing, False

        cur = self._conn.execute(
            """INSERT INTO leads (
                 platform, platform_user_id, profile_url, nickname,
                 dedupe_key, source_comment_id, first_seen_at, last_seen_at,
                 last_interaction_at, region_province, region_city,
                 region_source, region_confidence, region_manual_province,
                 region_manual_city, freshness_bucket, intent_score,
                 intent_level, intent_reasons, rule_version, pool, owner_id,
                 data_owner_user_id,
                 status, contact_allowed, contact_block_reason, note,
                 created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                lead["platform"],
                lead.get("platform_user_id"),
                lead.get("profile_url"),
                lead.get("nickname"),
                lead["dedupe_key"],
                lead.get("source_comment_id"),
                lead.get("first_seen_at", now),
                lead.get("last_seen_at", now),
                lead.get("last_interaction_at"),
                lead.get("region_province"),
                lead.get("region_city"),
                lead.get("region_source", "unknown"),
                lead.get("region_confidence", 0),
                lead.get("region_manual_province"),
                lead.get("region_manual_city"),
                lead.get("freshness_bucket", "unknown"),
                lead.get("intent_score", 0),
                lead.get("intent_level", "unknown"),
                _to_json(lead.get("intent_reasons", [])),
                lead.get("rule_version"),
                lead.get("pool", Pool.UNCLASSIFIED),
                lead.get("owner_id"),
                lead.get("data_owner_user_id"),
                lead.get("status", LeadStatus.NEW),
                lead.get("contact_allowed", 0),
                lead.get("contact_block_reason"),
                lead.get("note"),
                lead.get("created_at", now),
                now,
            ),
        )
        self._conn.commit()
        return cur.lastrowid, True

    @staticmethod
    def _latest_timestamp(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
        """返回较新的可解析时间，避免乱序回填把最近互动时间改旧。"""
        if not current:
            return candidate
        if not candidate:
            return current
        try:
            from datetime import datetime

            current_dt = datetime.fromisoformat(str(current).replace("Z", "+00:00"))
            candidate_dt = datetime.fromisoformat(str(candidate).replace("Z", "+00:00"))
            return candidate if candidate_dt > current_dt else current
        except (TypeError, ValueError):
            # 非 ISO 的平台时间无法可靠比较时，保留已有值，避免回填造成时间倒退。
            return current

    def get_lead_id_by_dedupe(self, dedupe_key: str) -> Optional[int]:
        row = self._conn.execute(
            "SELECT id FROM leads WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return row["id"] if row else None

    def get(self, lead_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # 分页查询
    # ------------------------------------------------------------------
    def list_leads(self, query: LeadQuery) -> Page[LeadSummary]:
        """按 LeadQuery 白名单筛选分页，返回 Page[LeadSummary]。"""
        where = []
        params: list = []

        # 线索一旦进入互动流程，就不应继续出现在待处理线索池中；否则
        # 用户会重复选择同一条线索。取消或拒绝后恢复可见，便于重新处理。
        where.append(
            "NOT EXISTS ("
            "SELECT 1 FROM interaction_drafts id "
            "WHERE id.lead_id = l.id "
            "AND id.status NOT IN (?, ?)"
            ")"
        )
        params.extend([DraftStatus.CANCELLED, DraftStatus.REJECTED])

        if query.platform:
            where.append("l.platform = ?")
            params.append(query.platform)

        if query.data_owner_user_id is not None:
            # 与 leads.owner_id（业务负责人）分开，专用于员工数据隔离。
            where.append("l.data_owner_user_id = ?")
            params.append(int(query.data_owner_user_id))

        if query.task_id is not None:
            # 兼容历史证据：早期链路没有写 lead_evidence.task_id，
            # 但 evidence.video_id/comment_id 仍可反查作品所属任务。
            where.append(
                "EXISTS ("
                "SELECT 1 FROM lead_evidence et "
                "LEFT JOIN comments ec ON ec.id = et.comment_id "
                "LEFT JOIN videos ev ON ev.id = COALESCE(et.video_id, ec.video_id) "
                "WHERE et.lead_id = l.id "
                "AND COALESCE(et.task_id, ev.task_id) = ?"
                ")"
            )
            params.append(query.task_id)

        # 地域：优先按省 + 可信度阈值由 service 处理，这里按省筛选
        if query.province:
            where.append("l.region_province = ?")
            params.append(query.province)

        if query.pool:
            where.append("l.pool = ?")
            params.append(query.pool)

        if query.freshness_bucket:
            where.append("l.freshness_bucket = ?")
            params.append(query.freshness_bucket)

        if query.intent_level:
            where.append("l.intent_level = ?")
            params.append(query.intent_level)

        if query.owner_id is not None:
            where.append("l.owner_id = ?")
            params.append(query.owner_id)

        if query.status:
            where.append("l.status = ?")
            params.append(query.status)

        if query.keyword:
            where.append("(l.nickname LIKE ? OR l.note LIKE ? OR l.id IN "
                         "(SELECT e.lead_id FROM lead_evidence e "
                         "LEFT JOIN comments c ON c.id = e.comment_id "
                         "LEFT JOIN videos v ON v.id = e.video_id "
                         "LEFT JOIN tasks t ON t.id = COALESCE(e.task_id, v.task_id) "
                         "WHERE COALESCE(e.evidence_text, c.content, '') LIKE ? "
                         "OR t.keyword LIKE ?))")
            kw = f"%{query.keyword}%"
            params += [kw, kw, kw, kw]

        if query.date_from:
            where.append("l.updated_at >= ?")
            params.append(query.date_from)
        if query.date_to:
            where.append("l.updated_at <= ?")
            params.append(query.date_to)

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        page = max(1, query.page)
        page_size = min(max(1, query.page_size), 500)

        sort_col = query.sort_by if query.sort_by in query.SORTABLE else "updated_at"
        sort_dir = "ASC" if query.sort_order.lower() == "asc" else "DESC"

        total = self._conn.execute(
            f"SELECT COUNT(*) AS c FROM leads l{where_sql}", params
        ).fetchone()["c"]

        # 选定任务后，列表中的评论原文/时间/原作地址也必须来自该任务，
        # 不能因为同一用户还有更新的其它任务评论而串数据。
        evidence_filter = ""
        evidence_params: list = []
        if query.task_id is not None:
            evidence_filter = (
                " AND COALESCE(e.task_id, v.task_id) = ?"
            )
            evidence_params = [query.task_id, query.task_id, query.task_id]

        sort_params: list = []
        if sort_col == "comment_time":
            # 评论时间存放在 evidence/comment 侧，不在 leads 主表中；用与
            # 列表展示相同的“该线索最新评论”规则排序，避免排序与显示错位。
            sort_expression = (
                "(SELECT COALESCE(e.occurred_at, c.comment_time) "
                "FROM lead_evidence e "
                "LEFT JOIN comments c ON c.id = e.comment_id "
                "LEFT JOIN videos v ON v.id = COALESCE(e.video_id, c.video_id) "
                "WHERE e.lead_id = l.id" + evidence_filter + " "
                "ORDER BY COALESCE(e.occurred_at, c.comment_time) DESC, e.id DESC "
                "LIMIT 1)"
            )
            if query.task_id is not None:
                sort_params.append(query.task_id)
        else:
            sort_expression = f"l.{sort_col}"

        rows = self._conn.execute(
            f"""SELECT l.*, o.name AS owner_name,
                       (SELECT COALESCE(e.evidence_text, c.content, '') FROM lead_evidence e
                         LEFT JOIN comments c ON c.id = e.comment_id
                         LEFT JOIN videos v ON v.id = COALESCE(e.video_id, c.video_id)
                         WHERE e.lead_id = l.id{evidence_filter}
                         ORDER BY e.occurred_at DESC, e.id DESC LIMIT 1) AS summary_text,
                       (SELECT c.comment_time
                          FROM lead_evidence e
                          LEFT JOIN comments c ON c.id = e.comment_id
                          LEFT JOIN videos v ON v.id = COALESCE(e.video_id, c.video_id)
                         WHERE e.lead_id = l.id{evidence_filter}
                         ORDER BY COALESCE(e.occurred_at, c.comment_time) DESC,
                                  e.id DESC LIMIT 1) AS comment_time,
                       (SELECT v.url
                          FROM lead_evidence e
                          LEFT JOIN comments c ON c.id = e.comment_id
                          LEFT JOIN videos v ON v.id = COALESCE(e.video_id, c.video_id)
                         WHERE e.lead_id = l.id{evidence_filter}
                         ORDER BY COALESCE(e.occurred_at, c.comment_time) DESC,
                                  e.id DESC LIMIT 1) AS source_url
            FROM leads l
                LEFT JOIN owners o ON o.id = l.owner_id
                {where_sql}
                 ORDER BY {sort_expression} {sort_dir}, l.id DESC
                LIMIT ? OFFSET ?""",
            evidence_params + params + sort_params + [page_size, (page - 1) * page_size],
        ).fetchall()

        items = [LeadSummary.from_row(dict(r)) for r in rows]
        return Page(items=items, total=total, page=page, page_size=page_size)

    def list_provinces(self, data_owner_user_id: Optional[int] = None) -> List[str]:
        """返回当前线索中已有的省份，供界面筛选下拉框使用。"""
        where = "region_province IS NOT NULL AND TRIM(region_province) <> ''"
        params: list[Any] = []
        if data_owner_user_id is not None:
            where += " AND data_owner_user_id = ?"
            params.append(int(data_owner_user_id))
        rows = self._conn.execute(
            "SELECT DISTINCT region_province FROM leads WHERE " + where +
            " ORDER BY region_province", params
        ).fetchall()
        return [str(row["region_province"]) for row in rows]

    def list_tasks(self, data_owner_user_id: Optional[int] = None) -> List[dict]:
        """返回已有线索证据对应的采集任务，供线索中心按任务筛选。"""
        sql = """SELECT DISTINCT t.id, t.keyword, t.platform, t.status,
                              t.created_at, t.updated_at
                 FROM tasks t
                 WHERE EXISTS (
                   SELECT 1
                     FROM lead_evidence e
                     LEFT JOIN comments c ON c.id = e.comment_id
                     LEFT JOIN videos v ON v.id = COALESCE(e.video_id, c.video_id)
                    WHERE COALESCE(e.task_id, v.task_id) = t.id
                 )"""
        params: list[Any] = []
        if data_owner_user_id is not None:
            sql += """ AND EXISTS (
                SELECT 1 FROM lead_evidence escope
                JOIN leads lscope ON lscope.id = escope.lead_id
                LEFT JOIN comments cscope ON cscope.id = escope.comment_id
                LEFT JOIN videos vscope ON vscope.id = COALESCE(escope.video_id, cscope.video_id)
                WHERE lscope.data_owner_user_id = ?
                  AND COALESCE(escope.task_id, vscope.task_id) = t.id
            )"""
            params.append(int(data_owner_user_id))
        sql += " ORDER BY t.id DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 更新
    # ------------------------------------------------------------------
    def update_fields(self, lead_id: int, fields: Dict[str, Any]) -> None:
        """安全更新指定字段（白名单来自 leads 真实列）。"""
        allowed = {
            "region_province", "region_city", "region_source", "region_confidence",
            "region_manual_province", "region_manual_city",
            "freshness_bucket", "intent_score", "intent_level", "intent_reasons",
            "rule_version", "pool", "owner_id", "status", "contact_allowed",
            "contact_block_reason", "note", "last_interaction_at", "last_seen_at",
        }
        clean = {k: v for k, v in fields.items() if k in allowed}
        if not clean:
            return
        if "intent_reasons" in clean and isinstance(clean["intent_reasons"], list):
            clean["intent_reasons"] = _to_json(clean["intent_reasons"])
        cols = ", ".join(f"{k} = ?" for k in clean)
        params = list(clean.values()) + [_now_iso(), lead_id]
        self._conn.execute(f"UPDATE leads SET {cols}, updated_at = ? WHERE id = ?", params)
        self._conn.commit()

    def transition_status(self, lead_id: int, target: str) -> str:
        """按冻结状态机变更线索状态，返回变更前状态。"""
        lead = self.get(lead_id)
        if lead is None:
            raise ValueError(f"线索不存在: {lead_id}")
        current = lead.get("status") or LeadStatus.NEW
        if not is_valid_lead_transition(current, target):
            raise ValueError(f"非法线索状态变更: {current} -> {target}")
        if current != target:
            self.update_fields(lead_id, {"status": target})
        return current

    # ------------------------------------------------------------------
    # 证据
    # ------------------------------------------------------------------
    def add_evidence(self, evidence: dict) -> Optional[int]:
        """新增一条证据（幂等：同 lead+comment+type 唯一）。"""
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO lead_evidence
                 (lead_id, task_id, video_id, comment_id, evidence_type,
                  evidence_text, occurred_at, collected_at, metadata)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                evidence["lead_id"],
                evidence.get("task_id"),
                evidence.get("video_id"),
                evidence.get("comment_id"),
                evidence["evidence_type"],
                evidence.get("evidence_text"),
                evidence.get("occurred_at"),
                _now_iso(),
                _to_json(evidence.get("metadata", {})),
            ),
        )
        self._conn.commit()
        return cur.lastrowid if cur.rowcount else None

    def evidence_for(self, lead_id: int) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM lead_evidence WHERE lead_id = ? ORDER BY occurred_at, id",
            (lead_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["metadata"] = _from_json(d.get("metadata"))
            out.append(d)
        return out

    def comment_timeline(self, lead_id: int) -> List[dict]:
        """线索相关评论时间线（由证据关联 comment_id 反查评论内容）。"""
        rows = self._conn.execute(
            """SELECT c.id, c.platform, c.nickname, c.content, c.comment_time,
                      c.intent_score, c.intent_label, e.occurred_at
               FROM lead_evidence e
               JOIN comments c ON c.id = e.comment_id
               WHERE e.lead_id = ?
               ORDER BY COALESCE(e.occurred_at, c.comment_time) DESC""",
            (lead_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 分配
    # ------------------------------------------------------------------
    def create_owner(self, name: str, province: Optional[str] = None,
                     contact: Optional[str] = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO owners (name, province, contact, enabled, created_at) VALUES (?,?,?,1,?)",
            (name, province, contact, _now_iso()),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_owners(self, enabled_only: bool = True) -> List[dict]:
        sql = "SELECT * FROM owners"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name"
        return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def get_owner(self, owner_id: int) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM owners WHERE id = ?", (owner_id,)).fetchone()
        return dict(row) if row else None

    def update_owner(self, owner_id: int, name=None, province=None,
                     contact=None, enabled=None) -> None:
        fields = []
        params = []
        if name is not None:
            fields.append("name = ?"); params.append(name)
        if province is not None:
            fields.append("province = ?"); params.append(province)
        if contact is not None:
            fields.append("contact = ?"); params.append(contact)
        if enabled is not None:
            fields.append("enabled = ?"); params.append(int(bool(enabled)))
        if fields:
            self._conn.execute(f"UPDATE owners SET {', '.join(fields)} WHERE id = ?", params + [owner_id])
            self._conn.commit()

    def add_assignment(self, lead_id: int, action: str, from_owner_id=None,
                       to_owner_id=None, province=None, reason=None) -> int:
        cur = self._conn.execute(
            """INSERT INTO lead_assignments
                 (lead_id, from_owner_id, to_owner_id, province, action, reason, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (lead_id, from_owner_id, to_owner_id, province, action, reason, _now_iso()),
        )
        self._conn.commit()
        return cur.lastrowid

    def assignments_for(self, lead_id: int) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM lead_assignments WHERE lead_id = ? ORDER BY created_at, id",
            (lead_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def audit(self, lead_id: int, actor: str, action: str,
              before_value=None, after_value=None) -> None:
        self._conn.execute(
            """INSERT INTO lead_audit_log
                 (lead_id, actor, action, before_value, after_value, created_at)
               VALUES (?,?,?,?,?,?)""",
            (lead_id, actor, action,
             _to_json(before_value) if isinstance(before_value, (dict, list)) else before_value,
             _to_json(after_value) if isinstance(after_value, (dict, list)) else after_value,
             _now_iso()),
        )
        self._conn.commit()

    def audit_log_for(self, lead_id: int) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM lead_audit_log WHERE lead_id = ? ORDER BY created_at, id",
            (lead_id,),
        ).fetchall()
        return [dict(r) for r in rows]
