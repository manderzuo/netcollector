# -*- coding: utf-8 -*-
"""互动应用服务（技术方案 Agent D）。

实现：
- create_draft  : 生成草稿（不发送）
- review        : 批准/拒绝，记录审核前后内容
- queue         : 逐条资格检查后排队；默认功能开关关闭
- add_suppression / remove_suppression：勿扰名单
- send          : 调用 Sender（MVP 默认 DisabledSender，明确拒绝）

所有业务状态改变写审计/事件（5.5）。草稿状态转换遵守 DRAFT_TRANSITIONS。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from leads.models import (
    BatchResult,
    DraftStatus,
    EligibilityResult,
    InteractionEventType,
    LeadStatus,
    Pool,
)
from leads.repository import LeadRepository
from .models import DraftDetail, ReplyActionResult, ReplyCandidate
from .policy import ReplyEligibilityPolicy
from .repository import InteractionRepository
from .reply_target import ReplyTarget, ReplyTargetError, ReplyTargetResolver
from .sender import DisabledSender, SendResult, Sender
from .templates import TemplateEngine

log = logging.getLogger(__name__)


class InteractionService:
    def __init__(
        self,
        repo: InteractionRepository,
        lead_repo: LeadRepository,
        *,
        policy: Optional[ReplyEligibilityPolicy] = None,
        templates: Optional[TemplateEngine] = None,
        sender: Optional[Sender] = None,
        reply_adapter=None,
        private_message_adapter=None,
        config: Optional[dict] = None,
    ):
        self._repo = repo
        self._lead_repo = lead_repo
        # 配置：优先显式传入，否则从 AppConfig 加载（P3-3）
        if config is None:
            from config_loader import AppConfig

            config = AppConfig().load() or {}
        self._config = config or {}
        interaction_config = (config or {}).get("interaction", config or {})
        lead_config = (config or {}).get("lead_rules", {})
        if policy is None:
            policy = ReplyEligibilityPolicy(
                henan_confidence_threshold=lead_config.get(
                    "henan_confidence_threshold",
                    (interaction_config or {}).get("henan_confidence_threshold", 65),
                ),
                minimum_intent_for_draft=lead_config.get(
                    "minimum_intent_for_draft",
                    (interaction_config or {}).get("minimum_intent_for_draft", "medium")),
                feature_enabled=bool((interaction_config or {}).get("enabled", False)),
                real_sender_enabled=bool((interaction_config or {}).get("real_sender_enabled", False)),
                require_human_review=bool((interaction_config or {}).get("require_human_review", True)),
            )
        self._policy = policy
        self._target_resolver = ReplyTargetResolver(self._lead_repo._conn)
        self._reply_adapter = reply_adapter
        self._private_message_adapter = private_message_adapter
        self._browser_reply_enabled = bool(
            (interaction_config or {}).get("browser_reply_enabled", False)
        )
        if templates is None:
            templates = self._load_templates()
        self._templates = templates
        self._sender = sender or DisabledSender()

    @property
    def browser_reply_enabled(self) -> bool:
        """是否允许 UI 暴露浏览器回复入口。默认关闭。"""
        return self._browser_reply_enabled

    def _load_templates(self) -> TemplateEngine:
        """从持久化模板文件加载；不存在则用默认（P3-5）。"""
        try:
            from config_loader import templates_path

            engine = TemplateEngine.load(templates_path())
            # 默认模板缺失时补默认
            defaults = TemplateEngine().list_templates()
            for tid, content in defaults.items():
                if tid not in engine.list_templates():
                    engine._templates[tid] = content
            return engine
        except Exception:
            return TemplateEngine()

    def list_templates(self) -> Dict[str, str]:
        """返回当前本地话术模板，供互动中心下拉框使用。"""
        return self._templates.list_templates()

    def list_custom_variables(self) -> Dict[str, str]:
        """返回当前模板中可供运营编辑的自定义变量。"""
        return self._templates.list_custom_variables()

    def save_template(self, template_id: str, content: str,
                      custom_variables: Optional[Dict[str, Any]] = None) -> None:
        """保存模板和自定义变量；保存失败时不写入半成品配置。"""
        candidate = TemplateEngine(
            self._templates.list_templates(),
            custom_variables=(self._templates.list_custom_variables()
                              if custom_variables is None else custom_variables),
        )
        if custom_variables is not None:
            # 界面提交的是完整变量表，删除操作也应同步落盘，不能只做增量合并。
            candidate.replace_custom_variables(custom_variables)
        candidate.set_template(template_id, content)
        from config_loader import templates_path
        candidate.save(templates_path())
        self._templates = candidate

    def list_reply_accounts(self, include_unavailable: bool = False,
                            owner_user_id: Optional[int] = None) -> List[dict]:
        """返回互动中心待发送池可选择的账号。"""
        return self._repo.list_reply_accounts(
            include_unavailable=include_unavailable,
            owner_user_id=owner_user_id,
        )

    def reload_templates(self) -> None:
        """模板管理弹窗保存后刷新内存中的模板。"""
        self._templates = self._load_templates()

    def render_template_for_lead(self, template_id: str, lead_id: int) -> str:
        """按指定线索渲染模板，供界面切换话术时预填回复框。"""
        lead = self._lead_repo.get(lead_id)
        if lead is None:
            raise ValueError(f"线索不存在: {lead_id}")
        return self._render_template(template_id, lead)

    def update_draft_content(self, draft_id: int, content: str) -> None:
        """保存界面上人工修改的回复内容。"""
        if not str(content or "").strip():
            raise ValueError("回复内容不能为空")
        self._repo.update_draft_content(draft_id, str(content).strip())

    def assign_reply_account(self, draft_id: int, account_id: int) -> None:
        """保存待发送项选定的回复账号。"""
        draft = self._repo.get_draft(draft_id)
        if draft is None:
            raise ValueError(f"草稿不存在: {draft_id}")
        account = self._account_snapshot(account_id)
        if account is None:
            raise ValueError(f"账号不存在: {account_id}")
        lead = self._lead_repo.get(draft["lead_id"])
        if lead is None:
            raise ValueError(f"线索不存在: {draft['lead_id']}")
        source_platform = str(lead.get("platform") or draft.get("channel") or "")
        account_platform = str(account.get("platform") or "")
        if source_platform != account_platform:
            platform_labels = {
                "douyin": "抖音", "xhs": "小红书",
                "weibo": "微博", "bilibili": "B站",
            }
            raise ValueError(
                "回复账号平台不匹配：内容来源为"
                f"{platform_labels.get(source_platform, source_platform or '未知平台')}，"
                "所选账号属于"
                f"{platform_labels.get(account_platform, account_platform or '未知平台')}"
            )
        self._repo.assign_reply_account(draft_id, account_id)

    def move_draft_to_send(self, draft_id: int, content: Optional[str] = None) -> None:
        """把待生成草稿放入待发送池，不打开浏览器、不执行发送。"""
        draft = self._repo.get_draft(draft_id)
        if draft is None:
            raise ValueError(f"草稿不存在: {draft_id}")
        if draft.get("status") == DraftStatus.QUEUED:
            return
        if draft.get("status") != DraftStatus.DRAFT:
            raise ValueError(f"草稿当前状态不允许进入发送页: {draft.get('status')}")
        if content is not None:
            self.update_draft_content(draft_id, content)
        self._repo.update_draft_status(draft_id, DraftStatus.QUEUED)
        self._repo.log_event(
            draft["lead_id"], InteractionEventType.DRAFT_QUEUED,
            draft_id=draft_id, result="manual_enter_send_page",
        )

    def delete_draft(self, draft_id: int) -> None:
        """从互动流程中删除待生成/待发送/失败项，保留取消状态和审计链。"""
        draft = self._repo.get_draft(draft_id)
        if draft is None:
            raise ValueError(f"草稿不存在: {draft_id}")
        if draft.get("status") not in (
                DraftStatus.DRAFT, DraftStatus.QUEUED, DraftStatus.FAILED):
            raise ValueError("只有待生成、待发送或失败内容可以删除")
        self._repo.update_draft_status(draft_id, DraftStatus.CANCELLED)
        self._repo.log_event(
            draft["lead_id"], "draft_deleted", draft_id=draft_id,
            result="cancelled", detail="用户从互动中心删除",
        )

    def return_failed_to_draft(self, draft_id: int) -> None:
        """把失败项退回待生成，清除旧账号和审核上下文但保留回复内容。"""
        draft = self._repo.get_draft(draft_id)
        if draft is None:
            raise ValueError(f"草稿不存在: {draft_id}")
        if draft.get("status") != DraftStatus.FAILED:
            raise ValueError("只有失败内容可以退回待生成")
        self._repo.update_draft_status(draft_id, DraftStatus.DRAFT)
        self._repo.reset_draft_for_generation(draft_id)
        self._repo.log_event(
            draft["lead_id"], "draft_returned_to_generation", draft_id=draft_id,
            result="draft", detail="用户将失败内容退回待生成",
        )

    def return_queued_to_draft(self, draft_id: int) -> None:
        """把待发送项退回待生成，允许重新录入并重新审核。"""
        draft = self._repo.get_draft(draft_id)
        if draft is None:
            raise ValueError(f"草稿不存在: {draft_id}")
        if draft.get("status") != DraftStatus.QUEUED:
            raise ValueError("只有待发送内容可以退回待生成")
        self._repo.update_draft_status(draft_id, DraftStatus.DRAFT)
        # 旧账号和审核上下文不能带入下一轮重新录入流程。
        self._repo.reset_draft_for_generation(draft_id)
        self._repo.log_event(
            draft["lead_id"], "draft_returned_to_generation", draft_id=draft_id,
            result="draft", detail="用户将待发送内容退回待生成重新录入",
        )

    def mark_reply_failed(self, draft_id: int, message: str,
                          account_id: Optional[int] = None) -> None:
        """记录待发送定位/填充失败，使失败内容进入失败页签。"""
        draft = self._repo.get_draft(draft_id)
        if draft is None:
            raise ValueError(f"草稿不存在: {draft_id}")
        if draft.get("status") not in (DraftStatus.QUEUED, DraftStatus.SENDING):
            return
        self._repo.update_draft_status(draft_id, DraftStatus.FAILED)
        self._repo.log_event(
            draft["lead_id"], InteractionEventType.SEND_FAILED,
            draft_id=draft_id, account_id=account_id,
            result="failed", detail=message,
        )

    # ------------------------------------------------------------------
    # 草稿创建（不发送）
    # ------------------------------------------------------------------
    def create_draft(self, lead_id: int, template_id: Optional[str] = None,
                     content: Optional[str] = None,
                     generation_mode: str = "rule-based",
                     manual_override: bool = False,
                     interaction_type: str = "comment_reply") -> int:
        """生成草稿，不发送。内容来源：模板渲染 / 显式传入 / 默认文案。

        普通入口用 can_create_draft 做资格检查；线索中心明确勾选后传入
        ``generation_mode="manual-selection"``（或 ``manual_override=True``）时，
        视为运营人员明确放行，跳过地域、时效、意向、置信度、勿扰/归档和已联系等
        自动资格限制，只负责把内容导入互动中心。后续人工审核和发送仍按各自流程执行。
        """
        interaction_type = str(interaction_type or "comment_reply").strip().lower()
        if interaction_type not in {"comment_reply", "private_message"}:
            raise ValueError(f"不支持的互动类型: {interaction_type}")
        lead = self._lead_repo.get(lead_id)
        if lead is None:
            raise ValueError(f"线索不存在: {lead_id}")
        lead["platform"] = lead.get("platform") or "unknown"

        manual_selection = bool(manual_override or generation_mode == "manual-selection")
        if manual_selection:
            eligibility = EligibilityResult(
                True,
                ["人工选择放行：跳过自动资格限制"],
                "manual-selection-override-v1",
            )
        else:
            eligibility = self._policy.can_create_draft(
                lead,
                {"in_suppression": self._repo.is_suppressed(lead.get("dedupe_key") or "")},
            )
            if not eligibility.allowed:
                raise ValueError(f"无法生成草稿: {'; '.join(eligibility.reasons)}")

        # 草稿创建前推进线索状态；被勿扰/归档的线索已在资格策略中拒绝。
        current_status = lead.get("status") or LeadStatus.NEW
        if current_status == LeadStatus.NEW:
            self._lead_repo.transition_status(lead_id, LeadStatus.QUALIFIED)
            current_status = LeadStatus.QUALIFIED
        if current_status in (LeadStatus.QUALIFIED, LeadStatus.ASSIGNED):
            self._lead_repo.transition_status(lead_id, LeadStatus.DRAFT_READY)

        # 渲染内容
        if content is None:
            if template_id is None:
                template_id = "greeting"
            content = self._render_template(template_id, lead)

        reply_target = None
        if interaction_type == "comment_reply":
            try:
                reply_target = self._target_resolver.resolve_for_lead(lead_id)
            except ReplyTargetError as exc:
                # 历史线索可能没有来源评论；仍允许生成草稿，但后续填充时必须补齐目标。
                log.warning("草稿 %s 缺少可定位的原评论: %s", lead_id, exc)

        draft_id = self._repo.create_draft(
            lead_id=lead_id,
            channel=lead["platform"],
            content=content,
            generation_mode=generation_mode,
            template_id=template_id,
            policy_snapshot={"policy_version": eligibility.policy_version,
                             "reasons": eligibility.reasons,
                             "interaction_type": interaction_type,
                             "source_comment_id": (
                                 reply_target.local_comment_id if reply_target else None
                             )},
            target=reply_target,
            interaction_type=interaction_type,
        )
        self._repo.log_event(lead_id, InteractionEventType.DRAFT_CREATED,
                             draft_id=draft_id,
                             detail={"template_id": template_id,
                                     "interaction_type": interaction_type,
                                     "manual_selection": manual_selection})
        return draft_id

    def create_private_message_draft(self, lead_id: int,
                                     template_id: Optional[str] = None,
                                     content: Optional[str] = None,
                                     generation_mode: str = "manual-selection") -> int:
        """创建私信草稿；私信不依赖来源作品/原评论定位。"""
        return self.create_draft(
            lead_id,
            template_id=template_id,
            content=content,
            generation_mode=generation_mode,
            manual_override=True,
            interaction_type="private_message",
        )

    def _render_template(self, template_id: str, lead: Dict[str, Any]) -> str:
        variables = {
            "nickname": lead.get("nickname") or "",
            "platform": lead.get("platform") or "",
            "province": lead.get("region_province") or "",
            "city": lead.get("region_city") or "",
            "product": "",   # 运营配置，MVP 留空
            "contact_hint": "可私信我们了解详情。",
        }
        try:
            return self._templates.render(template_id, variables)
        except (KeyError, ValueError) as exc:
            # 模板渲染失败回退默认文案，不影响草稿创建
            log.warning("模板渲染失败 %s: %s，使用默认文案", template_id, exc)
            return f"您好{lead.get('nickname') or '朋友'}！如需了解详情可私信我们。"

    # ------------------------------------------------------------------
    # 提交审核（draft → pending_review）
    # ------------------------------------------------------------------
    def submit_for_review(self, draft_id: int) -> None:
        """把草稿提交审核。状态 draft → pending_review（6.4 状态机）。"""
        draft = self._repo.get_draft(draft_id)
        if draft is None:
            raise ValueError(f"草稿不存在: {draft_id}")
        if draft["status"] != DraftStatus.DRAFT:
            raise ValueError(f"草稿当前状态不允许提交审核: {draft['status']}")
        self._repo.update_draft_status(draft_id, DraftStatus.PENDING_REVIEW)
        self._lead_repo.transition_status(draft["lead_id"], LeadStatus.AWAITING_REVIEW)
        self._repo.log_event(draft["lead_id"], "draft_submitted", draft_id=draft_id)

    # ------------------------------------------------------------------
    # 审核
    # ------------------------------------------------------------------
    def review(self, draft_id: int, decision: str, reviewer: str,
               content: str) -> None:
        """批准或拒绝草稿；记录审核前后内容。

        decision: approved | rejected
        content: 审核后的最终文案（批准时生效）
        """
        draft = self._repo.get_draft(draft_id)
        if draft is None:
            raise ValueError(f"草稿不存在: {draft_id}")
        if draft["status"] != DraftStatus.PENDING_REVIEW:
            raise ValueError(f"草稿当前状态不允许审核: {draft['status']}")

        if decision not in ("approved", "rejected"):
            raise ValueError(f"非法审核决定: {decision}")

        lead = self._lead_repo.get(draft["lead_id"])
        if lead is None:
            raise ValueError(f"线索不存在: {draft['lead_id']}")
        if lead.get("status") in (LeadStatus.SUPPRESSED, LeadStatus.ARCHIVED):
            raise ValueError("线索已勿扰或归档，不能继续审核草稿")

        before_content = draft.get("content") or ""
        if decision == "approved":
            new_status = DraftStatus.APPROVED
            # 批准时以审核后的内容为准
            self._repo.update_draft_content(draft_id, content)
        else:
            new_status = DraftStatus.REJECTED
            # 拒绝时也保存审核后的内容（通常是修改说明或原文）
            if content and content != before_content:
                self._repo.update_draft_content(draft_id, content)

        from datetime import datetime

        self._repo.update_draft_status(
            draft_id, new_status,
            reviewed_by=reviewer,
            reviewed_at=datetime.now().isoformat(timespec="seconds"),
        )
        self._repo.log_event(
            draft["lead_id"], InteractionEventType.DRAFT_REVIEWED,
            draft_id=draft_id,
            result=decision,
            detail={"reviewer": reviewer,
                    "before": before_content[:200],
                    "after": content[:200]},
        )
        # 线索状态联动
        if decision == "approved":
            self._lead_repo.transition_status(draft["lead_id"], LeadStatus.APPROVED)
        else:
            self._lead_repo.transition_status(draft["lead_id"], LeadStatus.DRAFT_READY)

    def stage_for_send(self, draft_id: int, account_id: Optional[int] = None) -> None:
        """批准后进入待发送池；账号在待发送池的一键回复时选择。

        这里只做排队和审计，不打开浏览器、不填充页面，也不点击发送。
        """
        draft = self._repo.get_draft(draft_id)
        if draft is None:
            raise ValueError(f"草稿不存在: {draft_id}")
        if draft["status"] == DraftStatus.QUEUED:
            return
        if draft["status"] != DraftStatus.APPROVED:
            raise ValueError(f"草稿当前状态不允许进入待发送: {draft['status']}")
        if account_id is not None and self._account_snapshot(account_id) is None:
            raise ValueError(f"账号不存在: {account_id}")
        self._repo.update_draft_status(draft_id, DraftStatus.QUEUED)
        self._repo.log_event(
            draft["lead_id"], InteractionEventType.DRAFT_QUEUED,
            draft_id=draft_id, account_id=account_id,
            result="waiting_for_manual_send",
        )

    # ------------------------------------------------------------------
    # 排队（逐条资格检查，默认开关关闭）
    # ------------------------------------------------------------------
    def queue(self, draft_ids: List[int], account_id: int) -> BatchResult:
        """逐条执行资格检查后进入 queued；单条失败不阻断其余。

        默认功能开关关闭 → 全部被拒（拒绝原因里给出具体不满足项）。
        """
        failed: List[Dict[str, Any]] = []
        queued = 0
        for did in draft_ids:
            try:
                draft = self._repo.get_draft(did)
                if draft is None:
                    failed.append({"ref": did, "reason": "草稿不存在"})
                    continue
                lead = self._lead_repo.get(draft["lead_id"])
                if lead is None:
                    failed.append({"ref": did, "reason": "线索不存在"})
                    continue
                # 草稿须已批准
                if draft["status"] != DraftStatus.APPROVED:
                    failed.append({"ref": did, "reason": "草稿未批准"})
                    continue
                account = self._account_snapshot(account_id)
                if account_id is not None and account is None:
                    failed.append({"ref": did, "reason": f"账号不存在: {account_id}"})
                    continue
                eligibility = self._policy.evaluate(
                    lead,
                    {
                        "feature_enabled": True,   # queue 环节走策略内部开关
                        "draft_status": DraftStatus.APPROVED,
                        "account_platform": account.get("platform") if account else None,
                        "account_status": account.get("status") if account else None,
                        "in_suppression": self._repo.is_suppressed(lead.get("dedupe_key") or ""),
                        "manual_selection": self._is_manual_selection_draft(draft),
                        "real_sender_enabled": False,  # MVP 不允许真实发送
                    },
                )
                if not eligibility.allowed:
                    failed.append({"ref": did, "reason": "; ".join(eligibility.reasons)})
                    continue
                # 资格通过 → queued
                self._repo.update_draft_status(did, DraftStatus.QUEUED)
                self._repo.log_event(draft["lead_id"], InteractionEventType.DRAFT_QUEUED,
                                     draft_id=did, account_id=account_id)
                queued += 1
            except Exception as exc:  # noqa: BLE001 失败隔离
                log.warning("草稿 %s 排队失败: %s", did, exc)
                failed.append({"ref": did, "reason": str(exc)})
        return BatchResult(success_count=queued, failed=failed)

    def _account_snapshot(self, account_id: int) -> Optional[Dict[str, Any]]:
        conn = self._lead_repo._conn
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _is_manual_selection_draft(draft: Optional[Dict[str, Any]]) -> bool:
        """线索中心人工勾选进入互动中心的草稿，视为人工筛选通过。"""
        if not draft:
            return False
        if str(draft.get("generation_mode") or "") == "manual-selection":
            return True
        # 兼容已保存的人工放行草稿，即使历史记录的 generation_mode 不完整。
        return "manual-selection-override-v1" in str(
            draft.get("policy_snapshot") or ""
        )

    # ------------------------------------------------------------------
    # 浏览器回复：填充与发送分离，发送必须显式确认
    # ------------------------------------------------------------------
    def resolve_reply_target(self, draft_id: int,
                             account_id: Optional[int] = None) -> ReplyTarget:
        draft = self._repo.get_draft(draft_id)
        if draft is None:
            raise ReplyTargetError(f"草稿不存在: {draft_id}")
        if (draft.get("interaction_type") or "comment_reply") != "comment_reply":
            raise ReplyTargetError("私信草稿不使用评论定位目标")
        return self._target_resolver.resolve_for_draft(draft_id, account_id)

    def fill_browser_reply(self, draft_id: int,
                           account_id: Optional[int] = None,
                           *, simulate: bool = False) -> ReplyActionResult:
        """把已批准/待发送草稿填入来源作品的回复框，不点击发送。"""
        if not self._browser_reply_enabled and not simulate:
            raise ValueError("浏览器回复功能开关未开启")
        if self._reply_adapter is None:
            raise ValueError("未配置浏览器回复适配器")
        draft = self._repo.get_draft(draft_id)
        if draft is None:
            raise ValueError(f"草稿不存在: {draft_id}")
        if (draft.get("interaction_type") or "comment_reply") != "comment_reply":
            raise ValueError("当前草稿是私信，请使用私信发送流程")
        if draft.get("status") not in (DraftStatus.APPROVED, DraftStatus.QUEUED):
            raise ValueError("只有审核通过或待发送的草稿才能填入浏览器")
        target = self.resolve_reply_target(draft_id, account_id)
        if target.account_status in ("waiting_human", "frozen", "dead"):
            raise ValueError(f"回复账号当前不可用: {target.account_status}")
        result = self._reply_adapter.reply(target, draft.get("content") or "", confirm=False)
        self._repo.log_event(
            draft["lead_id"], InteractionEventType.REPLY_FILLED,
            draft_id=draft_id, account_id=target.account_id,
            result="ok" if result.ok else "failed",
            detail={"stage": result.stage, "message": result.message,
                    "local_comment_id": target.local_comment_id,
                    "video_id": target.video_id},
        )
        return result

    def simulate_browser_reply(self, draft_id: int,
                               account_id: Optional[int] = None) -> ReplyActionResult:
        """测试专用：执行昨天验证过的定位/填充链路，明确禁止发送。"""
        return self.fill_browser_reply(draft_id, account_id, simulate=True)

    def send_browser_reply(self, draft_id: int,
                           account_id: Optional[int] = None,
                           *, confirm: bool = False,
                           real_send_enabled: bool = False) -> ReplyActionResult:
        """重新定位并发送回复；确认与真实发送开关缺一不可。"""
        target = self.resolve_reply_target(draft_id, account_id)
        draft = self._repo.get_draft(draft_id)
        if draft is None or draft.get("status") not in (DraftStatus.APPROVED, DraftStatus.QUEUED):
            raise ValueError("只有审核通过或待发送的草稿才能发送")
        if (draft.get("interaction_type") or "comment_reply") != "comment_reply":
            raise ValueError("当前草稿是私信，请使用私信发送流程")
        if not confirm:
            return ReplyActionResult(
                ok=False, stage="confirmation_required",
                message="需要人工确认后才能点击发送", target=target,
            )
        # 兼容已通过配置显式开放浏览器回复的调用；GUI 新流程则必须由
        # 用户主动打开“真实发送”开关后传入 real_send_enabled=True。
        send_authorized = bool(real_send_enabled or self._browser_reply_enabled)
        if not send_authorized:
            raise ValueError("真实发送开关未开启，未点击发送")
        if self._reply_adapter is None:
            raise ValueError("未配置浏览器回复适配器")
        if target.account_status in ("waiting_human", "frozen", "dead"):
            raise ValueError(f"回复账号当前不可用: {target.account_status}")

        lead = self._lead_repo.get(draft["lead_id"]) or {}
        account = self._account_snapshot(target.account_id) if target.account_id else None
        eligibility = self._policy.evaluate(
            lead,
            {
                "feature_enabled": True,
                "draft_status": DraftStatus.APPROVED,
                "account_platform": account.get("platform") if account else None,
                "account_status": account.get("status") if account else None,
                "in_suppression": self._repo.is_suppressed(lead.get("dedupe_key") or ""),
                "manual_selection": self._is_manual_selection_draft(draft),
                "real_sender_enabled": send_authorized,
            },
        )
        if not eligibility.allowed:
            result = ReplyActionResult(
                ok=False, stage="policy_blocked",
                message="发送资格不满足: " + "; ".join(eligibility.reasons),
                target=target,
            )
            self._repo.log_event(
                draft["lead_id"], InteractionEventType.SEND_FAILED,
                draft_id=draft_id, account_id=target.account_id,
                result="failed", detail=result.message,
            )
            return result

        self._repo.update_draft_status(draft_id, DraftStatus.QUEUED)
        self._repo.update_draft_status(draft_id, DraftStatus.SENDING)
        result = self._reply_adapter.reply(
            target, draft.get("content") or "", confirm=True
        )
        if result.ok and result.verified:
            self._repo.update_draft_status(draft_id, DraftStatus.SENT)
            self._lead_repo.transition_status(draft["lead_id"], LeadStatus.CONTACTED)
            self._repo.log_event(
                draft["lead_id"], InteractionEventType.SEND_SUCCESS,
                draft_id=draft_id, account_id=target.account_id,
                result="ok", detail={"stage": result.stage,
                                      "local_comment_id": target.local_comment_id,
                                      "video_id": target.video_id},
            )
        else:
            self._repo.update_draft_status(draft_id, DraftStatus.FAILED)
            self._repo.log_event(
                draft["lead_id"], InteractionEventType.SEND_FAILED,
                draft_id=draft_id, account_id=target.account_id,
                result="failed", detail={"stage": result.stage,
                                          "message": result.message},
            )
        return result

    # ------------------------------------------------------------------
    # 浏览器私信：独立于来源作品评论定位
    # ------------------------------------------------------------------
    def resolve_private_message_target(
            self, draft_id: int, account_id: Optional[int] = None):
        try:
            from .private_message import PrivateMessageTarget
        except ImportError:  # pragma: no cover
            from interactions.private_message import PrivateMessageTarget  # type: ignore

        draft = self._repo.get_draft(draft_id)
        if draft is None:
            raise ValueError(f"草稿不存在: {draft_id}")
        if (draft.get("interaction_type") or "comment_reply") != "private_message":
            raise ValueError("当前草稿不是私信")
        lead = self._lead_repo.get(draft["lead_id"])
        if lead is None:
            raise ValueError(f"线索不存在: {draft['lead_id']}")
        selected_account = account_id if account_id is not None else draft.get("reply_account_id")
        account = self._account_snapshot(selected_account) if selected_account else None
        if account is None:
            raise ValueError("请先为私信选择发送账号")
        platform = str(lead.get("platform") or draft.get("channel") or "unknown")
        if str(account.get("platform") or "") != platform:
            raise ValueError("私信账号与线索平台不一致")
        profile_url = str(lead.get("profile_url") or "").strip()
        if not profile_url:
            profile_url = self._fallback_profile_url(
                platform, lead.get("platform_user_id")
            )
        return PrivateMessageTarget(
            lead_id=int(lead["id"]),
            draft_id=int(draft_id),
            platform=platform,
            account_id=int(account["id"]),
            account_name=account.get("name"),
            account_status=account.get("status"),
            bb_window_id=account.get("bb_window_id"),
            platform_user_id=lead.get("platform_user_id"),
            nickname=lead.get("nickname"),
            profile_url=profile_url,
        )

    @staticmethod
    def _fallback_profile_url(platform: str, platform_user_id: Any) -> str:
        """有平台用户 ID 但采集未落个人主页地址时，生成可复核的直达地址。"""
        user_id = str(platform_user_id or "").strip()
        if not user_id:
            return ""
        encoded = quote(user_id, safe="")
        return {
            "douyin": f"https://www.douyin.com/user/{encoded}",
            "xhs": f"https://www.xiaohongshu.com/user/profile/{encoded}",
            "bilibili": f"https://space.bilibili.com/{encoded}",
            "weibo": f"https://weibo.com/u/{encoded}",
            "kuaishou": f"https://www.kuaishou.com/profile/{encoded}",
        }.get(str(platform or "").strip().lower(), "")

    def fill_browser_private_message(
            self, draft_id: int, account_id: Optional[int] = None,
            *, simulate: bool = False) -> ReplyActionResult:
        """将私信正文填入目标用户会话，不点击发送。"""
        if not self._browser_reply_enabled and not simulate:
            raise ValueError("浏览器私信功能开关未开启")
        if self._private_message_adapter is None:
            raise ValueError("未配置浏览器私信适配器")
        draft = self._repo.get_draft(draft_id)
        if draft is None:
            raise ValueError(f"草稿不存在: {draft_id}")
        if draft.get("status") not in (DraftStatus.APPROVED, DraftStatus.QUEUED):
            raise ValueError("只有审核通过或待发送的私信才能填入浏览器")
        target = self.resolve_private_message_target(draft_id, account_id)
        if target.account_status in ("waiting_human", "frozen", "dead"):
            raise ValueError(f"私信账号当前不可用: {target.account_status}")
        result = self._private_message_adapter.send_message(
            target, draft.get("content") or "", confirm=False
        )
        self._repo.log_event(
            draft["lead_id"], "private_message_filled",
            draft_id=draft_id, account_id=target.account_id,
            result="ok" if result.ok else "failed",
            detail={"stage": result.stage, "message": result.message,
                    "profile_url": target.profile_url},
        )
        return result

    def simulate_private_message(self, draft_id: int,
                                 account_id: Optional[int] = None) -> ReplyActionResult:
        """测试专用：打开用户主页并填充私信，但禁止点击发送。"""
        return self.fill_browser_private_message(draft_id, account_id, simulate=True)

    def send_browser_private_message(
            self, draft_id: int, account_id: Optional[int] = None,
            *, confirm: bool = False,
            real_send_enabled: bool = False) -> ReplyActionResult:
        """重新定位用户并发送私信；确认和真实发送开关缺一不可。"""
        target = self.resolve_private_message_target(draft_id, account_id)
        draft = self._repo.get_draft(draft_id)
        if draft is None or draft.get("status") not in (DraftStatus.APPROVED, DraftStatus.QUEUED):
            raise ValueError("只有审核通过或待发送的私信才能发送")
        if not confirm:
            return ReplyActionResult(
                ok=False, stage="confirmation_required",
                message="需要人工确认后才能点击发送", target=target,
            )
        send_authorized = bool(real_send_enabled or self._browser_reply_enabled)
        if not send_authorized:
            raise ValueError("真实发送开关未开启，未点击发送")
        if self._private_message_adapter is None:
            raise ValueError("未配置浏览器私信适配器")
        if target.account_status in ("waiting_human", "frozen", "dead"):
            raise ValueError(f"私信账号当前不可用: {target.account_status}")

        lead = self._lead_repo.get(draft["lead_id"]) or {}
        account = self._account_snapshot(target.account_id) if target.account_id else None
        eligibility = self._policy.evaluate(
            lead,
            {
                "feature_enabled": True,
                "draft_status": DraftStatus.APPROVED,
                "account_platform": account.get("platform") if account else None,
                "account_status": account.get("status") if account else None,
                "in_suppression": self._repo.is_suppressed(lead.get("dedupe_key") or ""),
                "manual_selection": self._is_manual_selection_draft(draft),
                "real_sender_enabled": send_authorized,
            },
        )
        if not eligibility.allowed:
            result = ReplyActionResult(
                ok=False, stage="policy_blocked",
                message="发送资格不满足: " + "; ".join(eligibility.reasons),
                target=target,
            )
            self._repo.log_event(
                draft["lead_id"], InteractionEventType.SEND_FAILED,
                draft_id=draft_id, account_id=target.account_id,
                result="failed", detail=result.message,
            )
            return result

        self._repo.update_draft_status(draft_id, DraftStatus.QUEUED)
        self._repo.update_draft_status(draft_id, DraftStatus.SENDING)
        result = self._private_message_adapter.send_message(
            target, draft.get("content") or "", confirm=True
        )
        if result.ok and result.verified:
            self._repo.update_draft_status(draft_id, DraftStatus.SENT)
            self._lead_repo.transition_status(draft["lead_id"], LeadStatus.CONTACTED)
            self._repo.log_event(
                draft["lead_id"], InteractionEventType.SEND_SUCCESS,
                draft_id=draft_id, account_id=target.account_id,
                result="ok", detail={"stage": result.stage,
                                      "profile_url": target.profile_url},
            )
        else:
            self._repo.update_draft_status(draft_id, DraftStatus.FAILED)
            self._repo.log_event(
                draft["lead_id"], InteractionEventType.SEND_FAILED,
                draft_id=draft_id, account_id=target.account_id,
                result="failed", detail={"stage": result.stage,
                                          "message": result.message},
            )
        return result

    # ------------------------------------------------------------------
    # 发送（MVP：DisabledSender 明确拒绝；任何真实发送前必须资格复检）
    # ------------------------------------------------------------------
    def send(self, draft_id: int, account_id: int, idempotency_key: str) -> SendResult:
        draft = self._repo.get_draft(draft_id)
        if draft is None:
            return SendResult(False, "草稿不存在")
        lead = self._lead_repo.get(draft["lead_id"]) or {}
        lead["platform"] = lead.get("platform") or "unknown"

        # 发送前强制资格复检（方案 12.3：草稿批准后仍需再次资格检查；P2-4）
        account = self._account_snapshot(account_id)
        if account_id is not None and account is None:
            return SendResult(False, f"账号不存在: {account_id}", event_type="send_blocked_account")
        eligibility = self._policy.evaluate(
            lead,
            {
                "feature_enabled": True,
                "draft_status": draft.get("status"),
                "account_platform": account.get("platform") if account else None,
                "account_status": account.get("status") if account else None,
                "in_suppression": self._repo.is_suppressed(lead.get("dedupe_key") or ""),
                "manual_selection": self._is_manual_selection_draft(draft),
                "real_sender_enabled": False,  # MVP：真实发送开关恒关
            },
        )
        if not eligibility.allowed:
            # 资格不过：记录事件并拒绝，绝不经 sender
            self._repo.log_event(
                draft["lead_id"], InteractionEventType.SEND_FAILED,
                draft_id=draft_id, account_id=account_id,
                idempotency_key=idempotency_key,
                result="failed",
                detail="; ".join(eligibility.reasons),
            )
            return SendResult(False, "发送资格不满足: " + "; ".join(eligibility.reasons),
                              event_type="send_blocked_policy")

        result = self._sender.send(
            lead=lead,
            draft_id=draft_id,
            account_id=account_id,
            idempotency_key=idempotency_key,
        )
        event_type = InteractionEventType.SEND_SUCCESS if result.ok \
            else InteractionEventType.SEND_FAILED
        self._repo.log_event(
            draft["lead_id"], event_type, draft_id=draft_id,
            account_id=account_id, idempotency_key=idempotency_key,
            result="ok" if result.ok else "failed",
            detail=result.message,
        )
        return result

    # ------------------------------------------------------------------
    # 勿扰名单
    # ------------------------------------------------------------------
    def add_suppression(self, lead_id: int, reason: str, actor: str = "user") -> None:
        lead = self._lead_repo.get(lead_id)
        if lead is None:
            raise ValueError(f"线索不存在: {lead_id}")
        dedupe_key = lead.get("dedupe_key")
        self._repo.add_suppression(
            lead.get("platform") or "unknown", dedupe_key, reason,
            platform_user_id=lead.get("platform_user_id"),
        )
        # 标记线索为 suppressed，所有待处理草稿失效
        self._lead_repo.transition_status(lead_id, LeadStatus.SUPPRESSED)
        self._repo.cancel_open_drafts(lead_id)
        self._repo.log_event(lead_id, InteractionEventType.SUPPRESSED,
                             detail={"reason": reason})
        self._lead_repo.audit(lead_id, actor, "add_suppression",
                              after_value={"reason": reason})

    def remove_suppression(self, lead_id: int, actor: str = "user") -> None:
        lead = self._lead_repo.get(lead_id)
        if lead is None:
            raise ValueError(f"线索不存在: {lead_id}")
        self._repo.remove_suppression(lead.get("dedupe_key") or "")
        if lead.get("status") == LeadStatus.SUPPRESSED:
            target = LeadStatus.QUALIFIED if lead.get("pool") == Pool.HENAN else LeadStatus.NEW
            self._lead_repo.transition_status(lead_id, target)
        self._lead_repo.audit(lead_id, actor, "remove_suppression")

    def list_drafts(self, status: Optional[str] = None, lead_id: Optional[int] = None,
                    lead_status: Optional[str] = None,
                    page: int = 1, page_size: int = 50, *,
                    statuses: Optional[List[str]] = None,
                    platform: Optional[str] = None,
                    account_id: Optional[int] = None,
                    interaction_type: Optional[str] = None,
                    data_owner_user_id: Optional[int] = None) -> Dict[str, Any]:
        return self._repo.list_drafts(status=status, lead_id=lead_id,
                                      lead_status=lead_status,
                                      statuses=statuses, platform=platform,
                                      account_id=account_id,
                                      interaction_type=interaction_type,
                                      data_owner_user_id=data_owner_user_id,
                                      page=page, page_size=page_size)
