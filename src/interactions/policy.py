# -*- coding: utf-8 -*-
"""回复资格策略（技术方案 6.5 与 Agent D）。

``ReplyEligibilityPolicy.evaluate(lead, context)`` 返回 EligibilityResult：
- allowed: bool
- reasons: list[str]（允许时的通过项 / 拒绝时的具体原因）
- policy_version: str

默认拒绝优先：任何字段缺失、异常或状态不明时返回不允许，而不是猜测允许。
本策略**不执行发送**，只做资格判断（发送由 sender 负责且当前默认禁用）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from leads.models import (
    DraftStatus,
    EligibilityResult,
    IntentLevel,
    LeadStatus,
    Pool,
)

POLICY_VERSION = "reply-eligibility-v4-manual-interaction"
BILIBILI_MANUAL_REVIEW_PLATFORM = "bilibili"


class ReplyEligibilityPolicy:
    """回复资格策略。

    构造参数：
    - h_enan_confidence_threshold : 河南可信度阈值（默认 65）
    - minimum_intent_for_draft     : 允许生成草稿的最低意向（默认 medium）
    - feature_enabled              : 功能总开关（默认 False）
    - real_sender_enabled          : 真实发送开关（默认 False）
    """

    def __init__(
        self,
        *,
        henan_confidence_threshold: int = 65,
        minimum_intent_for_draft: str = IntentLevel.MEDIUM,
        feature_enabled: bool = False,
        real_sender_enabled: bool = False,
        require_human_review: bool = True,
    ):
        self._henan_threshold = henan_confidence_threshold
        self._min_intent = minimum_intent_for_draft
        self._feature_enabled = feature_enabled
        self._real_sender_enabled = real_sender_enabled
        self._require_human_review = require_human_review

    # ------------------------------------------------------------------
    @staticmethod
    def _is_bilibili_manual_review(lead: Dict[str, Any]) -> bool:
        """B 站当前没有可靠地域字段，默认交由人工选择和审核。"""
        return str(lead.get("platform") or "").strip().lower() in {
            BILIBILI_MANUAL_REVIEW_PLATFORM, "b站",
        }

    def evaluate(self, lead: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> EligibilityResult:
        """评估一条线索当前是否具备回复/互动资格。

        lead 至少需包含：
          region_province / region_confidence / intent_level /
          pool / status / contact_allowed
        context 可含：
          draft_status / in_suppression / account_platform / account_status /
          feature_enabled / real_sender_enabled
        """
        ctx = context or {}
        reasons: List[str] = []
        bilibili_manual = self._is_bilibili_manual_review(lead)
        manual_selection = bool(
            ctx.get("manual_selection") or ctx.get("manual_override")
        )

        # 1. 功能总开关（默认关闭）
        enabled = ctx.get("feature_enabled", self._feature_enabled)
        if not enabled:
            return EligibilityResult(False, ["功能开关未开启"], POLICY_VERSION)

        # 2. 字段缺失保护（默认拒绝）
        required = {} if manual_selection else {"intent_level": lead.get("intent_level")}
        if not bilibili_manual and not manual_selection:
            required.update({
                "region_province": lead.get("region_province"),
                "region_confidence": lead.get("region_confidence"),
            })
        missing = [k for k, v in required.items() if v is None or v == ""]
        if missing:
            return EligibilityResult(False, [f"字段缺失: {', '.join(missing)}"], POLICY_VERSION)

        # 3. 地域：普通平台必须河南且可信度达标；B 站目前无可靠地域字段，
        #    默认交由人工选择和审核，不做地域/地域池/可信度拦截。
        if not bilibili_manual and not manual_selection:
            if lead.get("region_province") != "河南省":
                return EligibilityResult(False, ["线索不在河南，禁止自动互动"], POLICY_VERSION)
            if lead.get("pool") != Pool.HENAN:
                return EligibilityResult(False, ["线索未进入河南有效池"], POLICY_VERSION)
            if int(lead.get("region_confidence", 0) or 0) < self._henan_threshold:
                return EligibilityResult(
                    False, [f"地域可信度不足: {lead.get('region_confidence')} < {self._henan_threshold}"],
                    POLICY_VERSION,
                )

        # 4. 意向：普通平台达到阈值；B 站默认全量交人工控制。
        if (not bilibili_manual and not manual_selection
                and IntentLevel.RANK.get(lead.get("intent_level"), -1)
                < IntentLevel.RANK.get(self._min_intent, 0)):
            return EligibilityResult(False, [f"意向不足: {lead.get('intent_level')}"], POLICY_VERSION)

        # 5. 勿扰名单
        if ctx.get("in_suppression"):
            return EligibilityResult(False, ["在勿扰名单中"], POLICY_VERSION)

        # 6. 未成功发送过首次联系（MVP 阶段只看 lead.status）
        if lead.get("status") in (LeadStatus.CONTACTED, LeadStatus.REPLIED,
                                  LeadStatus.CONVERTED, LeadStatus.CLOSED):
            return EligibilityResult(False, ["已完成首次联系"], POLICY_VERSION)

        # 7. 账号平台与线索平台一致 + 账号状态正常
        account_platform = ctx.get("account_platform")
        if account_platform and account_platform != lead.get("platform"):
            return EligibilityResult(False, ["账号平台与线索平台不一致"], POLICY_VERSION)
        account_status = ctx.get("account_status")
        if account_status in ("waiting_human", "frozen", "dead"):
            return EligibilityResult(False, [f"账号状态不可用: {account_status}"], POLICY_VERSION)

        # 8. 草稿必须经人工批准
        draft_status = ctx.get("draft_status")
        if self._require_human_review and draft_status != DraftStatus.APPROVED:
            return EligibilityResult(False, ["草稿未经人工批准"], POLICY_VERSION)

        # 9. 真实发送开关（当前默认关闭 → 即使其它全过也不允许真实发送）
        if not ctx.get("real_sender_enabled", self._real_sender_enabled):
            return EligibilityResult(False, ["当前版本未启用真实发送"], POLICY_VERSION)

        reasons.append("全部资格条件满足")
        return EligibilityResult(True, reasons, POLICY_VERSION)

    # ------------------------------------------------------------------
    def can_create_draft(self, lead: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> EligibilityResult:
        """判断"能否为这条线索生成草稿"。

        与发送资格不同：草稿生成**不需要草稿已批准、不需要真实发送开关**。
        普通平台只需要前置条件：河南+可信度达标+意向达标+不在勿扰+未联系过。
        B 站因当前没有可靠地域信息，默认跳过地域、地域池、可信度和意向限制，
        由人工选择和审核；勿扰、归档、已联系过的安全限制仍然生效。

        时效只作为线索信息展示，不参与自动资格拦截；是否处理由运营人员手动控制。
        """
        ctx = context or {}
        reasons: List[str] = []
        bilibili_manual = self._is_bilibili_manual_review(lead)

        if not bilibili_manual and lead.get("region_province") != "河南省":
            return EligibilityResult(False, ["线索不在河南"], POLICY_VERSION)
        if not bilibili_manual and lead.get("pool") != Pool.HENAN:
            return EligibilityResult(False, ["线索未进入河南有效池"], POLICY_VERSION)
        if not bilibili_manual and int(lead.get("region_confidence", 0) or 0) < self._henan_threshold:
            return EligibilityResult(False, ["地域可信度不足"], POLICY_VERSION)
        if not bilibili_manual and IntentLevel.RANK.get(lead.get("intent_level"), -1) < IntentLevel.RANK.get(self._min_intent, 0):
            return EligibilityResult(False, ["意向不足"], POLICY_VERSION)
        if (ctx.get("in_suppression")
                or lead.get("status") in (LeadStatus.SUPPRESSED, LeadStatus.ARCHIVED)
                or lead.get("pool") == Pool.ARCHIVED):
            return EligibilityResult(False, ["在勿扰/归档名单"], POLICY_VERSION)
        if lead.get("status") in (LeadStatus.CONTACTED, LeadStatus.REPLIED,
                                  LeadStatus.CONVERTED, LeadStatus.CLOSED):
            return EligibilityResult(False, ["已完成首次联系"], POLICY_VERSION)

        reasons.append("可以生成草稿")
        return EligibilityResult(True, reasons, POLICY_VERSION)
