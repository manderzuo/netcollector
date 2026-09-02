# -*- coding: utf-8 -*-
"""可复现调试追踪日志。

每次调试操作使用独立 ``run_id``，以 JSONL 追加写入：
``data/logs/<component>.jsonl``。

该组件不依赖全局 logging 配置，后台程序、GUI 线程和临时测试脚本都能
直接使用；写日志失败不会阻断原本的业务操作。
"""

from __future__ import annotations

import json
import os
import threading
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional


_WRITE_LOCK = threading.Lock()
_SINK_LOCK = threading.RLock()
_TRACE_SINKS: list[Callable[[dict], None]] = []


def register_trace_sink(sink: Callable[[dict], None]) -> None:
    """注册结构化追踪汇总器。

    组件仍会各自写入 ``<component>.jsonl``，注册的汇总器只接收一份副本，
    用于 GUI 的统一操作日志。汇总器异常会被隔离，不能影响业务动作。
    """
    if not callable(sink):
        return
    with _SINK_LOCK:
        if sink not in _TRACE_SINKS:
            _TRACE_SINKS.append(sink)


def unregister_trace_sink(sink: Callable[[dict], None]) -> None:
    """注销结构化追踪汇总器。"""
    with _SINK_LOCK:
        try:
            _TRACE_SINKS.remove(sink)
        except ValueError:
            pass


_SENSITIVE_KEY_PARTS = (
    "cookie", "authorization", "password", "passwd", "secret",
    "api_key", "apikey", "token",
)


def _safe(value: Any, *, max_string: int = 4000, _depth: int = 0) -> Any:
    """把运行时对象转换为可写入 JSON 的值，避免日志本身再次报错。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= max_string:
            return value
        return value[:max_string] + "...[truncated]"
    if isinstance(value, Mapping):
        if _depth >= 8:
            return "...[max-depth]"
        result = {}
        for key, item in list(value.items())[:300]:
            key_text = str(key)
            key_lower = key_text.lower().replace("-", "_")
            if any(part in key_lower for part in _SENSITIVE_KEY_PARTS):
                result[key_text] = "***"
            else:
                result[key_text] = _safe(item, max_string=max_string, _depth=_depth + 1)
        if len(value) > 300:
            result["...[truncated_keys]"] = len(value) - 300
        return result
    if isinstance(value, (list, tuple, set)):
        if _depth >= 8:
            return "...[max-depth]"
        items = list(value)
        result = [_safe(v, max_string=max_string, _depth=_depth + 1) for v in items[:300]]
        if len(items) > 300:
            result.append(f"...[truncated_items:{len(items) - 300}]")
        return result
    try:
        return _safe(vars(value), max_string=max_string, _depth=_depth + 1)
    except TypeError:
        return repr(value)[:max_string]


class DebugTrace:
    """追加式结构化调试日志。"""

    def __init__(self, component: str, *, log_path: Optional[str] = None,
                 run_id: Optional[str] = None):
        self.component = component
        self.run_id = run_id or uuid.uuid4().hex
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.path = log_path or os.path.join(
            project_root, "data", "logs", f"{component}.jsonl"
        )
        self._sequence = 0

    def emit(self, event: str, **fields: Any) -> dict:
        """记录一个事件并返回实际写入的记录。"""
        self._sequence += 1
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "sequence": self._sequence,
            "component": self.component,
            "event": event,
            "fields": _safe(fields),
        }
        try:
            with _WRITE_LOCK:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False,
                                            separators=(",", ":")) + "\n")
        except OSError:
            # 调试日志不能反过来阻断定位/填充/发送流程。
            pass
        with _SINK_LOCK:
            sinks = list(_TRACE_SINKS)
        for sink in sinks:
            try:
                sink(dict(record))
            except Exception:
                # 汇总显示失败不影响原始 JSONL 追踪文件和业务流程。
                pass
        return record

    def exception(self, event: str, exc: BaseException, **fields: Any) -> dict:
        fields = dict(fields)
        fields.update({
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "traceback": traceback.format_exc(),
        })
        return self.emit(event, **fields)
