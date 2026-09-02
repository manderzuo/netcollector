# -*- coding: utf-8 -*-
"""幂等数据库迁移执行器（对应技术方案 Agent A）。

只新增表、字段或索引，绝不破坏旧数据。用法见 runner 模块。
导入本包会同时导入 versions 子包，触发全部迁移版本注册。
"""
from . import versions  # noqa: F401  触发注册
from .runner import MigrationRunner

__all__ = ["MigrationRunner"]
