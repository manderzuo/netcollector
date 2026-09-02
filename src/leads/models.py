# -*- coding: utf-8 -*-
"""线索运营领域「冻结契约」。

本模块把技术方案第 5、6、7 章里提到的枚举值、状态机与领域数据类集中定义，
作为各 Agent 公共接口的单一事实来源（见 11.3「模型或枚举冲突以 models.py
的冻结契约为准」）。

任何状态名/枚举改动必须同步迁移、服务、UI、导出和测试（11.3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 地域
# ---------------------------------------------------------------------------
class RegionSource:
    """地域来源（可信度由低到高排序见 REGION_CONFIDENCE）。"""

    UNKNOWN = "unknown"
    TEXT_INFERRED = "text_inferred"
    IP_LABEL = "ip_label"
    PROFILE = "profile"
    SELF_DECLARED = "self_declared"
    MANUAL = "manual"

    ALL = (
        UNKNOWN,
        TEXT_INFERRED,
        IP_LABEL,
        PROFILE,
        SELF_DECLARED,
        MANUAL,
    )


REGION_CONFIDENCE = {
    RegionSource.MANUAL: 100,
    RegionSource.SELF_DECLARED: 90,
    RegionSource.PROFILE: 80,
    RegionSource.IP_LABEL: 65,
    RegionSource.TEXT_INFERRED: 40,
    RegionSource.UNKNOWN: 0,
}


# ---------------------------------------------------------------------------
# 时效
# ---------------------------------------------------------------------------
class FreshnessBucket:
    """时效分桶（见技术方案 2.3）。"""

    HOT = "hot"            # 0~3 天
    ACTIVE = "active"      # 4~14 天
    COOLING = "cooling"    # 15~30 天
    EXPIRED = "expired"    # >30 天
    UNKNOWN = "unknown"    # 无可靠时间

    ALL = (HOT, ACTIVE, COOLING, EXPIRED, UNKNOWN)


# ---------------------------------------------------------------------------
# 意向
# ---------------------------------------------------------------------------
class IntentLevel:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"

    ALL = (LOW, MEDIUM, HIGH, UNKNOWN)

    # 用于资格判断的数值排序（越高意向越强）
    RANK = {LOW: 0, MEDIUM: 1, HIGH: 2, UNKNOWN: -1}


# ---------------------------------------------------------------------------
# 线索池
# ---------------------------------------------------------------------------
class Pool:
    HENAN = "henan"
    OTHER_PROVINCE = "other_province"
    REGION_REVIEW = "region_review"
    UNKNOWN_REGION = "unknown_region"
    ARCHIVED = "archived"
    UNCLASSIFIED = "unclassified"

    ALL = (HENAN, OTHER_PROVINCE, REGION_REVIEW, UNKNOWN_REGION, ARCHIVED, UNCLASSIFIED)


# ---------------------------------------------------------------------------
# 线索状态机（6.3）
# ---------------------------------------------------------------------------
class LeadStatus:
    NEW = "new"
    QUALIFIED = "qualified"
    ASSIGNED = "assigned"
    DRAFT_READY = "draft_ready"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    CONTACTED = "contacted"
    REPLIED = "replied"
    CONVERTED = "converted"
    CLOSED = "closed"
    SUPPRESSED = "suppressed"
    ARCHIVED = "archived"

    ALL = (
        NEW, QUALIFIED, ASSIGNED, DRAFT_READY, AWAITING_REVIEW, APPROVED,
        CONTACTED, REPLIED, CONVERTED, CLOSED, SUPPRESSED, ARCHIVED,
    )


# 线索状态机合法跳转表（不变量：任何未列举跳转一律拒绝）
LEAD_TRANSITIONS = {
    LeadStatus.NEW: {LeadStatus.QUALIFIED, LeadStatus.SUPPRESSED, LeadStatus.ARCHIVED},
    LeadStatus.QUALIFIED: {LeadStatus.ASSIGNED, LeadStatus.DRAFT_READY,
                           LeadStatus.SUPPRESSED, LeadStatus.ARCHIVED},
    LeadStatus.ASSIGNED: {LeadStatus.QUALIFIED, LeadStatus.DRAFT_READY,
                          LeadStatus.SUPPRESSED, LeadStatus.ARCHIVED},
    # 线索中心人工选定后可以直接进入待发送池，不必重复走“提交审核”状态；
    # 真实发送成功后，draft_ready -> contacted 是合法且可审计的路径。
    LeadStatus.DRAFT_READY: {LeadStatus.AWAITING_REVIEW, LeadStatus.CONTACTED,
                             LeadStatus.SUPPRESSED, LeadStatus.ARCHIVED},
    LeadStatus.AWAITING_REVIEW: {LeadStatus.APPROVED, LeadStatus.DRAFT_READY,
                                 LeadStatus.SUPPRESSED, LeadStatus.ARCHIVED},
    LeadStatus.APPROVED: {LeadStatus.CONTACTED, LeadStatus.SUPPRESSED,
                          LeadStatus.ARCHIVED},
    LeadStatus.CONTACTED: {LeadStatus.REPLIED, LeadStatus.SUPPRESSED,
                           LeadStatus.ARCHIVED},
    LeadStatus.REPLIED: {LeadStatus.CONVERTED, LeadStatus.CLOSED,
                         LeadStatus.SUPPRESSED, LeadStatus.ARCHIVED},
    LeadStatus.CONVERTED: {LeadStatus.CLOSED, LeadStatus.ARCHIVED},
    LeadStatus.CLOSED: {LeadStatus.ARCHIVED},
    # 只有显式 remove_suppression 才允许解除勿扰，默认流程不会自动恢复。
    LeadStatus.SUPPRESSED: {LeadStatus.QUALIFIED, LeadStatus.NEW},
    LeadStatus.ARCHIVED: {LeadStatus.SUPPRESSED},
}


def is_valid_lead_transition(current: str, target: str) -> bool:
    """判断线索状态是否允许变更；重复写入同一状态视为幂等。"""
    if current == target:
        return True
    return target in LEAD_TRANSITIONS.get(current, set())


# ---------------------------------------------------------------------------
# 互动草稿状态机（6.4）
# ---------------------------------------------------------------------------
class DraftStatus:
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ALL = (
        DRAFT, PENDING_REVIEW, APPROVED, QUEUED, SENDING, SENT,
        REJECTED, FAILED, CANCELLED,
    )


DRAFT_TRANSITIONS = {
    DraftStatus.DRAFT: {DraftStatus.PENDING_REVIEW, DraftStatus.QUEUED,
                        DraftStatus.CANCELLED},
    DraftStatus.PENDING_REVIEW: {DraftStatus.APPROVED, DraftStatus.REJECTED,
                                 DraftStatus.CANCELLED},
    DraftStatus.APPROVED: {DraftStatus.QUEUED, DraftStatus.CANCELLED},
    # 待发送项允许退回待生成，重新编辑/审核后再进入发送池。
    DraftStatus.QUEUED: {DraftStatus.DRAFT, DraftStatus.SENDING,
                         DraftStatus.FAILED, DraftStatus.CANCELLED},
    DraftStatus.SENDING: {DraftStatus.SENT, DraftStatus.FAILED},
    DraftStatus.SENT: set(),
    DraftStatus.REJECTED: set(),
    # 回复失败后允许运营人员退回待生成重新编辑，或直接删除。
    DraftStatus.FAILED: {DraftStatus.DRAFT, DraftStatus.CANCELLED},
    DraftStatus.CANCELLED: set(),
}


def is_valid_draft_transition(current: str, target: str) -> bool:
    """判断草稿状态是否允许变更；重复写入同一状态视为幂等。"""
    if current == target:
        return True
    return target in DRAFT_TRANSITIONS.get(current, set())


# 互动事件类型（5.5）
class InteractionEventType:
    DRAFT_CREATED = "draft_created"
    DRAFT_REVIEWED = "draft_reviewed"
    DRAFT_QUEUED = "draft_queued"
    REPLY_TARGET_RESOLVED = "reply_target_resolved"
    REPLY_FILLED = "reply_filled"
    SEND_REQUESTED = "send_requested"
    SEND_SUCCESS = "send_success"
    SEND_FAILED = "send_failed"
    SUPPRESSED = "suppressed"


# ---------------------------------------------------------------------------
# 领域数据类
# ---------------------------------------------------------------------------
@dataclass
class RegionResult:
    """地域判定结果。"""

    province: Optional[str] = None
    city: Optional[str] = None
    source: str = RegionSource.UNKNOWN
    confidence: int = 0
    notes: List[str] = field(default_factory=list)


@dataclass
class FreshnessResult:
    bucket: str = FreshnessBucket.UNKNOWN
    reference_time: Optional[str] = None  # 判定用的基准时间（ISO）


@dataclass
class IntentResult:
    score: int = 0
    level: str = IntentLevel.UNKNOWN
    reasons: List[str] = field(default_factory=list)
    negatives: List[str] = field(default_factory=list)
    rule_version: Optional[str] = None


@dataclass
class DedupeResult:
    key: str
    basis: str  # 采用哪一种去重依据（user_id / profile_url / anonymous）


@dataclass
class LeadSummary:
    """线索列表页的一行摘要。"""

    id: int
    platform: str
    nickname: Optional[str]
    platform_user_id: Optional[str]
    profile_url: Optional[str]
    region_province: Optional[str]
    region_source: str
    region_confidence: int
    freshness_bucket: str
    intent_level: str
    intent_score: int
    intent_reasons: List[str]
    pool: str
    owner_id: Optional[int]
    owner_name: Optional[str]
    status: str
    last_interaction_at: Optional[str]
    summary_text: Optional[str] = None
    comment_time: Optional[str] = None
    source_url: Optional[str] = None

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "LeadSummary":
        def _j(v):
            if not v:
                return []
            if isinstance(v, list):
                return v
            import json

            try:
                return json.loads(v)
            except Exception:
                return []

        return cls(
            id=row["id"],
            platform=row["platform"],
            nickname=row.get("nickname"),
            platform_user_id=row.get("platform_user_id"),
            profile_url=row.get("profile_url"),
            region_province=row.get("region_province"),
            region_source=row.get("region_source") or RegionSource.UNKNOWN,
            region_confidence=row.get("region_confidence") or 0,
            freshness_bucket=row.get("freshness_bucket") or FreshnessBucket.UNKNOWN,
            intent_level=row.get("intent_level") or IntentLevel.UNKNOWN,
            intent_score=row.get("intent_score") or 0,
            intent_reasons=_j(row.get("intent_reasons")),
            pool=row.get("pool") or Pool.UNCLASSIFIED,
            owner_id=row.get("owner_id"),
            owner_name=row.get("owner_name"),
            status=row.get("status") or LeadStatus.NEW,
            last_interaction_at=row.get("last_interaction_at"),
            summary_text=row.get("summary_text"),
            comment_time=row.get("comment_time") or row.get("last_interaction_at"),
            source_url=row.get("source_url"),
        )


@dataclass
class LeadDetail:
    """线索详情视图：线索 + 证据 + 分配 + 时间线 + 审计 + 互动事件。"""

    lead: Dict[str, Any]
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    assignments: List[Dict[str, Any]] = field(default_factory=list)
    timeline: List[Dict[str, Any]] = field(default_factory=list)
    audit: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class EligibilityResult:
    """回复资格策略结果（6.5）。"""

    allowed: bool
    reasons: List[str]
    policy_version: str


@dataclass
class BatchResult:
    """批量操作结果：单条失败不阻断其余。"""

    success_count: int
    failed: List[Dict[str, Any]]  # [{ref, reason}]


@dataclass
class Page:
    items: List[Any]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size) if self.page_size else 1
