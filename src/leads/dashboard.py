# -*- coding: utf-8 -*-
"""总览运营统计（技术方案 Agent G / 8.5）。

所有指标通过参数化 SQL 从数据库统计，禁止 UI 遍历全量数据现算。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List

from leads.models import DraftStatus, FreshnessBucket, IntentLevel, LeadStatus, Pool


class DashboardQueryService:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # ------------------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        """总览核心指标（8.5）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        soon_expire_from = datetime.now().isoformat(timespec="seconds")
        soon_expire_to = (datetime.now() + timedelta(hours=48)).isoformat(timespec="seconds")

        def _one(sql, params=()):
            row = self._conn.execute(sql, params).fetchone()
            return row[0] if row else 0

        stats = {
            "today_new": _one(
                "SELECT COUNT(*) FROM leads WHERE substr(created_at,1,10) = ?", (today,)),
            "henan": _one(
                "SELECT COUNT(*) FROM leads WHERE pool = ?", (Pool.HENAN,)),
            "high_intent": _one(
                "SELECT COUNT(*) FROM leads WHERE intent_level = ?", (IntentLevel.HIGH,)),
            "expiring_48h": _one(
                """SELECT COUNT(*) FROM leads
                   WHERE freshness_bucket = ?
                     AND last_interaction_at >= ?
                     AND last_interaction_at <= ?""",
                (FreshnessBucket.COOLING, soon_expire_from, soon_expire_to)),
            "region_review": _one(
                "SELECT COUNT(*) FROM leads WHERE pool = ?", (Pool.REGION_REVIEW,)),
            "awaiting_review": _one(
                "SELECT COUNT(*) FROM interaction_drafts WHERE status = ?",
                (DraftStatus.PENDING_REVIEW,)),
            "suppressed": _one(
                "SELECT COUNT(*) FROM leads WHERE status = ?", (LeadStatus.SUPPRESSED,)),
        }
        return stats

    # ------------------------------------------------------------------
    def funnel(self) -> Dict[str, int]:
        """采集→有效→已分配→已联系→已回复 漏斗（8.5）。"""
        return {
            "collected": self._count("SELECT COUNT(*) FROM leads"),
            "valid": self._count(
                "SELECT COUNT(*) FROM leads WHERE pool = ?", (Pool.HENAN,)),
            "assigned": self._count(
                "SELECT COUNT(*) FROM leads WHERE owner_id IS NOT NULL AND status != ?",
                (LeadStatus.SUPPRESSED,)),
            "contacted": self._count(
                "SELECT COUNT(*) FROM leads WHERE status IN (?,?,?,?)",
                (LeadStatus.CONTACTED, LeadStatus.REPLIED,
                 LeadStatus.CONVERTED, LeadStatus.CLOSED)),
            "replied": self._count(
                "SELECT COUNT(*) FROM leads WHERE status IN (?,?)",
                (LeadStatus.REPLIED, LeadStatus.CONVERTED)),
        }

    # ------------------------------------------------------------------
    def region_stats(self) -> List[Dict[str, Any]]:
        """河南/其他省份/未知地区统计（9.8）。"""
        rows = self._conn.execute(
            "SELECT pool, COUNT(*) AS cnt FROM leads GROUP BY pool"
        ).fetchall()
        by_pool = {r["pool"]: r["cnt"] for r in rows}
        return [
            {"pool": Pool.HENAN, "count": by_pool.get(Pool.HENAN, 0)},
            {"pool": Pool.OTHER_PROVINCE, "count": by_pool.get(Pool.OTHER_PROVINCE, 0)},
            {"pool": Pool.REGION_REVIEW, "count": by_pool.get(Pool.REGION_REVIEW, 0)},
            {"pool": Pool.UNKNOWN_REGION, "count": by_pool.get(Pool.UNKNOWN_REGION, 0)},
        ]

    def _count(self, sql: str, params=()) -> int:
        row = self._conn.execute(sql, params).fetchone()
        return row[0] if row else 0
