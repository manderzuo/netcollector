# -*- coding: utf-8 -*-
"""可配置回复模板与变量白名单（技术方案 Agent D）。

- 模板存于本地配置（JSON），变量以 ``{{name}}`` 形式引用；
- 变量白名单：只允许安全字段，**禁止** Cookie、窗口 ID、内部备注等；
- 渲染时对白名单之外的变量直接拒绝（安全红线 9.5）。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_\u4e00-\u9fff]+)\s*\}\}")

# 模板变量白名单（只读、可脱敏的字段）。中文名称是新模板的标准写法，
# 英文别名保留用于兼容已经保存的旧模板。
VARIABLE_ALIASES = {
    "昵称": "nickname",
    "平台": "platform",
    "省份": "province",
    "城市": "city",
    "产品": "product",
    "联系方式提示": "contact_hint",
}
ALLOWED_VARS = set(VARIABLE_ALIASES) | {
    "nickname",          # 用户昵称
    "platform",          # 平台名
    "province",          # 省份
    "city",              # 城市
    "product",           # 产品名（运营配置）
    "contact_hint",      # 通用联系方式引导语（运营配置）
}

# 明令禁止的变量名（即使模板里出现也拒绝）
FORBIDDEN_VARS = {"cookie", "token", "window_id", "bb_window_id", "password",
                  "account", "note", "internal", "secret"}

DEFAULT_TEMPLATES = {
    "greeting": "您好{{昵称}}！看到您在{{省份}}关注了我们的{{产品}}，"
                "如需了解详情可随时联系我们。{{联系方式提示}}",
    "price_inquiry": "您好{{昵称}}！关于{{产品}}的价格和方案，"
                     "欢迎联系我们获取详细报价。{{联系方式提示}}",
}

# 内置模板的中文名称只用于界面展示，存储键保持稳定，避免影响已有草稿。
TEMPLATE_LABELS = {
    "greeting": "问候话术",
    "price_inquiry": "价格咨询",
}


def template_label(template_id: str) -> str:
    """返回模板在界面中的中文名称。自定义模板默认使用其名称。"""
    return TEMPLATE_LABELS.get(template_id, template_id)


class TemplateEngine:
    """模板渲染：只允许白名单变量，禁用变量直接报错。"""

    def __init__(self, templates: Optional[Dict[str, str]] = None,
                 allowed_vars: Optional[set] = None,
                 custom_variables: Optional[Dict[str, Any]] = None):
        self._templates = (dict(DEFAULT_TEMPLATES) if templates is None
                           else dict(templates))
        self._custom_variables = {
            str(name).strip(): "" if value is None else str(value)
            for name, value in (custom_variables or {}).items()
            if str(name).strip()
        }
        self._allowed = allowed_vars or set(ALLOWED_VARS)
        self._allowed.update(self._custom_variables)

    # ------------------------------------------------------------------
    def list_templates(self) -> Dict[str, str]:
        return dict(self._templates)

    def get(self, template_id: str) -> Optional[str]:
        return self._templates.get(template_id)

    def set_template(self, template_id: str, content: str) -> None:
        """新增或更新模板，并在写入前完成名称和变量校验。"""
        clean_id = str(template_id or "").strip()
        if not clean_id:
            raise ValueError("模板名称不能为空")
        if not re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fff]+", clean_id):
            raise ValueError("模板名称只能包含中文、英文、数字、下划线或短横线")
        clean_content = str(content or "").strip()
        if not clean_content:
            raise ValueError("模板内容不能为空")
        risks = self.validate(clean_id, clean_content)
        if risks:
            raise ValueError("；".join(risks))
        self._templates[clean_id] = clean_content

    def list_custom_variables(self) -> Dict[str, str]:
        """返回运营人员配置的静态变量及其替换内容。"""
        return dict(self._custom_variables)

    @staticmethod
    def validate_custom_variable(name: str, value: Any) -> List[str]:
        """校验自定义变量名称和值；自定义变量不能覆盖系统字段。"""
        clean_name = str(name or "").strip()
        risks: List[str] = []
        if not clean_name:
            risks.append("变量名称不能为空")
        elif not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff]+", clean_name):
            risks.append("变量名称只能包含中文、英文、数字或下划线")
        elif clean_name.lower() in FORBIDDEN_VARS:
            risks.append(f"变量名称“{clean_name}”属于禁用字段")
        elif clean_name in ALLOWED_VARS:
            risks.append(f"变量名称“{clean_name}”与系统变量重复")
        if not str("" if value is None else value).strip():
            risks.append("变量内容不能为空")
        return risks

    def set_custom_variable(self, name: str, value: Any) -> None:
        """新增或更新一个可持久化的静态模板变量。"""
        clean_name = str(name or "").strip()
        risks = self.validate_custom_variable(clean_name, value)
        if risks:
            raise ValueError("；".join(risks))
        self._custom_variables[clean_name] = str(value).strip()
        self._allowed.add(clean_name)

    def replace_custom_variables(self, values: Dict[str, Any]) -> None:
        """用完整变量表替换当前自定义变量，支持界面删除变量。"""
        validated: Dict[str, str] = {}
        for name, value in (values or {}).items():
            clean_name = str(name or "").strip()
            risks = self.validate_custom_variable(clean_name, value)
            if risks:
                raise ValueError("；".join(risks))
            validated[clean_name] = str(value).strip()
        self._custom_variables = validated
        self._allowed = set(ALLOWED_VARS) | set(validated)

    def validate(self, template_id: str, content: str) -> List[str]:
        """校验模板：返回风险清单（空列表=通过）。"""
        risks: List[str] = []
        used = set(_VAR_RE.findall(content))
        for var in used:
            if var.lower() in FORBIDDEN_VARS:
                risks.append(f"模板使用了禁用变量 {{{{{var}}}}}")
            elif var not in self._allowed:
                risks.append(f"模板变量不在白名单: {var}")
        return risks

    def render(self, template_id: str, variables: Dict[str, Any]) -> str:
        """渲染模板。未提供白名单变量时以空串占位（不抛异常，便于预览）。"""
        content = self._templates.get(template_id)
        if content is None:
            raise KeyError(f"模板不存在: {template_id}")
        risks = self.validate(template_id, content)
        if risks:
            raise ValueError("; ".join(risks))
        variables = {**self._custom_variables, **variables}

        def _sub(m):
            name = m.group(1)
            canonical_name = VARIABLE_ALIASES.get(name, name)
            val = variables.get(canonical_name, variables.get(name))
            if val is None:
                return ""
            return str(val)

        return _VAR_RE.sub(_sub, content)

    # ------------------------------------------------------------------
    @staticmethod
    def load(path: Optional[str]) -> "TemplateEngine":
        """从 JSON 文件加载模板；兼容旧版纯模板字典格式。"""
        if not path or not os.path.exists(path):
            return TemplateEngine()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (isinstance(data, dict) and data.get("version") == 2
                    and isinstance(data.get("templates"), dict)):
                return TemplateEngine(
                    data.get("templates"),
                    custom_variables=(data.get("custom_variables") or {}),
                )
            if isinstance(data, dict):
                return TemplateEngine(data)
            return TemplateEngine()
        except (OSError, ValueError):
            return TemplateEngine()

    def save(self, path: str) -> None:
        """持久化模板到 JSON 文件（P3-5）。"""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "version": 2,
                "templates": self._templates,
                "custom_variables": self._custom_variables,
            }, f, ensure_ascii=False, indent=2)
