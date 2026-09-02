# -*- coding: utf-8 -*-
"""启动用于B站测试的普通 Chrome，并登记 CDP 地址。

使用默认 Chrome 用户目录以保留用户已有登录态；不读取 Cookie 或本地存储。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS_FILE = os.path.join(ROOT, "data", "bilibili_chrome_window.txt")
CHROME = os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                      "Google", "Chrome", "Application", "chrome.exe")
PORT = 9222
PROFILE = os.path.join(ROOT, ".chrome_bilibili_test_profile")


def _version():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def main():
    info = _version()
    if not info:
        if not os.path.exists(CHROME):
            raise FileNotFoundError(f"未找到 Chrome: {CHROME}")
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        subprocess.Popen([
            CHROME,
            f"--remote-debugging-port={PORT}",
            "--remote-allow-origins=*",
            f"--user-data-dir={PROFILE}",
            "--profile-directory=Default",
            "https://www.bilibili.com/",
        ], close_fds=True, creationflags=flags,
           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while time.time() < deadline:
            info = _version()
            if info:
                break
            time.sleep(.4)
    if not info or not info.get("webSocketDebuggerUrl"):
        raise RuntimeError("Chrome已启动，但CDP端口未就绪")
    os.makedirs(os.path.dirname(WS_FILE), exist_ok=True)
    with open(WS_FILE, "w", encoding="utf-8") as f:
        f.write("chrome-bilibili\n" + info["webSocketDebuggerUrl"] + "\n")
    print(f"B站 Chrome 已就绪: {info['webSocketDebuggerUrl']}")
    print(f"连接文件: {WS_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"启动失败: {exc}", file=sys.stderr)
        raise
