# -*- coding: utf-8 -*-
"""迁移版本集合。

导入本包会触发注册全部迁移版本。版本文件名形如 ``v001_xxx.py``，
以 ``v`` 开头便于作为模块导入；导入模块会执行其中的 ``@register``
装饰器，把版本注册到 ``src.migrations.runner.MIGRATIONS``。
"""
import importlib

# 不能依赖 ``os.listdir(__file__)``：源码运行时目录存在，但 PyInstaller
# 会把迁移模块放进 PYZ，发布包中没有可供 listdir 的 ``versions`` 目录。
# 显式列出版本模块后，源码和免安装 EXE 都能注册完整迁移，且仍按版本号
# 的固定顺序导入；新增迁移时在这里追加模块名即可。
_VERSION_MODULES = (
    "v001_leads_interactions",
    "v002_reply_targets",
    "v003_operations_foundation",
    "v004_lead_scoring_identity",
    "v004_publishing",
    "v005_task_keyword_searches",
    "v006_publish_workspace",
    "v007_message_center_full",
    "v008_interaction_types",
    "v009_kuaishou_comment_fields",
    "v010_employee_data_scope",
    "v011_admin_control_plane",
)

for _mod_name in _VERSION_MODULES:
    importlib.import_module(f"{__name__}.{_mod_name}")
