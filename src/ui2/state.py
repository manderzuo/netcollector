# -*- coding: utf-8 -*-
"""2.0 界面状态模型（无 Qt 依赖，可单元测试）。"""

from __future__ import annotations

import copy
from typing import Any, Mapping


PAGE_KEYS = (
    "overview", "tasks", "accounts", "leads", "interaction", "publish",
    "analytics", "diagnostics"
)
PAGE_LABELS = {
    "overview": "采集总览",
    "tasks": "任务中心",
    "accounts": "账号管理",
    "leads": "线索中心",
    "interaction": "互动中心",
    "publish": "发布中心",
    "analytics": "数据分析",
    "diagnostics": "诊断与设置",
}
PLATFORM_LABELS = {
    "douyin": "抖音",
    "xhs": "小红书",
    "bilibili": "B站",
    "weibo": "微博",
    "kuaishou": "快手",
}
STATUS_LABELS = {
    "pending": "待采集",
    "phase_a_search": "搜索采集中",
    "phase_b_comments": "评论采集中",
    "running": "采集中",
    "paused": "已暂停",
    "incomplete": "待继续采集",
    "waiting_account": "等待账号",
    "no_account": "无可用账号",
    "done": "已完成",
    "no_more": "暂无更多视频",
    "failed": "失败",
    "aborted": "已中止",
    "stopped": "已停止",
    "completed": "已完成",
    "idle": "空闲",
    "working": "工作中",
    "cooldown": "冷却中",
    "waiting_human": "待人工验证",
    "dead": "不可用",
}
INTENT_LABELS = {
    "high": "高",
    "medium": "中",
    "low": "低",
    "unknown": "未知",
}
LEAD_STATUS_LABELS = {
    "new": "新线索",
    "qualified": "已筛选",
    "assigned": "已分配",
    "draft_ready": "待生成",
    "awaiting_review": "待审核",
    "approved": "已批准",
    "contacted": "已联系",
    "replied": "已回复",
    "converted": "已转化",
    "closed": "已关闭",
    "suppressed": "已屏蔽",
    "archived": "已归档",
}
INTERACTION_STATUS_LABELS = {
    "draft": "待生成",
    "queued": "待发送",
    "sent": "已回复",
    "replied": "已回复",
    "failed": "失败",
}
INTERACTION_TYPE_LABELS = {
    "comment_reply": "评论回复",
    "private_message": "发私信",
}


class Ui2State:
    """把后台报表转换为界面所需的稳定、可序列化视图模型。"""

    def __init__(self):
        self.page = "overview"
        self.connection = "disconnected"
        self.last_error = ""
        self.snapshot: dict[str, Any] = {
            "paused": False,
            "tasks": {},
            "accounts": {},
            "totals": {},
        }
        self.leads: dict[str, Any] = {
            "items": [], "total": 0, "page": 1, "page_size": 50, "pages": 0,
            "tasks": [], "provinces": [], "stats": {},
        }
        self.interactions: dict[str, Any] = {
            "items": [], "total": 0, "page": 1, "page_size": 50, "pages": 0,
            "status": "draft", "accounts": [], "templates": [], "custom_variables": [],
        }
        self.publishing: dict[str, Any] = {
            "items": [], "total": 0, "page": 1, "page_size": 50,
            "pages": 0, "status": "all", "platform": "",
            "status_options": [], "platform_options": [],
            "account_contents": [], "account_content_total": 0,
            "account_content_page": 1, "account_content_pages": 0,
            "account_content_account_id": 0, "account_content_platform": "",
            "account_content_sync_source": "", "account_content_sync_status": "",
            "account_content_sync_error": "", "account_content_profile_url": "",
            "account_content_selected": {}, "account_content_comments": [],
            "generated_contents": [], "generated_total": 0,
            "messages": [], "message_total": 0, "message_unread": 0,
            "message_groups": [],
            "message_type": "", "message_type_options": [],
            "message_sync_status": "", "message_sync_summary": {},
        }
        self.diagnostics: dict[str, Any] = {
            "checked_at": "",
            "bitbrowser": {},
            "llm_api": {},
            "accounts": [],
            "health": [],
            "logs": [],
            "log_stats": {"total": 0, "normal": 0, "warning": 0, "error": 0, "alerts": []},
            "bitbrowser_inspection": {"healthy": False, "checks": [], "windows": []},
            "live_health": {"rows": [], "checked_at": "", "send_executed": False},
            "export_path": "",
        }

    @property
    def page_index(self) -> int:
        return PAGE_KEYS.index(self.page)

    @property
    def page_label(self) -> str:
        return PAGE_LABELS[self.page]

    def select_page(self, page: str) -> str:
        page = str(page or "")
        if page not in PAGE_KEYS:
            raise ValueError(f"未知页面：{page}")
        self.page = page
        return page

    def apply_snapshot(self, snapshot: Mapping[str, Any] | None) -> None:
        if not isinstance(snapshot, Mapping):
            raise TypeError("后台状态必须是对象")
        self.snapshot = copy.deepcopy(dict(snapshot))
        self.connection = "connected"
        self.last_error = ""

    def mark_error(self, error: Any) -> None:
        self.connection = "error"
        self.last_error = str(error or "后台服务连接异常")

    def mark_command_error(self, error: Any) -> None:
        """记录单次业务命令失败，但不要把仍然在线的服务标成断线。"""
        self.last_error = str(error or "后台命令执行失败")

    def append_log_event(self, payload: Mapping[str, Any] | None) -> None:
        """实时追加后台日志，不触发任务/列表状态的整页刷新。"""
        if not isinstance(payload, Mapping):
            return
        item = {
            "timestamp": str(payload.get("timestamp") or ""),
            "level": str(payload.get("level") or "info"),
            "source": str(payload.get("source") or "scheduler"),
            "event": str(payload.get("event") or "scheduler_log"),
            "message": str(payload.get("message") or ""),
            "action": str(payload.get("action") or ""),
            "outcome": str(payload.get("outcome") or ""),
            "duration_ms": payload.get("duration_ms"),
            "details": copy.deepcopy(payload.get("details") or {}),
        }
        logs = list(self.diagnostics.get("logs") or [])
        logs.append(item)
        self.diagnostics["logs"] = logs[-5000:]

    def apply_leads(self, result: Mapping[str, Any] | None) -> None:
        """保存线索查询结果；线索刷新不覆盖任务/账号状态快照。"""
        if not isinstance(result, Mapping):
            raise TypeError("线索查询结果必须是对象")
        current = dict(result)
        current["items"] = list(current.get("items") or [])
        current["tasks"] = list(current.get("tasks") or [])
        current["provinces"] = list(current.get("provinces") or [])
        current["stats"] = dict(current.get("stats") or {})
        current["total"] = int(current.get("total") or 0)
        current["page"] = max(1, int(current.get("page") or 1))
        current["page_size"] = max(1, int(current.get("page_size") or 50))
        current["pages"] = max(0, int(current.get("pages") or 0))
        self.leads = copy.deepcopy(current)
        self.connection = "connected"
        self.last_error = ""

    def lead_rows(self) -> list[dict[str, Any]]:
        """把后端线索记录转换成中文界面字段。"""
        rows = []
        for raw in self.leads.get("items") or []:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            platform = str(row.get("platform") or "unknown")
            intent = str(row.get("intent_level") or "unknown")
            status = str(row.get("status") or "new")
            row["id"] = int(row.get("id") or 0)
            row["platform_label"] = PLATFORM_LABELS.get(platform, platform or "未知平台")
            row["nickname"] = str(row.get("nickname") or "匿名用户")
            row["comment"] = str(row.get("comment") or row.get("summary_text") or "暂无评论原文")
            row["comment_time"] = str(row.get("comment_time") or "暂无时间")
            row["source_url"] = str(row.get("source_url") or "")
            row["source_url_label"] = "点击查看" if row["source_url"] else "暂无地址"
            row["region_label"] = str(row.get("region_province") or "未识别")
            row["intent_label"] = INTENT_LABELS.get(intent, "未知")
            row["status_label"] = LEAD_STATUS_LABELS.get(status, status)
            rows.append(row)
        return rows

    def lead_task_options(self) -> list[dict[str, Any]]:
        options = [{"id": 0, "label": "全部任务"}]
        for raw in self.leads.get("tasks") or []:
            if not isinstance(raw, Mapping):
                continue
            task_id = int(raw.get("id") or 0)
            if task_id <= 0:
                continue
            platform = PLATFORM_LABELS.get(
                str(raw.get("platform") or ""), str(raw.get("platform") or "未知平台")
            )
            keyword = str(raw.get("keyword") or "未命名任务")
            options.append({
                "id": task_id,
                "keyword": keyword,
                "platform": raw.get("platform") or "",
                "label": f"任务#{task_id} · {platform} · {keyword}",
            })
        return options

    def lead_province_options(self) -> list[str]:
        return ["全部地区"] + [str(item) for item in self.leads.get("provinces") or []]

    def apply_interactions(self, result: Mapping[str, Any] | None) -> None:
        """保存互动列表结果；回复内容始终保留完整正文。"""
        if not isinstance(result, Mapping):
            raise TypeError("互动查询结果必须是对象")
        current = dict(result)
        current["items"] = list(current.get("items") or [])
        current["accounts"] = list(current.get("accounts") or [])
        current["templates"] = list(current.get("templates") or [])
        current["custom_variables"] = list(current.get("custom_variables") or [])
        current["total"] = int(current.get("total") or 0)
        current["page"] = max(1, int(current.get("page") or 1))
        current["page_size"] = max(1, int(current.get("page_size") or 50))
        current["pages"] = max(0, int(current.get("pages") or 0))
        current["status"] = str(current.get("status") or "draft")
        current["interaction_type"] = str(
            current.get("interaction_type") or "comment_reply"
        )
        self.interactions = copy.deepcopy(current)
        self.connection = "connected"
        self.last_error = ""

    def apply_publishing(self, result: Mapping[str, Any] | None) -> None:
        """保存发布中心列表结果，不影响采集/互动列表状态。"""
        if not isinstance(result, Mapping):
            raise TypeError("发布中心查询结果必须是对象")
        current = dict(result)
        current["items"] = list(current.get("items") or [])
        current["status_options"] = list(current.get("status_options") or [])
        current["platform_options"] = list(current.get("platform_options") or [])
        current["total"] = int(current.get("total") or 0)
        current["page"] = max(1, int(current.get("page") or 1))
        current["page_size"] = max(1, int(current.get("page_size") or 50))
        current["pages"] = max(0, int(current.get("pages") or 0))
        current["status"] = str(current.get("status") or "all")
        current["platform"] = str(current.get("platform") or "")
        self.publishing = copy.deepcopy(current)
        self.connection = "connected"
        self.last_error = ""

    def apply_account_contents(self, result: Mapping[str, Any] | None) -> None:
        if not isinstance(result, Mapping):
            raise TypeError("账号内容结果必须是对象")
        current = dict(result)
        current["items"] = list(current.get("items") or [])
        current["total"] = int(current.get("total") or 0)
        current["page"] = max(1, int(current.get("page") or 1))
        current["pages"] = max(0, int(current.get("pages") or 0))
        # 初次加载与手动同步可能并发返回。busy 响应只是“已有请求在执行”的
        # 状态回报，不能用空/旧列表覆盖刚刚显示的实时主页内容。
        same_scope = (
            int(self.publishing.get("account_content_account_id") or 0)
            == int(current.get("account_id") or 0)
            and str(self.publishing.get("account_content_platform") or "")
            == str(current.get("platform") or "")
        )
        if str(current.get("sync_status") or "") == "busy" and same_scope:
            self.publishing["account_content_sync_source"] = str(current.get("sync_source") or "")
            self.publishing["account_content_sync_status"] = "busy"
            self.publishing["account_content_sync_error"] = str(current.get("sync_error") or "")
            self.connection = "connected"
            self.last_error = ""
            return
        self.publishing["account_contents"] = copy.deepcopy(current["items"])
        self.publishing["account_content_total"] = current["total"]
        self.publishing["account_content_page"] = current["page"]
        self.publishing["account_content_pages"] = current["pages"]
        self.publishing["account_content_account_id"] = int(current.get("account_id") or 0)
        self.publishing["account_content_platform"] = str(current.get("platform") or "")
        self.publishing["account_content_sync_source"] = str(current.get("sync_source") or "")
        self.publishing["account_content_sync_status"] = str(current.get("sync_status") or "")
        self.publishing["account_content_sync_error"] = str(current.get("sync_error") or "")
        self.publishing["account_content_profile_url"] = str(current.get("profile_url") or "")
        self.connection = "connected"
        self.last_error = ""

    def apply_content_comments(self, result: Mapping[str, Any] | None) -> None:
        if not isinstance(result, Mapping):
            raise TypeError("作品评论结果必须是对象")
        self.publishing["account_content_comments"] = copy.deepcopy(list(result.get("items") or []))

    def apply_generated_contents(self, result: Mapping[str, Any] | None) -> None:
        if not isinstance(result, Mapping):
            raise TypeError("生成内容结果必须是对象")
        self.publishing["generated_contents"] = copy.deepcopy(list(result.get("items") or []))
        self.publishing["generated_total"] = int(result.get("total") or 0)
        self.connection = "connected"
        self.last_error = ""

    def apply_published_messages(self, result: Mapping[str, Any] | None) -> None:
        if not isinstance(result, Mapping):
            raise TypeError("消息结果必须是对象")
        rows = copy.deepcopy(list(result.get("items") or []))
        self.publishing["messages"] = rows
        self.publishing["message_groups"] = self._group_published_messages(rows)
        self.publishing["message_total"] = int(result.get("total") or 0)
        self.publishing["message_unread"] = int(result.get("unread") or 0)
        self.publishing["message_type"] = str(result.get("message_type") or "")
        self.publishing["message_type_options"] = copy.deepcopy(list(result.get("message_type_options") or []))
        self.connection = "connected"
        self.last_error = ""

    @staticmethod
    def _group_published_messages(rows: list[Any]) -> list[dict[str, Any]]:
        """按账号、平台和用户身份聚合消息，形成可直接展示的会话列表。

        同名用户在不同平台或不同账号下不能混成一个会话；如果平台没有返回
        user_id，则退回使用昵称作为身份键，这是当前数据可用范围内最稳定的
        兼容方案。
        """
        groups: dict[str, dict[str, Any]] = {}
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            row = copy.deepcopy(dict(raw))
            platform = str(row.get("platform") or "unknown").strip().lower() or "unknown"
            try:
                account_id = int(row.get("account_id") or 0)
            except (TypeError, ValueError):
                account_id = 0
            user_id = str(row.get("user_id") or "").strip()
            nickname = str(row.get("nickname") or "匿名用户").strip() or "匿名用户"
            identity = f"id:{user_id}" if user_id else f"name:{nickname}"
            key = f"{account_id}|{platform}|{identity}"

            group = groups.get(key)
            if group is None:
                group = {
                    "key": key,
                    "account_id": account_id,
                    "account_name": str(row.get("account_name") or "未命名账号"),
                    "platform": platform,
                    "platform_label": str(row.get("platform_label") or platform),
                    "nickname": nickname,
                    "user_id": user_id,
                    "display_id": user_id or "未获取ID",
                    "message_count": 0,
                    "unread_count": 0,
                    "latest_time": "",
                    "latest_preview": "",
                    "latest_message_type_label": "其他",
                    "can_reply": False,
                    "rows": [],
                }
                groups[key] = group
            group["rows"].append(row)

        result: list[dict[str, Any]] = []
        for group in groups.values():
            group_rows = group["rows"]
            group["message_count"] = len(group_rows)
            group["unread_count"] = sum(1 for row in group_rows if not bool(row.get("is_read")))
            group["can_reply"] = any(bool(row.get("can_reply")) for row in group_rows)
            if group_rows:
                latest = group_rows[0]
                group["latest_time"] = str(latest.get("event_time") or latest.get("created_at") or "")
                group["latest_message_type_label"] = str(latest.get("message_type_label") or "其他")
                group["latest_preview"] = str(
                    latest.get("content")
                    or latest.get("action")
                    or latest.get("quote_content")
                    or "暂无文字内容"
                )
            result.append(group)
        return result

    def apply_message_sync(self, result: Mapping[str, Any] | None) -> None:
        if not isinstance(result, Mapping):
            raise TypeError("消息同步结果必须是对象")
        self.publishing["message_sync_status"] = "success" if result.get("ok") else "failed"
        self.publishing["message_sync_summary"] = copy.deepcopy(dict(result))

    def publishing_rows(self) -> list[dict[str, Any]]:
        rows = []
        for raw in self.publishing.get("items") or []:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            row["id"] = int(row.get("id") or 0)
            row["title"] = str(row.get("title") or "未命名内容")
            row["source_content"] = str(row.get("source_content") or "")
            row["status"] = str(row.get("status") or "draft")
            row["status_label"] = str(row.get("status_label") or row["status"])
            row["platform_labels"] = list(row.get("platform_labels") or [])
            row["variants"] = list(row.get("variants") or [])
            row["assets"] = list(row.get("assets") or [])
            row["asset_count"] = int(row.get("asset_count") or 0)
            rows.append(row)
        return rows

    def interaction_rows(self) -> list[dict[str, Any]]:
        rows = []
        for raw in self.interactions.get("items") or []:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            platform = str(row.get("platform") or row.get("channel") or "unknown")
            status = str(row.get("status") or self.interactions.get("status") or "draft")
            row["draft_id"] = int(row.get("draft_id") or row.get("id") or 0)
            row["platform_label"] = PLATFORM_LABELS.get(platform, platform or "未知平台")
            interaction_type = str(row.get("interaction_type") or "comment_reply")
            row["interaction_type"] = interaction_type
            row["interaction_type_label"] = INTERACTION_TYPE_LABELS.get(
                interaction_type, interaction_type
            )
            row["nickname"] = str(row.get("nickname") or "匿名用户")
            row["original_comment"] = str(row.get("original_comment") or "暂无评论原文")
            row["content"] = str(row.get("content") or "")
            row["status_label"] = INTERACTION_STATUS_LABELS.get(status, status)
            row["reply_account_name"] = str(row.get("reply_account_name") or "未选择账号")
            row["failure_reason"] = str(row.get("failure_reason") or "")
            row["task_id"] = int(row.get("task_id") or 0)
            row["reply_status_label"] = str(row.get("reply_status_label") or row["status_label"])
            row["customer_replied"] = bool(row.get("customer_replied"))
            row["customer_replied_label"] = "是" if row["customer_replied"] else "否"
            rows.append(row)
        return rows

    def interaction_account_options(self) -> list[dict[str, Any]]:
        options = []
        for raw in self.interactions.get("accounts") or []:
            if not isinstance(raw, Mapping):
                continue
            account = dict(raw)
            account["id"] = int(account.get("id") or 0)
            platform = str(account.get("platform") or "unknown")
            account["platform_label"] = PLATFORM_LABELS.get(platform, platform or "未知平台")
            account["label"] = f"{account.get('platform_label')} · {account.get('name') or '未命名账号'}"
            options.append(account)
        return options

    def apply_diagnostics(self, result: Mapping[str, Any] | None) -> None:
        """保存诊断摘要；摘要不包含 API Key、Cookie 或浏览器敏感标识。"""
        if not isinstance(result, Mapping):
            raise TypeError("诊断结果必须是对象")
        current = dict(result)
        current["accounts"] = list(current.get("accounts") or [])
        current["health"] = list(current.get("health") or [])
        current["logs"] = list(current.get("logs") or [])
        current["log_stats"] = dict(current.get("log_stats") or {})
        current["log_stats"].setdefault("total", len(current["logs"]))
        current["log_stats"].setdefault("normal", 0)
        current["log_stats"].setdefault("warning", 0)
        current["log_stats"].setdefault("error", 0)
        current["log_stats"]["alerts"] = list(current["log_stats"].get("alerts") or [])
        current["bitbrowser"] = dict(current.get("bitbrowser") or {})
        current["llm_api"] = dict(current.get("llm_api") or {})
        current["bitbrowser_inspection"] = dict(current.get("bitbrowser_inspection") or {})
        current["live_health"] = dict(current.get("live_health") or {})
        current["export_path"] = str(current.get("export_path") or "")
        self.diagnostics = copy.deepcopy(current)
        self.connection = "connected"
        self.last_error = ""

    def apply_bitbrowser_inspection(self, result: Mapping[str, Any] | None) -> None:
        if not isinstance(result, Mapping):
            raise TypeError("BitBrowser 检测结果必须是对象")
        self.diagnostics["bitbrowser_inspection"] = copy.deepcopy(dict(result))
        self.connection = "connected"
        self.last_error = ""

    def apply_live_health(self, result: Mapping[str, Any] | None) -> None:
        if not isinstance(result, Mapping):
            raise TypeError("真实浏览器诊断结果必须是对象")
        self.diagnostics["live_health"] = copy.deepcopy(dict(result))
        self.connection = "connected"
        self.last_error = ""

    def apply_log_export(self, result: Mapping[str, Any] | None) -> None:
        if not isinstance(result, Mapping):
            raise TypeError("日志导出结果必须是对象")
        self.diagnostics["export_path"] = str(result.get("path") or "")
        self.connection = "connected"
        self.last_error = ""

    def diagnostic_health_rows(self) -> list[dict[str, Any]]:
        rows = []
        for raw in self.diagnostics.get("health") or []:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            platform = str(row.get("platform") or "unknown")
            status = str(row.get("status") or "warning")
            row["platform_label"] = PLATFORM_LABELS.get(platform, platform or "未知平台")
            row["status_label"] = {
                "ok": "正常", "warning": "需配置", "human_required": "需人工处理",
                "login_required": "需登录", "failed": "异常",
            }.get(status, status)
            rows.append(row)
        return rows

    def diagnostic_account_rows(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.diagnostics.get("accounts") or []
                if isinstance(item, Mapping)]

    def diagnostic_live_health_rows(self) -> list[dict[str, Any]]:
        """把真实浏览器诊断结果转换成稳定的中文展示字段。"""
        rows = []
        status_labels = {
            "ok": "正常", "warning": "需检查", "human_required": "需人工",
            "login_required": "需登录", "failed": "异常",
        }
        for raw in (self.diagnostics.get("live_health") or {}).get("rows") or []:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            platform = str(row.get("platform") or "unknown")
            status = str(row.get("status") or "warning")
            row["platform_label"] = PLATFORM_LABELS.get(platform, platform or "未知平台")
            row["status_label"] = status_labels.get(status, status)
            row["detail"] = str(row.get("detail") or "暂无诊断说明")
            details = []
            for raw_detail in row.get("account_details") or []:
                if not isinstance(raw_detail, Mapping):
                    continue
                detail = dict(raw_detail)
                detail["account"] = str(detail.get("account") or "未命名账号")
                detail["status_label"] = status_labels.get(
                    str(detail.get("status") or "warning"),
                    str(detail.get("status") or "未知"),
                )
                details.append(detail)
            row["account_details"] = details
            rows.append(row)
        return rows

    def task_rows(self) -> list[dict[str, Any]]:
        tasks = self.snapshot.get("tasks") or {}
        rows = []
        for key, raw in tasks.items():
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            row["id"] = int(row.get("id") or key)
            row["platform_label"] = PLATFORM_LABELS.get(
                row.get("platform"), row.get("platform") or "未知平台"
            )
            row["keyword"] = str(row.get("keyword") or "未命名任务")
            row["status"] = str(row.get("status") or "pending")
            row["status_label"] = STATUS_LABELS.get(row["status"], row["status"])
            row["comments"] = int(row.get("comments") or 0)
            row["videos_total"] = int(row.get("videos_total") or 0)
            row["video_done"] = int(row.get("video_done") or 0)
            target = int(row.get("effective_target_count") or row.get("target_count") or 0)
            row["progress"] = (
                min(100, round(row["video_done"] * 100 / target))
                if target > 0 else 0
            )
            rows.append(row)
        return sorted(rows, key=lambda item: item["id"], reverse=True)

    def account_rows(self) -> list[dict[str, Any]]:
        """把账号报表转换为稳定的中文界面字段。

        账号键使用 ``platform:id``，但界面不依赖这个内部键；即使旧库中
        某个账号字段缺失，也保留该账号并显示可解释的默认值。
        """
        accounts = self.snapshot.get("accounts") or {}
        rows = []
        for key, raw in accounts.items():
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            row["identity"] = str(key)
            row["id"] = int(row.get("id") or 0)
            row["name"] = str(row.get("name") or key or "未命名账号")
            platform = str(row.get("platform") or "")
            row["platform_label"] = PLATFORM_LABELS.get(platform, platform or "未知平台")
            status = str(row.get("status") or "idle")
            row["status"] = status
            row["status_label"] = STATUS_LABELS.get(status, status)
            window_id = str(row.get("bb_window_id") or "").strip()
            row["window_id"] = window_id
            row["binding_label"] = "已绑定窗口" if window_id else "未绑定窗口"
            row["cooldown_left_seconds"] = max(
                0, int(float(row.get("cooldown_left_seconds") or 0))
            )
            row["processed_count"] = int(row.get("processed_count") or 0)
            row["batch_count"] = int(row.get("batch_count") or 0)
            rows.append(row)
        return sorted(
            rows,
            key=lambda item: (item["platform_label"], item["name"], item["id"]),
        )

    def overview(self) -> dict[str, Any]:
        totals = self.snapshot.get("totals") or {}
        rows = self.task_rows()
        return {
            "tasks": int(totals.get("tasks") or len(rows)),
            "running_tasks": int(totals.get("tasks_running") or 0),
            "videos": int(totals.get("videos_total") or 0),
            "completed_videos": int(totals.get("videos_done") or 0),
            "comments": int(totals.get("comments") or 0),
            "accounts": int(totals.get("accounts") or 0),
            "waiting_human": list(totals.get("waiting_human") or []),
        }

    def to_view_model(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "page_index": self.page_index,
            "page_label": self.page_label,
            "connection": self.connection,
            "last_error": self.last_error,
            "overview": self.overview(),
            "tasks": self.task_rows(),
            "accounts": self.account_rows(),
            "leads": self.lead_rows(),
            "lead_meta": {
                "total": int(self.leads.get("total") or 0),
                "page": int(self.leads.get("page") or 1),
                "page_size": int(self.leads.get("page_size") or 50),
                "pages": int(self.leads.get("pages") or 0),
                "tasks": self.lead_task_options(),
                "provinces": self.lead_province_options(),
                "stats": dict(self.leads.get("stats") or {}),
            },
            "interactions": self.interaction_rows(),
            "interaction_meta": {
                "total": int(self.interactions.get("total") or 0),
                "page": int(self.interactions.get("page") or 1),
                "page_size": int(self.interactions.get("page_size") or 50),
                "pages": int(self.interactions.get("pages") or 0),
                "status": self.interactions.get("status") or "draft",
                "interaction_type": self.interactions.get("interaction_type") or "comment_reply",
                "accounts": self.interaction_account_options(),
                "templates": list(self.interactions.get("templates") or []),
                "custom_variables": list(self.interactions.get("custom_variables") or []),
            },
            "publishing": {
                "items": self.publishing_rows(),
                "total": int(self.publishing.get("total") or 0),
                "page": int(self.publishing.get("page") or 1),
                "page_size": int(self.publishing.get("page_size") or 50),
                "pages": int(self.publishing.get("pages") or 0),
                "status": self.publishing.get("status") or "all",
                "platform": self.publishing.get("platform") or "",
                "status_options": list(self.publishing.get("status_options") or []),
                "platform_options": list(self.publishing.get("platform_options") or []),
                "account_contents": list(self.publishing.get("account_contents") or []),
                "account_content_total": int(self.publishing.get("account_content_total") or 0),
                "account_content_page": int(self.publishing.get("account_content_page") or 1),
                "account_content_pages": int(self.publishing.get("account_content_pages") or 0),
                "account_content_account_id": int(self.publishing.get("account_content_account_id") or 0),
                "account_content_platform": self.publishing.get("account_content_platform") or "",
                "account_content_sync_source": self.publishing.get("account_content_sync_source") or "",
                "account_content_sync_status": self.publishing.get("account_content_sync_status") or "",
                "account_content_sync_error": self.publishing.get("account_content_sync_error") or "",
                "account_content_profile_url": self.publishing.get("account_content_profile_url") or "",
                "account_content_selected": dict(self.publishing.get("account_content_selected") or {}),
                "account_content_comments": list(self.publishing.get("account_content_comments") or []),
                "generated_contents": list(self.publishing.get("generated_contents") or []),
                "generated_total": int(self.publishing.get("generated_total") or 0),
                "messages": list(self.publishing.get("messages") or []),
                "message_groups": copy.deepcopy(self.publishing.get("message_groups") or []),
                "message_total": int(self.publishing.get("message_total") or 0),
                "message_unread": int(self.publishing.get("message_unread") or 0),
                "message_type": self.publishing.get("message_type") or "",
                "message_type_options": list(self.publishing.get("message_type_options") or []),
                "message_sync_status": self.publishing.get("message_sync_status") or "",
                "message_sync_summary": dict(self.publishing.get("message_sync_summary") or {}),
            },
            "diagnostics": {
                "checked_at": self.diagnostics.get("checked_at") or "",
                "bitbrowser": dict(self.diagnostics.get("bitbrowser") or {}),
                "llm_api": dict(self.diagnostics.get("llm_api") or {}),
                "accounts": self.diagnostic_account_rows(),
                "health": self.diagnostic_health_rows(),
                "logs": list(self.diagnostics.get("logs") or []),
                "log_stats": dict(self.diagnostics.get("log_stats") or {}),
                "bitbrowser_inspection": dict(self.diagnostics.get("bitbrowser_inspection") or {}),
                "live_health": {
                    **dict(self.diagnostics.get("live_health") or {}),
                    "rows": self.diagnostic_live_health_rows(),
                },
                "export_path": self.diagnostics.get("export_path") or "",
            },
        }


__all__ = [
    "PAGE_KEYS", "PAGE_LABELS", "PLATFORM_LABELS", "STATUS_LABELS",
    "INTENT_LABELS", "LEAD_STATUS_LABELS", "INTERACTION_STATUS_LABELS", "Ui2State"
]
