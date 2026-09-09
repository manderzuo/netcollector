# -*- coding: utf-8 -*-
"""2.0 本地后台服务的消息协议。

协议故意只依赖 Python 标准库，供旧版 Tk GUI、新版 QML GUI 和后台服务
共同使用。传输采用 UTF-8 JSON Lines：一行一个消息，不把 Python 对象或
SQLite 连接暴露给界面层。

消息方向：
    GUI -> 后台：kind=command
    后台 -> GUI：kind=response / event / hello

所有命令都要经过服务端白名单校验。``args`` 只允许 JSON 基础类型，避免
未来接入新界面时形成可执行任意方法的后门。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 4 * 1024 * 1024

COMMANDS = frozenset({
    "auth_register",
    "auth_login",
    "auth_restore",
    "auth_logout",
    "auth_me",
    "auth_list_users",
    "auth_approve_user",
    "auth_reject_user",
    "auth_disable_user",
    "auth_reset_password",
    "status",
    "get_task",
    "create_task",
    "start",
    "pause",
    "resume",
    "resume_task",
    "stop_task",
    "delete_task",
    "export_task",
    "open_task_folder",
    "create_browser_window",
    "delete_browser_window",
    "add_account",
    "open_account_browser",
    "bind_account",
    "refresh_account_nicknames",
    "remove_account",
    "resolve_human",
    "waiting_accounts",
    "configure_monitoring",
    "start_monitoring",
    "stop_monitoring",
    "run_monitoring_once",
    "list_leads",
    "list_keyword_groups",
    "create_keyword_group",
    "update_keyword_group",
    "add_leads_to_interaction",
    "add_leads_to_private_message",
    "export_leads",
    "export_leads_by_task",
    "tag_leads",
    "list_interactions",
    "export_interactions_by_task",
    "interaction_action",
    "send_interactions",
    "list_publish_drafts",
    "create_publish_draft",
    "update_publish_variant",
    "publish_draft_action",
    "delete_publish_draft",
    "add_publish_assets",
    "delete_publish_asset",
    "preview_publish_draft",
    "real_publish_draft",
    "list_account_contents",
    "sync_account_contents",
    "list_content_comments",
    "list_generated_contents",
    "generate_content",
    "import_generated_content",
    "delete_generated_content",
    "schedule_publish",
    "list_published_messages",
    "sync_published_messages",
    "mark_published_message",
    "open_published_message_browser",
    "test_llm_api",
    "save_template",
    "diagnostics_snapshot",
    "run_platform_health",
    "save_settings",
    "save_tieba_settings",
    "inspect_bitbrowser",
    "run_live_diagnostics",
    "export_operation_log",
    "sync_status",
    "save_sync_settings",
    "sync_now",
    "admin_dashboard",
    "admin_audit_logs",
    "admin_create_backup",
    "admin_list_backups",
    "admin_validate_backup",
    "admin_restore_backup",
    "admin_set_device_status",
})


class ProtocolError(ValueError):
    """消息不是当前版本协议可接受的 JSON 对象。"""


def _new_id() -> str:
    return uuid.uuid4().hex


def _json_line(message: Mapping[str, Any]) -> bytes:
    try:
        raw = json.dumps(
            dict(message), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"消息无法编码为 JSON：{exc}") from exc
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("消息超过协议允许的大小")
    return raw


def encode_message(message: Mapping[str, Any]) -> bytes:
    """编码一条带换行符的 JSON 消息。"""
    if not isinstance(message, Mapping):
        raise ProtocolError("消息必须是对象")
    return _json_line(message)


def decode_message(raw: bytes | str) -> dict[str, Any]:
    """解码一行消息，并验证协议版本和对象类型。"""
    if isinstance(raw, bytes):
        if len(raw) > MAX_FRAME_BYTES:
            raise ProtocolError("消息超过协议允许的大小")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("消息不是有效的 UTF-8") from exc
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
            raise ProtocolError("消息超过协议允许的大小")
        text = raw
    else:
        raise ProtocolError("消息必须是 UTF-8 文本或字节串")
    try:
        value = json.loads(text.strip())
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"消息不是有效的 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("消息根节点必须是对象")
    version = value.get("protocol", PROTOCOL_VERSION)
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"不支持的协议版本：{version}")
    return value


def make_command(command: str, args: Mapping[str, Any] | None = None,
                 *, token: str | None = None,
                 request_id: str | None = None) -> dict[str, Any]:
    if command not in COMMANDS:
        raise ProtocolError(f"未知命令：{command}")
    message: dict[str, Any] = {
        "protocol": PROTOCOL_VERSION,
        "kind": "command",
        "request_id": request_id or _new_id(),
        "command": command,
        "args": dict(args or {}),
    }
    if token is not None:
        message["token"] = str(token)
    return message


def validate_command(message: Mapping[str, Any]) -> dict[str, Any]:
    """严格校验命令，返回普通 dict 供服务端使用。"""
    if message.get("kind") != "command":
        raise ProtocolError("客户端消息 kind 必须为 command")
    request_id = message.get("request_id")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise ProtocolError("缺少合法 request_id")
    command = message.get("command")
    if command not in COMMANDS:
        raise ProtocolError(f"未知命令：{command}")
    args = message.get("args", {})
    if not isinstance(args, dict):
        raise ProtocolError("命令 args 必须是对象")
    token = message.get("token")
    if token is not None and not isinstance(token, str):
        raise ProtocolError("命令 token 必须是字符串")
    return {
        "protocol": PROTOCOL_VERSION,
        "kind": "command",
        "request_id": request_id,
        "command": command,
        "args": dict(args),
        "token": token,
    }


def make_response(request_id: str, *, ok: bool, result: Any = None,
                  error: Mapping[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "protocol": PROTOCOL_VERSION,
        "kind": "response",
        "request_id": str(request_id),
        "ok": bool(ok),
    }
    if ok:
        response["result"] = result
    else:
        response["error"] = dict(error or {"code": "unknown", "message": "未知错误"})
    return response


def make_hello(*, server_pid: int | None = None,
               token_required: bool = True) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "kind": "hello",
        "server_pid": int(server_pid or os.getpid()),
        "token_required": bool(token_required),
    }


def make_event(event: str, payload: Any = None, *, sequence: int = 0) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "kind": "event",
        "event": str(event),
        "sequence": int(sequence),
        "payload": payload,
    }


@dataclass(frozen=True)
class BackendEndpoint:
    """GUI 连接后台服务所需的最小信息。"""

    host: str
    port: int
    token: str
    pid: int | None = None
    started_at: str | None = None
    protocol: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "host": self.host,
            "port": int(self.port),
            "token": self.token,
            "pid": self.pid,
            "started_at": self.started_at,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BackendEndpoint":
        try:
            protocol = int(value.get("protocol", PROTOCOL_VERSION))
            host = str(value["host"])
            port = int(value["port"])
            token = str(value["token"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError("后台服务地址文件格式错误") from exc
        if protocol != PROTOCOL_VERSION or not host or not 1 <= port <= 65535 or not token:
            raise ProtocolError("后台服务地址文件内容无效")
        return cls(
            host=host,
            port=port,
            token=token,
            pid=int(value["pid"]) if value.get("pid") is not None else None,
            started_at=str(value["started_at"]) if value.get("started_at") else None,
            protocol=protocol,
        )


def write_endpoint(endpoint: BackendEndpoint, path: str) -> str:
    """原子写入地址文件；返回绝对路径。"""
    target = os.path.abspath(path)
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    temp = target + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8") as stream:
            json.dump(endpoint.to_dict(), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass
    return target


def read_endpoint(path: str) -> BackendEndpoint:
    try:
        with open(os.path.abspath(path), encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        raise ProtocolError(f"无法读取后台服务地址：{exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("后台服务地址必须是对象")
    return BackendEndpoint.from_mapping(value)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
