# -*- coding: utf-8 -*-
"""线索管道批量回填（技术方案 Agent C）。

把已有评论批量转换为线索。作为命令/批处理函数存在，**默认不自动运行**（9.4：
批量回填旧评论的命令函数不自动触发；也不得在采集线程中执行耗时全库扫描）。

设计要点：
- 分批提交（默认 500 条/批，符合 13 章 200~1000 建议）；
- 失败隔离：单条坏数据不阻断整批，返回失败明细；
- 幂等：通过 ingest_comment 内部 dedupe_key 保证重复执行不产生重复线索/证据。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .service import LeadService

log = logging.getLogger(__name__)


class IngestStats:
    def __init__(self):
        self.total = 0
        self.created = 0
        self.updated = 0
        self.failed: List[Dict[str, Any]] = []

    def as_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "created": self.created,
            "updated": self.updated,
            "failed_count": len(self.failed),
            "failed": self.failed[:50],
        }


def batch_ingest_comments(
    service: LeadService,
    comment_ids: List[int],
    *,
    batch_size: int = 500,
    context_factory=None,
) -> IngestStats:
    """批量把评论转线索。逐条失败隔离，返回汇总统计。

    context_factory: 可选 callable（comment_id -> dict 或 None），为每条评论
    提供额外地域/互动上下文；不提供则走纯文本推断。
    """
    stats = IngestStats()
    stats.total = len(comment_ids)

    for i in range(0, len(comment_ids), batch_size):
        batch = comment_ids[i:i + batch_size]
        for cid in batch:
            try:
                ctx = context_factory(cid) if context_factory else None
                _lead_id, created = service.ingest_comment_and_report(cid, context=ctx)
                if created:
                    stats.created += 1
                else:
                    stats.updated += 1
            except Exception as exc:  # noqa: BLE001 失败隔离
                log.warning("评论 %s 转线索失败: %s", cid, exc)
                stats.failed.append({"ref": cid, "reason": str(exc)})
    return stats
