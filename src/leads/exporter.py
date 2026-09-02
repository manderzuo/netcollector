# -*- coding: utf-8 -*-
"""线索导出器（技术方案 Agent G / 9.8）。

- UTF-8 BOM CSV；
- 导出字段白名单（最小必要），不导出 Cookie、窗口 ID、内部错误堆栈；
- 文件名清洗，跨电脑可移动路径；
- 导出失败不改变线索分配状态。
"""

from __future__ import annotations

import csv
import json
import os
import re
from typing import Any, Dict, List, Optional

from leads.repository import LeadRepository

# 导出字段白名单（最小必要）
EXPORT_FIELDS = [
    "platform", "platform_user_id", "profile_url", "nickname",
    "region_province", "region_city", "region_source", "region_confidence",
    "freshness_bucket", "intent_level", "intent_score", "intent_reasons",
    "last_interaction_at", "owner_id", "status", "note",
]

# 明令禁止出现在导出中的字段名（防御性检查）
_FORBIDDEN = {"cookie", "token", "password", "window_id", "bb_window_id",
              "traceback", "stack"}


class LeadExporter:
    def __init__(self, repo: LeadRepository):
        self._repo = repo

    def export(self, lead_ids: List[int], target_dir: str,
               filename: Optional[str] = None) -> str:
        """导出指定线索为 UTF-8 BOM CSV，返回文件路径。"""
        os.makedirs(target_dir, exist_ok=True)
        fname = filename or self._default_filename()
        out_path = os.path.join(target_dir, self._safe_filename(fname))
        if not out_path.lower().endswith(".csv"):
            out_path += ".csv"

        rows = []
        for lid in lead_ids:
            lead = self._repo.get(lid)
            if lead is not None:
                rows.append(self._to_row(lead))

        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return out_path

    def export_all_pool(self, pool: str, target_dir: str,
                        filename: Optional[str] = None) -> str:
        """按池导出全部线索（河南/外省/待确认等）。

        循环翻页取完所有匹配行，不限于单页（修复 P2-5）。
        """
        from leads.repository import LeadQuery

        ids: List[int] = []
        page = 1
        page_size = 500
        while True:
            result = self._repo.list_leads(LeadQuery(pool=pool, page=page,
                                                     page_size=page_size))
            ids.extend(item.id for item in result.items)
            if page * page_size >= result.total:
                break
            page += 1
        fname = filename or f"leads_pool_{pool}.csv"
        return self.export(ids, target_dir, filename=fname)

    # ------------------------------------------------------------------
    @classmethod
    def _to_row(cls, lead: dict) -> Dict[str, Any]:
        row = {k: lead.get(k) for k in EXPORT_FIELDS}
        reasons = row.get("intent_reasons")
        if reasons and not isinstance(reasons, str):
            row["intent_reasons"] = json.dumps(reasons, ensure_ascii=False)
        # 防御：白名单外字段绝不进入导出
        return row

    @staticmethod
    def _default_filename() -> str:
        from datetime import datetime

        return f"leads_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    @staticmethod
    def _safe_filename(name: str) -> str:
        s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(name)).strip()
        return s or "export"
