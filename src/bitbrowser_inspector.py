# -*- coding: utf-8 -*-
"""BitBrowser 健康和端口识别，不执行打开/关闭窗口操作。"""

from __future__ import annotations

import time


def _mapping(value) -> dict:
    """兼容 BitBrowser 返回的直接映射和包裹映射。"""
    if not isinstance(value, dict):
        return {}
    current = value
    for _ in range(3):
        nested = None
        for key in ("data", "ports", "pids"):
            candidate = current.get(key)
            if isinstance(candidate, dict):
                nested = candidate
                break
        if nested is None:
            return current
        current = nested
    return current


def _browser_id(row: dict):
    return row.get("id") or row.get("browserId") or row.get("browser_id")


def _browser_rows(value) -> list:
    """兼容 /browser/list 的直接 list 与 data.list 返回形状。"""
    if not isinstance(value, dict):
        return []
    if isinstance(value.get("list"), list):
        return value["list"]
    nested = value.get("data")
    if isinstance(nested, dict) and isinstance(nested.get("list"), list):
        return nested["list"]
    return []


class BitBrowserInspector:
    """读取健康状态、窗口、进程和远程调试端口，供设置页和诊断使用。"""

    def __init__(self, client):
        self.client = client

    def inspect(self) -> dict:
        result = {"healthy": False, "health": None, "error": None,
                  "windows": [], "checks": []}
        def add_check(name, status, detail, started):
            result["checks"].append({
                "name": name,
                "status": status,
                "detail": detail,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            })
        try:
            started = time.perf_counter()
            result["health"] = self.client.health()
            result["healthy"] = True
            add_check("本地服务", "正常", "BitBrowser 健康检查通过", started)
        except Exception as exc:
            result["error"] = str(exc)
            add_check("本地服务", "失败", str(exc), started)
        try:
            started = time.perf_counter()
            raw_windows = self.client.list_browsers(page=0, page_size=100)
            windows = _browser_rows(raw_windows)
            add_check("窗口列表", "正常", f"读取到 {len(windows)} 个窗口", started)
        except Exception as exc:
            result["error"] = result["error"] or str(exc)
            add_check("窗口列表", "失败", str(exc), started)
            windows = []
        try:
            started = time.perf_counter()
            port_map = _mapping(self.client.ports())
            add_check("调试端口", "正常", f"识别到 {len(port_map)} 个端口", started)
        except Exception as exc:
            result["error"] = result["error"] or str(exc)
            add_check("调试端口", "失败", str(exc), started)
            port_map = {}
        try:
            started = time.perf_counter()
            pid_map = _mapping(self.client.pids_all())
            add_check("浏览器进程", "正常", f"识别到 {len(pid_map)} 个进程", started)
        except Exception as exc:
            result["error"] = result["error"] or str(exc)
            add_check("浏览器进程", "失败", str(exc), started)
            pid_map = {}
        try:
            rows = []
            for browser in windows:
                if not isinstance(browser, dict):
                    continue
                browser_id = _browser_id(browser)
                key = str(browser_id) if browser_id is not None else ""
                port = port_map.get(key, port_map.get(browser_id))
                pid = pid_map.get(key, pid_map.get(browser_id))
                # 某些版本会把端口/PID直接放在窗口对象内，作为映射接口的兜底。
                port = port if port not in (None, "") else (
                    browser.get("port") or browser.get("debugPort")
                    or browser.get("remoteDebuggingPort")
                    or browser.get("remote_debugging_port")
                )
                pid = pid if pid not in (None, "") else browser.get("pid")
                rows.append({
                    "id": browser_id or "",
                    "name": browser.get("name") or browser.get("remark") or "未命名窗口",
                    "platform": browser.get("platform") or "",
                    "opened": bool(pid or port),
                    "pid": pid or "",
                    "port": str(port) if port not in (None, "") else "",
                })
            result["windows"] = rows
        except Exception as exc:
            result["error"] = result["error"] or str(exc)
        return result
