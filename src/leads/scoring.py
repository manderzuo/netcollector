# -*- coding: utf-8 -*-
"""可解释的线索规则评分，不依赖 AI。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


DEFAULT_SCORING_RULES = {
    "version": "lead-score-v1",
    "thresholds": {"high": 70, "medium": 40},
    "purchase_keywords": {"价格": 25, "多少钱": 25, "购买": 25, "报名": 22, "怎么联系": 28},
    "region_keywords": {"河南": 12, "郑州": 12, "洛阳": 12},
    "question_markers": {"怎么": 10, "如何": 10, "哪里": 8, "吗": 6, "？": 6},
    "contact_markers": {"微信": 18, "电话": 18, "联系方式": 20, "私信": 12},
    "negative_keywords": {"不需要": -35, "招聘": -30, "求职": -30, "路过": -20},
    "repeat_bonus": 8,
}


@dataclass
class ScoreResult:
    score: int
    level: str
    reasons: list[str] = field(default_factory=list)
    rule_version: str = "lead-score-v1"


def _merge_map(default: dict, custom: dict | None) -> dict:
    value = dict(default)
    if isinstance(custom, dict):
        for key, weight in custom.items():
            try:
                value[str(key)] = int(weight)
            except (TypeError, ValueError):
                continue
    return value


class ConfigurableLeadScorer:
    """用词典、地域、重复互动和问题明确度给出 0–100 分。"""

    def __init__(self, rules: dict | None = None):
        custom = rules if isinstance(rules, dict) else {}
        self.rules = {**DEFAULT_SCORING_RULES, **custom}
        for key in ("purchase_keywords", "region_keywords", "question_markers",
                    "contact_markers", "negative_keywords"):
            self.rules[key] = _merge_map(DEFAULT_SCORING_RULES[key], custom.get(key))
        thresholds = dict(DEFAULT_SCORING_RULES["thresholds"])
        thresholds.update(custom.get("thresholds") or {})
        self.rules["thresholds"] = thresholds
        self.version = str(self.rules.get("version") or "lead-score-v1")
        self.thresholds = {
            "high": int(self.rules.get("thresholds", {}).get("high", 70)),
            "medium": int(self.rules.get("thresholds", {}).get("medium", 40)),
        }

    @staticmethod
    def _hits(text: str, mapping: dict) -> Iterable[tuple[str, int]]:
        for word, weight in mapping.items():
            if word and word in text:
                yield str(word), int(weight)

    def score(self, text: str | None, *, region: str | None = None,
              repeat_count: int = 0) -> ScoreResult:
        content = str(text or "")
        reasons: list[str] = []
        total = 0
        groups = (
            ("购买/咨询", "purchase_keywords"),
            ("地区", "region_keywords"),
            ("问题明确度", "question_markers"),
            ("联系方式倾向", "contact_markers"),
            ("负向/无效", "negative_keywords"),
        )
        for label, key in groups:
            mapping = self.rules.get(key, {})
            for word, weight in self._hits(content, mapping):
                total += weight
                sign = "+" if weight >= 0 else ""
                reasons.append(f"{label}命中「{word}」 {sign}{weight}分")
        if region and str(region) in content:
            total += 8
            reasons.append(f"地区字段与评论一致 +8分（{region}）")
        repeat = max(0, int(repeat_count or 0))
        if repeat:
            bonus = min(20, repeat * int(self.rules.get("repeat_bonus", 8)))
            total += bonus
            reasons.append(f"重复互动 {repeat} 次 +{bonus}分")
        score = max(0, min(100, total))
        if score >= self.thresholds["high"]:
            level = "high"
        elif score >= self.thresholds["medium"]:
            level = "medium"
        elif content:
            level = "low"
        else:
            level = "unknown"
        return ScoreResult(score, level, reasons, self.version)
