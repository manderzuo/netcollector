# -*- coding: utf-8 -*-
"""本地配置加载（技术方案第 14 章 / P3-3）。

从项目数据目录加载 ``app_config.json``（不存在时使用内置默认值）。
只做加载与合并，不写死绝对路径；路径解析：用户选择路径 → 本地配置相对路径 →
项目/安装目录默认数据目录（方案 14）。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

# 内置默认配置（方案 14 示例值）
DEFAULT_CONFIG: Dict[str, Any] = {
    "bitbrowser": {
        "base_url": "http://127.0.0.1:54345",
        "timeout": 20,
    },
    "llm_api": {
        "enabled": False,
        "provider": "",
        "base_url": "",
        "api_key": "",
        "model": "",
        "timeout": 30,
    },
    "tieba_api": {
        # 贴吧 skill 使用官方 API 令牌，不依赖 BitBrowser 窗口。
        "enabled": False,
        "token": "",
        "timeout": 30,
    },
    "lead_rules": {
        "henan_confidence_threshold": 65,
        "hot_days": 3,
        "active_days": 14,
        "cooling_days": 30,
        "minimum_intent_for_draft": "medium",
    },
    "interaction": {
        "enabled": False,
        "real_sender_enabled": False,
        # 浏览器填充/发送仍需显式配置；默认关闭，避免升级后触发外部操作。
        "browser_reply_enabled": False,
        "require_human_review": True,
        # 批量评论回复和私信共用节流规则：默认每条间隔 5 秒，
        # 连续处理 10 条后再额外冷却 1 分钟。
        "batch_reply_cooldown_seconds": 5.0,
        "batch_reply_long_cooldown_seconds": 60.0,
        "batch_reply_long_cooldown_every": 10,
    },
    "sync": {
        "enabled": False,
        "server_url": "",
        "api_token": "",
        "device_name": "",
        "timeout": 20,
    },
    "auth": {
        # 统一账号服务只承载注册、登录和审批，不接收浏览器 Cookie 或业务数据。
        "server_url": "https://www.gemstory.cn",
        "timeout": 8,
    },
}


def default_config_dir() -> str:
    """项目默认数据目录（可移动：随项目走，不含盘符/用户名）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "data", "config")


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：override 覆盖 base，保留未覆盖的默认值。"""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class AppConfig:
    """应用配置：懒加载 + 内存合并 + 可选持久化。"""

    def __init__(self, config_dir: Optional[str] = None):
        self._dir = config_dir or default_config_dir()
        self._data: Dict[str, Any] = {}
        self.load()

    # ------------------------------------------------------------------
    def load(self) -> Dict[str, Any]:
        path = self._path()
        data: Dict[str, Any] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                data = {}
        self._data = _deep_merge(DEFAULT_CONFIG, data)
        return self._data

    def save(self) -> None:
        os.makedirs(self._dir, exist_ok=True)
        with open(self._path(), "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def _path(self) -> str:
        return os.path.join(self._dir, "app_config.json")

    # ------------------------------------------------------------------
    def get(self, section: str, key: Optional[str] = None, default=None):
        sec = self._data.get(section, {})
        if key is None:
            return sec
        if isinstance(sec, dict):
            return sec.get(key, default)
        return default

    def lead_rules(self) -> dict:
        return self._data.get("lead_rules", {})

    def interaction(self) -> dict:
        return self._data.get("interaction", {})

    def bitbrowser(self) -> dict:
        return self._data.get("bitbrowser", {})

    def llm_api(self) -> dict:
        return self._data.get("llm_api", {})

    def tieba_api(self) -> dict:
        """百度贴吧 skill 配置；令牌只用于 tieba.baidu.com。"""
        return self._data.get("tieba_api", {})

    def sync(self) -> dict:
        """员工数据同步配置；默认关闭，只有手动点击同步才会联网。"""
        return self._data.get("sync", {})

    def auth(self) -> dict:
        """统一账号服务配置；认证页面使用，业务数据仍保存在本机。"""
        return self._data.get("auth", {})

    def update_section(self, section: str, values: dict) -> None:
        """更新一个配置分区并落盘；不会把配置内容写入日志。"""
        if not isinstance(values, dict):
            raise TypeError("配置分区必须是对象")
        current = self._data.get(section)
        if not isinstance(current, dict):
            current = {}
        current.update(values)
        self._data[section] = current
        self.save()


def templates_path(config_dir: Optional[str] = None) -> str:
    """模板持久化文件路径（P3-5）。"""
    d = config_dir or default_config_dir()
    return os.path.join(d, "templates.json")
