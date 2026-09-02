# -*- coding: utf-8 -*-
"""发送器接口与禁用实现（技术方案 7.4 / 9.5 / 6.4）。

第一、二阶段（M1/M2）**只提供接口和 DisabledSender**：调用真实发送必须明确
返回“当前版本未启用自动发送”。任何 Agent 都不得在本文件中实现真实平台发送。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class SendResult:
    ok: bool
    message: str
    event_type: str = "send_requested"


class Sender:
    """发送器抽象接口。子类实现真实发送（仅 M3 及以后、且经用户批准）。"""

    name = "abstract"

    def send(self, *, lead: Dict[str, Any], draft_id: int,
             account_id: int, idempotency_key: str,
             context: Optional[Dict[str, Any]] = None) -> SendResult:
        raise NotImplementedError


class DisabledSender(Sender):
    """禁用发送器：MVP 默认实现。任何调用都明确拒绝。"""

    name = "disabled"

    def send(self, *, lead: Dict[str, Any], draft_id: int,
             account_id: int, idempotency_key: str,
             context: Optional[Dict[str, Any]] = None) -> SendResult:
        return SendResult(
            ok=False,
            message="当前版本未启用自动发送（DisabledSender）",
            event_type="send_blocked_disabled",
        )
