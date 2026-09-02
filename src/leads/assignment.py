# -*- coding: utf-8 -*-
"""负责人分配（技术方案 Agent G / 7.2）。

- assign：批量分配并写审计；单条失败不能导致其余结果丢失
- export_for_owner：按最小必要字段导出（复用 LeadExporter），不含 Cookie/内部窗口 ID
"""

from __future__ import annotations

from typing import Any, Dict, List

from leads.models import BatchResult, LeadStatus, Pool
from leads.repository import LeadRepository
from .exporter import LeadExporter


class AssignmentService:
    def __init__(self, repo: LeadRepository):
        self._repo = repo

    # ------------------------------------------------------------------
    def assign(self, lead_ids: List[int], owner_id: int, reason: str,
               actor: str = "user") -> BatchResult:
        """批量分配；单条失败不阻断其余。写分配记录 + 审计。"""
        failed: List[Dict[str, Any]] = []
        ok = 0
        for lid in lead_ids:
            try:
                lead = self._repo.get(lid)
                if lead is None:
                    failed.append({"ref": lid, "reason": "线索不存在"})
                    continue
                owner = self._repo.get_owner(owner_id)
                if owner is None:
                    failed.append({"ref": lid, "reason": f"负责人不存在: {owner_id}"})
                    continue
                if not owner.get("enabled"):
                    failed.append({"ref": lid, "reason": f"负责人已停用: {owner_id}"})
                    continue

                from_owner = lead.get("owner_id")
                current_status = lead.get("status") or LeadStatus.NEW
                if current_status in (LeadStatus.SUPPRESSED, LeadStatus.ARCHIVED):
                    failed.append({"ref": lid, "reason": "勿扰或归档线索不能分配"})
                    continue
                if current_status == LeadStatus.NEW:
                    if lead.get("pool") != Pool.HENAN:
                        failed.append({"ref": lid, "reason": "线索尚未通过河南资格判定"})
                        continue
                    self._repo.transition_status(lid, LeadStatus.QUALIFIED)
                    current_status = LeadStatus.QUALIFIED
                if current_status == LeadStatus.QUALIFIED:
                    self._repo.transition_status(lid, LeadStatus.ASSIGNED)
                self._repo.update_fields(lid, {"owner_id": owner_id})
                self._repo.add_assignment(
                    lid, action="assign",
                    from_owner_id=from_owner,
                    to_owner_id=owner_id,
                    province=lead.get("region_province"),
                    reason=reason,
                )
                self._repo.audit(lid, actor, "assign",
                                 before_value={"owner_id": from_owner},
                                 after_value={"owner_id": owner_id})
                ok += 1
            except Exception as exc:  # noqa: BLE001 失败隔离
                failed.append({"ref": lid, "reason": str(exc)})
        return BatchResult(success_count=ok, failed=failed)

    def unassign(self, lead_ids: List[int], reason: str, actor: str = "user") -> BatchResult:
        """撤回分配（owner_id 置空），保留分配历史。"""
        failed: List[Dict[str, Any]] = []
        ok = 0
        for lid in lead_ids:
            try:
                lead = self._repo.get(lid)
                if lead is None:
                    failed.append({"ref": lid, "reason": "线索不存在"})
                    continue
                from_owner = lead.get("owner_id")
                if lead.get("status") == LeadStatus.ASSIGNED:
                    self._repo.transition_status(lid, LeadStatus.QUALIFIED)
                self._repo.update_fields(lid, {"owner_id": None})
                self._repo.add_assignment(lid, action="unassign",
                                          from_owner_id=from_owner,
                                          to_owner_id=None,
                                          reason=reason)
                self._repo.audit(lid, actor, "unassign",
                                 before_value={"owner_id": from_owner},
                                 after_value={"owner_id": None})
                ok += 1
            except Exception as exc:  # noqa: BLE001
                failed.append({"ref": lid, "reason": str(exc)})
        return BatchResult(success_count=ok, failed=failed)

    # ------------------------------------------------------------------
    def export_for_owner(self, owner_id: int, lead_ids: List[int],
                         target_dir: str) -> str:
        """按最小必要字段导出 CSV（UTF-8 BOM，复用 LeadExporter）。

        - 输出目录由调用方确定（用户选择/配置），不写死；
        - 导出失败不改变线索分配状态；
        - 文件名清洗，避免路径注入。
        """
        owner = self._repo.get_owner(owner_id)
        owner_name = owner["name"] if owner else f"owner-{owner_id}"
        fname = f"leads_{LeadExporter._safe_filename(owner_name)}_{owner_id}.csv"
        return LeadExporter(self._repo).export(lead_ids, target_dir, filename=fname)
