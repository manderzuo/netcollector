# -*- coding: utf-8 -*-
"""统一的北京时间工具。

桌面端可能运行在不同系统时区，定时任务不能把本地机器时区当成业务时区。
所有面向用户的计划时间都按 ``Asia/Shanghai`` 解释，并在写入任务表前
转换为带 ``+08:00`` 的 ISO 时间。旧数据/内部调用仍可传入不带时区的
``YYYY-MM-DD HH:MM[:SS]``，此时明确按北京时间处理。
"""

from __future__ import annotations

import re
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo


BEIJING_TZ = ZoneInfo("Asia/Shanghai")

# 只接受日期和时间，不接受“只有日期”或任意自然语言。带时区的输入用于
# 兼容旧接口/测试；界面选择器发送的是不带时区的北京时间字符串。
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d{1,6})?)?"
    r"(?:Z|[+-]\d{2}:\d{2})?$"
)


def beijing_now() -> datetime:
    """返回当前北京时间（带时区、精确到秒）。"""

    return datetime.now(BEIJING_TZ).replace(microsecond=0)


def _with_beijing_tz(value: datetime, *, default_tz: tzinfo = BEIJING_TZ) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=default_tz)
    return value.astimezone(BEIJING_TZ).replace(microsecond=0)


def parse_beijing_datetime(value: str | datetime) -> datetime:
    """解析一个严格的计划时间，并返回北京时间 aware datetime。

    不带时区的字符串按北京时间解释，不能按运行机器的系统时区解释。
    """

    if isinstance(value, datetime):
        return _with_beijing_tz(value)
    raw = str(value or "").strip()
    if not raw or not _DATETIME_RE.fullmatch(raw):
        raise ValueError("时间格式无效，请选择日期和时间")
    # fromisoformat 不接受所有版本中的 Z 写法，先转换为等价的 UTC 偏移。
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("时间格式无效，请选择日期和时间") from exc
    return _with_beijing_tz(parsed)


def normalize_scheduled_at(value: str | datetime, *, now: datetime | None = None) -> str:
    """校验并规范化定时发布时间。

    返回带 ``+08:00`` 的 ISO 秒级时间；计划时间必须严格晚于当前北京
    时间。``now`` 只用于确定性测试，生产调用省略即可。
    """

    scheduled = parse_beijing_datetime(value)
    reference = beijing_now() if now is None else _with_beijing_tz(now)
    if scheduled <= reference:
        raise ValueError("定时发布时间必须晚于当前北京时间")
    return scheduled.isoformat(timespec="seconds")


def format_beijing_minute(value: datetime | str) -> str:
    """将时间显示为界面使用的 ``YYYY-MM-DD HH:MM``。"""

    parsed = parse_beijing_datetime(value)
    return parsed.strftime("%Y-%m-%d %H:%M")


__all__ = [
    "BEIJING_TZ",
    "beijing_now",
    "parse_beijing_datetime",
    "normalize_scheduled_at",
    "format_beijing_minute",
]
