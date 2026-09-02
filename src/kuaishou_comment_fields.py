# -*- coding: utf-8 -*-
"""快手评论 DOM 字段清洗。

快手评论卡片的昵称节点在部分版本中会同时包含“昵称 + 相对时间”，
而内容节点有时会退化为整张卡片的文本。这里仅对 DOM 兜底来源做
保守清洗，接口返回的结构化评论不经过本模块，避免误改真实正文。
"""

from __future__ import annotations

import re


# 快手常见的相对时间和日期展示。使用 search 而不是 fullmatch，兼容
# “昵称 2小时前”这类被错误包进同一个 DOM 节点的文本。
KUAISHOU_TIME_RE = re.compile(
    r"(?:"
    r"\d{4}[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}(?:日)?"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?"
    r"|\d{1,2}[-/.月]\s*\d{1,2}(?:日)?"
    r"|刚刚|昨天|今天|前天"
    r"|\d+\s*(?:秒|分钟|小时|天|周|月|年)前"
    r")"
)


def clean_kuaishou_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def extract_kuaishou_time(value) -> str:
    """从节点混合文本中提取一个时间 token。"""
    match = KUAISHOU_TIME_RE.search(clean_kuaishou_text(value))
    return clean_kuaishou_text(match.group(0)) if match else ""


def _without_time(value: str) -> str:
    return clean_kuaishou_text(KUAISHOU_TIME_RE.sub(" ", clean_kuaishou_text(value)))


def normalize_kuaishou_dom_fields(
    nickname, content, comment_time
) -> tuple[str, str, str]:
    """拆开快手 DOM 兜底结果中的昵称、正文、时间。

    返回值中的正文为空，表示该行只有头像/昵称/时间/操作按钮，没有可
    保存的文字正文。图片或表情的占位文本不会被本函数过滤。
    """
    nick = clean_kuaishou_text(nickname)
    body = clean_kuaishou_text(content)
    explicit_time = clean_kuaishou_text(comment_time)
    time_text = extract_kuaishou_time(explicit_time)
    if not time_text:
        time_text = extract_kuaishou_time(nick) or extract_kuaishou_time(body)

    # 昵称选择器命中了“昵称 + 时间”时，只保留昵称。
    nick_without_time = _without_time(nick)
    if nick_without_time:
        nick = nick_without_time

    # 内容选择器命中了整张卡片的“昵称 + 时间”，或仅命中了时间，
    # 必须丢弃，否则会把 UI 元数据写进评论原文。
    body_without_time = _without_time(body)
    if not body_without_time or body_without_time == nick:
        body = ""
    elif nick and body_without_time.startswith(f"{nick} "):
        # 某些卡片把昵称放在正文节点前面，去掉这一段后保留真实正文。
        body = clean_kuaishou_text(body_without_time[len(nick):])
    else:
        body = body_without_time if time_text else body

    return nick, body, time_text

