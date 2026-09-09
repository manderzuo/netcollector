# -*- coding: utf-8 -*-
"""跨平台运行时辅助。

业务代码不应直接判断 Windows 的 ``os.startfile``、Chrome 安装目录或
可写数据位置。这个模块只处理操作系统差异，BitBrowser/CDP 协议保持不变。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path


APP_DATA_NAME = "DouyinXhsCollector"


def user_data_root(project_root: str | os.PathLike[str]) -> Path:
    """返回可写的用户数据根目录。

    Windows/Linux 保持原有的项目内 ``data`` 目录，避免影响既有安装。
    macOS 使用 Application Support，避免把数据写进只读的 ``.app`` 包。
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DATA_NAME
    return Path(project_root) / "data"


def open_path(path: str | os.PathLike[str]) -> None:
    """用当前系统的文件管理器打开文件或目录。"""
    target = str(Path(path).expanduser().resolve())
    if sys.platform == "darwin":
        subprocess.Popen(["open", target], close_fds=True)
    elif os.name == "nt" and hasattr(os, "startfile"):
        os.startfile(target)  # type: ignore[attr-defined]
    elif sys.platform.startswith("linux"):
        subprocess.Popen(["xdg-open", target], close_fds=True)
    else:
        webbrowser.open(target)


def open_url(url: str, *, prefer_chrome: bool = False) -> None:
    """打开网页；macOS 使用 ``open -a``，不依赖 Windows 的 chrome.exe 路径。"""
    target = str(url or "").strip()
    if not target:
        return
    if prefer_chrome and sys.platform == "darwin":
        try:
            subprocess.Popen(["open", "-a", "Google Chrome", target], close_fds=True)
            return
        except OSError:
            pass
    if prefer_chrome and os.name == "nt":
        candidates = [
            shutil.which("chrome"),
            os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ]
        chrome = next((p for p in candidates if p and os.path.exists(p)), None)
        if chrome:
            subprocess.Popen([chrome, target], close_fds=True)
            return
    if not webbrowser.open(target):
        raise OSError(f"无法打开地址: {target}")


__all__ = ["APP_DATA_NAME", "user_data_root", "open_path", "open_url"]
