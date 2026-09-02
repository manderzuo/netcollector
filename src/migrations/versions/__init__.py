# -*- coding: utf-8 -*-
"""迁移版本集合。

导入本包会触发注册全部迁移版本。版本文件名形如 ``v001_xxx.py``，
以 ``v`` 开头便于作为模块导入；导入模块会执行其中的 ``@register``
装饰器，把版本注册到 ``src.migrations.runner.MIGRATIONS``。
"""
import importlib
import os

_VERSION_PREFIX = "v"

for _fname in sorted(os.listdir(os.path.dirname(__file__))):
    if not _fname.startswith(_VERSION_PREFIX) or not _fname.endswith(".py"):
        continue
    _mod_name = _fname[:-3]
    importlib.import_module(f"{__name__}.{_mod_name}")
