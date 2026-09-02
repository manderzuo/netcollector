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
        # 登录切换时，旧账号的异步查询可能仍在后台运行。每次认证身份变化
        # 都递增会话编号，旧会话的结果即使晚到也不能写回当前界面。
        self._session_generation = 0
        self._auth_scope_user_id: int | None = None
        self._auth_scope_role = ""
        self._awaiting_auth_snapshot = False
        self._connect_inflight = False
        self._refresh_inflight = False
        self._refresh_pending = False
        self._refresh_pending_callback: Callable[[dict[str, Any]], None] | None = None
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
                with self._async_lock:
                    if not self._snapshot_scope_matches(event.get("payload")):
                        return
                    self.state.apply_snapshot(event.get("payload"))
                    self._awaiting_auth_snapshot = False
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
            self._apply_snapshot_for_session(self.client.request("status"))
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

    def set_auth_scope(self, user: Mapping[str, Any] | None) -> int:
        """切换界面认证身份并清空上一用户的内存数据。

        后台权限校验负责阻止越权查询；这里负责处理前端页面常驻和异步
        请求带来的另一类泄露：登录前一个账号的查询返回后，不能再覆盖
        新账号的列表。
        """
        user = user if isinstance(user, Mapping) else {}
        try:
            user_id = int(user.get("id") or 0)
        except (TypeError, ValueError):
            user_id = 0
        user_id = user_id if user_id > 0 else None
        role = str(user.get("role") or "")
        with self._async_lock:
            self._session_generation += 1
            generation = self._session_generation
            self._auth_scope_user_id = user_id
            self._auth_scope_role = role
            self._awaiting_auth_snapshot = True
            self._last_lead_args = {"page": 1, "page_size": 50}
            self._last_interaction_args = {
                "status": "draft", "interaction_type": "comment_reply",
                "page": 1, "page_size": 50,
            }
            self._lead_refresh_pending = None
            self._interaction_refresh_pending = None
            if self._refresh_inflight:
                self._refresh_pending = True
                self._refresh_pending_callback = None
            self.state.reset_user_scoped()
            self.state.connection = "disconnected"
        self._notify()
        return generation

    def session_generation(self) -> int:
        """返回当前认证会话编号，供上层测试和受控流程使用。"""
        with self._async_lock:
            return self._session_generation

    def _snapshot_scope_matches(self, snapshot: Any, *, allow_legacy: bool = False) -> bool:
        """判断后台状态快照是否属于当前认证会话。

        新版后台会在状态快照中带 ``_auth_scope``。对旧版后台保留兼容：
        正常会话期间允许无元数据快照，但刚切换身份、尚未拿到新状态时
        暂不接受无元数据的旧事件。
        """
        if not isinstance(snapshot, Mapping):
            return False
        scope = snapshot.get("_auth_scope")
        if not isinstance(scope, Mapping):
            return allow_legacy or not self._awaiting_auth_snapshot
        try:
            actual_id = int(scope.get("user_id") or 0)
        except (TypeError, ValueError):
            return False
        expected_id = int(self._auth_scope_user_id or 0)
        return actual_id == expected_id

    def _apply_snapshot_for_session(
        self, snapshot: Mapping[str, Any] | None, generation: int | None = None,
    ) -> bool:
        """在会话锁内应用状态快照，返回是否成功应用。"""
        with self._async_lock:
            if generation is not None and generation != self._session_generation:
                return False
            # 这是当前会话主动发出的 status 请求，generation 已经把旧请求
            # 与新请求分开；即使连接到旧版后台没有返回范围元数据，也可以
            # 安全应用这次主动读取的结果。异步广播则必须走严格匹配。
            if not self._snapshot_scope_matches(snapshot, allow_legacy=True):
                return False
            self.state.apply_snapshot(snapshot)
            self._awaiting_auth_snapshot = False
            return True

    def select_page(self, page: str) -> dict[str, Any]:
        self.state.select_page(page)
        self._notify()
        return self.state.to_view_model()

    def refresh(self) -> dict[str, Any]:
        if self.client is None:
            return self.state.to_view_model()
        with self._async_lock:
            generation = self._session_generation
        try:
            self._apply_snapshot_for_session(self.client.request("status"), generation)
        except Exception as exc:
            with self._async_lock:
                if generation == self._session_generation:
                    self.state.mark_error(exc)
        self._notify()
        return self.state.to_view_model()

    def refresh_async(self, callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        """异步读取最新状态；多个页面可安全触发，不阻塞 Qt 事件循环。"""
        with self._async_lock:
            if self._refresh_inflight:
                # 普通重复刷新继续合并掉；登录切换时由 set_auth_scope
                # 显式标记 _refresh_pending，保证新账号至少再读取一次状态。
                return
            self._refresh_inflight = True
            generation = self._session_generation

        def worker() -> None:
            current = False
            try:
                if self.client is None:
                    return
                result = self.client.request("status")
                with self._async_lock:
                    current = generation == self._session_generation
                if not current or not self._apply_snapshot_for_session(result, generation):
                    return
                self._notify()
                view = self.state.to_view_model()
                if callable(callback):
                    try:
                        callback(view)
                    except Exception:
                        pass
            except Exception as exc:
                with self._async_lock:
                    current = generation == self._session_generation
                    if current:
                        self.state.mark_error(exc)
                if current:
                    self._notify()
            finally:
                pending_callback = None
                restart = False
                with self._async_lock:
                    if self._refresh_pending:
                        pending_callback = self._refresh_pending_callback
                        self._refresh_pending = False
                        self._refresh_pending_callback = None
                        self._refresh_inflight = False
                        restart = True
                    else:
                        self._refresh_inflight = False
                if restart:
                    self.refresh_async(pending_callback)

        threading.Thread(target=worker, name="ui2-backend-refresh", daemon=True).start()

    def refresh_leads_async(
        self,
        args: Mapping[str, Any] | None = None,
        callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """异步刷新线索列表；快速切换筛选时只保留最后一次查询。"""
        with self._async_lock:
            request_args = dict(args or self._last_lead_args)
            request_args.setdefault("page", 1)
            request_args.setdefault("page_size", 50)
            self._last_lead_args = dict(request_args)
            generation = self._session_generation
            if self._lead_refresh_inflight:
                self._lead_refresh_pending = request_args
                return
            self._lead_refresh_inflight = True

        def worker(current_args: dict[str, Any]) -> None:
            current = False
            try:
                result = self.command("list_leads", current_args)
                with self._async_lock:
                    current = generation == self._session_generation
                    if current:
                        self.state.apply_leads(result)
                if not current:
                    return
                self._notify()
                if callable(callback):
                    try:
                        callback(self.state.to_view_model())
                    except Exception:
                        pass
            except Exception as exc:
                with self._async_lock:
                    current = generation == self._session_generation
                    if current:
                        self.state.mark_error(exc)
                if current:
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
        with self._async_lock:
            request_args = dict(args or self._last_interaction_args)
            request_args.setdefault("page", 1)
            request_args.setdefault("page_size", 50)
            self._last_interaction_args = dict(request_args)
            generation = self._session_generation
            if self._interaction_refresh_inflight:
                self._interaction_refresh_pending = request_args
                return
            self._interaction_refresh_inflight = True

        def worker(current_args: dict[str, Any]) -> None:
            current = False
            try:
                result = self.command("list_interactions", current_args)
                with self._async_lock:
                    current = generation == self._session_generation
                    if current:
                        self.state.apply_interactions(result)
                if not current:
                    return
                self._notify()
                if callable(callback):
                    try:
                        callback(self.state.to_view_model())
                    except Exception:
                        pass
            except Exception as exc:
                with self._async_lock:
                    current = generation == self._session_generation
                    if current:
                        self.state.mark_error(exc)
                if current:
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
        with self._async_lock:
            generation = self._session_generation

        def worker() -> None:
            try:
                result = self.command(name, args, timeout=timeout)
            except Exception as exc:
                with self._async_lock:
                    if generation != self._session_generation:
                        return
                if callable(on_error):
                    try:
                        on_error(exc)
                    except Exception:
                        pass
                return
            with self._async_lock:
                if generation != self._session_generation:
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
