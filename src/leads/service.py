# -*- coding: utf-8 -*-
"""线索应用服务（技术方案 Agent C）。

把评论幂等转换为用户级线索和证据；重新分类时保留人工修正。
依赖：Agent A 的 Repository、Agent B 的分类引擎。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional

from .dedupe import build_dedupe_key
from .freshness import FreshnessClassifier
from .intent import IntentScorer
from .scoring_store import LeadScoreStore
from .models import (
    IntentLevel,
    IntentResult,
    LeadDetail,
    LeadStatus,
    LeadSummary,
    Page,
    Pool,
    REGION_CONFIDENCE,
    RegionResult,
    RegionSource,
)
from .region import RegionClassifier
from .repository import LeadQuery, LeadRepository

log = logging.getLogger(__name__)


class LeadService:
    """线索应用服务。构造时接收 Repository/分类引擎，便于测试注入。"""

    DEFAULT_REPLY_ELIGIBLE_INTENT = IntentLevel.MEDIUM

    def __init__(
        self,
        repo: LeadRepository,
        *,
        region: Optional[RegionClassifier] = None,
        freshness: Optional[FreshnessClassifier] = None,
        intent: Optional[IntentScorer] = None,
        scorer=None,
        config: Optional[dict] = None,
    ):
        self._repo = repo
        # 配置：优先显式传入，否则从 AppConfig 加载 lead_rules（P3-3）
        if config is None:
            from config_loader import AppConfig

            config = AppConfig().lead_rules() or {}
        self._config = config or {}
        rcfg = (config or {}).get("lead_rules", config or {})
        threshold = rcfg.get("henan_confidence_threshold",
                             (config or {}).get("henan_confidence_threshold", 65))
        self._region = region or RegionClassifier(confidence_threshold=threshold)
        self._freshness = freshness or FreshnessClassifier(
            hot_days=rcfg.get("hot_days"),
            active_days=rcfg.get("active_days"),
            cooling_days=rcfg.get("cooling_days"),
        )
        self._intent = intent or IntentScorer()
        # 新评分器为显式 opt-in，默认保留原有 5/3/1 意向逻辑，避免旧数据和
        # 互动资格规则被无意改变。
        self._lead_scorer = scorer
        self._score_store = LeadScoreStore(repo._conn)

    # ------------------------------------------------------------------
    # 评论 → 线索（幂等）
    # ------------------------------------------------------------------
    def ingest_comment_and_report(self, comment_id: int, context: Optional[dict] = None) -> tuple[int, bool]:
        """幂等地从一条评论生成或更新用户级线索。

        返回 (lead_id, created_new)，供批处理统计使用。
        公开契约见 ``ingest_comment``。context 可携带采集接入点额外信息：
          - profile_region / self_declared / ip_label  地域来源
          - task_id / video_id                          证据关联
        默认全为 None 时只做文本弱推断（unknown 地域）。
        """
        ctx = context or {}
        comment = self._fetch_comment(comment_id)
        if comment is None:
            raise ValueError(f"评论不存在: {comment_id}")

        platform = comment["platform"] or "unknown"
        platform_user_id = comment.get("user_id")
        nickname = comment.get("nickname")
        extra = self._comment_extra(comment.get("extra"))
        # 旧采集链路把主页/IP 属地放在 comments.extra 中，而不是独立列。
        # 这里统一成领域服务可用的字段，避免真实采集数据在入线索时丢失。
        profile_url = comment.get("profile_url") or extra.get("profile_url") or extra.get("homepage")
        comment_time = self._first_valid_time(comment.get("comment_time"),
                                              ctx.get("interaction_at"))
        collected_at = comment.get("collected_at") or comment.get("video_collected_at")

        dedupe = build_dedupe_key(
            platform=platform,
            platform_user_id=platform_user_id,
            profile_url=profile_url,
            source_comment_id=comment_id,
        )
        # 员工数据隔离不能只靠查询过滤：两个员工采集同一个用户时，
        # 也不能因为全局 dedupe_key 唯一而互相合并线索。
        data_owner_user_id = ctx.get("data_owner_user_id")
        if data_owner_user_id not in (None, ""):
            dedupe = type(dedupe)(
                key=f"{dedupe.key}:employee:{int(data_owner_user_id)}",
                basis=dedupe.basis,
            )

        # 地域判定：人工修正优先（9.3）
        manual_province = ctx.get("manual_province")
        manual_city = ctx.get("manual_city")
        existing_id = self._repo.get_lead_id_by_dedupe(dedupe.key)
        if manual_province is None:
            # 若该 dedupe_key 已有 lead，沿用其人工修正
            existing = self._repo.get(existing_id) if existing_id else None
            if existing:
                manual_province = existing.get("region_manual_province")
                manual_city = existing.get("region_manual_city")

        region_result = self._region.classify(
            self_declared=ctx.get("self_declared") or extra.get("self_declared"),
            profile_region=ctx.get("profile_region") or extra.get("profile_region"),
            ip_label=ctx.get("ip_label") or extra.get("ip_label") or extra.get("region"),
            text=comment.get("content"),
            manual_province=manual_province,
            manual_city=manual_city,
        )

        freshness_result = self._freshness.classify(comment_time)
        intent_override = ctx.get("intent_override")
        if isinstance(intent_override, dict):
            override_level = str(intent_override.get("level") or "").strip().lower()
            if override_level not in IntentLevel.ALL:
                override_level = IntentLevel.LOW
            try:
                override_score = int(intent_override.get("score"))
            except (TypeError, ValueError):
                override_score = {IntentLevel.HIGH: 5, IntentLevel.MEDIUM: 3,
                                  IntentLevel.LOW: 1}.get(override_level, 0)
            intent_result = IntentResult(
                score=override_score,
                level=override_level,
                reasons=intent_override.get("reasons") or ["批量意向分析"],
                rule_version=intent_override.get("rule_version") or "intent-override-v1",
            )
        elif self._lead_scorer is not None:
            repeat_count = 0
            if existing_id:
                try:
                    repeat_count = len(self._repo.evidence_for(existing_id))
                except Exception:
                    repeat_count = 0
            result = self._lead_scorer.score(
                comment.get("content"), region=region_result.province,
                repeat_count=repeat_count,
            )
            intent_result = IntentResult(
                score=result.score, level=result.level,
                reasons=result.reasons, rule_version=result.rule_version,
            )
        else:
            intent_result = self._intent.score(comment.get("content"))
        pool = self._region.pool_for(region_result)

        now = self._now()
        classification = {
            "region_province": region_result.province,
            "region_city": region_result.city,
            "region_source": region_result.source,
            "region_confidence": region_result.confidence,
            "region_manual_province": manual_province,
            "region_manual_city": manual_city,
            "freshness_bucket": freshness_result.bucket,
            "intent_score": intent_result.score,
            "intent_level": intent_result.level,
            "intent_reasons": intent_result.reasons,
            "rule_version": intent_result.rule_version,
            "pool": pool,
            "data_owner_user_id": data_owner_user_id,
        }
        lead_id, created = self._repo.upsert_lead({
            "platform": platform,
            "platform_user_id": platform_user_id,
            "profile_url": profile_url,
            "nickname": nickname,
            "dedupe_key": dedupe.key,
            "source_comment_id": comment_id,
            "first_seen_at": collected_at or now,
            "last_seen_at": now,
            "last_interaction_at": comment_time,
            **classification,
            "pool": pool,
            # 只有满足河南地域资格的线索进入 qualified；其它线索保留 new。
            "status": LeadStatus.QUALIFIED if pool == Pool.HENAN else LeadStatus.NEW,
        })

        # upsert_lead 的更新分支刻意不覆盖业务判定字段；更新线索时必须显式
        # 写入本次重新计算的分类结果，否则同一用户的新评论永远不会刷新意向/分池。
        if not created:
            self._repo.update_fields(lead_id, classification)
            current = self._repo.get(lead_id) or {}
            if current.get("status") == LeadStatus.NEW and pool == Pool.HENAN:
                self._repo.transition_status(lead_id, LeadStatus.QUALIFIED)
        if self._lead_scorer is not None:
            try:
                self._score_store.record(lead_id, intent_result)
            except sqlite3.Error:
                log.warning("线索评分历史写入失败", exc_info=True)

        # 写证据（幂等：同 lead+comment+type 唯一）
        self._repo.add_evidence({
            "lead_id": lead_id,
            "task_id": ctx.get("task_id"),
            "video_id": ctx.get("video_id") or comment.get("video_id"),
            "comment_id": comment_id,
            "evidence_type": "comment",
            "evidence_text": comment.get("content"),
            "occurred_at": comment_time,
            "metadata": {"region": region_result.province,
                         "region_source": region_result.source,
                         "intent_level": intent_result.level},
        })

        if created:
            self._repo.audit(lead_id, "pipeline", "created_from_comment",
                             after_value={"comment_id": comment_id})
        return lead_id, created

    def ingest_comment(self, comment_id: int, context: Optional[dict] = None) -> int:
        """公开契约（技术方案 7.1）：幂等生成/更新线索，返回 lead_id。"""
        lead_id, _created = self.ingest_comment_and_report(comment_id, context)
        return lead_id

    # ------------------------------------------------------------------
    # 重新分类（保留人工修正）
    # ------------------------------------------------------------------
    def reclassify(self, lead_id: int) -> None:
        """重新执行地域、时效和意向判定；人工修正值不被覆盖。"""
        lead = self._repo.get(lead_id)
        if lead is None:
            raise ValueError(f"线索不存在: {lead_id}")

        manual_province = lead.get("region_manual_province")
        manual_city = lead.get("region_manual_city")

        # 汇总现有证据文本来推断意向/地域
        ev = self._repo.evidence_for(lead_id)
        texts = [e.get("evidence_text") for e in ev if e.get("evidence_text")]
        combined_text = " ".join(filter(None, texts))

        region_result = self._region.classify(
            text=combined_text,
            manual_province=manual_province,
            manual_city=manual_city,
        )
        # 重新分类时没有再次拿到主页/IP 原始字段；如果当前已有更高可信度的
        # 自动判定，不能被一次“证据文本未提及地域”降级为 unknown。
        previous_source = lead.get("region_source") or RegionSource.UNKNOWN
        previous_province = lead.get("region_province")
        previous_confidence = REGION_CONFIDENCE.get(previous_source, 0)
        if (not manual_province and previous_province
                and previous_confidence > region_result.confidence):
            region_result = RegionResult(
                province=previous_province,
                city=lead.get("region_city"),
                source=previous_source,
                confidence=lead.get("region_confidence") or previous_confidence,
                notes=["保留已有高可信度地域判定"],
            )
        # 若之前有更高可信的来源(profile/ip)可单独传，这里用文本兜底
        if self._lead_scorer is not None:
            result = self._lead_scorer.score(
                combined_text,
                region=lead.get("region_province"),
                repeat_count=max(0, len(ev) - 1),
            )
            intent_result = IntentResult(
                score=result.score, level=result.level,
                reasons=result.reasons, rule_version=result.rule_version,
            )
        else:
            intent_result = self._intent.score(combined_text)

        # 收集互动时间（证据里最新的 occurred_at 或 last_interaction_at）
        ref_time = lead.get("last_interaction_at")
        for e in ev:
            if e.get("occurred_at") and (not ref_time or e["occurred_at"] > ref_time):
                ref_time = e["occurred_at"]
        freshness_result = self._freshness.classify(ref_time)
        pool = self._region.pool_for(region_result)

        self._repo.update_fields(lead_id, {
            "region_province": region_result.province,
            "region_city": region_result.city,
            "region_source": region_result.source,
            "region_confidence": region_result.confidence,
            "region_manual_province": manual_province,
            "region_manual_city": manual_city,
            "freshness_bucket": freshness_result.bucket,
            "intent_score": intent_result.score,
            "intent_level": intent_result.level,
            "intent_reasons": intent_result.reasons,
            "rule_version": intent_result.rule_version,
            "pool": pool,
        })
        if self._lead_scorer is not None:
            try:
                self._score_store.record(lead_id, intent_result, source="reclassify")
            except sqlite3.Error:
                log.warning("线索评分历史写入失败", exc_info=True)
        if lead.get("status") == LeadStatus.NEW and pool == Pool.HENAN:
            self._repo.transition_status(lead_id, LeadStatus.QUALIFIED)
        self._repo.audit(lead_id, "service", "reclassify",
                         before_value={"status": lead.get("status")},
                         after_value={"pool": pool})

    # ------------------------------------------------------------------
    # 分页 / 详情
    # ------------------------------------------------------------------
    def list_leads(self, query: LeadQuery) -> Page[LeadSummary]:
        return self._repo.list_leads(query)

    def list_provinces(self, data_owner_user_id: Optional[int] = None) -> List[str]:
        return self._repo.list_provinces(data_owner_user_id)

    def list_tasks(self, data_owner_user_id: Optional[int] = None) -> List[dict]:
        """返回有线索证据的采集任务，供线索中心筛选。"""
        return self._repo.list_tasks(data_owner_user_id)

    def get_detail(self, lead_id: int) -> LeadDetail:
        lead = self._repo.get(lead_id)
        if lead is None:
            raise ValueError(f"线索不存在: {lead_id}")
        lead["intent_reasons"] = lead.get("intent_reasons") or "[]"
        try:
            lead["intent_reasons"] = json.loads(lead["intent_reasons"]) \
                if isinstance(lead["intent_reasons"], str) else lead["intent_reasons"]
        except Exception:
            lead["intent_reasons"] = []
        return LeadDetail(
            lead=lead,
            evidence=self._repo.evidence_for(lead_id),
            assignments=self._repo.assignments_for(lead_id),
            timeline=self._repo.comment_timeline(lead_id),
            audit=self._repo.audit_log_for(lead_id),
            events=[],
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _fetch_comment(self, comment_id: int) -> Optional[dict]:
        conn = self._repo._conn
        row = conn.execute(
            """SELECT c.*, v.platform AS video_platform,
                      v.collected_at AS video_collected_at
               FROM comments c LEFT JOIN videos v ON v.id = c.video_id
               WHERE c.id = ?""",
            (comment_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if not d.get("platform"):
            d["platform"] = d.get("video_platform") or "unknown"
        return d

    @staticmethod
    def _comment_extra(value: Any) -> dict:
        """把旧表 comments.extra 安全解析为字典。"""
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _first_valid_time(*values: Optional[str]) -> Optional[str]:
        for v in values:
            if v:
                return v
        return None

    @staticmethod
    def _now() -> str:
        from datetime import datetime

        return datetime.now().isoformat(timespec="seconds")
