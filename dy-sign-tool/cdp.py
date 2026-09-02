#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cdp.py — 基于 websockets 的极简 Chrome DevTools Protocol 客户端（Python 版）。

用途：驱动 BitBrowser 窗口（浏览器级 WebSocket），完成导航 / JS 求值 / DOM 提取 /
截图 / 等待。多账号窗口 = 多连接，互不干扰；本模块只依赖 websockets 标准用法。

连接方式：BitBrowser 的 /browser/open 返回 browser 级 ws（ws://host:port/devtools/browser/<uuid>），
直接连它，然后 Target.attachToTarget(flatten=True) 进 page 会话，命令带 sessionId 发送。

示例：
    c = CdpSession("ws://127.0.0.1:61623/devtools/browser/xxx")
    await c.connect()
    page = await c.attach_page()          # 复用已有 page，或建新 tab
    await c.cmd("Page.navigate", {"url": "https://..."}, page)
    await c.wait_load(page)
    title = await c.eval("document.title", page)
    await c.close()
"""

import asyncio
import json

import websockets


class CdpError(Exception):
    pass


class CdpSession:
    def __init__(self, browser_ws_url: str, timeout: float = 30.0):
        self.url = browser_ws_url
        self.timeout = timeout
        self.ws = None
        self._reader = None
        self._id = 0
        self._pending = {}
        self._listeners = {}

    async def connect(self):
        self.ws = await websockets.connect(self.url, max_size=64 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read_loop())
        return self

    async def _read_loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                mid = msg.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if fut.done():
                        continue
                    if "error" in msg:
                        fut.set_exception(CdpError(msg["error"].get("message", "cdp error")))
                    else:
                        fut.set_result(msg.get("result", {}))
                else:
                    # 事件消息：路由给监听器
                    method = msg.get("method", "")
                    if method in self._listeners:
                        for cb in list(self._listeners[method]):
                            try:
                                cb(msg.get("params", {}))
                            except Exception:
                                pass
        except Exception:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CdpError("connection closed"))
            self._pending.clear()

    def on(self, method: str, cb):
        """订阅事件：cb(params) 在每个该事件触发时被调用。返回取消订阅函数。"""
        self._listeners.setdefault(method, []).append(cb)
        return lambda: self._listeners.get(method, []).remove(cb) if cb in self._listeners.get(method, []) else None

    async def cmd(self, method: str, params: dict = None, session_id: str = None,
                  timeout: float = None) -> dict:
        if self.ws is None:
            raise CdpError("not connected")
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        fut = asyncio.get_running_loop().create_future()
        command_id = self._id
        self._pending[command_id] = fut
        try:
            await self.ws.send(json.dumps(msg))
            return await asyncio.wait_for(fut, timeout or self.timeout)
        finally:
            self._pending.pop(command_id, None)

    async def attach_page(self, create_if_missing: bool = True) -> str:
        """返回 page 的 sessionId（flatten 会话）。"""
        info = await self.cmd("Target.getTargets")
        page = next((t for t in info.get("targetInfos", []) if t["type"] == "page"), None)
        if page is None and create_if_missing:
            cr = await self.cmd("Target.createTarget", {"url": "about:blank"})
            page = {"targetId": cr["targetId"]}
        if page is None:
            raise CdpError("no page target")
        att = await self.cmd("Target.attachToTarget",
                             {"targetId": page["targetId"], "flatten": True})
        sid = att["sessionId"]
        await self.cmd("Page.enable", session_id=sid)
        await self.cmd("Runtime.enable", session_id=sid)
        return sid

    async def eval(self, expression: str, session_id: str, return_by_value: bool = True):
        r = await self.cmd("Runtime.evaluate",
                           {"expression": expression, "returnByValue": return_by_value,
                            "awaitPromise": True}, session_id=session_id)
        if "exceptionDetails" in r:
            raise CdpError("eval exception: " + json.dumps(r["exceptionDetails"], ensure_ascii=False)[:500])
        res = r.get("result", {})
        if return_by_value:
            return res.get("value")
        return res

    async def navigate(self, url: str, session_id: str, wait_load: bool = True,
                       timeout: float = 25.0):
        await self.cmd("Page.navigate", {"url": url}, session_id=session_id)
        if wait_load:
            await self.wait_load(session_id, timeout)

    async def wait_load(self, session_id: str, timeout: float = 25.0):
        """等 Page.loadEventFired（挂监听；超时不算错，SPA 常无 load 事件）。"""
        try:
            await asyncio.wait_for(self._wait_event("Page.loadEventFired", session_id), timeout)
        except asyncio.TimeoutError:
            pass

    async def _wait_event(self, method: str, session_id: str):
        ev = asyncio.get_running_loop().create_future()
        orig = self._pending  # 监听用独立 way：直接在 msg 循环里勾
        # 简化：用一个一次性回调注册（通过 cmd 轮询不可行，改用注入式）
        # 这里用组合：循环等待事件 0.2s 轮询页面 readyState
        while True:
            state = await self.eval("document.readyState", session_id)
            if state == "complete":
                return
            await asyncio.sleep(0.2)

    async def get_body(self, request_id: str, session_id: str, timeout: float = 15.0) -> str:
        """按 requestId 取 Network 响应体（需 Network.enable 已开启）。"""
        r = await self.cmd("Network.getResponseBody", {"requestId": request_id},
                           session_id=session_id, timeout=timeout)
        return r.get("body", "")

    async def screenshot(self, session_id: str, out_path: str):
        r = await self.cmd("Page.captureScreenshot", {"format": "png"}, session_id=session_id)
        import base64
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(r["data"]))

    async def close(self):
        if self._reader:
            self._reader.cancel()
        if self.ws:
            await self.ws.close()
            self.ws = None
        if self._reader:
            try:
                await self._reader
            except asyncio.CancelledError:
                pass


async def quick_eval(ws_url: str, expression: str, nav_url: str = None, wait: float = 1.0):
    """命令行快速求值：连 ws → 附着 page → （可选导航）→ 返回 eval 结果。"""
    c = CdpSession(ws_url)
    await c.connect()
    sid = await c.attach_page()
    if nav_url:
        await c.navigate(nav_url, sid)
        await asyncio.sleep(wait)
    val = await c.eval(expression, sid)
    await c.close()
    return val


if __name__ == "__main__":
    import sys
    ws_url = sys.argv[1]
    expr = sys.argv[2]
    nav = sys.argv[3] if len(sys.argv) > 3 else None
    w = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
    print(asyncio.run(quick_eval(ws_url, expr, nav, w)))
