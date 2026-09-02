# -*- coding: utf-8 -*-
"""errors.py — 统一异常体系。

采集链路中区分三类错误：
- HumanBlock：需要人工接管（验证码/登录/风控），调度器转为 waiting_human
- RateLimited：请求过频，调度器转为 cooldown 后重试
- NetworkError：网络层失败，调度器按重试策略处理
"""


class CollectError(Exception):
    """采集错误基类。"""

    category = "generic"

    def __init__(self, reason: str = "", message: str = None):
        self.reason = str(reason or "unknown")
        super().__init__(str(message or self.reason))


class HumanBlock(CollectError):
    """需要人工接管的页面状态（验证码/登录/风控冻结）。"""

    category = "human_required"


class RateLimited(CollectError):
    """请求过频，应冷却后重试。"""

    category = "rate_limited"


class NetworkError(CollectError):
    """网络/连接层失败。"""

    category = "network"


class TaskCancelled(Exception):
    """任务被取消（用户停止）。"""
