# -*- coding: utf-8 -*-
"""各平台的安全基础健康检查。"""

from __future__ import annotations

import importlib
from datetime import datetime

from .health import HealthStatus, HealthStore


PLATFORMS = {
    "douyin": ("抖音", "dy_collect"),
    "xhs": ("小红书", "xhs_collect3"),
    "weibo": ("微博", "weibo_chrome"),
    "bilibili": ("B站", "bilibili_adapter"),
    "kuaishou": ("快手", "kuaishou_collect"),
}


class PlatformHealthChecker:
    """运行不触碰浏览器的平台基础探针，并保存事件。"""

    def __init__(self, conn, clock=None):
        self.conn = conn
        self.clock = clock or (lambda: datetime.now().isoformat(timespec="seconds"))

    @staticmethod
    def _module_ready(module_name: str) -> tuple[bool, str]:
        try:
            importlib.import_module(module_name)
            return True, "本地采集适配器可加载"
        except Exception as exc:  # noqa: BLE001
            return False, f"适配器加载失败：{type(exc).__name__}: {exc}"

    def run(self) -> dict:
        accounts = self.conn.execute(
            "SELECT id, name, platform, bb_window_id, status FROM accounts ORDER BY id"
        ).fetchall()
        account_map = {platform: [] for platform in PLATFORMS}
        for row in accounts:
            if row["platform"] in account_map:
                account_map[row["platform"]].append(dict(row))
        rows = []
        store = HealthStore(self.conn, clock=self.clock)
        reply_ok, _reply_detail = self._module_ready("interactions.browser_reply")
        for platform, (label, module_name) in PLATFORMS.items():
            adapter_ok, adapter_detail = self._module_ready(module_name)
            platform_accounts = account_map[platform]
            bound_count = sum(1 for item in platform_accounts if str(item.get("bb_window_id") or "").strip())
            if not adapter_ok:
                status = HealthStatus.FAILED
                detail = adapter_detail
            elif not bound_count:
                status = HealthStatus.WARNING
                detail = "采集适配器正常，但尚未绑定可用浏览器账号"
            elif not reply_ok:
                status = HealthStatus.FAILED
                detail = "回复适配器不可加载"
            else:
                status = HealthStatus.OK
                detail = (f"采集/回复适配器正常，已绑定 {bound_count} 个账号；"
                          "登录状态需通过实际浏览器检测确认")
            store.record(
                platform, "基础健康检查", status, detail,
                metadata={"adapter_module": module_name, "adapter_ready": adapter_ok,
                          "reply_adapter_ready": reply_ok, "bound_account_count": bound_count},
            )
            rows.append({
                "platform": platform, "platform_label": label, "status": status,
                "status_label": {HealthStatus.OK: "正常", HealthStatus.WARNING: "需配置",
                                  HealthStatus.FAILED: "异常"}.get(status, status),
                "adapter": "正常" if adapter_ok else "异常",
                "reply": "正常" if reply_ok else "异常",
                "accounts": bound_count, "detail": detail,
            })
        return {"rows": rows, "checked_at": self.clock()}
