# -*- coding: utf-8 -*-
"""四平台真实浏览器深度诊断。

有已采集评论时，复用正式回复适配器执行：打开作品、定位评论、点击回复、
填入测试文本，然后停在发送前。没有测试数据时只返回“未执行”，绝不伪造
样本，也不会打开浏览器。
"""

from __future__ import annotations

import json
import random
from datetime import datetime

from .browser_health import PLATFORM_INFO
from .health import HealthStatus, HealthStore
from interactions.browser_reply import BitBrowserReplyAdapter
from interactions.reply_target import ReplyTarget


_STATUS_LABEL = {
    HealthStatus.OK: "填充成功",
    HealthStatus.WARNING: "未执行",
    HealthStatus.HUMAN_REQUIRED: "需人工",
    HealthStatus.LOGIN_REQUIRED: "需登录",
    HealthStatus.FAILED: "失败",
}


def _extra_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class DeepBrowserDiagnosticRunner:
    """按平台随机抽取一条已有评论，执行只填不发的真实链路。"""

    def __init__(self, conn, bitbrowser, *, clock=None, reply_adapter=None):
        self.conn = conn
        self.bb = bitbrowser
        self.clock = clock or (lambda: datetime.now().isoformat(timespec="seconds"))
        self.reply_adapter = reply_adapter or BitBrowserReplyAdapter(bitbrowser)

    def run(self, manual_sample: dict | None = None) -> dict:
        accounts = self._accounts()
        samples = self._samples()
        run_id = f"browser-deep-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
        store = HealthStore(self.conn, clock=self.clock)
        rows = []
        for platform, (label, _domains, _home_url) in PLATFORM_INFO.items():
            platform_accounts = self._pick_accounts(accounts.get(platform, []))
            sample, sample_reason = self._pick_sample(
                samples.get(platform, []), platform, manual_sample
            )
            if not platform_accounts:
                platform_accounts = [None]
            account_rows = [
                self._run_one(platform, label, account, sample, store, run_id, sample_reason)
                for account in platform_accounts
            ]
            rows.append(self._merge_platform_rows(platform, label, account_rows))
        return {
            "rows": rows,
            "checked_at": self.clock(),
            "mode": "browser_deep_simulation",
            "diagnostic_run_id": run_id,
            "send_executed": False,
            "sample_source": "manual" if manual_sample else "random_local_data",
        }

    def _accounts(self) -> dict[str, list[dict]]:
        rows = self.conn.execute(
            "SELECT id, name, platform, status, bb_window_id FROM accounts "
            "WHERE bb_window_id IS NOT NULL AND TRIM(bb_window_id) <> '' ORDER BY id"
        ).fetchall()
        grouped = {platform: [] for platform in PLATFORM_INFO}
        for row in rows:
            if row["platform"] in grouped:
                grouped[row["platform"]].append(dict(row))
        return grouped

    def _samples(self) -> dict[str, list[dict]]:
        rows = self.conn.execute(
            "SELECT c.id AS local_comment_id, c.platform, c.user_id, c.nickname, c.content, "
            "c.comment_time, c.extra, v.id AS video_id, v.vid, v.url, v.platform AS video_platform "
            "FROM comments c JOIN videos v ON v.id = c.video_id "
            "WHERE c.content IS NOT NULL AND TRIM(c.content) <> '' "
            "AND v.url IS NOT NULL AND TRIM(v.url) <> '' "
            "ORDER BY c.id DESC"
        ).fetchall()
        grouped = {platform: [] for platform in PLATFORM_INFO}
        for row in rows:
            platform = row["video_platform"] or row["platform"]
            if platform in grouped:
                grouped[platform].append(dict(row))
        return grouped

    @staticmethod
    def _pick_accounts(accounts: list[dict]) -> list[dict]:
        usable = [item for item in accounts if item.get("status") not in {
            "waiting_human", "frozen", "dead", "disabled"
        }]
        # 同一平台下的每个已绑定账号都必须检测；不再随机抽一个账号。
        return usable or accounts

    @staticmethod
    def _merge_platform_rows(platform, label, rows: list[dict]) -> dict:
        priority = {
            HealthStatus.OK: 0, HealthStatus.WARNING: 1,
            HealthStatus.LOGIN_REQUIRED: 2, HealthStatus.HUMAN_REQUIRED: 3,
            HealthStatus.FAILED: 4,
        }
        status = max((row.get("status") for row in rows),
                     key=lambda value: priority.get(value, 4),
                     default=HealthStatus.WARNING)
        account_details = [item for row in rows for item in (row.get("account_details") or [])]
        real_accounts = sum(int(row.get("accounts") or 0) for row in rows)
        messages = [row.get("detail") for row in rows if row.get("detail")]
        return {
            "platform": platform, "platform_label": label,
            "status": status, "status_label": _STATUS_LABEL.get(status, "未知"),
            "adapter": "深度模拟", "reply": "只填不发", "accounts": real_accounts,
            "detail": "；".join(messages), "account_details": account_details,
        }

    @staticmethod
    def _pick_sample(samples: list[dict], platform: str, manual_sample: dict | None):
        if manual_sample and (manual_sample.get("platform") or platform) == platform:
            return dict(manual_sample), ""
        if not samples:
            return None, "暂无测试数据，未执行；请先采集一条评论或使用手动测试"
        if platform == "bilibili":
            # 当前 B 站回复脚本按一级评论定位；楼中回复需要额外的父评论关系。
            # 深度诊断优先选一级评论，避免把楼中回复误判成定位失败。
            roots = [item for item in samples
                     if not _extra_dict(item.get("extra")).get("is_reply")]
            if roots:
                return random.choice(roots), ""
            return None, "B站现有样本均为楼中回复，缺少可定位的一级评论，未执行"
        return random.choice(samples), ""

    def _run_one(self, platform, label, account, sample, store, run_id, sample_reason="") -> dict:
        if account is None:
            detail = {
                "platform": platform, "platform_label": label,
                "status": HealthStatus.WARNING, "status_label": "未执行",
                "adapter": "深度模拟", "reply": "不发送", "accounts": 0,
                "detail": "未绑定浏览器账号，未执行深度诊断",
                "account_details": [{"status": "no_account", "detail": "未绑定浏览器账号"}],
            }
            store.record(platform, "深度浏览器诊断", HealthStatus.WARNING,
                         detail["detail"], metadata={"sample_available": bool(sample),
                                                    "send_executed": False})
            return detail
        if sample is None:
            detail = {
                "platform": platform, "platform_label": label,
                "status": HealthStatus.WARNING, "status_label": "未执行",
                "adapter": "深度模拟", "reply": "不发送", "accounts": 1,
                "detail": sample_reason or "暂无测试数据，未执行；请先采集一条评论或使用手动测试",
                "account_details": [{"account": account["name"], "status": "no_data",
                                     "detail": sample_reason or "暂无测试数据，未执行"}],
            }
            store.record(platform, "深度浏览器诊断", HealthStatus.WARNING,
                         detail["detail"], account_id=account["id"],
                         metadata={"sample_available": False, "send_executed": False})
            return detail

        target = self._target(platform, account, sample)
        test_content = f"[诊断测试] 仅填入不发送 {datetime.now().strftime('%H:%M:%S')}"
        if account.get("status") in {"waiting_human", "frozen", "dead", "disabled"}:
            status = (HealthStatus.HUMAN_REQUIRED if account.get("status") == "waiting_human"
                      else HealthStatus.FAILED)
            message = f"账号当前状态为{account.get('status')}，未执行填充"
            result = None
        else:
            try:
                result = self.reply_adapter.reply(target, test_content, confirm=False)
                status, message = self._map_result(result)
            except Exception as exc:  # noqa: BLE001
                status = HealthStatus.FAILED
                message = f"深度诊断异常：{type(exc).__name__}: {exc}"
                result = None

        account_result = {
            "account": account["name"], "status": status,
            "detail": message, "checks": {
                "sample_available": True,
                "target_page": bool(result and result.stage not in {
                    "target_page_mismatch", "target_page_not_visible"
                }),
                "comment_target": bool(result and result.stage not in {
                    "target_page_mismatch", "target_page_not_visible",
                    "target_content_timeout", "comment_not_found"
                }),
                "reply_filled": bool(result and result.ok),
                "send_executed": False,
            },
            "sample": {"video_url": sample.get("url"), "nickname": sample.get("nickname"),
                       "comment_preview": str(sample.get("content") or "")[:80]},
        }
        store.record(platform, "深度浏览器诊断", status, message,
                     account_id=account["id"],
                     metadata={"sample_available": True, "send_executed": False,
                               "stage": result.stage if result else "exception",
                               "diagnostic_run_id": run_id})
        return {
            "platform": platform, "platform_label": label, "status": status,
            "status_label": _STATUS_LABEL.get(status, "未知"),
            "adapter": "深度模拟", "reply": "只填不发", "accounts": 1,
            "detail": message, "account_details": [account_result],
        }

    @staticmethod
    def _map_result(result):
        stage = str(getattr(result, "stage", "") or "")
        message = str(getattr(result, "message", "") or "深度诊断完成")
        if getattr(result, "ok", False) and stage == "filled_waiting_confirmation":
            return HealthStatus.OK, "已打开作品、定位评论并填入测试内容；未点击发送"
        if "login" in stage or "登录" in message:
            return HealthStatus.LOGIN_REQUIRED, message
        if any(word in stage.lower() for word in ("captcha", "human", "verification")):
            return HealthStatus.HUMAN_REQUIRED, message
        return HealthStatus.FAILED, message

    @staticmethod
    def _target(platform, account, sample) -> ReplyTarget:
        extra = _extra_dict(sample.get("extra"))
        platform_comment_id = (extra.get("platform_comment_id") or extra.get("cid")
                               or extra.get("comment_id"))
        return ReplyTarget(
            lead_id=0, draft_id=None, platform=platform,
            account_id=account.get("id"), account_name=account.get("name"),
            account_status=account.get("status"), bb_window_id=account.get("bb_window_id"),
            video_id=sample.get("video_id"), video_platform_id=sample.get("vid"),
            video_url=sample.get("url"), local_comment_id=sample.get("local_comment_id"),
            platform_comment_id=str(platform_comment_id) if platform_comment_id else None,
            platform_user_id=sample.get("user_id"), nickname=sample.get("nickname"),
            content=sample.get("content"), comment_time=sample.get("comment_time"),
        )
