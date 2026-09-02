# -*- coding: utf-8 -*-
"""后台操作日志存储。

GUI 日志原先只写入左侧文本框，切换页面或重启后无法回看。本模块把已经
脱敏的操作消息追加保存为 JSONL，同时保留最近记录供设置页实时显示和导出。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from typing import Any, Iterable, Mapping


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """把结构化详情压成稳定 JSON，避免单个响应撑爆 GUI 日志。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > 4000:
            return value[:4000] + "...[截断]"
        return value
    if depth >= 8:
        return "...[最大层级]"
    if isinstance(value, Mapping):
        items = list(value.items())
        result = {str(k): _json_safe(v, depth=depth + 1) for k, v in items[:300]}
        if len(items) > 300:
            result["...[截断字段数]"] = len(items) - 300
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [_json_safe(v, depth=depth + 1) for v in items[:300]]
        if len(items) > 300:
            result.append(f"...[截断元素数:{len(items) - 300}]")
        return result
    return str(value)[:4000]


class OperationLog:
    """线程安全的追加式后台操作日志。"""

    def __init__(self, path: str, *, memory_limit: int = 5000):
        self.path = os.path.abspath(path)
        self.memory_limit = max(100, int(memory_limit))
        self._lock = threading.RLock()
        self._records: list[dict] = []

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="milliseconds")

    def append(self, message: str, *, level: str = "info", source: str = "gui",
               event: str = "message", details: Mapping[str, Any] | None = None,
               run_id: str | None = None, action: str | None = None,
               outcome: str | None = None, duration_ms: float | None = None) -> dict:
        """追加一条日志；可同时携带完整的结构化操作详情。"""
        record = {
            "id": uuid.uuid4().hex,
            "timestamp": self._now(),
            "level": str(level or "info"),
            "source": str(source or "gui"),
            "event": str(event or "message"),
            "message": str(message or ""),
        }
        if run_id:
            record["run_id"] = str(run_id)
        if action:
            record["action"] = str(action)
        if outcome:
            record["outcome"] = str(outcome)
        if duration_ms is not None:
            record["duration_ms"] = round(float(duration_ms), 2)
        if details:
            record["details"] = _json_safe(details)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._records.append(record)
            if len(self._records) > self.memory_limit:
                del self._records[:-self.memory_limit]
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
            except OSError:
                pass
        return record

    def append_trace(self, trace_record: Mapping[str, Any]) -> dict:
        """把 DebugTrace 事件汇总到统一操作日志。"""
        component = str(trace_record.get("component") or "trace")
        event = str(trace_record.get("event") or "trace_event")
        fields = trace_record.get("fields")
        return self.append(
            f"[{component}] {event}",
            level="trace",
            source=component,
            event=event,
            details=fields if isinstance(fields, Mapping) else {"value": fields},
            run_id=str(trace_record.get("run_id") or ""),
        )

    def recent(self, limit: int = 1000) -> list[dict]:
        """返回内存中的最近记录；首次启动时从历史 JSONL 载入。"""
        limit = max(1, int(limit))
        with self._lock:
            if not self._records and os.path.exists(self.path):
                try:
                    with open(self.path, encoding="utf-8") as stream:
                        for line in stream.readlines()[-self.memory_limit:]:
                            try:
                                item = json.loads(line)
                            except (TypeError, ValueError):
                                continue
                            if isinstance(item, dict) and "message" in item:
                                self._records.append(item)
                except OSError:
                    pass
            return [dict(item) for item in self._records[-limit:]]

    @staticmethod
    def format_record(record: dict) -> str:
        timestamp = str(record.get("timestamp") or "")
        clock = timestamp[11:23] if len(timestamp) >= 23 else timestamp
        source = str(record.get("source") or "")
        event = str(record.get("event") or "")
        prefix = f"[{clock}]"
        if source or event:
            prefix += f" [{source}/{event}]"
        text = f"{prefix} {record.get('message', '')}"
        details = record.get("details")
        if details:
            text += " | 详情=" + json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        return text

    def save_hourly_snapshot(self, directory: str | None = None,
                             when: datetime | None = None) -> dict:
        """保存当前日志快照；由 GUI 每小时调用，也可在退出时调用。"""
        now = when or datetime.now().astimezone()
        target_dir = os.path.abspath(directory or os.path.join(
            os.path.dirname(self.path), "hourly"
        ))
        stamp = now.strftime("%Y%m%d_%H")
        json_target = os.path.join(target_dir, f"后台操作日志_{stamp}.jsonl")
        text_target = os.path.join(target_dir, f"后台操作日志_{stamp}.txt")
        items = self.recent(self.memory_limit)
        os.makedirs(target_dir, exist_ok=True)
        json_tmp = json_target + ".tmp"
        text_tmp = text_target + ".tmp"
        with self._lock:
            with open(json_tmp, "w", encoding="utf-8") as stream:
                for item in items:
                    stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            with open(text_tmp, "w", encoding="utf-8") as stream:
                stream.write("多平台采集工作台后台操作日志（小时快照）\n")
                stream.write(f"快照时间：{self._now()}\n")
                stream.write("=" * 72 + "\n")
                for item in items:
                    stream.write(self.format_record(item) + "\n")
            os.replace(json_tmp, json_target)
            os.replace(text_tmp, text_target)
        return {"jsonl": json_target, "text": text_target, "count": len(items)}

    def export_text(self, destination: str, records: Iterable[dict] | None = None) -> str:
        """导出为便于人工查看的 UTF-8 文本文件，返回绝对路径。"""
        target = os.path.abspath(destination)
        items = list(records) if records is not None else self.recent(5000)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as stream:
            stream.write("多平台采集工作台后台操作日志\n")
            stream.write(f"导出时间：{self._now()}\n")
            stream.write("=" * 72 + "\n")
            for item in items:
                stream.write(self.format_record(item) + "\n")
        return target
