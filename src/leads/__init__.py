# -*- coding: utf-8 -*-
"""线索运营领域包（对应技术方案 Agent B 与 Agent C）。

- models.py  ：冻结契约（枚举/状态常量 + 领域数据类）
- region.py  ：地域规范化与可信度
- freshness.py：时效分桶
- intent.py  ：意向评分
- dedupe.py  ：稳定去重键
- service.py ：LeadService 应用服务（Agent C）
- repository.py：LeadRepository 数据访问（Agent A）
"""
