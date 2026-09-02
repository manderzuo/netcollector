# -*- coding: utf-8 -*-
"""评论时间的采集格式归一化。"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Optional, Tuple


# 小红书当前年份通常显示为 ``07-18河南``，其它年份可能显示为
# ``2025-07-18河南`` 或 ``2025年07月18日河南``。
_XHS_DATE_REGION_RE = re.compile(
    r"^\s*"
    r"(?:(?P<year>\d{4})\s*(?:[-/.年]\s*))?"
    r"(?P<month>\d{1,2})\s*(?:[-/.月]\s*)"
    r"(?P<day>\d{1,2})\s*日?\s*"
    r"(?P<region>[\u4e00-\u9fff]{2,8})?\s*$"
)


def normalize_xhs_comment_time(
    value: Optional[str], *, now: Optional[datetime] = None
) -> Tuple[Optional[str], Optional[str]]:
    """把小红书评论时间转为 ``YYYY-MM-DD``，同时提取尾部地区。

    无年份时按当前自然年补齐；如果补齐后会落在未来，则按上一自然年处理。
    这是小红书“本年不显示年份”的实际表现：例如当前是 2026 年 8 月，
    ``12-25河南`` 应解释为 2025-12-25，而不是未来的 2026-12-25。
    无法确认格式时原样返回，并且不提取地区。
    """
    if value is None or not str(value).strip():
        return value, None
    text = str(value).strip()
    match = _XHS_DATE_REGION_RE.match(text)
    if match is None:
        return value, None

    current = now or datetime.now()
    month = int(match.group("month"))
    day = int(match.group("day"))
    explicit_year = match.group("year")
    if explicit_year:
        year = int(explicit_year)
    else:
        year = current.year
        # 无年份的日期不应被解析成未来时间；跨年后小红书会继续省略年份，
        # 因此 12 月日期在次年 1 月/上半年出现时属于上一年。
        if (month, day) > (current.month, current.day):
            year -= 1
    try:
        normalized = datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return value, None
    return normalized, (match.group("region") or None)
