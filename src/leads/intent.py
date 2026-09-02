# -*- coding: utf-8 -*-
"""意向评分（技术方案 Agent B）。

需求（9.3）：意向规则版本化、命中原因（reasons）、负向条件（negatives）、行业词典
版本。兼容现有 ``src/intent_rules.json`` 旧格式（顶层为 high/medium/low/recruit 分组，
每组含 score/label/keywords），同时支持带版号的新格式。

规则结构（新格式）::

    {
      "version": "intent-rules-v1",
      "negative": ["不需要", "随便问问", "路过"],
      "levels": {
        "high":   {"score": 5, "keywords": [...]},
        "medium": {"score": 3, "keywords": [...]},
        "low":    {"score": 1, "keywords": []},
        "recruit":{"score": 0, "keywords": [...]}
      }
    }

纯函数，不访问数据库，不发送。
"""

from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Optional

from .models import IntentLevel, IntentResult

import os

DEFAULT_RULE_VERSION = "intent-rules-v1"

# 内置默认负向词（规则文件未配置 negative 时启用；9.3 负向条件）
DEFAULT_NEGATIVE_WORDS = [
    "不需要", "随便问问", "路过", "已买", "已购买", "不考虑", "没兴趣",
    "只是看看", "问问而已", "没有需求", "不用了", "谢谢不用",
]


def _load_rules(rules_path: Optional[str]) -> dict:
    """从 JSON 文件加载规则；文件不存在返回空 dict。"""
    if not rules_path:
        return {}
    try:
        with open(rules_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        return {}
    return {}


class IntentScorer:
    """版本化意向评分器。

    用法::

        scorer = IntentScorer(rules)        # dict 或 json 路径
        result = scorer.score("想了解价格，怎么联系")
    """

    def __init__(self, rules: Optional[Dict | str] = None):
        if rules is None:
            # 默认加载项目内现有词典（与 cwd 无关）
            rules = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "intent_rules.json",
            )
        if not isinstance(rules, dict):
            rules = _load_rules(rules)
        self._rules = self._normalize(rules)
        self._version = self._rules.get("version", DEFAULT_RULE_VERSION)
        # 负向词：规则未配置时使用内置默认（方案 9.3：意向需负向条件）
        self._negative = self._rules.get("negative") or DEFAULT_NEGATIVE_WORDS
        self._levels = self._rules.get("levels", {})

    @staticmethod
    def _normalize(rules: dict) -> dict:
        """把旧格式（顶层为水平分组）归一化为新格式。"""
        if "levels" in rules:
            return rules
        levels = {}
        for key in ("high", "medium", "low", "recruit"):
            group = rules.get(key)
            if isinstance(group, dict):
                levels[key] = {
                    "score": group.get("score", 0),
                    "keywords": group.get("keywords") or [],
                }
        return {
            "version": rules.get("version", DEFAULT_RULE_VERSION),
            "negative": rules.get("negative", []),
            "levels": levels,
        }

    # ------------------------------------------------------------------
    def score(self, text: Optional[str]) -> IntentResult:
        """对一条文本计算意向。返回分数、层级、命中原因与负向理由。"""
        if not text:
            return IntentResult(score=0, level=IntentLevel.UNKNOWN,
                                rule_version=self._version)
        t = str(text)

        # 负向词命中优先扣减/降级
        negatives_hit = [w for w in self._negative if w and w in t]

        best_rank = -1
        best_level = IntentLevel.UNKNOWN
        best_score = 0
        reasons: List[str] = []

        for level_key, level_cfg in self._levels.items():
            rank = IntentLevel.RANK.get(level_key, -1)
            kws = level_cfg.get("keywords") or []
            hit = [w for w in kws if w and w in t]
            if hit:
                level_score = level_cfg.get("score", 0)
                if rank > best_rank:
                    best_rank = rank
                    best_level = level_key
                    best_score = level_score
                    reasons = [f"命中「{level_key}」关键词: {', '.join(hit[:5])}"]

        if best_rank < 0 and not negatives_hit:
            # 未命中任何词典 → 视为低意向
            best_level = IntentLevel.LOW
            best_score = 0

        # 负向词把意向压到 low
        if negatives_hit:
            best_level = IntentLevel.LOW
            best_score = min(best_score, 0)
            return IntentResult(
                score=best_score,
                level=best_level,
                reasons=reasons,
                negatives=[f"负向词: {', '.join(negatives_hit)}"],
                rule_version=self._version,
            )

        return IntentResult(
            score=best_score,
            level=best_level,
            reasons=reasons,
            negatives=[],
            rule_version=self._version,
        )

    def rule_version(self) -> str:
        return self._version
