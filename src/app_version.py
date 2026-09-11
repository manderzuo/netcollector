# -*- coding: utf-8 -*-
"""统一读取应用版本号。

版本只维护在项目根目录的 ``VERSION.txt``，启动器、界面和后台登记使用
同一份值，避免发布包出现“文件版本已更新、界面仍显示旧版本”的不一致。
"""

from __future__ import annotations

from pathlib import Path


DEFAULT_APP_VERSION = "2.2.3"


def _read_version() -> str:
    version_file = Path(__file__).resolve().parents[1] / "VERSION.txt"
    try:
        value = version_file.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        value = ""
    return value or DEFAULT_APP_VERSION


APP_VERSION = _read_version()

__all__ = ["APP_VERSION", "DEFAULT_APP_VERSION"]
