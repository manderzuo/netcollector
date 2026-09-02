# -*- coding: utf-8 -*-
"""时效分桶（技术方案 2.3 与 Agent B）。

默认按用户互动时间（interaction_at）计算时效。阈值必须可配置（不硬编码进
GUI/采集器）。正确处理未来时间、空时间和无法解析时间。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from comment_time import normalize_xhs_comment_time
from .models import FreshnessBucket, FreshnessResult

# 默认阈值（天）。由调用方 config 覆盖。
DEFAULT_BUCKET_DAYS = {
    FreshnessBucket.HOT: 3,
    FreshnessBucket.ACTIVE: 14,
    FreshnessBucket.COOLING: 30,
}


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """解析 ISO 8601 时间串为 aware datetime；无法解析返回 None。

    naive（无时区）时间视为本地时间并转为 aware，便于与时区化 now 比较。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _parse_interaction_time(value: Optional[str], reference_now: datetime) -> Optional[datetime]:
    """解析采集器返回的互动时间。

    除 ISO 8601 外，兼容小红书 ``07-18河南`` 这种“月-日+地区”展示文本。
    无年份按 reference_now 推断自然年，未来日期归入上一年，地区不会参与日期解析。
    """
    parsed = _parse_iso(value)
    if parsed is not None:
        return parsed
    normalized, _region = normalize_xhs_comment_time(value, now=reference_now)
    if normalized == value:
        return None
    try:
        candidate = datetime.fromisoformat(normalized)
    except (TypeError, ValueError):
        return None
    return candidate.replace(tzinfo=reference_now.tzinfo)


class FreshnessClassifier:
    def __init__(
        self,
        hot_days: int = None,
        active_days: int = None,
        cooling_days: int = None,
        *,
        now: Optional[datetime] = None,
    ):
        self._hot = hot_days if hot_days is not None else DEFAULT_BUCKET_DAYS[FreshnessBucket.HOT]
        self._active = active_days if active_days is not None else DEFAULT_BUCKET_DAYS[FreshnessBucket.ACTIVE]
        self._cooling = cooling_days if cooling_days is not None else DEFAULT_BUCKET_DAYS[FreshnessBucket.COOLING]
        self._now = now or datetime.now(timezone.utc).astimezone()

    def classify(self, interaction_at: Optional[str]) -> FreshnessResult:
        """按互动时间分桶。

        - 无法解析 / 空时间 → UNKNOWN；
        - 未来时间 → UNKNOWN（不得误判为最新高价值，验收红线 9.3）。
        """
        now = self._now
        dt = _parse_interaction_time(interaction_at, now)
        if dt is None:
            return FreshnessResult(bucket=FreshnessBucket.UNKNOWN, reference_time=interaction_at)
        if dt > now + timedelta(minutes=5):
            # 明显未来时间（容忍 5 分钟时钟偏差）
            return FreshnessResult(bucket=FreshnessBucket.UNKNOWN, reference_time=interaction_at)

        days = (now - dt).total_seconds() / 86400.0
        if days < 0:
            days = 0.0
        bucket = FreshnessBucket.EXPIRED
        if days <= self._hot:
            bucket = FreshnessBucket.HOT
        elif days <= self._active:
            bucket = FreshnessBucket.ACTIVE
        elif days <= self._cooling:
            bucket = FreshnessBucket.COOLING
        return FreshnessResult(bucket=bucket, reference_time=interaction_at)

    def is_fresh_enough(self, interaction_at: Optional[str], *, minimum: str = FreshnessBucket.ACTIVE) -> bool:
        """互动时间是否仍新鲜（用于资格判断）。minimum 为最低分桶。"""
        bucket = self.classify(interaction_at).bucket
        if bucket == FreshnessBucket.UNKNOWN:
            return False
        order = {
            FreshnessBucket.HOT: 4,
            FreshnessBucket.ACTIVE: 3,
            FreshnessBucket.COOLING: 2,
            FreshnessBucket.EXPIRED: 1,
            FreshnessBucket.UNKNOWN: 0,
        }
        return order[bucket] >= order.get(minimum, 0)
