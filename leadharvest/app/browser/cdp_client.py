# -*- coding: utf-8 -*-
"""cdp_client.py — 浏览器控制层。

封装 Chrome DevTools Protocol 连接与会话管理，向平台适配器提供统一接口：
- connect_window(window_id) -> (CdpSession, session_id)
- 窗口注册表（window_id -> ws_url），支持文件缓存与 BitBrowser 打开

依赖注入设计：平台适配器不直接 new CdpSession，
而是通过本客户端获取已连接的会话，便于测试与多窗口管理。
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Optional, Tuple

from ..browser.cdp import CdpSession


class WindowSession:
    """单个窗口的 CDP 会话包装。"""

    def __init__(self, session: CdpSession, session_id: str):
        self.session = session
        self.session_id = session_id


class CdpClient:
    """CDP 窗口管理器。

    维护 window_id -> (CdpSession, session_id) 映射，
    避免重复连接同一窗口。
    """

    def __init__(self, window_store=None):
        self._sessions: dict = {}
        self._lock = threading.Lock()
        self._window_store = window_store or WindowStore()

    async def connect_window(self, window_id: str) -> Tuple[CdpSession, str]:
        """连接指定窗口，返回 (session, session_id)。

        注意：asyncio.run() 每次创建新事件循环，跨循环缓存的连接会失效。
        因此本方法不做跨循环缓存；如需复用请在同一事件循环内调用。
        """
        # 检查当前循环内是否已有可用连接
        loop = asyncio.get_running_loop()
        cached = self._sessions.get(window_id)
        if cached is not None and getattr(cached.session, "_loop_id", None) == id(loop):
            if cached.session.ws is not None:
                return cached.session, cached.session_id

        # 旧连接属于其它循环，先关闭避免泄漏
        if cached is not None:
            try:
                await cached.session.close()
            except Exception:
                pass
            with self._lock:
                self._sessions.pop(window_id, None)

        ws_url = self._window_store.resolve(window_id)
        if not ws_url:
            raise RuntimeError(f"窗口 {window_id} 无可用 CDP 地址")

        session = CdpSession(ws_url)
        session._loop_id = id(loop)
        await session.connect()
        sid = await session.attach_page()
        with self._lock:
            self._sessions[window_id] = WindowSession(session, sid)
        return session, sid

    async def close(self, window_id: str = None):
        """关闭窗口会话。window_id 为空则全部关闭。"""
        with self._lock:
            targets = [window_id] if window_id else list(self._sessions.keys())
        for wid in targets:
            ws_entry = self._sessions.pop(wid, None)
            if ws_entry:
                try:
                    await ws_entry.session.close()
                except Exception:
                    pass

    def has_window(self, window_id: str) -> bool:
        return window_id in self._sessions


class WindowStore:
    """窗口 CDP 地址存储。

    支持两种来源：
    1. 窗口文件缓存（data/windows/{platform}/{window_id}.json）
    2. BitBrowser API 打开窗口获取（通过注入的 opener）
    """

    def __init__(self, data_dir: str = None, opener=None):
        self._data_dir = data_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data", "windows",
        )
        self._opener = opener

    def resolve(self, window_id: str) -> str:
        """解析窗口的 CDP ws 地址。"""
        # 1. 文件缓存
        path = os.path.join(self._data_dir, f"{window_id}.json")
        if os.path.exists(path):
            import json
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("ws"):
                    return data["ws"]
            except Exception:
                pass
        # 2. BitBrowser 打开窗口
        if self._opener is not None:
            ws = self._opener(window_id)
            if ws:
                self.cache(window_id, ws)
                return ws
        return ""

    def cache(self, window_id: str, ws_url: str):
        """写入窗口缓存文件。"""
        import json
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            path = os.path.join(self._data_dir, f"{window_id}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"ws": ws_url}, f, ensure_ascii=False)
        except Exception:
            pass
