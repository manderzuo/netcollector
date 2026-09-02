# -*- coding: utf-8 -*-
"""2.0 本地后台服务客户端。

客户端只负责连接、请求应答和事件分发，不直接持有 Scheduler、浏览器或
SQLite 连接。未来 Tk/QML 页面可以共用此客户端。
"""

from __future__ import annotations

import socket
import threading
from typing import Any, Callable, Mapping

try:
    from .backend_protocol import (
        BackendEndpoint,
        ProtocolError,
        decode_message,
        encode_message,
        make_command,
        read_endpoint,
    )
except ImportError:  # pragma: no cover
    from backend_protocol import (  # type: ignore
        BackendEndpoint,
        ProtocolError,
        decode_message,
        encode_message,
        make_command,
        read_endpoint,
    )


class BackendError(RuntimeError):
    """后台返回了失败响应，包含稳定错误码。"""

    def __init__(self, code: str, message: str):
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


class BackendClient:
    def __init__(self, endpoint: BackendEndpoint, *, timeout: float = 10.0,
                 event_callback: Callable[[dict[str, Any]], None] | None = None):
        self.endpoint = endpoint
        self.timeout = max(0.1, float(timeout))
        self.event_callback = event_callback
        self._socket: socket.socket | None = None
        self._stream = None
        self._write_lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, dict[str, Any] | None]] = {}
        self._pending_lock = threading.RLock()
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._connect_error: Exception | None = None

    @classmethod
    def from_endpoint_file(cls, path: str, **kwargs) -> "BackendClient":
        return cls(read_endpoint(path), **kwargs)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def connect(self) -> None:
        if self.connected:
            return
        # 上一次连接可能刚完成关闭但 reader 线程尚未退出；先完成收尾，
        # 避免重连后旧线程误把旧 socket 的状态写入新连接。
        if self._reader is not None and self._reader.is_alive():
            self.close()
        self._stop.clear()
        self._connect_error = None
        sock = socket.create_connection(
            (self.endpoint.host, self.endpoint.port), timeout=self.timeout
        )
        sock.settimeout(None)
        self._socket = sock
        self._stream = sock.makefile("rb")
        self._reader = threading.Thread(
            target=self._read_loop, name="backend-client-reader", daemon=True
        )
        self._reader.start()
        if not self._connected.wait(self.timeout):
            self.close()
            raise TimeoutError("等待后台服务握手超时")
        if self._connect_error is not None:
            error = self._connect_error
            self.close()
            raise ConnectionError(str(error)) from error

    def close(self) -> None:
        self._stop.set()
        self._connected.clear()
        sock = self._socket
        stream = self._stream
        self._socket = None
        self._stream = None
        reader = self._reader
        self._reader = None
        # 先关闭底层 socket，唤醒正在阻塞 readline() 的 reader 线程；
        # Windows 下如果先关闭 makefile，另一个线程仍在读同一对象时可能
        # 卡在 stream.close()，导致 GUI 重连/退出无法收尾。
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for waiter, _ in pending:
            waiter.set()
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1)

    def _read_loop(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            for raw in stream:
                if self._stop.is_set():
                    break
                message = decode_message(raw)
                kind = message.get("kind")
                if kind == "hello":
                    self._connected.set()
                elif kind == "response":
                    request_id = str(message.get("request_id") or "")
                    with self._pending_lock:
                        pending = self._pending.get(request_id)
                        if pending is not None:
                            waiter, _ = pending
                            self._pending[request_id] = (waiter, message)
                            waiter.set()
                elif kind == "event":
                    callback = self.event_callback
                    if callable(callback):
                        try:
                            callback(message)
                        except Exception:
                            pass
        except Exception as exc:
            if not self._stop.is_set():
                self._connect_error = exc
        finally:
            self._connected.clear()
            with self._pending_lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for waiter, _ in pending:
                waiter.set()

    def request(self, command: str, args: Mapping[str, Any] | None = None,
                *, timeout: float | None = None) -> Any:
        if not self.connected:
            self.connect()
        message = make_command(command, args, token=self.endpoint.token)
        request_id = message["request_id"]
        waiter = threading.Event()
        with self._pending_lock:
            self._pending[request_id] = (waiter, None)
        try:
            raw = encode_message(message)
            with self._write_lock:
                if self._socket is None:
                    raise ConnectionError("后台服务连接已断开")
                self._socket.sendall(raw)
            wait_seconds = self.timeout if timeout is None else max(0.1, float(timeout))
            if not waiter.wait(wait_seconds):
                raise TimeoutError(f"后台命令超时：{command}")
            with self._pending_lock:
                response = self._pending.pop(request_id, (waiter, None))[1]
            if not response:
                raise ConnectionError("后台服务连接已断开")
            if not response.get("ok"):
                error = response.get("error") or {}
                raise BackendError(error.get("code", "backend_error"), error.get("message", "后台命令失败"))
            return response.get("result")
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)


__all__ = ["BackendClient", "BackendError"]
