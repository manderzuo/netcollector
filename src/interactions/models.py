# -*- coding: utf-8 -*-
"""互动中心领域数据类（技术方案 Agent D 与 Agent A）。

草稿状态(DraftStatus)、事件类型(InteractionEventType)等冻结常量在
``src.leads.models`` 已定义，本模块复用而不重复定义（11.3「模型冲突以
models.py 冻结契约为准」）。这里只补充互动专属数据类。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from leads.models import DraftStatus, EligibilityResult, InteractionEventType  # noqa: F401
from .reply_target import ReplyTarget


@dataclass
class DraftDetail:
    """审核卡片所需的草稿视图。"""

    id: int
    lead_id: int
    channel: str
    template_id: Optional[str]
    content: str
    generation_mode: str
    policy_snapshot: Dict[str, Any]
    status: str
    reviewed_by: Optional[str]
    reviewed_at: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]
    reply_account_id: Optional[int] = None
    reply_account_name: Optional[str] = None
    reply_account_platform: Optional[str] = None
    source_content: Optional[str] = None
    failure_reason: Optional[str] = None
    task_id: Optional[int] = None
    customer_replied: bool = False
    interaction_type: str = "comment_reply"

    # 关联线索上下文（用于展示与资格复检）
    lead: Optional[Dict[str, Any]] = None
    eligibility: Optional[EligibilityResult] = None

    @classmethod
    def from_row(cls, row: dict) -> "DraftDetail":
        import json

        def _j(v, default):
            if not v:
                return default
            if isinstance(v, (dict, list)):
                return v
            try:
                return json.loads(v)
            except Exception:
                return default

        return cls(
            id=row["id"],
            lead_id=row["lead_id"],
            channel=row.get("channel") or "unknown",
            interaction_type=row.get("interaction_type") or "comment_reply",
            template_id=row.get("template_id"),
            content=row.get("content") or "",
            generation_mode=row.get("generation_mode") or "manual",
            policy_snapshot=_j(row.get("policy_snapshot"), {}),
            status=row.get("status") or DraftStatus.DRAFT,
            reviewed_by=row.get("reviewed_by"),
            reviewed_at=row.get("reviewed_at"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            reply_account_id=row.get("reply_account_id"),
            reply_account_name=row.get("reply_account_name"),
            reply_account_platform=row.get("reply_account_platform"),
            source_content=row.get("source_content") or row.get("original_comment"),
            failure_reason=row.get("failure_reason"),
        )


@dataclass
class ReplyCandidate:
    """回复生成器的输出（7.4）。只返回候选，不执行发送。"""

    text: str
    used_rules: List[str] = field(default_factory=list)
    risk_notes: List[str] = field(default_factory=list)
    generator: str = "rule-based"


@dataclass
class ReviewSnapshot:
    """审核前后内容快照（供审计）。"""

    decision: str
    reviewer: str
    before_content: str
    after_content: str


@dataclass
class ReplyActionResult:
    """浏览器回复动作结果；填充成功不等于平台已发送。"""

    ok: bool
    stage: str
    message: str
    verified: bool = False
    details: Dict[str, Any] = field(default_factory=dict)
    target: Optional[ReplyTarget] = None
