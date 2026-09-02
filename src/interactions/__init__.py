# -*- coding: utf-8 -*-
"""互动中心领域包（技术方案 Agent A 落库基础 + Agent D 策略）。

- models.py   ：互动专属领域数据类（草稿、资格快照等）
- policy.py   ：回复资格策略（Agent D）
- templates.py：可配置模板与变量白名单（Agent D）
- service.py  ：InteractionService（Agent D）
- sender.py   ：DisableSender 占位（第一、二阶段仅接口和禁用实现）
- repository.py：InteractionRepository 数据访问（Agent A）
"""
