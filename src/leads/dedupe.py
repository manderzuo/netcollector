# -*- coding: utf-8 -*-
"""稳定去重键（技术方案 5.2 与 Agent B）。

默认规则（优先级从高到低）：
1. 有平台用户 ID：sha256(platform + ":" + platform_user_id)
2. 无用户 ID：sha256(platform + ":" + normalized_profile_url)
3. 都没有：sha256(platform + ":anonymous:" + source_comment_id)

禁止只用昵称去重（验收红线 9.3）。
"""

from __future__ import annotations

import hashlib
from typing import Optional

from .models import DedupeResult


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_profile_url(url: Optional[str]) -> Optional[str]:
    """对主页 URL 做稳定归一化（去空白、去尾部斜杠、小写协议/主机）。"""
    if not url:
        return None
    u = str(url).strip().rstrip("/")
    if not u:
        return None
    # 主页地址常见但不改变身份的前缀统一化
    u = u.replace("https://", "").replace("http://", "").lower()
    return u


def build_dedupe_key(
    *,
    platform: str,
    platform_user_id: Optional[str] = None,
    profile_url: Optional[str] = None,
    source_comment_id: Optional[int] = None,
) -> DedupeResult:
    """按方案计算稳定去重键。任何依据缺失时逐级降级，绝不用昵称。"""
    platform = (platform or "").strip().lower()
    if platform_user_id is not None and str(platform_user_id).strip():
        uid = str(platform_user_id).strip()
        return DedupeResult(key=_sha(f"{platform}:{uid}"), basis="user_id")

    norm_url = normalize_profile_url(profile_url)
    if norm_url:
        return DedupeResult(key=_sha(f"{platform}:{norm_url}"), basis="profile_url")

    if source_comment_id is not None:
        return DedupeResult(
            key=_sha(f"{platform}:anonymous:{source_comment_id}"), basis="anonymous"
        )

    # 完全无依据：用空串哈希，避免产生 None 崩溃；调用方应视为不可去重。
    return DedupeResult(key=_sha(f"{platform}:anonymous:0"), basis="anonymous")


def dedupe_key_from_comment(
    *,
    platform: str,
    comment_row: dict,
    profile_url: Optional[str] = None,
) -> DedupeResult:
    """便捷版：从评论行构造去重键。

    comment_row 需包含 user_id / id 字段。
    """
    return build_dedupe_key(
        platform=platform,
        platform_user_id=comment_row.get("user_id"),
        profile_url=profile_url or comment_row.get("profile_url"),
        source_comment_id=comment_row.get("id"),
    )
