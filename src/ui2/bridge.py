# -*- coding: utf-8 -*-
"""2.0 UI 与本地后台服务之间的无 Qt 连接层。"""

from __future__ import annotations

import threading
from typing import Any, Callable, Mapping

try:
    from ..backend_client import BackendClient  # type: ignore
    from ..backend_protocol import BackendEndpoint  # type: ignore
except ImportError:  # pragma: no cover - 支持直接执行 qml_app.py
    from backend_client import BackendClient  # type: ignore
    from backend_protocol import BackendEndpoint  # type: ignore

from .state import Ui2State


class Ui2Bridge:
    """供 Tk/QML 两种界面复用的后台操作外观。"""

    def __init__(self, client: BackendClient | None = None):
        self.state = Ui2State()
        self.client = client
        self._lock = threading.RLock()
        self._async_lock = threading.Lock()
        self._connect_inflight = False
        self._refresh_inflight = False
        self._lead_refresh_inflight = False
        self._lead_refresh_pending: dict[str, Any] | None = None
        self._interaction_refresh_inflight = False
        self._interaction_refresh_pending: dict[str, Any] | None = None
        self._last_interaction_args: dict[str, Any] = {
            "status": "draft", "interaction_type": "comment_reply",
            "page": 1, "page_size": 50,
        }
        self._last_lead_args: dict[str, Any] = {"page": 1, "page_size": 50}
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._log_listeners: list[Callable[[dict[str, Any]], None]] = []
        if self.client is not None:
            self.client.event_callback = self._on_backend_event

    @classmethod
    def from_endpoint(cls, endpoint: BackendEndpoint) -> "Ui2Bridge":
        return cls(BackendClient(endpoint, event_callback=None))

    def add_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        if callable(callback):
            with self._lock:
                self._listeners.append(callback)

    def _notify(self) -> None:
        view = self.state.to_view_model()
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(view)
            except Exception:
                pass

    def add_log_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        if callable(callback):
            with self._lock:
                self._log_listeners.append(callback)

    def _notify_logs(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            listeners = list(self._log_listeners)
        for callback in listeners:
            try:
                callback(dict(payload))
            except Exception:
                pass

    def _on_backend_event(self, event: Mapping[str, Any]) -> None:
        kind = str(event.get("event") or "")
        if kind == "state_snapshot":
            try:
                self.state.apply_snapshot(event.get("payload"))
            except Exception as exc:
                self.state.mark_error(exc)
        elif kind == "backend_error":
            payload = event.get("payload") or {}
            self.state.mark_error(payload.get("message") if isinstance(payload, Mapping) else payload)
        elif kind == "log":
            payload = event.get("payload") or {}
            if isinstance(payload, Mapping):
                self.state.append_log_event(payload)
                self._notify_logs(payload)
            return
        self._notify()

    def connect(self) -> dict[str, Any]:
        if self.client is None:
            self.state.mark_error("未配置后台服务地址")
            self._notify()
            return self.state.to_view_model()
        try:
            self.client.connect()
            self.state.apply_snapshot(self.client.request("status"))
        except Exception as exc:
            self.state.mark_error(exc)
        self._notify()
        return self.state.to_view_model()

    def connect_async(self, callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        """异步连接后台，避免 QML 主线程等待网络握手或首次状态读取。"""
        with self._async_lock:
            if self._connect_inflight:
                return
            self._connect_inflight = True

        def worker() -> None:
            try:
                view = self.connect()
                if callable(callback):
                    try:
                        callback(view)
                    except Exception:
                        pass
            finally:
                with self._async_lock:
                    self._connect_inflight = False

        threading.Thread(target=worker, name="ui2-backend-connect", daemon=True).start()

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    def select_page(self, page: str) -> dict[str, Any]:
        self.state.select_page(page)
        self._notify()
        return self.state.to_view_model()

    def refresh(self) -> dict[str, Any]:
        if self.client is None:
            return self.state.to_view_model()
        try:
            self.state.apply_snapshot(self.client.request("status"))
        except Exception as exc:
            self.state.mark_error(exc)
        self._notify()
        return self.state.to_view_model()

    def refresh_async(self, callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        """异步读取最新状态；多个页面可安全触发，不阻塞 Qt 事件循环。"""
        with self._async_lock:
            if self._refresh_inflight:
                return
            self._refresh_inflight = True

        def worker() -> None:
            try:
                view = self.refresh()
                if callable(callback):
                    try:
                        callback(view)
                    except Exception:
                        pass
            finally:
                with self._async_lock:
                    self._refresh_inflight = False

        threading.Thread(target=worker, name="ui2-backend-refresh", daemon=True).start()

    def refresh_leads_async(
        self,
        args: Mapping[str, Any] | None = None,
        callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """异步刷新线索列表；快速切换筛选时只保留最后一次查询。"""
        request_args = dict(args or self._last_lead_args)
        request_args.setdefault("page", 1)
        request_args.setdefault("page_size", 50)
        self._last_lead_args = dict(request_args)
        with self._async_lock:
            if self._lead_refresh_inflight:
                self._lead_refresh_pending = request_args
                return
            self._lead_refresh_inflight = True

        def worker(current_args: dict[str, Any]) -> None:
            try:
                result = self.command("list_leads", current_args)
                self.state.apply_leads(result)
                self._notify()
                if callable(callback):
                    try:
                        callback(self.state.to_view_model())
                    except Exception:
                        pass
            except Exception as exc:
                self.state.mark_error(exc)
                self._notify()
            finally:
                with self._async_lock:
                    pending = self._lead_refresh_pending
                    self._lead_refresh_pending = None
                    if pending is None:
                        self._lead_refresh_inflight = False
                if pending is not None:
                    self.refresh_leads_async(pending, callback)

        threading.Thread(
            target=worker, args=(request_args,), name="ui2-leads-refresh", daemon=True
        ).start()

    def refresh_interactions_async(
        self,
        args: Mapping[str, Any] | None = None,
        callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """异步刷新互动列表；快速切换状态/筛选时合并为最后一次请求。"""
        request_args = dict(args or self._last_interaction_args)
        request_args.setdefault("page", 1)
        request_args.setdefault("page_size", 50)
        self._last_interaction_args = dict(request_args)
        with self._async_lock:
            if self._interaction_refresh_inflight:
                self._interaction_refresh_pending = request_args
                return
            self._interaction_refresh_inflight = True

        def worker(current_args: dict[str, Any]) -> None:
            try:
                result = self.command("list_interactions", current_args)
                self.state.apply_interactions(result)
                self._notify()
                if callable(callback):
                    try:
                        callback(self.state.to_view_model())
                    except Exception:
                        pass
            except Exception as exc:
                self.state.mark_error(exc)
                self._notify()
            finally:
                with self._async_lock:
                    pending = self._interaction_refresh_pending
                    self._interaction_refresh_pending = None
                    if pending is None:
                        self._interaction_refresh_inflight = False
                if pending is not None:
                    self.refresh_interactions_async(pending, callback)

        threading.Thread(
            target=worker,
            args=(request_args,),
            name="ui2-interaction-refresh",
            daemon=True,
        ).start()

    def command(
        self,
        name: str,
        args: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if self.client is None:
            raise RuntimeError("未连接后台服务")
        if timeout is None:
            # 保持普通调用的兼容性，也允许轻量测试客户端只实现原有签名。
            return self.client.request(name, args or {})
        return self.client.request(name, args or {}, timeout=timeout)

    def command_async(
        self,
        name: str,
        args: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """异步发送后台命令，结果通过回调交给调用方。"""
        def worker() -> None:
            try:
                result = self.command(name, args, timeout=timeout)
            except Exception as exc:
                if callable(on_error):
                    try:
                        on_error(exc)
                    except Exception:
                        pass
                return
            if callable(on_success):
                try:
                    on_success(result)
                except Exception:
                    pass

        threading.Thread(
            target=worker, name=f"ui2-command-{name}", daemon=True
        ).start()


__all__ = ["Ui2Bridge"]
