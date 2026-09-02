# -*- coding: utf-8 -*-
"""2.0 本地后台任务服务兼容层。

本模块不改变现有 GUI 的启动方式。它把已有 ``Scheduler`` 包装成一个仅
监听本机回环地址的 JSON Lines 服务，先用于集成测试，后续由 Tk/QML GUI
逐步接入。后台拥有 Scheduler，客户端只能调用白名单命令和接收事件。
"""

from __future__ import annotations

import hashlib
import csv
import json
import os
import re
import secrets
import sqlite3
import socketserver
import threading
import time
from datetime import datetime
from typing import Any, Mapping

try:
    from .backend_protocol import (
        BackendEndpoint,
        ProtocolError,
        decode_message,
        encode_message,
        make_event,
        make_hello,
        make_response,
        now_iso,
        validate_command,
        write_endpoint,
    )
except ImportError:  # pragma: no cover - 支持 python src/backend_service.py
    from backend_protocol import (  # type: ignore
        BackendEndpoint,
        ProtocolError,
        decode_message,
        encode_message,
        make_event,
        make_hello,
        make_response,
        now_iso,
        validate_command,
        write_endpoint,
    )


class _BackendTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


_AUTH_COMMANDS = frozenset({
    "auth_register", "auth_login", "auth_logout", "auth_me",
    "auth_list_users", "auth_approve_user", "auth_reject_user",
    "auth_disable_user", "auth_reset_password",
})


class _BackendClientHandler(socketserver.StreamRequestHandler):
    """一个 TCP 客户端一条读循环；写入由发送锁保护。"""

    def setup(self):
        super().setup()
        self._write_lock = threading.Lock()
        self._closed = False
        self.auth_user: dict[str, Any] | None = None
        # 一个本地后台服务对应一台工作终端；编号由服务端持久化，重启 GUI
        # 或重连 TCP 后仍保持一致。不使用电脑名、用户名或浏览器窗口 ID。
        self.device_id = str(getattr(self.service, "device_id", "") or secrets.token_hex(16))
        self.device_name = ""

    @property
    def service(self) -> "BackendService":
        return self.server.backend_service  # type: ignore[attr-defined]

    def send_message(self, message: Mapping[str, Any]) -> None:
        raw = encode_message(message)
        with self._write_lock:
            if self._closed:
                raise ConnectionError("客户端连接已关闭")
            self.wfile.write(raw)
            self.wfile.flush()

    def _dispatch_and_reply(self, message: Mapping[str, Any]) -> None:
        """在独立线程执行命令，避免耗时采集堵住暂停/停止命令。"""
        response = self.service.dispatch(message, client=self)
        try:
            self.send_message(response)
        except (BrokenPipeError, ConnectionError, OSError, ValueError):
            # 客户端可能已经退出；后台任务本身不应因此报错。
            pass

    def handle(self):
        service = self.service
        service._register_client(self)
        try:
            self.send_message(make_hello(token_required=True))
            try:
                for raw in self.rfile:
                    if not raw:
                        break
                    try:
                        message = validate_command(decode_message(raw))
                        # 不能在当前读循环内同步 dispatch：start/resume_task
                        # 可能执行长时间的搜索，导致同一连接上的 pause/stop
                        # 命令一直排队。每个请求独立处理，响应由写锁保证完整。
                        threading.Thread(
                            target=self._dispatch_and_reply,
                            args=(message,),
                            name="backend-command",
                            daemon=True,
                        ).start()
                        continue
                    except ProtocolError as exc:
                        request_id = ""
                        try:
                            request_id = str(decode_message(raw).get("request_id") or "")
                        except ProtocolError:
                            pass
                        response = make_response(
                            request_id,
                            ok=False,
                            error={"code": "protocol_error", "message": str(exc)},
                        )
                    try:
                        self.send_message(response)
                    except (BrokenPipeError, ConnectionError, OSError):
                        break
            except (BrokenPipeError, ConnectionError, OSError):
                # 客户端正常退出时 Windows 可能返回 10053/10054；这不是
                # 后台业务异常，不应在控制台输出完整堆栈污染诊断日志。
                pass
        finally:
            self._closed = True
            service._unregister_client(self)


class BackendService:
    """把一个 Scheduler 暴露成受控的本地后台服务。"""

    def __init__(self, scheduler, *, host: str = "127.0.0.1", port: int = 0,
                 token: str | None = None, endpoint_path: str | None = None,
                 snapshot_interval: float = 0.5):
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("后台服务只允许监听本机回环地址")
        if not 0 <= int(port) <= 65535:
            raise ValueError("端口必须在 0–65535 之间")
        self.scheduler = scheduler
        self.host = host
        self.port = int(port)
        self.token = str(token or secrets.token_urlsafe(24))
        self.endpoint_path = os.path.abspath(endpoint_path) if endpoint_path else None
        self.snapshot_interval = max(0.1, float(snapshot_interval))
        self._server: _BackendTCPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._watcher_thread: threading.Thread | None = None
        self._hourly_log_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._device_id_path = os.path.join(
            os.path.dirname(os.path.abspath(self.scheduler.db_path)),
            "device_id.txt",
        )
        self._device_id = self._load_or_create_device_id()
        self._clients: set[_BackendClientHandler] = set()
        self._clients_lock = threading.RLock()
        # 删除运行中任务需要等待 worker 安全退出。相同任务的重复点击不应
        # 同时进入 stop/delete 流程，否则会互相等待并放大按钮的迟钝感。
        self._task_delete_lock = threading.Lock()
        self._task_deletions_inflight: set[int] = set()
        # 线索查询和互动草稿写入使用独立 SQLite 连接，但仍串行化这两类
        # 操作，避免多个 QML 请求同时触发迁移、提交或状态推进。
        self._lead_lock = threading.RLock()
        # 账号主页同步会导航对应 BitBrowser 窗口；同一账号同时导航两次会
        # 让两个 CDP 会话互相覆盖页面，最终把 B 站推荐页/微博入口页当成
        # 账号主页。每个账号只允许一个同步请求进入浏览器。
        self._account_content_sync_locks: dict[int, threading.Lock] = {}
        self._account_content_sync_locks_guard = threading.Lock()
        # 消息中心同步会导航账号窗口；同一账号不能同时刷新消息和账号主页，
        # 否则两个 CDP 会话会相互覆盖当前页面。
        self._message_sync_locks: dict[int, threading.Lock] = {}
        self._message_sync_locks_guard = threading.Lock()
        self._sequence = 0
        self._last_snapshot_digest = ""
        self._last_snapshot_digests: dict[int, str] = {}
        self._endpoint: BackendEndpoint | None = None
        try:
            from .operation_log import OperationLog  # type: ignore
        except ImportError:  # pragma: no cover
            from operation_log import OperationLog  # type: ignore
        try:
            from .auth_store import AuthError, AuthStore  # type: ignore
        except ImportError:  # pragma: no cover
            from auth_store import AuthError, AuthStore  # type: ignore
        self._auth_error_type = AuthError
        self._auth_store = AuthStore(self.scheduler.db_path)
        self._operation_log = OperationLog(os.path.join(
            os.path.dirname(os.path.abspath(self.scheduler.db_path)),
            "logs", "operation_events.jsonl",
        ))
        self._previous_log_callback = getattr(scheduler, "log_callback", None)
        scheduler.log_callback = self._on_scheduler_log

    def _load_or_create_device_id(self) -> str:
        """读取或生成本机终端编号；文件只含随机编号，不含员工个人数据。"""
        try:
            with open(self._device_id_path, encoding="ascii") as stream:
                value = stream.read(128).strip()
            if value and re.fullmatch(r"[0-9a-f]{32}", value, flags=re.IGNORECASE):
                return value.lower()
        except (OSError, UnicodeError):
            pass
        value = secrets.token_hex(16)
        temporary = self._device_id_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._device_id_path), exist_ok=True)
            with open(temporary, "w", encoding="ascii") as stream:
                stream.write(value)
            os.replace(temporary, self._device_id_path)
        except OSError:
            # 无法落盘时仍允许服务工作；本次进程内的编号依然可用于设备管理。
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass
        return value

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def endpoint(self) -> BackendEndpoint:
        if self._endpoint is None:
            raise RuntimeError("后台服务尚未启动")
        return self._endpoint

    @property
    def address(self) -> tuple[str, int]:
        return self.endpoint.host, self.endpoint.port

    def start(self) -> BackendEndpoint:
        if self._server is not None:
            return self.endpoint
        server = _BackendTCPServer((self.host, self.port), _BackendClientHandler)
        server.backend_service = self  # type: ignore[attr-defined]
        self._server = server
        actual_host, actual_port = server.server_address[:2]
        self._endpoint = BackendEndpoint(
            host=str(actual_host),
            port=int(actual_port),
            token=self.token,
            pid=os.getpid(),
            started_at=now_iso(),
        )
        if self.endpoint_path:
            write_endpoint(self.endpoint, self.endpoint_path)
        self._stop.clear()
        self._server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="backend-service-server",
            daemon=True,
        )
        self._server_thread.start()
        self._watcher_thread = threading.Thread(
            target=self._watch_state,
            name="backend-service-state",
            daemon=True,
        )
        self._watcher_thread.start()
        self._hourly_log_thread = threading.Thread(
            target=self._hourly_log_loop,
            name="backend-operation-log-hourly",
            daemon=True,
        )
        self._hourly_log_thread.start()
        # 2.0 服务启动后立即接管已配置的定时增量监控，避免仅保存规则
        # 却没有后台轮询线程。
        try:
            self.scheduler.start_monitoring()
        except Exception as exc:
            self._on_scheduler_log(f"[monitor] 后台监控启动失败：{type(exc).__name__}: {exc}")
        # 重启后先复核数据库里残留的人工冻结；复核在后台执行，不阻塞服务监听。
        reconcile = getattr(self.scheduler, "reconcile_human_waiting_async", None)
        if callable(reconcile):
            reconcile()
        return self.endpoint

    def stop(self, *, shutdown_scheduler: bool = False) -> None:
        server = self._server
        if server is None:
            return
        self._stop.set()
        try:
            self.scheduler.stop_monitoring()
        except Exception:
            pass
        try:
            self._operation_log.save_hourly_snapshot()
        except Exception:
            pass
        if shutdown_scheduler:
            try:
                self.scheduler.shutdown(close_connections=False)
            except Exception:
                pass
        server.shutdown()
        server.server_close()
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.connection.shutdown(2)
            except OSError:
                pass
            try:
                client.connection.close()
            except OSError:
                pass
        if self._server_thread is not None and self._server_thread is not threading.current_thread():
            self._server_thread.join(timeout=2)
        if self._watcher_thread is not None and self._watcher_thread is not threading.current_thread():
            self._watcher_thread.join(timeout=2)
        if self._hourly_log_thread is not None and self._hourly_log_thread is not threading.current_thread():
            self._hourly_log_thread.join(timeout=2)
        self._server = None
        self._server_thread = None
        self._watcher_thread = None
        self._hourly_log_thread = None
        if self.endpoint_path:
            try:
                with open(self.endpoint_path, encoding="utf-8") as stream:
                    current = json.load(stream)
                if current.get("token") == self.token:
                    os.remove(self.endpoint_path)
            except (OSError, ValueError, AttributeError):
                pass

    def _register_client(self, client: _BackendClientHandler) -> None:
        with self._clients_lock:
            self._clients.add(client)

    def _unregister_client(self, client: _BackendClientHandler) -> None:
        with self._clients_lock:
            self._clients.discard(client)
        self._last_snapshot_digests.pop(id(client), None)

    def _broadcast(self, message: Mapping[str, Any]) -> None:
        failed = []
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.send_message(message)
            except (BrokenPipeError, ConnectionError, OSError):
                failed.append(client)
        if failed:
            with self._clients_lock:
                for client in failed:
                    self._clients.discard(client)

    def _emit_event(self, event: str, payload: Any = None) -> None:
        self._sequence += 1
        self._broadcast(make_event(event, payload, sequence=self._sequence))

    def _on_scheduler_log(self, message: str) -> None:
        record = self._operation_log.append(
            str(message), source="scheduler", event="scheduler_log",
        )
        self._emit_event("log", record)
        callback = self._previous_log_callback
        if callable(callback) and callback is not self._on_scheduler_log:
            try:
                callback(message)
            except Exception:
                pass

    def _hourly_log_loop(self) -> None:
        while not self._stop.wait(3600.0):
            try:
                result = self._operation_log.save_hourly_snapshot()
                self._on_scheduler_log(
                    f"后台操作日志小时快照已保存：{result.get('jsonl', '')}"
                )
            except Exception as exc:
                self._on_scheduler_log(
                    f"后台操作日志小时快照失败：{type(exc).__name__}: {exc}"
                )

    @staticmethod
    def _safe_operation_args(args: Mapping[str, Any]) -> dict[str, Any]:
        secret_names = {
            "api_key", "api_token", "token", "password", "cookie", "cookies",
            "authorization",
        }

        def scrub(value: Any, key: str = "") -> Any:
            if key.lower() in secret_names:
                return "***已脱敏***"
            if isinstance(value, Mapping):
                return {str(k): scrub(v, str(k)) for k, v in list(value.items())[:100]}
            if isinstance(value, (list, tuple)):
                return [scrub(item, key) for item in list(value)[:100]]
            if isinstance(value, str) and len(value) > 4000:
                return value[:4000] + "...[截断]"
            return value

        return scrub(dict(args))

    def _watch_state(self) -> None:
        while not self._stop.is_set():
            with self._clients_lock:
                has_clients = bool(self._clients)
                clients = list(self._clients)
            if has_clients:
                # 状态广播也必须按登录员工分别裁剪。此前这里直接把
                # scheduler 的全量快照广播给所有 TCP 客户端，会泄露其它
                # 员工的任务和账号列表。
                for client in clients:
                    try:
                        snapshot = self._scoped_status_report(client)
                        digest = hashlib.sha256(
                            json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                       default=str).encode("utf-8")
                        ).hexdigest()
                        client_key = id(client)
                        if digest == self._last_snapshot_digests.get(client_key):
                            continue
                        self._last_snapshot_digests[client_key] = digest
                        client.send_message(make_event(
                            "state_snapshot", snapshot, sequence=self._sequence + 1
                        ))
                        self._sequence += 1
                    except Exception as exc:
                        try:
                            client.send_message(make_event(
                                "backend_error",
                                {"type": type(exc).__name__, "message": str(exc)},
                                sequence=self._sequence + 1,
                            ))
                            self._sequence += 1
                        except Exception:
                            pass
            self._stop.wait(self.snapshot_interval)

    def _authorized(self, message: Mapping[str, Any]) -> bool:
        supplied = message.get("token")
        return isinstance(supplied, str) and secrets.compare_digest(supplied, self.token)

    @staticmethod
    def _int_arg(args: Mapping[str, Any], name: str, *, required: bool = True):
        value = args.get(name)
        if value is None and not required:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"参数 {name} 必须是整数") from exc

    @staticmethod
    def _optional_text(args: Mapping[str, Any], name: str) -> str | None:
        value = args.get(name)
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    @staticmethod
    def _publish_topics(value: Any) -> list[str]:
        """把界面编辑的中文逗号/换行话题转换为平台版本数组。"""
        if isinstance(value, list):
            raw_values = value
        else:
            raw_values = re.split(r"[,，\n]", str(value or ""))
        result: list[str] = []
        for raw in raw_values:
            item = str(raw or "").strip()
            if item and item not in result:
                result.append(item)
        return result

    @staticmethod
    def _variant_text(variant: Mapping[str, Any], field: str,
                      source_field: str) -> str:
        """读取平台版本正文；空字符串也是用户明确保存的值，不能回退旧原文。"""
        value = variant.get(field)
        if value is not None:
            return str(value)
        return str(variant.get(source_field) or "")

    def _apply_publish_editor_overrides(self, service, draft_id: int,
                                        platform: str,
                                        args: Mapping[str, Any]) -> bool:
        """发布前保存界面当前编辑值，避免直接发布数据库里的旧版本。"""
        keys = ("editor_title", "editor_body", "editor_topics", "editor_content_type")
        if not any(key in args for key in keys):
            return False
        current = service.get_variant(draft_id, platform)
        title = (
            str(args.get("editor_title") or "")
            if "editor_title" in args
            else self._variant_text(current, "title", "source_title")
        )
        body = (
            str(args.get("editor_body") or "")
            if "editor_body" in args
            else self._variant_text(current, "body", "source_content")
        )
        topics = (
            self._publish_topics(args.get("editor_topics"))
            if "editor_topics" in args
            else list(current.get("topics") or [])
        )
        content_type = (
            str(args.get("editor_content_type") or current.get("content_type") or "text")
            if "editor_content_type" in args
            else str(current.get("content_type") or "text")
        )
        service.update_variant(
            draft_id=draft_id, platform=platform, title=title, body=body,
            topics=topics, content_type=content_type,
        )
        self._on_scheduler_log(
            f"发布平台版本已保存最新编辑：草稿#{draft_id} · {platform} · "
            f"标题{len(title)}字 · 正文{len(body)}字 · 话题{len(topics)}个"
        )
        return True

    @staticmethod
    def _publish_verification_summary(result: Mapping[str, Any]) -> str:
        """生成不泄露整段正文的核对摘要，便于日志定位错在哪个输入框。"""
        labels = {"title": "标题", "body": "正文", "topics": "话题"}
        mismatches = [str(item) for item in result.get("mismatches") or []]
        expected = result.get("expected") if isinstance(result.get("expected"), Mapping) else {}
        actual = result.get("actual") if isinstance(result.get("actual"), Mapping) else {}
        if not mismatches and result.get("ok"):
            return "填入后内容核对通过（标题、正文及适用的话题均与本地平台版本一致）"
        fields = mismatches or ["编辑器内容"]
        details = []
        for field in fields:
            name = labels.get(field, field)
            wanted = str(expected.get(field) or "")
            received = str(actual.get(field) or "")
            details.append(f"{name}本地{len(wanted)}字/页面{len(received)}字")
        return "填入后内容核对失败：" + "、".join(details)

    def _lead_connection(self):
        """为后台数据命令打开独立连接，避免占用 Scheduler 采集连接。"""
        try:
            from . import db  # type: ignore
        except ImportError:  # pragma: no cover
            import db  # type: ignore
        return db.init_db(self.scheduler.db_path, check_same_thread=False)

    @staticmethod
    def _lead_row(item) -> dict[str, Any]:
        """把领域对象转换为协议允许的普通 JSON 对象。"""
        return {
            "id": int(item.id),
            "platform": str(item.platform or "unknown"),
            "nickname": item.nickname or "匿名用户",
            "platform_user_id": item.platform_user_id or "",
            "profile_url": item.profile_url or "",
            "region_province": item.region_province or "",
            "region_source": item.region_source or "unknown",
            "region_confidence": int(item.region_confidence or 0),
            "freshness_bucket": item.freshness_bucket or "unknown",
            "intent_level": item.intent_level or "unknown",
            "intent_score": int(item.intent_score or 0),
            "intent_reasons": list(item.intent_reasons or []),
            "pool": item.pool or "unclassified",
            "owner_id": item.owner_id,
            "owner_name": item.owner_name or "",
            "status": item.status or "new",
            "last_interaction_at": item.last_interaction_at or "",
            "comment": item.summary_text or "",
            "summary_text": item.summary_text or "",
            "comment_time": item.comment_time or "",
            "source_url": item.source_url or "",
        }

    def _list_leads(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        try:
            from .leads.repository import LeadQuery, LeadRepository  # type: ignore
            from .leads.service import LeadService  # type: ignore
        except ImportError:  # pragma: no cover
            from leads.repository import LeadQuery, LeadRepository  # type: ignore
            from leads.service import LeadService  # type: ignore

        page = max(1, self._int_arg(args, "page", required=False) or 1)
        page_size = min(100, max(1, self._int_arg(args, "page_size", required=False) or 50))
        task_id = self._int_arg(args, "task_id", required=False)
        if task_id is not None and task_id <= 0:
            task_id = None
        query = LeadQuery(
            page=page,
            page_size=page_size,
            task_id=task_id,
            platform=self._optional_text(args, "platform"),
            province=self._optional_text(args, "province"),
            intent_level=self._optional_text(args, "intent_level"),
            keyword=self._optional_text(args, "keyword"),
            data_owner_user_id=self._owner_user_id(client),
            sort_by=self._optional_text(args, "sort_by") or "updated_at",
            sort_order=self._optional_text(args, "sort_order") or "desc",
        )
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                service = LeadService(LeadRepository(conn), config={})
                result = service.list_leads(query)
                owner_id = self._owner_user_id(client)
                tasks = service.list_tasks(owner_id)
                provinces = service.list_provinces(owner_id)
                try:
                    from .leads.dashboard import DashboardQueryService  # type: ignore
                except ImportError:  # pragma: no cover
                    from leads.dashboard import DashboardQueryService  # type: ignore
                try:
                    stats = DashboardQueryService(conn).summary()
                    stats = dict(stats)
                    if owner_id is not None:
                        owner_clause = " WHERE data_owner_user_id = ?"
                        owner_params = (owner_id,)
                        stats["total"] = int(conn.execute(
                            "SELECT COUNT(*) FROM leads" + owner_clause, owner_params
                        ).fetchone()[0])
                        stats["region_review"] = int(conn.execute(
                            "SELECT COUNT(*) FROM leads" + owner_clause +
                            " AND pool = 'region_review'", owner_params
                        ).fetchone()[0])
                        stats["human_required"] = stats["region_review"]
                        sent_row = conn.execute(
                            "SELECT COUNT(*) AS c FROM interaction_drafts d JOIN leads l ON l.id = d.lead_id "
                            "WHERE l.data_owner_user_id = ? AND d.status = 'sent'", owner_params
                        ).fetchone()
                        total_row = conn.execute(
                            "SELECT COUNT(*) AS c FROM interaction_drafts d JOIN leads l ON l.id = d.lead_id "
                            "WHERE l.data_owner_user_id = ? AND d.status NOT IN ('cancelled','rejected')",
                            owner_params,
                        ).fetchone()
                    else:
                        sent_row = conn.execute(
                            "SELECT COUNT(*) AS c FROM interaction_drafts WHERE status = 'sent'"
                        ).fetchone()
                        total_row = conn.execute(
                            "SELECT COUNT(*) AS c FROM interaction_drafts "
                            "WHERE status NOT IN ('cancelled','rejected')"
                        ).fetchone()
                    stats["human_required"] = int(stats.get("region_review") or 0)
                    sent_count = int(sent_row["c"] if sent_row else 0)
                    total_count = int(total_row["c"] if total_row else 0)
                    stats["reply_rate"] = round(sent_count * 100 / total_count, 1) if total_count else 0
                except sqlite3.Error:
                    # 旧数据库缺少统计表时，列表仍然可用，卡片回退到基础数量。
                    stats = {}
                return {
                    "items": [self._lead_row(item) for item in result.items],
                    "total": int(result.total),
                    "page": int(result.page),
                    "page_size": int(result.page_size),
                    "pages": (int(result.total) + int(result.page_size) - 1)
                    // int(result.page_size) if result.total else 0,
                    "tasks": tasks,
                    "provinces": provinces,
                    "stats": stats,
                }
            finally:
                conn.close()

    def _list_keyword_groups(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        """提供 2.0 新建任务使用的预制关键词组，复用 1.2 的存储逻辑。"""
        try:
            from .operations.keywords import KeywordGroupStore  # type: ignore
        except ImportError:  # pragma: no cover
            from operations.keywords import KeywordGroupStore  # type: ignore
        conn = self._lead_connection()
        try:
            groups = []
            owner_id = self._owner_user_id(client)
            for item in KeywordGroupStore(conn).list_groups(enabled_only=True):
                if owner_id is not None and int(item.get("owner_user_id") or 0) != owner_id:
                    continue
                # 任务下拉只需要名称，线索中心的关键词管理入口还需要
                # 直接编辑现有词条，因此统一返回完整词组内容。
                group = KeywordGroupStore(conn).get(item["id"]) or dict(item)
                group["id"] = int(group.get("id") or 0)
                group["platform"] = str(group.get("platform") or "")
                group["name"] = str(group.get("name") or "未命名关键词组")
                groups.append(group)
            return {"items": groups}
        finally:
            conn.close()

    def _create_keyword_group(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        """在新建任务流程中创建可复用的关键词组。"""
        try:
            from .operations.keywords import KeywordGroupStore  # type: ignore
        except ImportError:  # pragma: no cover
            from operations.keywords import KeywordGroupStore  # type: ignore

        def terms(value: Any) -> list[str]:
            if isinstance(value, list):
                return [str(item) for item in value]
            if value is None:
                return []
            return [str(value)]

        name = str(args.get("name") or "").strip()
        platform = str(args.get("platform") or "").strip() or None
        term_values = {
            "core": terms(args.get("core_terms")),
            "synonym": terms(args.get("synonym_terms")),
            "region": terms(args.get("region_terms")),
            "exclude": terms(args.get("exclude_terms")),
        }
        if not name:
            raise ValueError("关键词组名称不能为空")
        if not any(term_values[key] for key in ("core", "synonym")):
            raise ValueError("至少填写一个核心词或同义词")

        conn = self._lead_connection()
        store = KeywordGroupStore(conn)
        group_id = 0
        try:
            group_id = store.create_group(name, platform)
            store.set_terms(group_id, term_values)
            group = store.get(group_id) or {"id": group_id, "name": name, "platform": platform or ""}
            group["id"] = int(group.get("id") or group_id)
            group["name"] = str(group.get("name") or name)
            group["platform"] = str(group.get("platform") or "")
            self._set_owner_and_queue("keyword_groups", group_id, client, "keyword_group")
            return group
        except Exception:
            # 词组已经创建但词项保存失败时清理半成品，避免下拉框出现不可执行的词组。
            if group_id:
                try:
                    store.delete_group(group_id)
                except Exception:
                    pass
            raise
        finally:
            conn.close()

    def _update_keyword_group(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        """保存线索中心入口编辑后的关键词组，名称和词条只推进一次版本。"""
        try:
            from .operations.keywords import KeywordGroupStore  # type: ignore
        except ImportError:  # pragma: no cover
            from operations.keywords import KeywordGroupStore  # type: ignore

        group_id = self._int_arg(args, "group_id")
        name = str(args.get("name") or "").strip()
        platform = str(args.get("platform") or "").strip() or None

        def terms(value: Any) -> list[str]:
            if isinstance(value, list):
                return [str(item) for item in value]
            if value is None:
                return []
            return [str(value)]

        term_values = {
            "core": terms(args.get("core_terms")),
            "synonym": terms(args.get("synonym_terms")),
            "region": terms(args.get("region_terms")),
            "exclude": terms(args.get("exclude_terms")),
        }
        if not name:
            raise ValueError("关键词组名称不能为空")
        if not any(term_values[key] for key in ("core", "synonym")):
            raise ValueError("至少填写一个核心词或同义词")

        conn = self._lead_connection()
        try:
            store = KeywordGroupStore(conn)
            self._require_owned_row("keyword_groups", group_id, "owner_user_id", client, "关键词组")
            # set_terms 会统一推进一次版本；避免名称修改和词条修改造成
            # 同一次保存版本号跳两次。
            store.update_group(group_id, name, platform, bump_version=False)
            store.set_terms(group_id, term_values)
            group = store.get(group_id)
            if group is None:  # pragma: no cover - update_group 已保证存在
                raise KeyError(f"关键词组不存在: {group_id}")
            group["id"] = int(group.get("id") or group_id)
            group["platform"] = str(group.get("platform") or "")
            group["name"] = str(group.get("name") or name)
            self._queue_entity("keyword_groups", group_id, client, "keyword_group")
            return group
        finally:
            conn.close()

    @staticmethod
    def _platform_url(platform: str) -> str:
        return {
            "douyin": "https://www.douyin.com/user/self?from_nav=1",
            "xhs": "https://creator.xiaohongshu.com/new/note-manager?source=official",
            "bilibili": "https://www.bilibili.com/",
            "weibo": "https://weibo.com/",
            "kuaishou": "https://www.kuaishou.com/new-reco",
        }.get(str(platform or "douyin"), "https://www.douyin.com/")

    def _account_by_id(self, account_id: int) -> dict[str, Any]:
        for item in (self.scheduler.status_report().get("accounts") or {}).values():
            if int(item.get("id") or 0) == int(account_id):
                return dict(item)
        raise ValueError(f"账号不存在: {account_id}")

    def _account_ws_path(self, platform: str) -> str:
        filename = {
            "douyin": "dy_window.txt",
            "xhs": "xhs_window.txt",
            "weibo": "weibo_chrome_window.txt",
            "bilibili": "bilibili_chrome_window.txt",
            "kuaishou": "kuaishou_window.txt",
        }.get(str(platform or ""))
        if not filename:
            raise ValueError(f"不支持的平台: {platform}")
        return os.path.join(os.path.dirname(os.path.abspath(self.scheduler.db_path)), filename)

    def _open_account_browser(self, args: Mapping[str, Any]) -> dict[str, Any]:
        account_id = self._int_arg(args, "account_id")
        account = self._account_by_id(account_id)
        window_id = str(account.get("bb_window_id") or "").strip()
        if not window_id:
            raise RuntimeError("该账号尚未绑定 BitBrowser 窗口")
        if window_id in {"chrome", "chrome-bilibili"}:
            return {
                "account_id": account_id,
                "window_id": window_id,
                "opened": False,
                "message": "该账号使用外部 Chrome 会话，请手动打开浏览器",
            }
        bb = getattr(self.scheduler, "bb", None)
        if bb is None:
            raise RuntimeError("BitBrowser 客户端未配置")

        platform = str(account.get("platform") or "douyin")
        page_url = self._platform_url(platform)
        result = bb.open_browser(window_id, ignore_default_urls=True)
        if not isinstance(result, dict):
            raise RuntimeError("BitBrowser 未返回浏览器连接信息")
        ws = str(result.get("ws") or result.get("webSocketDebuggerUrl") or "").strip()
        if not ws:
            raise RuntimeError("浏览器已打开，但未返回 CDP 连接地址")

        # 与 1.2 保持一致：先打开唯一窗口，再通过 CDP 导航到平台初始页，
        # 避免 BitBrowser 默认导航页被误当成平台页面。
        try:
            import asyncio
            try:
                from .cdp import CdpSession  # type: ignore
            except ImportError:  # pragma: no cover
                from cdp import CdpSession  # type: ignore

            async def navigate():
                session = CdpSession(ws)
                await session.connect()
                try:
                    page_session = await session.attach_page()
                    await session.cmd("Page.navigate", {"url": page_url}, session_id=page_session)
                    await asyncio.sleep(1)
                finally:
                    await session.close()

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(navigate())
            finally:
                loop.close()
        except Exception as exc:
            self._on_scheduler_log(f"账号 {account.get('name') or account_id} 已打开，但平台导航失败：{exc}")

        path = self._account_ws_path(platform)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(window_id + "\n" + ws + "\n")
        self._on_scheduler_log(f"🌐 已打开账号浏览器：{account.get('name') or account_id}（{platform}）")
        return {"account_id": account_id, "window_id": window_id, "opened": True}

    def _create_browser_window(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """创建并打开一个 BitBrowser profile，供账号页随后选择绑定。"""
        raw_platform = str(args.get("platform") or "douyin").strip().lower()
        platform = {
            "dy": "douyin", "douyin": "douyin",
            "xhs": "xhs", "xiaohongshu": "xhs", "redbook": "xhs",
            "bili": "bilibili", "bilibili": "bilibili",
            "wb": "weibo", "weibo": "weibo",
            "ks": "kuaishou", "kuaishou": "kuaishou",
        }.get(raw_platform)
        if not platform:
            raise ValueError(f"不支持的平台：{raw_platform}")

        browser = getattr(self.scheduler, "bb", None)
        if browser is None:
            raise RuntimeError("BitBrowser 客户端未配置，请先在设置中检测本地服务")

        labels = {
            "douyin": "抖音", "xhs": "小红书", "bilibili": "B站",
            "weibo": "微博", "kuaishou": "快手",
        }
        label = labels[platform]
        name = str(args.get("name") or "").strip()
        if not name:
            name = f"{label}浏览器-{time.strftime('%H%M%S')}"
        page_url = self._platform_url(platform)

        created = browser.create_window(name=name, platform=page_url, url=page_url)
        if not isinstance(created, Mapping):
            raise RuntimeError("BitBrowser 创建窗口未返回窗口信息")
        window_id = str(
            created.get("id") or created.get("browserId")
            or created.get("browser_id") or ""
        ).strip()
        if not window_id:
            raise RuntimeError("BitBrowser 创建窗口成功，但未返回窗口 ID")

        opened = False
        open_error = ""
        try:
            opened_result = browser.open_browser(window_id, ignore_default_urls=True)
            if not isinstance(opened_result, Mapping):
                raise RuntimeError("打开窗口未返回连接信息")
            ws = str(
                opened_result.get("ws")
                or opened_result.get("webSocketDebuggerUrl") or ""
            ).strip()
            if not ws:
                raise RuntimeError("打开窗口未返回 CDP 连接地址")
            opened = True
            path = self._account_ws_path(platform)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(window_id + "\n" + ws + "\n")

            # 创建时传入的 URL 只是窗口配置；再次通过 CDP 导航，避免 BitBrowser
            # 仍然展示默认导航页，后续账号绑定才会读取到正确的平台登录态。
            try:
                import asyncio
                try:
                    from .cdp import CdpSession  # type: ignore
                except ImportError:  # pragma: no cover
                    from cdp import CdpSession  # type: ignore

                async def navigate():
                    session = CdpSession(ws)
                    await session.connect()
                    try:
                        page_session = await session.attach_page()
                        await session.cmd(
                            "Page.navigate", {"url": page_url},
                            session_id=page_session,
                        )
                    finally:
                        await session.close()

                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(navigate())
                finally:
                    loop.close()
            except Exception as exc:
                open_error = f"平台页面导航失败：{type(exc).__name__}: {exc}"
                self._on_scheduler_log(
                    f"⚠ 已创建并打开浏览器 {name}（窗口 {window_id}），{open_error}"
                )
        except Exception as exc:
            open_error = str(exc)
            self._on_scheduler_log(
                f"⚠ 浏览器窗口已创建但自动打开失败：{name}（窗口 {window_id}）· {exc}"
            )

        if opened and not open_error:
            self._on_scheduler_log(
                f"➕ 已创建并打开浏览器：{name}（平台={label}，窗口={window_id}）"
            )
        elif not opened:
            self._on_scheduler_log(
                f"➕ 已创建浏览器窗口：{name}（平台={label}，窗口={window_id}）"
            )
        return {
            "window_id": window_id,
            "name": name,
            "platform": platform,
            "platform_label": label,
            "opened": opened,
            "message": (
                "浏览器已创建并打开，可刷新窗口列表后添加账号"
                if opened and not open_error
                else "浏览器已创建，请在 BitBrowser 中打开后刷新窗口列表"
            ),
            "open_error": open_error,
        }

    def _delete_browser_window(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """删除未绑定的 BitBrowser profile。

        账号管理页展示的“窗口-only”条目才允许走这里；已绑定窗口必须先
        解除账号绑定，避免任务配置和本地 CDP 缓存指向一个被删除的 profile。
        BitBrowser 要求关闭后等待进程退出再删除，等待发生在后台命令线程，
        不会阻塞 QML 界面。
        """
        window_id = str(args.get("window_id") or args.get("id") or "").strip()
        if not window_id:
            raise ValueError("window_id 不能为空")

        for account in (self.scheduler.status_report().get("accounts") or {}).values():
            bound_id = str(
                account.get("bb_window_id") or account.get("window_id") or ""
            ).strip()
            if bound_id == window_id:
                name = str(account.get("name") or "该账号")
                raise RuntimeError(
                    f"窗口已绑定账号“{name}”，请先解除绑定后再删除窗口"
                )

        browser = getattr(self.scheduler, "bb", None)
        if browser is None:
            raise RuntimeError("BitBrowser 客户端未配置，请先在设置中检测本地服务")

        close_error = ""
        try:
            browser.close_browser(window_id)
            # BitBrowser 官方接口要求关闭后等待进程彻底退出，再执行删除。
            time.sleep(5)
        except Exception as exc:  # 已经关闭的窗口可能返回业务错误，继续尝试删除
            close_error = str(exc)
            self._on_scheduler_log(
                f"⚠ 删除窗口前关闭操作未成功，继续尝试删除：{window_id} · {exc}"
            )

        browser.delete_browser(window_id)

        # 删除与该窗口关联的旧 CDP 地址，防止下次打开账号时复用失效连接。
        cache_removed = []
        for platform in ("douyin", "xhs", "bilibili", "weibo", "kuaishou"):
            try:
                path = self._account_ws_path(platform)
                with open(path, encoding="utf-8") as stream:
                    first_line = stream.readline().strip()
                if first_line == window_id:
                    os.remove(path)
                    cache_removed.append(platform)
            except FileNotFoundError:
                continue
            except OSError as exc:
                self._on_scheduler_log(
                    f"⚠ 窗口已删除，但清理 {platform} 连接缓存失败：{exc}"
                )

        self._on_scheduler_log(
            f"🗑 已删除未绑定浏览器窗口：{window_id}"
            + (f"（关闭提示：{close_error}）" if close_error else "")
        )
        return {
            "window_id": window_id,
            "deleted": True,
            "close_error": close_error,
            "cache_removed": cache_removed,
        }

    def _bind_account(self, args: Mapping[str, Any]) -> dict[str, Any]:
        account_id = self._int_arg(args, "account_id")
        account = self._account_by_id(account_id)
        platform = str(account.get("platform") or "douyin")
        window_id = str(account.get("bb_window_id") or "").strip()
        if not window_id:
            raise RuntimeError("该账号尚未绑定 BitBrowser 窗口，无法读取登录账号")
        if window_id in {"chrome", "chrome-bilibili"}:
            raise RuntimeError("外部 Chrome 会话暂不支持自动读取并绑定")

        self._open_account_browser({"account_id": account_id})
        path = self._account_ws_path(platform)
        try:
            with open(path, encoding="utf-8") as stream:
                lines = [line.strip() for line in stream if line.strip()]
            ws = lines[-1] if lines and lines[0] == window_id else ""
        except OSError:
            ws = ""
        if not ws:
            raise RuntimeError("未取得该窗口的 CDP 连接地址")

        try:
            try:
                from .account_reader import read_account  # type: ignore
            except ImportError:  # pragma: no cover
                from account_reader import read_account  # type: ignore
            info = read_account(platform, ws) or {}
        except Exception as exc:
            raise RuntimeError(f"读取平台账号失败：{exc}") from exc
        if not info.get("logged_in"):
            raise RuntimeError("该窗口未登录或暂时无法读取账号信息")
        nickname = str(info.get("nick") or info.get("uid") or info.get("sec_uid") or "").strip()
        if not nickname:
            raise RuntimeError("已检测到登录状态，但未读取到账号昵称或用户 ID")

        with self._lead_lock:
            conn = self._lead_connection()
            try:
                duplicate = conn.execute(
                    "SELECT id FROM accounts WHERE platform = ? AND name = ? AND id <> ?",
                    (platform, nickname, account_id),
                ).fetchone()
                if duplicate:
                    raise RuntimeError(f"该平台账号已绑定到账号记录 #{int(duplicate['id'])}")
                conn.execute(
                    "UPDATE accounts SET name = ?, bb_window_id = ?, platform = ? WHERE id = ?",
                    (nickname, window_id, platform, account_id),
                )
                conn.commit()
            finally:
                conn.close()
        self._on_scheduler_log(f"✅ 已读取并绑定账号：{platform}·{nickname}（窗口 {window_id}）")
        return {"account_id": account_id, "name": nickname, "platform": platform, "window_id": window_id}

    def _add_leads_to_interaction(self, args: Mapping[str, Any]) -> dict[str, Any]:
        try:
            from .interactions.repository import InteractionRepository  # type: ignore
            from .interactions.service import InteractionService  # type: ignore
            from .leads.repository import LeadRepository  # type: ignore
        except ImportError:  # pragma: no cover
            from interactions.repository import InteractionRepository  # type: ignore
            from interactions.service import InteractionService  # type: ignore
            from leads.repository import LeadRepository  # type: ignore

        interaction_type = str(args.get("interaction_type") or "comment_reply").strip().lower()
        if interaction_type not in {"comment_reply", "private_message"}:
            raise ValueError("不支持的互动类型")
        raw_ids = args.get("lead_ids")
        if not isinstance(raw_ids, list):
            raise ValueError("lead_ids 必须是数组")
        lead_ids = []
        for raw_id in raw_ids:
            try:
                lead_id = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("lead_ids 只能包含整数") from exc
            if lead_id > 0 and lead_id not in lead_ids:
                lead_ids.append(lead_id)
        if not lead_ids:
            return {"added": [], "existing": [], "failed": []}

        added = []
        existing = []
        failed = []
        open_statuses = {"draft", "pending_review", "approved", "queued"}
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                lead_repo = LeadRepository(conn)
                interaction_repo = InteractionRepository(conn)
                service = InteractionService(
                    interaction_repo,
                    lead_repo,
                    config={},
                )
                for lead_id in lead_ids:
                    try:
                        current = interaction_repo.list_drafts(
                            lead_id=lead_id, page=1, page_size=20
                        )
                        if any(item.status in open_statuses
                               and getattr(item, "interaction_type", "comment_reply") == interaction_type
                               for item in current.get("items", [])):
                            existing.append({"lead_id": lead_id})
                            continue
                        draft_id = service.create_draft(
                            lead_id,
                            template_id="greeting",
                            generation_mode="manual-selection",
                            manual_override=True,
                            interaction_type=interaction_type,
                        )
                        added.append({"lead_id": lead_id, "draft_id": int(draft_id)})
                    except Exception as exc:  # noqa: BLE001
                        failed.append({"lead_id": lead_id, "reason": str(exc)})
                return {"added": added, "existing": existing, "failed": failed}
            finally:
                conn.close()

    def _add_leads_to_private_message(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """私信入口与评论回复共用导入流程，但持久化为独立互动类型。"""
        payload = dict(args or {})
        payload["interaction_type"] = "private_message"
        return self._add_leads_to_interaction(payload)

    def _lead_ids_arg(self, args: Mapping[str, Any]) -> list[int]:
        raw_ids = args.get("lead_ids")
        if not isinstance(raw_ids, list):
            raise ValueError("lead_ids 必须是数组")
        ids = []
        for raw_id in raw_ids:
            try:
                lead_id = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("lead_ids 只能包含整数") from exc
            if lead_id > 0 and lead_id not in ids:
                ids.append(lead_id)
        return ids

    def _export_leads(self, args: Mapping[str, Any]) -> dict[str, Any]:
        try:
            from .leads.exporter import LeadExporter  # type: ignore
            from .leads.repository import LeadRepository  # type: ignore
        except ImportError:  # pragma: no cover
            from leads.exporter import LeadExporter  # type: ignore
            from leads.repository import LeadRepository  # type: ignore
        lead_ids = self._lead_ids_arg(args)
        if not lead_ids:
            raise ValueError("请先选择要导出的线索")
        export_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(self.scheduler.db_path))),
            "data", "exports",
        )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                path = LeadExporter(LeadRepository(conn)).export(
                    lead_ids, export_dir, filename=f"线索导出_{stamp}.csv"
                )
            finally:
                conn.close()
        log = getattr(self.scheduler, "_emit_log", None)
        if callable(log):
            log(f"线索导出完成：{len(lead_ids)} 条，文件：{path}")
        return {"path": path, "count": len(lead_ids)}

    def _lead_ids_for_export_filters(self, args: Mapping[str, Any], client=None) -> list[int]:
        """按线索中心当前筛选条件取全量 ID，不受当前分页限制。"""
        try:
            from .leads.repository import LeadQuery, LeadRepository  # type: ignore
            from .leads.service import LeadService  # type: ignore
        except ImportError:  # pragma: no cover
            from leads.repository import LeadQuery, LeadRepository  # type: ignore
            from leads.service import LeadService  # type: ignore
        task_id = self._int_arg(args, "task_id", required=False)
        if task_id is not None and task_id <= 0:
            task_id = None
        query = LeadQuery(
            page=1, page_size=500, task_id=task_id,
            platform=self._optional_text(args, "platform"),
            province=self._optional_text(args, "province"),
            intent_level=self._optional_text(args, "intent_level"),
            keyword=self._optional_text(args, "keyword"),
            data_owner_user_id=self._owner_user_id(client),
            sort_by="updated_at", sort_order="desc",
        )
        ids: list[int] = []
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                service = LeadService(LeadRepository(conn), config={})
                page = 1
                while True:
                    query.page = page
                    result = service.list_leads(query)
                    ids.extend(int(item.id) for item in result.items if int(item.id) > 0)
                    if page * query.page_size >= int(result.total):
                        break
                    page += 1
            finally:
                conn.close()
        return list(dict.fromkeys(ids))

    def _export_leads_by_task(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        """将线索按来源任务拆分导出，包含评论、意向、地区和原作地址。"""
        raw_ids = args.get("lead_ids")
        if not bool(args.get("all_filtered")) and isinstance(raw_ids, list) and raw_ids:
            lead_ids = self._lead_ids_arg(args)
        else:
            lead_ids = self._lead_ids_for_export_filters(args, client)
        if not lead_ids:
            raise ValueError("当前筛选条件下没有可导出的线索")

        placeholders = ",".join("?" for _ in lead_ids)
        sql = f"""
            SELECT l.id AS lead_id, l.platform, l.platform_user_id, l.nickname,
                   l.region_province, l.region_city, l.region_source,
                   l.intent_level, l.intent_score, l.intent_reasons,
                   l.pool, l.status, l.owner_id, o.name AS owner_name,
                   l.last_interaction_at, l.note,
                   COALESCE(e.task_id, v.task_id, 0) AS task_id,
                   t.keyword AS task_keyword,
                   COALESCE(NULLIF(d.source_content, ''), c.content,
                            e.evidence_text, '') AS comment_content,
                   COALESCE(e.occurred_at, c.comment_time, '') AS comment_time,
                   COALESCE(NULLIF(d.source_video_url, ''), v.url, '') AS source_url,
                   CASE WHEN EXISTS (
                     SELECT 1 FROM interaction_drafts d2
                      WHERE d2.lead_id = l.id
                        AND d2.status NOT IN ('cancelled', 'rejected')
                   ) THEN '是' ELSE '否' END AS in_interaction
              FROM leads l
              LEFT JOIN owners o ON o.id = l.owner_id
              LEFT JOIN lead_evidence e ON e.id = (
                SELECT e2.id FROM lead_evidence e2
                 WHERE e2.lead_id = l.id
                 ORDER BY COALESCE(e2.occurred_at, e2.collected_at) DESC, e2.id DESC
                 LIMIT 1
              )
              LEFT JOIN comments c ON c.id = COALESCE(e.comment_id, l.source_comment_id)
              LEFT JOIN videos v ON v.id = COALESCE(e.video_id, c.video_id)
              LEFT JOIN tasks t ON t.id = COALESCE(e.task_id, v.task_id)
              LEFT JOIN interaction_drafts d ON d.id = (
                SELECT d2.id FROM interaction_drafts d2
                 WHERE d2.lead_id = l.id AND d2.status NOT IN ('cancelled', 'rejected')
                 ORDER BY d2.updated_at DESC, d2.id DESC LIMIT 1
              )
             WHERE l.id IN ({placeholders})
             ORDER BY COALESCE(t.id, 0), l.id
        """
        headers = [
            "任务编号", "任务关键词", "平台", "昵称", "用户ID", "评论原文",
            "评论时间", "原作地址", "地区", "地区城市", "地区来源", "意向等级",
            "意向分数", "意向原因", "线索池", "线索状态", "负责人", "最后互动时间",
            "是否已进入互动中心", "备注",
        ]
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                rows = [dict(row) for row in conn.execute(sql, lead_ids).fetchall()]
            finally:
                conn.close()
        groups: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(int(row.get("task_id") or 0), []).append(row)
        export_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(self.scheduler.db_path))),
            "data", "exports",
        )
        os.makedirs(export_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        paths = []
        for task_id, group in groups.items():
            label = f"任务{task_id}" if task_id > 0 else "未关联任务"
            path = os.path.join(export_dir, f"线索导出_{label}_{stamp}.csv")
            with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=headers)
                writer.writeheader()
                for row in group:
                    try:
                        reasons = json.loads(row.get("intent_reasons") or "[]")
                    except (TypeError, ValueError):
                        reasons = row.get("intent_reasons") or ""
                    writer.writerow({
                        "任务编号": row.get("task_id") or "",
                        "任务关键词": row.get("task_keyword") or "",
                        "平台": self._platform_label(row.get("platform") or ""),
                        "昵称": row.get("nickname") or "",
                        "用户ID": row.get("platform_user_id") or "",
                        "评论原文": row.get("comment_content") or "",
                        "评论时间": row.get("comment_time") or "",
                        "原作地址": row.get("source_url") or "",
                        "地区": row.get("region_province") or "",
                        "地区城市": row.get("region_city") or "",
                        "地区来源": row.get("region_source") or "",
                        "意向等级": row.get("intent_level") or "",
                        "意向分数": row.get("intent_score") or 0,
                        "意向原因": "、".join(str(item) for item in reasons) if isinstance(reasons, list) else reasons,
                        "线索池": row.get("pool") or "",
                        "线索状态": row.get("status") or "",
                        "负责人": row.get("owner_name") or row.get("owner_id") or "",
                        "最后互动时间": row.get("last_interaction_at") or "",
                        "是否已进入互动中心": row.get("in_interaction") or "否",
                        "备注": row.get("note") or "",
                    })
            paths.append(path)
        self._on_scheduler_log(
            f"线索按任务导出完成：{len(lead_ids)} 条，{len(paths)} 个任务文件"
        )
        return {"paths": paths, "count": len(lead_ids), "task_count": len(paths)}

    def _tag_leads(self, args: Mapping[str, Any]) -> dict[str, Any]:
        try:
            from .leads.repository import LeadRepository  # type: ignore
        except ImportError:  # pragma: no cover
            from leads.repository import LeadRepository  # type: ignore
        lead_ids = self._lead_ids_arg(args)
        tag = str(args.get("tag") or "").strip()
        if not lead_ids:
            raise ValueError("请先选择要打标签的线索")
        if not tag or len(tag) > 80:
            raise ValueError("标签不能为空且不能超过80个字符")
        updated = 0
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                repo = LeadRepository(conn)
                for lead_id in lead_ids:
                    lead = repo.get(lead_id)
                    if not lead:
                        continue
                    note = str(lead.get("note") or "")
                    lines = [line for line in note.splitlines()
                             if not line.startswith("人工标签：")]
                    lines.insert(0, f"人工标签：{tag}")
                    repo.update_fields(lead_id, {"note": "\n".join(lines)})
                    updated += 1
            finally:
                conn.close()
        log = getattr(self.scheduler, "_emit_log", None)
        if callable(log):
            log(f"线索批量标签完成：{updated} 条，标签：{tag}")
        return {"updated": updated, "tag": tag}

    @staticmethod
    def _draft_row(item) -> dict[str, Any]:
        lead = item.lead or {}
        interaction_type = str(getattr(item, "interaction_type", "comment_reply") or "comment_reply")
        return {
            "id": int(item.id),
            "draft_id": int(item.id),
            "lead_id": int(item.lead_id),
            "channel": str(item.channel or "unknown"),
            "interaction_type": interaction_type,
            "interaction_type_label": {
                "comment_reply": "评论回复",
                "private_message": "发私信",
            }.get(interaction_type, interaction_type),
            "platform": str(lead.get("platform") or item.channel or "unknown"),
            "nickname": lead.get("nickname") or "匿名用户",
            "platform_user_id": lead.get("platform_user_id") or "",
            "profile_url": lead.get("profile_url") or "",
            "original_comment": item.source_content or "暂无评论原文",
            "content": item.content or "",
            "status": item.status or "draft",
            "template_id": item.template_id or "",
            "generation_mode": item.generation_mode or "manual",
            "comment_time": lead.get("last_interaction_at") or "暂无评论时间",
            "region_province": lead.get("region_province") or "",
            "intent_level": lead.get("intent_level") or "unknown",
            "reply_account_id": item.reply_account_id,
            "reply_account_name": item.reply_account_name or "",
            "reply_account_platform": item.reply_account_platform or "",
            "failure_reason": item.failure_reason or "",
            "task_id": int(item.task_id or 0),
            "reply_status": item.status or "draft",
            "reply_status_label": {
                "draft": "待生成", "queued": "待发送", "sent": "已回复",
                "replied": "已回复", "failed": "失败",
            }.get(item.status or "draft", item.status or "draft"),
            "customer_replied": bool(item.customer_replied),
            "customer_replied_label": "是" if item.customer_replied else "否",
            "created_at": item.created_at or "",
            "updated_at": item.updated_at or "",
        }

    def _interaction_service(self, conn):
        try:
            from .interactions.repository import InteractionRepository  # type: ignore
            from .interactions.service import InteractionService  # type: ignore
            from .leads.repository import LeadRepository  # type: ignore
        except ImportError:  # pragma: no cover
            from interactions.repository import InteractionRepository  # type: ignore
            from interactions.service import InteractionService  # type: ignore
            from leads.repository import LeadRepository  # type: ignore

        reply_adapter = None
        private_message_adapter = None
        browser = getattr(self.scheduler, "bb", None)
        if browser is not None:
            try:
                try:
                    from .interactions.browser_reply import BitBrowserReplyAdapter  # type: ignore
                except ImportError:  # pragma: no cover
                    from interactions.browser_reply import BitBrowserReplyAdapter  # type: ignore
                reply_adapter = BitBrowserReplyAdapter(browser)
                try:
                    from .interactions.private_message import BitBrowserPrivateMessageAdapter  # type: ignore
                except ImportError:  # pragma: no cover
                    from interactions.private_message import BitBrowserPrivateMessageAdapter  # type: ignore
                private_message_adapter = BitBrowserPrivateMessageAdapter(browser)
            except Exception:
                # 列表和草稿编辑不应因浏览器适配器初始化失败而不可用。
                reply_adapter = None
                private_message_adapter = None
        return InteractionService(
            InteractionRepository(conn),
            LeadRepository(conn),
            reply_adapter=reply_adapter,
            private_message_adapter=private_message_adapter,
            config={},
        )

    def _publishing_service(self, conn):
        try:
            from .publishing.service import PublishingService  # type: ignore
        except ImportError:  # pragma: no cover
            from publishing.service import PublishingService  # type: ignore
        return PublishingService(conn)

    def _list_publish_drafts(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        page = max(1, self._int_arg(args, "page", required=False) or 1)
        page_size = min(100, max(1, self._int_arg(args, "page_size", required=False) or 50))
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                return self._publishing_service(conn).list_drafts(
                    status=str(args.get("status") or "all"),
                    platform=str(args.get("platform") or ""),
                    keyword=str(args.get("keyword") or ""),
                    page=page,
                    page_size=page_size,
                    owner_user_id=self._owner_user_id(client),
                )
            finally:
                conn.close()

    def _create_publish_draft(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        raw_platforms = args.get("platforms")
        if not isinstance(raw_platforms, list):
            raw_platforms = ["douyin", "xhs", "bilibili", "weibo"]
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                draft_id = self._publishing_service(conn).create_draft(
                    title=str(args.get("title") or ""),
                    body=str(args.get("body") or ""),
                    content_type=str(args.get("content_type") or "text"),
                    platforms=raw_platforms,
                    owner_user_id=self._owner_user_id(client),
                )
                self._set_owner_and_queue("publish_drafts", int(draft_id), client, "publish_draft")
                return {"draft_id": draft_id}
            finally:
                conn.close()

    def _update_publish_variant(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        draft_id = self._int_arg(args, "draft_id")
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                self._publishing_service(conn).update_variant(
                    draft_id=draft_id,
                    platform=str(args.get("platform") or ""),
                    title=str(args.get("title") or ""),
                    body=str(args.get("body") or ""),
                    topics=args.get("topics") if isinstance(args.get("topics"), list) else [],
                    content_type=str(args.get("content_type") or "text"),
                )
                self._queue_entity("publish_drafts", draft_id, client, "publish_draft")
                return {"draft_id": draft_id, "platform": str(args.get("platform") or "")}
            finally:
                conn.close()

    def _publish_draft_action(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        draft_id = self._int_arg(args, "draft_id")
        action = str(args.get("action") or "").strip().lower()
        status_by_action = {
            "submit_review": "review",
            "approve": "approved",
            "queue": "queued",
            "cancel": "cancelled",
            "restore": "draft",
        }
        if action not in status_by_action:
            raise ValueError("不支持的发布草稿操作")
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                status = status_by_action[action]
                self._publishing_service(conn).change_status(draft_id, status)
                self._queue_entity("publish_drafts", draft_id, client, "publish_draft")
                return {"draft_id": draft_id, "action": action, "status": status}
            finally:
                conn.close()

    def _delete_publish_draft(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        draft_id = self._int_arg(args, "draft_id")
        draft_snapshot = None
        if self._owner_user_id(client) is not None:
            draft_snapshot = self._require_owned_row(
                "publish_drafts", draft_id, "owner_user_id", client, "发布草稿"
            )
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                self._publishing_service(conn).delete_draft(draft_id)
                deleted = True
            finally:
                conn.close()
        if deleted and draft_snapshot is not None:
            self._queue_deleted_snapshot(
                "publish_drafts", draft_id, client, "publish_draft", draft_snapshot
            )
        return {"draft_id": draft_id, "deleted": deleted}

    def _add_publish_assets(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        draft_id = self._int_arg(args, "draft_id")
        raw_assets = args.get("assets")
        if not isinstance(raw_assets, list):
            raise ValueError("素材列表无效")
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                asset_ids = self._publishing_service(conn).add_assets(
                    draft_id, raw_assets
                )
                self._queue_entity("publish_drafts", draft_id, client, "publish_draft")
                return {"draft_id": draft_id, "asset_ids": asset_ids}
            finally:
                conn.close()

    def _delete_publish_asset(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        draft_id = self._int_arg(args, "draft_id")
        asset_id = self._int_arg(args, "asset_id")
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                self._publishing_service(conn).delete_asset(draft_id, asset_id)
                self._queue_entity("publish_drafts", draft_id, client, "publish_draft")
                return {"draft_id": draft_id, "asset_id": asset_id, "deleted": True}
            finally:
                conn.close()

    def _preview_publish_draft(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """打开选定账号的创作页并填入内容；绝不执行发布提交。"""
        draft_id = self._int_arg(args, "draft_id")
        account_id = self._int_arg(args, "account_id")
        platform = str(args.get("platform") or "").strip().lower()
        if account_id <= 0:
            raise ValueError("请先选择发布账号")
        account = self._account_by_id(account_id)
        if str(account.get("platform") or "").strip().lower() != platform:
            raise ValueError("发布账号与平台不匹配")
        browser = getattr(self.scheduler, "bb", None)
        if browser is None:
            raise RuntimeError("BitBrowser 客户端未配置")
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                service = self._publishing_service(conn)
                self._apply_publish_editor_overrides(service, draft_id, platform, args)
                variant = service.get_variant(draft_id, platform)
            finally:
                conn.close()
        try:
            try:
                from .publishing.browser import PublishingBrowserAdapter  # type: ignore
            except ImportError:  # pragma: no cover
                from publishing.browser import PublishingBrowserAdapter  # type: ignore
            result = PublishingBrowserAdapter(browser).preview_fill(
                account=account,
                platform=platform,
                content_type=str(variant.get("content_type") or "text"),
                title=self._variant_text(variant, "title", "source_title"),
                body=self._variant_text(variant, "body", "source_content"),
                topics=list(variant.get("topics") or []),
                assets=list(variant.get("assets") or []),
            )
        except Exception as exc:
            self._on_scheduler_log(
                f"发布预览填入失败：草稿#{draft_id} · {platform} · 账号#{account_id} · {exc}"
            )
            raise
        self._on_scheduler_log(
            f"发布预览填入完成：草稿#{draft_id} · {platform} · 账号#{account_id} · "
            f"{self._publish_verification_summary(result)} · 未点击发送"
        )
        return {
            "draft_id": draft_id,
            "account_id": account_id,
            "platform": platform,
            "send_clicked": False,
            "result": result,
        }

    def _real_publish_draft(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """执行发布中心的真实发布；必须经过界面二次确认。"""
        if not bool(args.get("confirm_real_publish", False)):
            raise ValueError("真实发布必须先完成确认")
        draft_id = self._int_arg(args, "draft_id")
        account_id = self._int_arg(args, "account_id")
        platform = str(args.get("platform") or "").strip().lower()
        if account_id <= 0:
            raise ValueError("请先选择发布账号")
        account = self._account_by_id(account_id)
        if str(account.get("platform") or "").strip().lower() != platform:
            raise ValueError("发布账号与平台不匹配")
        browser = getattr(self.scheduler, "bb", None)
        if browser is None:
            raise RuntimeError("BitBrowser 客户端未配置")
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                service = self._publishing_service(conn)
                self._apply_publish_editor_overrides(service, draft_id, platform, args)
                variant = service.get_variant(draft_id, platform)
                if str(variant.get("draft_status") or "") not in {"approved", "queued"}:
                    raise ValueError("请先提交审核并批准，或将内容加入待发布队列")
            finally:
                conn.close()
        try:
            try:
                from .publishing.browser import PublishingBrowserAdapter  # type: ignore
            except ImportError:  # pragma: no cover
                from publishing.browser import PublishingBrowserAdapter  # type: ignore
            def publish_log(message: str) -> None:
                self._on_scheduler_log(
                    f"真实发布：草稿#{draft_id} · {platform} · 账号#{account_id} · {message}"
                )

            result = PublishingBrowserAdapter(browser, on_log=publish_log).real_publish(
                account=account,
                platform=platform,
                content_type=str(variant.get("content_type") or "text"),
                title=self._variant_text(variant, "title", "source_title"),
                body=self._variant_text(variant, "body", "source_content"),
                topics=list(variant.get("topics") or []),
                assets=list(variant.get("assets") or []),
            )
            self._on_scheduler_log(
                f"真实发布填入核对：草稿#{draft_id} · {platform} · 账号#{account_id} · "
                f"{self._publish_verification_summary(result)}"
            )
        except Exception as exc:
            self._on_scheduler_log(
                f"真实发布失败：草稿#{draft_id} · {platform} · 账号#{account_id} · {exc}"
            )
            raise
        if not bool(result.get("send_clicked")):
            submission = result.get("submission") or {}
            status_text = "真实发布未确认" if submission.get("clicked") else "真实发布未执行"
            self._on_scheduler_log(
                f"{status_text}：草稿#{draft_id} · {platform} · 账号#{account_id} · "
                f"{result.get('message') or '未找到发布按钮'}"
            )
            return {
                "draft_id": draft_id, "account_id": account_id, "platform": platform,
                "published": False, "send_clicked": False, "result": result,
            }
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                self._publishing_service(conn).change_status(draft_id, "published")
            finally:
                conn.close()
        self._on_scheduler_log(
            f"真实发布已执行：草稿#{draft_id} · {platform} · 账号#{account_id} · "
            f"点击 {result.get('submission', {}).get('label') or '发布按钮'}"
        )
        return {
            "draft_id": draft_id, "account_id": account_id, "platform": platform,
            "published": True, "send_clicked": True, "result": result,
        }

    def _workspace_service(self, conn):
        try:
            from .publishing.workspace import PublishingWorkspaceService  # type: ignore
        except ImportError:  # pragma: no cover
            from publishing.workspace import PublishingWorkspaceService  # type: ignore
        return PublishingWorkspaceService(conn)

    def _list_account_contents(self, args: Mapping[str, Any]) -> dict[str, Any]:
        account_id = self._int_arg(args, "account_id")
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                return self._workspace_service(conn).list_account_contents(
                    account_id, str(args.get("platform") or ""),
                    str(args.get("content_type") or ""),
                    self._int_arg(args, "page", required=False) or 1,
                    self._int_arg(args, "page_size", required=False) or 30,
                )
            finally:
                conn.close()

    def _account_content_sync_lock(self, account_id: int) -> threading.Lock:
        with self._account_content_sync_locks_guard:
            return self._account_content_sync_locks.setdefault(account_id, threading.Lock())

    def _sync_account_contents(self, args: Mapping[str, Any]) -> dict[str, Any]:
        account_id = self._int_arg(args, "account_id")
        # QML 的初次加载、账号切换和用户手动点击可能在相邻时刻同时
        # 发起同步。重复请求直接返回当前缓存，不清空已显示的内容。
        lock = self._account_content_sync_lock(account_id)
        if not lock.acquire(blocking=False):
            with self._lead_lock:
                conn = self._lead_connection()
                try:
                    account = self._account_by_id(account_id)
                    platform = str(args.get("platform") or account.get("platform") or "").strip().lower()
                    result = self._workspace_service(conn).list_account_contents(
                        account_id, platform, page=1, page_size=30
                    )
                finally:
                    conn.close()
            result.update({
                "synced": 0,
                "sync_source": "busy",
                "sync_status": "busy",
                "sync_error": "该账号主页同步正在进行，请等待当前同步完成",
                "read_only": True,
            })
            self._on_scheduler_log(
                f"发布工作区账号主页同步跳过重复请求：账号#{account_id}"
            )
            return result
        try:
            return self._sync_account_contents_impl(args)
        finally:
            lock.release()

    def _sync_account_contents_impl(self, args: Mapping[str, Any]) -> dict[str, Any]:
        account_id = self._int_arg(args, "account_id")
        account = self._account_by_id(account_id)
        platform = str(args.get("platform") or account.get("platform") or "").strip().lower()
        limit = self._int_arg(args, "limit", required=False) or 100
        browser = getattr(self.scheduler, "bb", None)
        window_id = str(account.get("bb_window_id") or "").strip()
        live_result: dict[str, Any] = {}
        live_error = ""
        live_attempted = False

        # 账号信息必须优先走账号主页只读通路。旧版本这里直接把“分配给
        # 账号采集”的视频当成账号作品，导致新账号看不到自己的主页内容，
        # 或者看到不属于自己的作品。外部 Chrome 会话仍保持安全回退，
        # 不尝试控制用户手动打开的浏览器。
        if browser is not None and window_id and window_id not in {"chrome", "chrome-bilibili"}:
            live_attempted = True
            try:
                try:
                    from .publishing.account_content_reader import (  # type: ignore
                        ACCOUNT_CONTENT_READER_VERSION,
                        read_account_contents,
                    )
                except ImportError:  # pragma: no cover
                    from publishing.account_content_reader import (  # type: ignore
                        ACCOUNT_CONTENT_READER_VERSION,
                        read_account_contents,
                    )
                live_result = read_account_contents(
                    browser, window_id, platform, limit=max(1, min(500, int(limit)))
                ) or {}
                live_items = live_result.get("items") if isinstance(live_result, Mapping) else []
                diagnostics = live_result.get("diagnostics") if isinstance(live_result, Mapping) else {}
                if not isinstance(diagnostics, Mapping):
                    diagnostics = {}
                self._on_scheduler_log(
                    f"发布工作区账号主页同步诊断：账号#{account_id} · "
                    f"{self._platform_label(platform)} · 窗口 {window_id} · "
                    f"解析器 {live_result.get('reader_version') or ACCOUNT_CONTENT_READER_VERSION} · "
                    f"路由 {diagnostics.get('page_url') or live_result.get('profile_url') or '未知'} · "
                    f"作品区 {diagnostics.get('scope_selector') or '未知'}="
                    f"{('已找到' if diagnostics.get('scope_found') is True else '未确认')} · "
                    f"候选 {diagnostics.get('raw_unique', len(live_items) if isinstance(live_items, list) else 0)} · "
                    f"返回 {diagnostics.get('returned', len(live_items) if isinstance(live_items, list) else 0)} · "
                    f"轮次 {diagnostics.get('read_rounds', '未知')}"
                )
                if bool(live_result.get("ok")) and isinstance(live_items, list) and live_items:
                    with self._lead_lock:
                        conn = self._lead_connection()
                        try:
                            service = self._workspace_service(conn)
                            count = service.replace_profile_contents(
                                account_id, platform, live_items,
                                profile_url=str(live_result.get("profile_url") or ""),
                            )
                            result = service.list_account_contents(
                                account_id, platform, page=1, page_size=30
                            )
                        finally:
                            conn.close()
                    result.update({
                        "synced": count,
                        "sync_source": "profile_sync",
                        "sync_status": "success",
                        "sync_error": "",
                        "profile_url": str(live_result.get("profile_url") or ""),
                        "reader_version": str(live_result.get("reader_version") or ACCOUNT_CONTENT_READER_VERSION),
                        "sync_reason_code": str(live_result.get("reason_code") or "profile_read_success"),
                        "read_only": True,
                    })
                    self._on_scheduler_log(
                        f"发布工作区账号主页读取完成：账号#{account_id} · "
                        f"{self._platform_label(platform)} · 读取 {count} 条作品"
                    )
                    return result
                live_error = str(live_result.get("reason") or "主页未读取到作品")
                if live_result.get("logged_in") is False:
                    live_error = "账号主页未确认登录，未写入主页作品"
            except Exception as exc:
                live_error = f"账号主页读取失败：{type(exc).__name__}: {exc}"
                self._on_scheduler_log(
                    f"发布工作区账号主页读取失败：账号#{account_id} · "
                    f"{self._platform_label(platform)} · {live_error}"
                )

            # 真实浏览器同步只允许使用明确读取到的账号主页作品。推荐页、
            # 未登录页或主页尚未加载时，不能回退到本地采集缓存来伪装成
            # “账号自己的作品”；否则尤其容易把抖音推荐栏写入账号信息页。
            if live_attempted:
                with self._lead_lock:
                    conn = self._lead_connection()
                    try:
                        result = self._workspace_service(conn).list_account_contents(
                            account_id, platform, page=1, page_size=30
                        )
                    finally:
                        conn.close()
                cached_items = result.get("items") if isinstance(result, Mapping) else []
                result.update({
                    "synced": 0,
                    "sync_source": "profile_sync",
                    "sync_status": "failed" if live_error else "empty",
                    "sync_error": live_error or f"{self._platform_label(platform)}账号主页未读取到本人作品",
                    "profile_url": str(live_result.get("profile_url") or ""),
                    "reader_version": str(live_result.get("reader_version") or ACCOUNT_CONTENT_READER_VERSION),
                    "sync_reason_code": str(live_result.get("reason_code") or "profile_empty"),
                    "read_only": True,
                })
                self._on_scheduler_log(
                    f"发布工作区账号主页同步未写入：账号#{account_id} · "
                    f"{self._platform_label(platform)} · {result['sync_error']} · "
                    f"保留已有主页缓存 {len(cached_items) if isinstance(cached_items, list) else 0} 条"
                )
                return result

        # 兼容没有 BitBrowser、外部 Chrome 或主页暂时不可读的情况：只
        # 回退到“作品作者=账号昵称”的本地缓存，绝不使用 assigned_account。
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                service = self._workspace_service(conn)
                # 主页刚刚同步过但本次网络/登录态暂时失败时，保留上一份
                # 主页缓存；不能让一次短暂失败触发 seed 的清空逻辑。
                cached = service.list_account_contents(account_id, platform, page=1, page_size=30)
                if live_error and int(cached.get("total") or 0) > 0:
                    count = 0
                    result = cached
                    fallback_source = str(cached.get("items", [{}])[0].get("extra", {}).get("__account_content_origin") or "profile_sync")
                else:
                    count = service.seed_account_contents(account_id, platform, limit)
                    result = service.list_account_contents(account_id, platform, page=1, page_size=30)
                    fallback_source = "author_verified_collected" if count else "profile_sync"
                result.update({
                    "synced": count,
                    "sync_source": fallback_source,
                    "sync_status": "fallback" if live_error else "local_only",
                    "sync_error": live_error,
                    "profile_url": str(live_result.get("profile_url") or ""),
                    "read_only": True,
                })
            finally:
                conn.close()
        self._on_scheduler_log(
            f"发布工作区账号作品同步：账号#{account_id} · {self._platform_label(platform)} · "
            f"本地作者匹配回退 {count} 条" + (f" · {live_error}" if live_error else "")
        )
        return result

    def _list_content_comments(self, args: Mapping[str, Any]) -> dict[str, Any]:
        content_id = self._int_arg(args, "account_content_id")
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                return self._workspace_service(conn).list_content_comments(
                    content_id, self._int_arg(args, "page", required=False) or 1,
                    self._int_arg(args, "page_size", required=False) or 50,
                )
            finally:
                conn.close()

    def _list_generated_contents(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                return self._workspace_service(conn).list_generated(
                    self._int_arg(args, "page", required=False) or 1,
                    self._int_arg(args, "page_size", required=False) or 20,
                    self._owner_user_id(client),
                )
            finally:
                conn.close()

    def _generate_content(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        keyword = str(args.get("keyword") or "").strip()
        platform = str(args.get("platform") or "").strip().lower()
        source_ref = str(args.get("source_ref") or "").strip()
        try:
            from .config_loader import AppConfig  # type: ignore
            from .llm_api import LLMApiClient  # type: ignore
        except ImportError:  # pragma: no cover - 顶层脚本入口
            from config_loader import AppConfig  # type: ignore
            from llm_api import LLMApiClient  # type: ignore

        llm_config = AppConfig().llm_api() or {}
        llm_enabled = bool(llm_config.get("enabled"))
        llm_configured = bool(
            str(llm_config.get("base_url") or "").strip()
            and str(llm_config.get("model") or "").strip()
        )
        if llm_enabled and llm_configured:
            try:
                generated = LLMApiClient.generate_content(
                    llm_config, keyword, platform, source_ref
                )
            except Exception as exc:
                generated = {
                    "healthy": False,
                    "status": "client_error",
                    "detail": f"智能 API 调用异常：{type(exc).__name__}",
                }
            if generated.get("healthy"):
                with self._lead_lock:
                    conn = self._lead_connection()
                    try:
                        item = self._workspace_service(conn).save_generated(
                            title=str(generated.get("title") or ""),
                            body=str(generated.get("body") or generated.get("final_text") or ""),
                            platform=platform,
                            source_ref=source_ref,
                            source_type="llm",
                            topics=list(generated.get("topics") or []),
                            outline=str(generated.get("outline") or ""),
                            score=generated.get("score") or {},
                            owner_user_id=self._owner_user_id(client),
                        )
                    finally:
                        conn.close()
                self._on_scheduler_log(
                    f"发布工作区内容生成完成：{item.get('title') or '未命名内容'} · 智能 API"
                )
                self._set_owner_and_queue("generated_contents", int(item["id"]), client, "generated_content")
                return item
            self._on_scheduler_log(
                f"发布工作区智能生成失败，已使用本地模板兜底：{generated.get('detail') or 'API 未返回有效内容'}"
            )
        elif llm_enabled:
            self._on_scheduler_log(
                "发布工作区智能 API 配置不完整，已使用本地模板兜底：请填写 API 地址和模型"
            )
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                item = self._workspace_service(conn).generate_local(
                    keyword, platform, source_ref
                )
            finally:
                conn.close()
        self._set_owner_and_queue("generated_contents", int(item["id"]), client, "generated_content")
        self._on_scheduler_log(
            f"发布工作区内容生成完成：{item.get('title') or '未命名内容'} · 本地模板兜底"
        )
        return item

    def _import_generated_content(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        generated_id = self._int_arg(args, "generated_id")
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                item = self._workspace_service(conn).get_generated(generated_id)
                draft_id = self._publishing_service(conn).create_draft(
                    title=str(item.get("title") or ""),
                    body=str(item.get("body") or ""),
                    content_type=str(args.get("content_type") or "text"),
                    platforms=list(item.get("platforms") or []),
                    owner_user_id=self._owner_user_id(client),
                )
            finally:
                conn.close()
        self._set_owner_and_queue("publish_drafts", int(draft_id), client, "publish_draft")
        self._on_scheduler_log(f"生成内容已导入发布草稿：生成内容#{generated_id} → 草稿#{draft_id}")
        return {"generated_id": generated_id, "draft_id": draft_id}

    def _schedule_publish(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        draft_id = self._int_arg(args, "draft_id")
        account_id = self._int_arg(args, "account_id")
        platform = str(args.get("platform") or "").strip().lower()
        scheduled_at = str(args.get("scheduled_at") or "").strip()
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                service = self._publishing_service(conn)
                self._apply_publish_editor_overrides(service, draft_id, platform, args)
                job_id = service.schedule_variant(
                    draft_id=draft_id, platform=platform, account_id=account_id,
                    scheduled_at=scheduled_at,
                )
                self._queue_entity("publish_drafts", draft_id, client, "publish_draft")
            finally:
                conn.close()
        self._on_scheduler_log(
            f"发布任务已加入队列：草稿#{draft_id} · {self._platform_label(platform)} · "
            f"账号#{account_id} · {'定时 ' + scheduled_at if scheduled_at else '立即准备'}"
        )
        return {"job_id": job_id, "draft_id": draft_id, "account_id": account_id,
                "platform": platform, "scheduled_at": scheduled_at, "send_clicked": False}

    def _list_published_messages(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                return self._workspace_service(conn).list_messages(
                    str(args.get("platform") or ""),
                    self._int_arg(args, "account_id", required=False) or 0,
                    bool(args.get("unread_only", False)),
                    self._int_arg(args, "page", required=False) or 1,
                    self._int_arg(args, "page_size", required=False) or 50,
                    str(args.get("message_type") or ""),
                    self._owner_user_id(client),
                )
            finally:
                conn.close()

    def _mark_published_message(self, args: Mapping[str, Any]) -> dict[str, Any]:
        message_id = self._int_arg(args, "message_id")
        read = bool(args.get("read", True))
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                self._workspace_service(conn).mark_message_read(message_id, read)
            finally:
                conn.close()
        return {"message_id": message_id, "read": read}

    def _message_sync_lock(self, account_id: int) -> threading.Lock:
        with self._message_sync_locks_guard:
            return self._message_sync_locks.setdefault(int(account_id), threading.Lock())

    def _sync_published_messages(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        """按账号读取四个平台消息入口的全部可见消息并幂等入库。"""
        requested_account = self._int_arg(args, "account_id", required=False) or 0
        requested_platform = str(args.get("platform") or "").strip().lower()
        limit = self._int_arg(args, "limit", required=False) or 500
        limit = max(1, min(5000, limit))
        report = self._scoped_status_report(client)
        account_rows = [dict(row) for row in (report.get("accounts") or {}).values()]
        if requested_account:
            account_rows = [row for row in account_rows if int(row.get("id") or 0) == requested_account]
        if requested_platform:
            account_rows = [row for row in account_rows if str(row.get("platform") or "").lower() == requested_platform]
        if requested_account and not account_rows:
            raise ValueError(f"账号不存在或平台不匹配: {requested_account}")

        try:
            from .publishing.message_reader import read_account_messages  # type: ignore
        except ImportError:  # pragma: no cover
            from publishing.message_reader import read_account_messages  # type: ignore

        browser = getattr(self.scheduler, "bb", None)
        summary: list[dict[str, Any]] = []
        errors: list[str] = []
        total_processed = 0
        total_inserted = 0
        total_updated = 0
        for account in account_rows:
            account_id = int(account.get("id") or 0)
            platform = str(account.get("platform") or "").strip().lower()
            name = str(account.get("name") or account_id)
            window_id = str(account.get("bb_window_id") or "").strip()
            item_summary = {
                "account_id": account_id, "account_name": name,
                "platform": platform, "platform_label": self._platform_label(platform),
                "processed": 0, "inserted": 0, "updated": 0, "status": "skipped",
            }
            if browser is None or not window_id or window_id in {"chrome", "chrome-bilibili"}:
                reason = "未绑定可控的 BitBrowser 窗口，无法读取消息中心"
                item_summary.update({"status": "failed", "error": reason})
                errors.append(f"{name}：{reason}")
                summary.append(item_summary)
                continue
            lock = self._message_sync_lock(account_id)
            if not lock.acquire(blocking=False):
                reason = "该账号消息同步正在进行"
                item_summary.update({"status": "busy", "error": reason})
                errors.append(f"{name}：{reason}")
                summary.append(item_summary)
                continue
            try:
                self._on_scheduler_log(
                    f"消息中心全量同步开始：账号#{account_id} {name} · {self._platform_label(platform)}"
                )
                result = read_account_messages(
                    browser, window_id, platform, limit=limit, account_id=account_id,
                ) or {}
                raw_items = result.get("items") if isinstance(result, Mapping) else []
                if not isinstance(raw_items, list):
                    raw_items = []
                with self._lead_lock:
                    conn = self._lead_connection()
                    try:
                        write_result = self._workspace_service(conn).upsert_messages(
                            account_id, platform, raw_items,
                        )
                    finally:
                        conn.close()
                item_summary.update({
                    "status": "success" if bool(result.get("ok", True)) else "partial",
                    "processed": int(write_result.get("processed") or 0),
                    "inserted": int(write_result.get("inserted") or 0),
                    "updated": int(write_result.get("updated") or 0),
                    "categories": list(result.get("categories") or []),
                    "errors": list(result.get("errors") or []),
                })
                if result.get("errors"):
                    item_summary["status"] = "partial"
                    errors.extend(f"{name}：{str(error)}" for error in result.get("errors") or [])
                total_processed += item_summary["processed"]
                total_inserted += item_summary["inserted"]
                total_updated += item_summary["updated"]
                summary.append(item_summary)
                self._on_scheduler_log(
                    f"消息中心全量同步完成：账号#{account_id} {name} · "
                    f"读取 {item_summary['processed']} 条，新增 {item_summary['inserted']} 条，"
                    f"更新 {item_summary['updated']} 条"
                )
            except Exception as exc:
                item_summary.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
                errors.append(f"{name}：{type(exc).__name__}: {exc}")
                summary.append(item_summary)
                self._on_scheduler_log(
                    f"消息中心全量同步失败：账号#{account_id} {name} · {type(exc).__name__}: {exc}"
                )
            finally:
                lock.release()
        return {
            "ok": not errors or any(item.get("status") in {"success", "partial"} for item in summary),
            "accounts": summary,
            "account_count": len(summary),
            "processed": total_processed,
            "inserted": total_inserted,
            "updated": total_updated,
            "errors": errors,
            "platform": requested_platform,
            "account_id": requested_account,
        }

    def _test_llm_api(self) -> dict[str, Any]:
        try:
            from .config_loader import AppConfig  # type: ignore
            from .llm_api import LLMApiClient  # type: ignore
        except ImportError:  # pragma: no cover
            from config_loader import AppConfig  # type: ignore
            from llm_api import LLMApiClient  # type: ignore
        config = AppConfig().llm_api() or {}
        if not bool(config.get("enabled")):
            raise ValueError("智能 API 未启用")
        result = LLMApiClient.check_connection(config)
        self._on_scheduler_log(f"发布工作区智能 API 测试：{result.get('message') or '已完成'}")
        return result

    @staticmethod
    def _interaction_statuses(status: str) -> list[str]:
        if status == "sent":
            return ["sent", "replied"]
        if status in {"draft", "queued", "failed"}:
            return [status]
        raise ValueError("互动状态必须是 draft、queued、sent 或 failed")

    def _list_interactions(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        page = max(1, self._int_arg(args, "page", required=False) or 1)
        page_size = min(100, max(1, self._int_arg(args, "page_size", required=False) or 50))
        status = str(args.get("status") or "draft").strip().lower()
        interaction_type = str(args.get("interaction_type") or "comment_reply").strip().lower()
        if interaction_type not in {"comment_reply", "private_message"}:
            raise ValueError("不支持的互动类型")
        platform = self._optional_text(args, "platform")
        account_id = self._int_arg(args, "account_id", required=False)
        if account_id is not None and account_id <= 0:
            account_id = None
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                service = self._interaction_service(conn)
                result = service.list_drafts(
                    statuses=self._interaction_statuses(status),
                    platform=platform,
                    account_id=account_id,
                    interaction_type=interaction_type,
                    data_owner_user_id=self._owner_user_id(client),
                    page=page,
                    page_size=page_size,
                )
                accounts = service.list_reply_accounts(
                    include_unavailable=status == "sent",
                    owner_user_id=self._owner_user_id(client),
                )
                templates = service.list_templates()
                custom_variables = service.list_custom_variables()
                return {
                    "items": [self._draft_row(item) for item in result["items"]],
                    "total": int(result["total"]),
                    "page": int(result["page"]),
                    "page_size": int(result["page_size"]),
                    "pages": (int(result["total"]) + int(result["page_size"]) - 1)
                    // int(result["page_size"]) if result["total"] else 0,
                    "status": status,
                    "interaction_type": interaction_type,
                    "accounts": accounts,
                    "templates": [
                        {"id": str(template_id), "label": self._template_label(template_id),
                         "content": str(content or "")}
                        for template_id, content in templates.items()
                    ],
                    "custom_variables": [
                        {"name": str(name), "value": str(value or "")}
                        for name, value in custom_variables.items()
                    ],
                }
            finally:
                conn.close()

    def _export_interactions_by_task(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        """按来源任务导出互动全量记录，包含回复正文和客户后续互动。"""
        requested_task = self._int_arg(args, "task_id", required=False)
        if requested_task is not None and requested_task <= 0:
            requested_task = None
        requested_platform = self._optional_text(args, "platform")
        status = str(args.get("status") or "").strip().lower()
        status_values = None
        if status:
            status_values = self._interaction_statuses(status)
        where = ["d.status <> 'cancelled'"]
        params: list[Any] = []
        owner_id = self._owner_user_id(client)
        if owner_id is not None:
            where.append("l.data_owner_user_id = ?")
            params.append(owner_id)
        if requested_platform:
            where.append("l.platform = ?")
            params.append(requested_platform)
        if status_values:
            where.append("d.status IN (" + ",".join("?" for _ in status_values) + ")")
            params.extend(status_values)
        if requested_task is not None:
            where.append("COALESCE(e.task_id, v.task_id, 0) = ?")
            params.append(requested_task)
        sql = f"""
            SELECT d.id AS draft_id, d.lead_id, d.status, d.content,
                   COALESCE(NULLIF(d.interaction_type, ''), 'comment_reply') AS interaction_type,
                   d.created_at, d.updated_at, d.reply_account_id,
                   a.name AS reply_account_name,
                   l.platform, l.nickname, l.platform_user_id,
                   l.region_province, l.region_city, l.region_source,
                   l.intent_level, l.intent_score, l.status AS lead_status,
                   COALESCE(e.task_id, v.task_id, 0) AS task_id,
                   t.keyword AS task_keyword,
                   COALESCE(NULLIF(d.source_content, ''), c.content,
                            e.evidence_text, '') AS original_comment,
                   COALESCE(e.occurred_at, c.comment_time, '') AS comment_time,
                   COALESCE(NULLIF(d.source_video_url, ''), v.url, '') AS source_url,
                   (SELECT ie.detail FROM interaction_events ie
                     WHERE ie.draft_id = d.id AND ie.event_type = 'send_failed'
                     ORDER BY ie.created_at DESC, ie.id DESC LIMIT 1) AS failure_reason,
                   CASE WHEN EXISTS (
                     SELECT 1 FROM interaction_events ce
                      WHERE ce.draft_id = d.id
                        AND ce.event_type IN ('customer_reply', 'customer_replied')
                   ) OR EXISTS (
                     SELECT 1 FROM published_messages pm
                      WHERE pm.reply_draft_id = d.id
                        AND pm.message_type IN ('reply', 'comment')
                   ) THEN '是' ELSE '否' END AS customer_replied
              FROM interaction_drafts d
              LEFT JOIN leads l ON l.id = d.lead_id
              LEFT JOIN accounts a ON a.id = d.reply_account_id
              LEFT JOIN lead_evidence e ON e.id = (
                SELECT e2.id FROM lead_evidence e2
                 WHERE e2.lead_id = d.lead_id
                 ORDER BY COALESCE(e2.occurred_at, e2.collected_at) DESC, e2.id DESC
                 LIMIT 1
              )
              LEFT JOIN comments c ON c.id = COALESCE(e.comment_id, d.source_comment_id)
              LEFT JOIN videos v ON v.id = COALESCE(e.video_id, c.video_id, d.source_video_id)
              LEFT JOIN tasks t ON t.id = COALESCE(e.task_id, v.task_id)
             WHERE {" AND ".join(where)}
             ORDER BY COALESCE(t.id, 0), d.updated_at DESC, d.id DESC
        """
        headers = [
            "任务编号", "任务关键词", "草稿编号", "线索编号", "平台", "昵称", "用户ID",
            "评论原文", "评论时间", "原作地址", "地区", "地区城市", "地区来源",
            "意向等级", "意向分数", "线索状态", "互动方式", "互动标签", "回复话术", "回复状态",
            "回复账号", "客户是否回复", "失败原因", "创建时间", "更新时间",
        ]
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
            finally:
                conn.close()
        if not rows:
            raise ValueError("当前互动中心没有可导出的记录")
        status_labels = {
            "draft": "待生成", "queued": "待发送", "sent": "已回复",
            "replied": "已回复", "failed": "失败", "sending": "发送中",
        }
        groups: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(int(row.get("task_id") or 0), []).append(row)
        export_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(self.scheduler.db_path))),
            "data", "exports",
        )
        os.makedirs(export_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        paths = []
        for task_id, group in groups.items():
            label = f"任务{task_id}" if task_id > 0 else "未关联任务"
            path = os.path.join(export_dir, f"互动中心导出_{label}_{stamp}.csv")
            with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=headers)
                writer.writeheader()
                for row in group:
                    writer.writerow({
                        "任务编号": row.get("task_id") or "",
                        "任务关键词": row.get("task_keyword") or "",
                        "草稿编号": row.get("draft_id") or "",
                        "线索编号": row.get("lead_id") or "",
                        "平台": self._platform_label(row.get("platform") or ""),
                        "昵称": row.get("nickname") or "",
                        "用户ID": row.get("platform_user_id") or "",
                        "评论原文": row.get("original_comment") or "",
                        "评论时间": row.get("comment_time") or "",
                        "原作地址": row.get("source_url") or "",
                        "地区": row.get("region_province") or "",
                        "地区城市": row.get("region_city") or "",
                        "地区来源": row.get("region_source") or "",
                        "意向等级": row.get("intent_level") or "",
                        "意向分数": row.get("intent_score") or 0,
                        "线索状态": row.get("lead_status") or "",
                        "互动方式": {
                            "comment_reply": "评论回复",
                            "private_message": "发私信",
                        }.get(row.get("interaction_type") or "comment_reply", "未知"),
                        "互动标签": status_labels.get(row.get("status") or "", row.get("status") or ""),
                        "回复话术": row.get("content") or "",
                        "回复状态": status_labels.get(row.get("status") or "", row.get("status") or ""),
                        "回复账号": row.get("reply_account_name") or "",
                        "客户是否回复": row.get("customer_replied") or "否",
                        "失败原因": row.get("failure_reason") or "",
                        "创建时间": row.get("created_at") or "",
                        "更新时间": row.get("updated_at") or "",
                    })
            paths.append(path)
        self._on_scheduler_log(
            f"互动中心按任务导出完成：{len(rows)} 条，{len(paths)} 个任务文件"
        )
        return {"paths": paths, "count": len(rows), "task_count": len(paths)}

    def _interaction_action(self, args: Mapping[str, Any]) -> dict[str, Any]:
        draft_id = self._int_arg(args, "draft_id")
        action = str(args.get("action") or "").strip().lower()
        if action not in {
            "enter_send", "delete", "return_failed", "return_queued", "assign_account",
            "update_content", "apply_template",
        }:
            raise ValueError("不支持的互动操作")
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                service = self._interaction_service(conn)
                if action == "enter_send":
                    service.move_draft_to_send(draft_id, args.get("content"))
                elif action == "delete":
                    service.delete_draft(draft_id)
                elif action == "return_failed":
                    service.return_failed_to_draft(draft_id)
                elif action == "return_queued":
                    service.return_queued_to_draft(draft_id)
                elif action == "update_content":
                    service.update_draft_content(draft_id, args.get("content") or "")
                elif action == "apply_template":
                    template_id = str(args.get("template_id") or "").strip()
                    if not template_id:
                        raise ValueError("模板不能为空")
                    draft = service._repo.get_draft(draft_id)
                    if draft is None:
                        raise ValueError(f"草稿不存在: {draft_id}")
                    content = service.render_template_for_lead(template_id, draft["lead_id"])
                    service.update_draft_content(draft_id, content)
                else:
                    account_id = self._int_arg(args, "account_id")
                    service.assign_reply_account(draft_id, account_id)
                return {"draft_id": draft_id, "action": action, "ok": True}
            finally:
                conn.close()

    @staticmethod
    def _template_label(template_id: str) -> str:
        try:
            from .interactions.templates import template_label  # type: ignore
        except ImportError:  # pragma: no cover
            from interactions.templates import template_label  # type: ignore
        return str(template_label(template_id))

    def _save_template(self, args: Mapping[str, Any]) -> dict[str, Any]:
        template_id = str(args.get("template_id") or "").strip()
        content = str(args.get("content") or "")
        raw_variables = args.get("custom_variables")
        variables = raw_variables
        if isinstance(variables, list):
            variables = {
                str(item.get("name") or "").strip(): str(item.get("value") or "")
                for item in variables
                if isinstance(item, Mapping) and str(item.get("name") or "").strip()
            }
        if variables is not None and not isinstance(variables, Mapping):
            raise ValueError("自定义变量必须是对象")
        try:
            from .interactions.service import InteractionService  # type: ignore
            from .interactions.repository import InteractionRepository  # type: ignore
            from .leads.repository import LeadRepository  # type: ignore
        except ImportError:  # pragma: no cover
            from interactions.service import InteractionService  # type: ignore
            from interactions.repository import InteractionRepository  # type: ignore
            from leads.repository import LeadRepository  # type: ignore
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                service = InteractionService(
                    InteractionRepository(conn), LeadRepository(conn), config={}
                )
                service.save_template(
                    template_id, content,
                    None if variables is None else dict(variables),
                )
                return {
                    "saved": True,
                    "template_id": template_id,
                    "label": self._template_label(template_id),
                    "content": content.strip(),
                    "custom_variables": [
                        {"name": str(name), "value": str(value or "")}
                        for name, value in service.list_custom_variables().items()
                    ],
                }
            finally:
                conn.close()

    def _send_interactions(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raw_ids = args.get("draft_ids")
        if not isinstance(raw_ids, list):
            raise ValueError("draft_ids 必须是数组")
        draft_ids = []
        for raw_id in raw_ids:
            try:
                draft_id = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("draft_ids 只能包含整数") from exc
            if draft_id > 0 and draft_id not in draft_ids:
                draft_ids.append(draft_id)
        if not draft_ids:
            return {"results": []}
        account_id = self._int_arg(args, "account_id", required=False)
        real_send = bool(args.get("real_send", False))
        confirmed = bool(args.get("confirm_real_send", False))
        if real_send and not confirmed:
            raise ValueError("真实发送必须先完成确认")
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                service = self._interaction_service(conn)
                results = []
                for draft_id in draft_ids:
                    try:
                        draft = None
                        repo = getattr(service, "_repo", None)
                        if repo is not None and hasattr(repo, "get_draft"):
                            draft = repo.get_draft(draft_id)
                        interaction_type = str(
                            (draft or {}).get("interaction_type") or "comment_reply"
                        ).strip().lower()
                        if interaction_type == "private_message":
                            if real_send:
                                result = service.send_browser_private_message(
                                    draft_id, account_id, confirm=True,
                                    real_send_enabled=confirmed,
                                )
                            else:
                                result = service.simulate_private_message(draft_id, account_id)
                        elif real_send:
                            result = service.send_browser_reply(
                                draft_id, account_id, confirm=True,
                                real_send_enabled=confirmed,
                            )
                        else:
                            result = service.simulate_browser_reply(draft_id, account_id)
                        results.append({
                            "draft_id": draft_id,
                            "ok": bool(result.ok),
                            "stage": result.stage,
                            "message": result.message,
                            "verified": bool(result.verified),
                        })
                    except Exception as exc:  # noqa: BLE001
                        # 1.2 的批量回复在每条异常后都会落失败状态；2.0
                        # 不能只把异常塞进返回数组，否则内容会一直卡在
                        # 待发送池，用户也看不到失败原因。
                        message = str(exc)
                        try:
                            service.mark_reply_failed(draft_id, message, account_id)
                        except Exception:
                            # 原始异常优先返回；状态更新失败不应遮蔽真正原因。
                            pass
                        results.append({
                            "draft_id": draft_id,
                            "ok": False,
                            "stage": type(exc).__name__,
                            "message": message,
                            "verified": False,
                        })
                return {"results": results, "real_send": real_send}
            finally:
                conn.close()

    @staticmethod
    def _platform_label(platform: str) -> str:
        return {
            "douyin": "抖音", "xhs": "小红书",
            "bilibili": "B站", "weibo": "微博", "kuaishou": "快手",
        }.get(str(platform or ""), str(platform or "未知平台"))

    def _diagnostics_snapshot(self) -> dict[str, Any]:
        """读取诊断摘要，不自动触碰浏览器、不执行网络探测。"""
        try:
            from .config_loader import AppConfig  # type: ignore
        except ImportError:  # pragma: no cover
            from config_loader import AppConfig  # type: ignore

        config = AppConfig()
        bitbrowser = config.bitbrowser() or {}
        llm = config.llm_api() or {}
        health_rows = []
        account_rows = []
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                try:
                    health_rows = [dict(row) for row in conn.execute(
                        "SELECT h.* FROM health_events h "
                        "WHERE h.id = (SELECT h2.id FROM health_events h2 "
                        "WHERE h2.platform = h.platform AND h2.account_id IS h.account_id "
                        "AND h2.check_name = h.check_name "
                        "ORDER BY h2.observed_at DESC, h2.id DESC LIMIT 1) "
                        "ORDER BY h.platform, h.check_name"
                    ).fetchall()]
                except Exception:
                    health_rows = []
                raw_accounts = conn.execute(
                    "SELECT id, name, platform, bb_window_id, status "
                    "FROM accounts ORDER BY platform, name, id"
                ).fetchall()
                grouped = {}
                for row in raw_accounts:
                    item = dict(row)
                    platform = str(item.get("platform") or "unknown")
                    bucket = grouped.setdefault(platform, {
                        "platform": platform,
                        "platform_label": self._platform_label(platform),
                        "accounts": 0,
                        "bound_accounts": 0,
                        "waiting_human": 0,
                    })
                    bucket["accounts"] += 1
                    if str(item.get("bb_window_id") or "").strip():
                        bucket["bound_accounts"] += 1
                    if item.get("status") == "waiting_human":
                        bucket["waiting_human"] += 1
                for platform in ("douyin", "xhs", "bilibili", "weibo", "kuaishou"):
                    account_rows.append(grouped.get(platform, {
                        "platform": platform,
                        "platform_label": self._platform_label(platform),
                        "accounts": 0,
                        "bound_accounts": 0,
                        "waiting_human": 0,
                    }))
            finally:
                conn.close()

        log_items = []
        log_stats = {
            "date": datetime.now().astimezone().date().isoformat(),
            "total": 0, "normal": 0, "warning": 0, "error": 0,
            "alerts": [], "next_alert": None,
        }
        today = str(log_stats["date"])
        log_path = os.path.join(
            os.path.dirname(os.path.abspath(self.scheduler.db_path)),
            "logs", "operation_events.jsonl",
        )
        try:
            with open(log_path, encoding="utf-8") as stream:
                # 设置页展示当天完整日志；实时追加仍由 OperationLog 的内存上限
                # 保护，历史文件在这里按日期筛选，避免只看到最后 60 条。
                for line in stream:
                    try:
                        item = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(item, Mapping):
                        # 详情已在写入时完成密钥脱敏；诊断页保留操作参数、结果和耗时，
                        # 方便复现按钮点击及后台反馈，而不把 API Key 等敏感值带入界面。
                        timestamp = str(item.get("timestamp") or "")
                        if timestamp[:10] != today:
                            continue
                        level = str(item.get("level") or "info").lower()
                        if level in {"error", "failed", "critical"}:
                            normalized_level = "error"
                        elif level in {"warning", "warn"}:
                            normalized_level = "warning"
                        else:
                            normalized_level = "normal"
                        record = {
                            "timestamp": timestamp,
                            "level": normalized_level,
                            "source": str(item.get("source") or ""),
                            "event": str(item.get("event") or ""),
                            "message": str(item.get("message") or ""),
                            "action": str(item.get("action") or ""),
                            "outcome": str(item.get("outcome") or ""),
                            "duration_ms": item.get("duration_ms"),
                            "details": item.get("details") if isinstance(item.get("details"), Mapping) else {},
                        }
                        log_items.append(record)
                        log_stats["total"] += 1
                        log_stats[normalized_level] += 1
                        if normalized_level in {"warning", "error"}:
                            log_stats["alerts"].append(record)
        except OSError:
            pass

        log_stats["next_alert"] = log_stats["alerts"][0] if log_stats["alerts"] else None

        return {
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "bitbrowser": {
                "configured": bool(str(bitbrowser.get("base_url") or "").strip()),
                "timeout": int(float(bitbrowser.get("timeout") or 20)),
                "base_url": str(bitbrowser.get("base_url") or "").strip(),
            },
            "llm_api": {
                "enabled": bool(llm.get("enabled", False)),
                "provider": str(llm.get("provider") or ""),
                "model": str(llm.get("model") or ""),
                "base_url": str(llm.get("base_url") or "").split("?", 1)[0],
                "configured": bool(
                    str(llm.get("base_url") or "").strip()
                    and str(llm.get("model") or "").strip()
                ),
            },
            "accounts": account_rows,
            "health": health_rows,
            "logs": log_items,
            "log_stats": log_stats,
        }

    def _run_platform_health(self) -> dict[str, Any]:
        try:
            from .operations.platform_health import PlatformHealthChecker  # type: ignore
        except ImportError:  # pragma: no cover
            from operations.platform_health import PlatformHealthChecker  # type: ignore
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                return PlatformHealthChecker(conn).run()
            finally:
                conn.close()

    def _save_settings(self, args: Mapping[str, Any]) -> dict[str, Any]:
        try:
            from .config_loader import AppConfig  # type: ignore
        except ImportError:  # pragma: no cover
            from config_loader import AppConfig  # type: ignore

        bitbrowser = args.get("bitbrowser") or {}
        llm_api = args.get("llm_api") or {}
        if not isinstance(bitbrowser, Mapping) or not isinstance(llm_api, Mapping):
            raise ValueError("设置分区格式不正确")
        base_url = str(bitbrowser.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            raise ValueError("BitBrowser 本地 API 地址不能为空")
        try:
            timeout = max(1, min(120, int(bitbrowser.get("timeout") or 20)))
        except (TypeError, ValueError) as exc:
            raise ValueError("BitBrowser 超时必须是 1–120 的整数") from exc
        provider = str(llm_api.get("provider") or "").strip()
        api_base_url = str(llm_api.get("base_url") or "").strip().rstrip("/")
        model = str(llm_api.get("model") or "").strip()
        try:
            api_timeout = max(1, min(300, int(llm_api.get("timeout") or 60)))
        except (TypeError, ValueError) as exc:
            raise ValueError("智能 API 超时必须是 1–300 的整数") from exc
        api_key = str(llm_api.get("api_key") or "")
        if len(api_key) > 4096:
            raise ValueError("API Key 长度不合法")

        config = AppConfig()
        config.update_section("bitbrowser", {"base_url": base_url, "timeout": timeout})
        current_llm = config.llm_api() or {}
        # QML 不回读旧 Key。输入框留空表示保留已有 Key，避免修改其它字段时
        # 意外清除可用配置；需要更换 Key 时直接填写新值。
        if not api_key:
            api_key = str(current_llm.get("api_key") or "")
        api_values = {
            "enabled": bool(llm_api.get("enabled", current_llm.get("enabled", False))),
            "provider": provider,
            "base_url": api_base_url,
            "api_key": api_key,
            "model": model,
            "timeout": api_timeout,
        }
        config.update_section("llm_api", api_values)

        # 保存连接设置后，让独立后台的 Scheduler 和采集/回复对象立即使用新客户端。
        try:
            try:
                from .bitbrowser import BitBrowserClient  # type: ignore
            except ImportError:  # pragma: no cover
                from bitbrowser import BitBrowserClient  # type: ignore
            browser = BitBrowserClient(base_url=base_url, timeout=timeout)
            self.scheduler.bb = browser
            collectors = [
                getattr(self.scheduler, "collector", None),
                getattr(self.scheduler, "live_collector", None),
                getattr(self.scheduler, "weibo_collector", None),
                getattr(self.scheduler, "bilibili_collector", None),
            ]
            # HybridCollector 把抖音/小红书采集器放在 live 子对象中；
            # 只替换外层对象会导致设置页显示已应用，但实际采集仍用旧端口。
            collector = getattr(self.scheduler, "collector", None)
            if collector is not None:
                collectors.extend([
                    getattr(collector, "live", None),
                    getattr(collector, "weibo", None),
                    getattr(collector, "bilibili", None),
                ])
            for obj in collectors:
                if obj is not None and hasattr(obj, "bb"):
                    obj.bb = browser
        except Exception as exc:
            # 配置已经落盘；客户端替换失败仍返回明确状态，便于下一次检测重试。
            return {
                "saved": True,
                "applied": False,
                "error": f"连接客户端应用失败：{type(exc).__name__}",
                "bitbrowser": {"configured": True, "timeout": timeout, "base_url": base_url},
                "llm_api": {"enabled": api_values["enabled"], "provider": provider,
                            "model": model, "configured": bool(api_base_url and model)},
            }
        return {
            "saved": True,
            "applied": True,
            "bitbrowser": {"configured": True, "timeout": timeout, "base_url": base_url},
            "llm_api": {"enabled": api_values["enabled"], "provider": provider,
                        "model": model, "configured": bool(api_base_url and api_key)},
        }

    def _browser_for_diagnostics(self):
        browser = getattr(self.scheduler, "bb", None)
        # demo/离线运行时明确没有 BitBrowser，不能因为本机配置文件存在就
        # 越过 FakeCollector 连接真实浏览器；真实运行时由 build_runtime
        # 或 save_settings 注入当前客户端。
        return browser

    def _inspect_bitbrowser(self) -> dict[str, Any]:
        browser = self._browser_for_diagnostics()
        if browser is None:
            return {"healthy": False, "error": "尚未配置 BitBrowser 本地服务", "windows": [], "checks": []}
        try:
            try:
                from .bitbrowser_inspector import BitBrowserInspector  # type: ignore
            except ImportError:  # pragma: no cover
                from bitbrowser_inspector import BitBrowserInspector  # type: ignore
            return BitBrowserInspector(browser).inspect()
        except Exception as exc:  # noqa: BLE001
            return {"healthy": False, "error": str(exc), "windows": [], "checks": []}

    def _run_live_diagnostics(self) -> dict[str, Any]:
        browser = self._browser_for_diagnostics()
        if browser is None:
            return {"rows": [], "checked_at": "", "mode": "browser_unavailable", "send_executed": False}
        try:
            try:
                from .operations.browser_health import BrowserHealthChecker  # type: ignore
            except ImportError:  # pragma: no cover
                from operations.browser_health import BrowserHealthChecker  # type: ignore
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_dir = os.path.join(
                os.path.dirname(os.path.abspath(self.scheduler.db_path)),
                "diagnostics", f"browser_health_{stamp}", "screenshots",
            )
            conn = self._lead_connection()
            try:
                return BrowserHealthChecker(
                    conn, browser, screenshot_dir=screenshot_dir
                ).run()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            return {"rows": [], "checked_at": "", "mode": "failed", "error": str(exc), "send_executed": False}

    def _export_operation_log(self) -> dict[str, Any]:
        try:
            from .operation_log import OperationLog  # type: ignore
        except ImportError:  # pragma: no cover
            from operation_log import OperationLog  # type: ignore
        log_path = os.path.join(
            os.path.dirname(os.path.abspath(self.scheduler.db_path)),
            "logs", "operation_events.jsonl",
        )
        export_dir = os.path.join(os.path.dirname(log_path), "exports")
        os.makedirs(export_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = os.path.join(export_dir, f"后台操作日志_{stamp}.txt")
        result = OperationLog(log_path).export_text(destination)
        return {"path": result, "count": len(OperationLog(log_path).recent(5000))}

    def _sync_status(self, client) -> dict[str, Any]:
        owner_id = self._owner_user_id(client)
        if owner_id is None:
            return {
                "enabled": False, "status": "admin_view_only", "pending": 0,
                "message": "管理员可查看全部数据；请选择员工账号执行同步",
            }
        conn, store = self._sync_store()
        try:
            state = store.state(owner_id)
            return {
                "user_id": owner_id,
                "enabled": bool(state.get("enabled")),
                "server_url": str(state.get("server_url") or ""),
                "device_name": str(state.get("device_name") or ""),
                "api_token_configured": bool(str(state.get("api_token") or "").strip()),
                "last_sync_at": state.get("last_sync_at"),
                "status": str(state.get("status") or "idle"),
                "last_error": str(state.get("last_error") or ""),
                "pending": store.pending_count(owner_id),
            }
        finally:
            conn.close()

    def _save_sync_settings(self, args: Mapping[str, Any], client) -> dict[str, Any]:
        owner_id = self._owner_user_id(client)
        if owner_id is None:
            raise self._auth_error_type("forbidden", "管理员不能把同步数据归属到管理员账号")
        server_url = str(args.get("server_url") or "").strip()
        if server_url and not re.match(r"^https?://", server_url, re.IGNORECASE):
            raise ValueError("同步服务器地址必须以 http:// 或 https:// 开头")
        conn, store = self._sync_store()
        try:
            current_state = store.state(owner_id)
            supplied_token = str(args.get("api_token") or "").strip()
            # 与 LLM/API Key 一致：界面留空表示保留已保存密钥。
            token = supplied_token or str(current_state.get("api_token") or "")
            state = store.save_state(
                owner_id,
                enabled=bool(args.get("enabled", False)),
                server_url=server_url,
                api_token=token,
                device_name=str(args.get("device_name") or "").strip(),
                status="idle",
                last_error="",
            )
            result = {
                "saved": True, "enabled": bool(state.get("enabled")),
                "server_url": str(state.get("server_url") or ""),
                "device_name": str(state.get("device_name") or ""),
                "api_token_configured": bool(str(state.get("api_token") or "").strip()),
                "pending": store.pending_count(owner_id),
            }
        finally:
            conn.close()
        self._touch_device(
            self._client_user(client) or {}, client,
            device_name=str(args.get("device_name") or "").strip(),
        )
        self._on_scheduler_log(f"员工同步设置已保存：员工#{owner_id} · {'启用' if result['enabled'] else '关闭'}")
        return result

    def _sync_now(self, client) -> dict[str, Any]:
        owner_id = self._owner_user_id(client)
        if owner_id is None:
            raise self._auth_error_type("forbidden", "管理员同步需要明确指定员工账号")
        user = self._client_user(client) or {}
        self._touch_device(user, client)
        conn, store = self._sync_store()
        try:
            state = store.state(owner_id)
            pending = store.pending(owner_id, limit=500)
            if not bool(state.get("enabled")):
                return {"ok": False, "status": "disabled", "pending": len(pending),
                        "message": "同步开关未启用，未上传任何数据"}
            if not str(state.get("server_url") or "").strip():
                return {"ok": False, "status": "not_configured", "pending": len(pending),
                        "message": "尚未配置同步服务器地址，未上传任何数据"}
            if not pending:
                store.save_state(owner_id, status="idle", last_error="")
                return {"ok": True, "status": "idle", "synced": 0, "pending": 0,
                        "message": "没有待同步数据"}
            store.save_state(owner_id, status="syncing", last_error="")
            try:
                try:
                    from .data_scope import SyncClient  # type: ignore
                except ImportError:  # pragma: no cover
                    from data_scope import SyncClient  # type: ignore
                remote = SyncClient.push(state, user, pending)
                accepted_ids = remote.get("accepted_client_event_ids")
                if isinstance(accepted_ids, list):
                    accepted_set = {str(item) for item in accepted_ids}
                    sent_ids = [item["id"] for item in pending
                                if str(item.get("client_event_id")) in accepted_set]
                else:
                    accepted = remote.get("accepted")
                    try:
                        accepted = len(pending) if accepted is None else max(0, min(len(pending), int(accepted)))
                    except (TypeError, ValueError):
                        accepted = len(pending)
                    sent_ids = [item["id"] for item in pending[:accepted]]
                store.mark_sent(sent_ids)
                cursor = str(remote.get("cursor") or remote.get("next_cursor") or "")
                store.save_state(owner_id, status="success", last_error="",
                                 last_cursor=cursor,
                                 last_sync_at=datetime.now().isoformat(timespec="seconds"))
                remaining = store.pending_count(owner_id)
                self._on_scheduler_log(
                    f"员工数据手动同步完成：员工#{owner_id} · 已同步 {len(sent_ids)} 条 · 待同步 {remaining} 条"
                )
                return {"ok": True, "status": "success", "synced": len(sent_ids),
                        "pending": remaining, "remote": {
                            key: value for key, value in dict(remote).items()
                            if key not in {"api_token", "token"}
                        }}
            except Exception as exc:
                ids = [item["id"] for item in pending]
                store.mark_failed(ids, str(exc))
                store.save_state(owner_id, status="failed", last_error=str(exc))
                self._on_scheduler_log(
                    f"员工数据手动同步失败：员工#{owner_id} · {type(exc).__name__}: {exc}"
                )
                return {"ok": False, "status": "failed", "synced": 0,
                        "pending": store.pending_count(owner_id), "message": str(exc)}
        finally:
            conn.close()

    def _admin_audit_logs(self, args: Mapping[str, Any] | None = None,
                          client=None) -> dict[str, Any]:
        """管理员读取结构化审计日志；不把写日志动作放到界面线程。"""
        self._require_admin(client)
        args = args or {}
        try:
            limit = max(1, min(2000, int(args.get("limit") or 500)))
        except (TypeError, ValueError):
            limit = 500
        level_filter = str(args.get("level") or "").strip().lower()
        actor_filter = str(args.get("actor") or "").strip().casefold()
        records = self._operation_log.recent(max(2000, limit))
        result = []
        counts = {"normal": 0, "warning": 0, "error": 0}

        def normalize_level(value: Any) -> str:
            value = str(value or "").lower()
            if value in {"error", "failed", "critical"}:
                return "error"
            if value in {"warning", "warn"}:
                return "warning"
            return "normal"

        for raw in reversed(records):
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            level = normalize_level(item.get("level"))
            actor = item.get("details") if isinstance(item.get("details"), Mapping) else {}
            actor = actor.get("actor") if isinstance(actor, Mapping) else {}
            actor_name = str(actor.get("username") or "") if isinstance(actor, Mapping) else ""
            if level_filter and level != level_filter:
                continue
            if actor_filter and actor_filter not in actor_name.casefold():
                continue
            item["level"] = level
            item["actor_username"] = actor_name or "系统"
            result.append(item)
            counts[level] += 1
            if len(result) >= limit:
                break
        return {"items": result, "total": len(result), "counts": counts}

    def _admin_backup_rows(self, conn, limit: int = 50) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT id, path, kind, size_bytes, sha256, verified, created_at "
            "FROM backup_records ORDER BY created_at DESC, id DESC LIMIT ?",
            (max(1, min(200, int(limit))),),
        ).fetchall()
        return [dict(row) for row in rows]

    def _admin_backup_record(self, backup_id: int, client=None) -> dict[str, Any]:
        self._require_admin(client)
        conn = self._lead_connection()
        try:
            row = conn.execute(
                "SELECT id, path, kind, size_bytes, sha256, verified, created_at "
                "FROM backup_records WHERE id = ?", (int(backup_id),)
            ).fetchone()
            if row is None:
                raise ValueError(f"备份记录不存在: {int(backup_id)}")
            return dict(row)
        finally:
            conn.close()

    def _safe_backup_path(self, path: str) -> str:
        """只允许操作当前数据目录下由系统生成的备份文件。"""
        candidate = os.path.abspath(str(path or ""))
        backup_dir = os.path.abspath(os.path.join(
            os.path.dirname(os.path.abspath(self.scheduler.db_path)), "backups"
        ))
        try:
            inside = os.path.commonpath([candidate, backup_dir]).casefold() == backup_dir.casefold()
        except ValueError:
            inside = False
        if not inside or candidate == backup_dir or not os.path.isfile(candidate):
            raise ValueError("备份文件不在系统备份目录内")
        return candidate

    def _admin_dashboard(self, client=None) -> dict[str, Any]:
        """返回管理员工作台摘要，不包含密码、API Key、Cookie 或窗口 ID。"""
        self._require_admin(client)
        conn = self._lead_connection()
        try:
            users = [dict(row) for row in conn.execute(
                "SELECT id, username, employee_name, role, status, created_at, "
                "approved_at, last_login_at FROM app_users "
                "WHERE role <> 'admin' ORDER BY id"
            ).fetchall()]

            def count(sql: str, params: tuple[Any, ...]) -> int:
                try:
                    row = conn.execute(sql, params).fetchone()
                    return int(row[0] if row else 0)
                except sqlite3.Error:
                    return 0

            employees = []
            for user in users:
                uid = int(user["id"])
                sync = conn.execute(
                    "SELECT enabled, device_name, last_sync_at, status, last_error "
                    "FROM sync_state WHERE user_id = ?", (uid,)
                ).fetchone()
                device_count = count(
                    "SELECT COUNT(*) FROM employee_devices WHERE user_id = ? AND status = 'active'",
                    (uid,),
                )
                pending_sync = count(
                    "SELECT COUNT(*) FROM sync_outbox WHERE owner_user_id = ? "
                    "AND status IN ('pending', 'failed')", (uid,),
                )
                employees.append({
                    **user,
                    "tasks": count("SELECT COUNT(*) FROM tasks WHERE owner_user_id = ?", (uid,)),
                    "accounts": count("SELECT COUNT(*) FROM accounts WHERE owner_user_id = ?", (uid,)),
                    "keyword_groups": count(
                        "SELECT COUNT(*) FROM keyword_groups WHERE owner_user_id = ?", (uid,)
                    ),
                    "leads": count(
                        "SELECT COUNT(*) FROM leads WHERE data_owner_user_id = ?", (uid,)
                    ),
                    "interactions": count(
                        "SELECT COUNT(*) FROM interaction_drafts d JOIN leads l ON l.id = d.lead_id "
                        "WHERE l.data_owner_user_id = ?", (uid,)
                    ),
                    "publish_drafts": count(
                        "SELECT COUNT(*) FROM publish_drafts WHERE owner_user_id = ?", (uid,)
                    ),
                    "pending_sync": pending_sync,
                    "active_devices": device_count,
                    "sync_enabled": bool(sync and sync["enabled"]),
                    "sync_status": str(sync["status"] if sync else "idle"),
                    "last_sync_at": sync["last_sync_at"] if sync else None,
                    "sync_error": str(sync["last_error"] if sync else ""),
                })
            devices = [dict(row) for row in conn.execute(
                "SELECT d.id, d.user_id, u.username, u.employee_name, d.device_id, "
                "d.device_name, d.client_version, d.status, d.first_seen_at, d.last_seen_at "
                "FROM employee_devices d JOIN app_users u ON u.id = d.user_id "
                "ORDER BY d.last_seen_at DESC, d.id DESC LIMIT 100"
            ).fetchall()]
            backups = self._admin_backup_rows(conn)
            summary = {
                "employees": len(employees),
                "pending_users": sum(1 for user in users if user.get("status") == "pending"),
                "tasks": sum(int(user["tasks"]) for user in employees),
                "accounts": sum(int(user["accounts"]) for user in employees),
                "leads": sum(int(user["leads"]) for user in employees),
                "pending_sync": sum(int(user["pending_sync"]) for user in employees),
                "active_devices": sum(int(user["active_devices"]) for user in employees),
                "backups": len(backups),
            }
        finally:
            conn.close()
        audit = self._admin_audit_logs({"limit": 40}, client)
        return {"summary": summary, "employees": employees, "devices": devices,
                "backups": backups, "audit": audit}

    def _admin_create_backup(self, client=None) -> dict[str, Any]:
        self._require_admin(client)
        try:
            from .operations.backup import backup_sqlite  # type: ignore
        except ImportError:  # pragma: no cover
            from operations.backup import backup_sqlite  # type: ignore
        data_dir = os.path.dirname(os.path.abspath(self.scheduler.db_path))
        destination = os.path.join(data_dir, "backups")
        with self._lead_lock:
            conn = self._lead_connection()
            try:
                result = backup_sqlite(
                    self.scheduler.db_path, destination,
                    kind="admin_manual", record_conn=conn,
                )
            finally:
                conn.close()
        self._on_scheduler_log(f"管理员创建数据库备份：{result.path} · 校验={'通过' if result.verified else '失败'}")
        return {"id": None, "path": result.path, "size_bytes": result.size_bytes,
                "sha256": result.sha256, "verified": bool(result.verified),
                "message": "备份已完成" if result.verified else "备份已生成但校验未通过"}

    def _admin_list_backups(self, client=None) -> dict[str, Any]:
        self._require_admin(client)
        conn = self._lead_connection()
        try:
            rows = self._admin_backup_rows(conn, 100)
        finally:
            conn.close()
        return {"items": rows, "total": len(rows)}

    def _admin_validate_backup(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        backup_id = self._int_arg(args, "backup_id")
        record = self._admin_backup_record(backup_id, client)
        path = self._safe_backup_path(record["path"])
        try:
            from .operations.backup import validate_sqlite  # type: ignore
        except ImportError:  # pragma: no cover
            from operations.backup import validate_sqlite  # type: ignore
        verified, reason = validate_sqlite(path)
        conn = self._lead_connection()
        try:
            conn.execute("UPDATE backup_records SET verified = ? WHERE id = ?",
                         (int(verified), backup_id))
            conn.commit()
        finally:
            conn.close()
        return {"backup_id": backup_id, "path": path, "verified": bool(verified),
                "message": "校验通过" if verified else reason}

    def _admin_restore_backup(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        backup_id = self._int_arg(args, "backup_id")
        record = self._admin_backup_record(backup_id, client)
        path = self._safe_backup_path(record["path"])
        try:
            from .operations.backup import backup_sqlite, validate_sqlite  # type: ignore
        except ImportError:  # pragma: no cover
            from operations.backup import backup_sqlite, validate_sqlite  # type: ignore
        restore_dir = os.path.join(
            os.path.dirname(os.path.abspath(self.scheduler.db_path)), "restores"
        )
        result = backup_sqlite(
            path, restore_dir,
            kind=f"restore_{backup_id}_{secrets.token_hex(4)}",
        )
        verified, reason = validate_sqlite(result.path)
        if not verified:
            raise RuntimeError(f"恢复副本校验失败：{reason}")
        self._on_scheduler_log(
            f"管理员生成备份恢复副本：备份#{backup_id} → {result.path} · 当前运行库未覆盖"
        )
        return {"backup_id": backup_id, "path": result.path, "verified": True,
                "message": "已恢复到新文件，当前运行库未被覆盖"}

    def _admin_set_device_status(self, args: Mapping[str, Any], client=None) -> dict[str, Any]:
        self._require_admin(client)
        device_id = self._int_arg(args, "device_row_id")
        status = str(args.get("status") or "").strip().lower()
        if status not in {"active", "disabled"}:
            raise ValueError("设备状态只能是 active 或 disabled")
        conn = self._lead_connection()
        try:
            row = conn.execute(
                "SELECT id, user_id, device_name, status FROM employee_devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"设备不存在: {device_id}")
            conn.execute("UPDATE employee_devices SET status = ? WHERE id = ?",
                         (status, device_id))
            conn.commit()
            return {**dict(row), "status": status}
        finally:
            conn.close()

    @staticmethod
    def _export_task_label(task: Mapping[str, Any], task_id: int) -> str:
        """沿用 1.2 的任务导出命名，确保新旧界面导出的目录可对应。"""
        created = str(task.get("created_at") or "")[:10].replace("-", "")
        date = created if len(created) == 8 else datetime.now().strftime("%Y%m%d")
        keyword = str(task.get("keyword") or "未命名关键词").strip()
        platform = BackendService._platform_label(str(task.get("platform") or ""))
        label = f"{date}_{keyword}_批次{int(task_id)}_{platform}"
        return re.sub(r'[\\/:*?"<>|]+', "_", label).strip(" .")

    def _export_task(self, task_id: int) -> dict[str, Any]:
        """直接从任务数据库导出，放在 UI 客户端线程之外执行。"""
        tid = int(task_id)
        report = self.scheduler.status_report()
        tasks = report.get("tasks") or {}
        task = tasks.get(tid) or tasks.get(str(tid))
        if not task:
            raise ValueError(f"任务不存在: {tid}")
        try:
            from . import export_report  # type: ignore
        except ImportError:  # pragma: no cover
            import export_report  # type: ignore

        scheduler_conn = self.scheduler.conn
        with self.scheduler._conn_lock:
            videos = [dict(row) for row in scheduler_conn.execute(
                "SELECT * FROM videos WHERE task_id = ? ORDER BY id", (tid,)
            ).fetchall()]
            comments_by_video = {
                int(video["id"]): [dict(row) for row in scheduler_conn.execute(
                    "SELECT * FROM comments WHERE video_id = ? ORDER BY id",
                    (video["id"],),
                ).fetchall()]
                for video in videos
            }

        items = []
        for video in videos:
            parsed_comments = []
            for comment in comments_by_video.get(int(video["id"]), []):
                extra = {}
                try:
                    if comment.get("extra"):
                        extra = json.loads(comment["extra"])
                except Exception:
                    extra = {}
                if not isinstance(extra, Mapping):
                    extra = {}
                parsed_comments.append({
                    "uid": comment.get("user_id") or "",
                    "nickname": comment.get("nickname") or "",
                    "text": comment.get("content") or "",
                    "digg": extra.get("digg_count", extra.get("digg", 0)),
                    "ctime_str": comment.get("comment_time") or "",
                    "region": extra.get("region", ""),
                    "homepage": extra.get("homepage", ""),
                })
            extra = {}
            try:
                if video.get("extra"):
                    extra = json.loads(video["extra"])
            except Exception:
                extra = {}
            if not isinstance(extra, Mapping):
                extra = {}
            items.append({
                "kind": str(task.get("platform") or "douyin"),
                "pid": video.get("vid") or "",
                "title": video.get("title") or "",
                "author": video.get("author") or "",
                "pub_str": extra.get("create_time", ""),
                "collected_at": video.get("collected_at") or "",
                "url": video.get("url") or "",
                "comments": parsed_comments,
            })

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(self.scheduler.db_path)))
        base_dir = str(task.get("output_dir") or "").strip()
        if not base_dir:
            base_dir = os.path.join(project_root, "data", "exports")
        elif not os.path.isabs(base_dir):
            base_dir = os.path.join(project_root, base_dir)
        outdir = os.path.join(os.path.abspath(base_dir), self._export_task_label(task, tid))
        result = export_report.export_unified(items, outdir, self._export_task_label(task, tid))
        message = (
            f"任务#{tid} 导出完成：作品{result['n_videos']} 评论{result['n_comments']} "
            f"聚合用户{result['n_users']}，文件：{result['path']}"
        )
        log = getattr(self.scheduler, "_emit_log", None)
        if callable(log):
            log(message)
        return {"task_id": tid, "outdir": outdir, "path": result["path"],
                "n_videos": int(result["n_videos"]), "n_comments": int(result["n_comments"]),
                "n_users": int(result["n_users"])}

    def _resume_task(self, task_id: int) -> dict[str, Any]:
        """恢复 1.2 的继续语义：恢复人工账号、解除暂停并重新启动阶段。"""
        tid = int(task_id)
        task = self.scheduler.get_task(tid) or {}
        if not task:
            raise ValueError(f"任务不存在: {tid}")
        # search_exhausted 只代表“用户明确要求继续补采搜索”时的入口条件。
        # 若上一轮是详情采集阶段触发人工验证，即使任务此前已经搜索到尽头，
        # 继续也必须回到已有视频的详情断点，不能重新打开搜索阶段。
        report = self.scheduler.status_report()
        report_tasks = report.get("tasks") or {}
        task_view = report_tasks.get(tid) or report_tasks.get(str(tid)) or {}
        latest_run = task_view.get("latest_run") or {}
        interruption_text = " ".join(str(value or "") for value in (
            task.get("error_message"),
            latest_run.get("stop_reason"),
        ))
        # 运行记录是主要依据；waiting_accounts_for_task 兼容尚未来得及
        # 写入 paused 运行记录的旧任务/瞬时状态。
        human_interrupted = (
            "人工" in interruption_text or "验证" in interruption_text
            or bool(self.scheduler.waiting_accounts_for_task(tid))
        )
        force_search = bool(int(task.get("search_exhausted", 0) or 0)) and not human_interrupted
        bound_names = task.get("task_accounts") or "[]"
        if isinstance(bound_names, str):
            try:
                bound_names = json.loads(bound_names)
            except (TypeError, ValueError):
                bound_names = []
        if not isinstance(bound_names, list):
            bound_names = []
        waiting_ids = self.scheduler.waiting_accounts_for_task(tid)
        for _identity, account in (report.get("accounts") or {}).items():
            if (account.get("status") == "waiting_human" and
                    (int(account.get("id") or 0) in waiting_ids or
                     (not waiting_ids and
                      (not bound_names or account.get("name") in bound_names)))):
                self.scheduler.resolve_human(int(account["id"]))
        self.scheduler.resume(tid)
        self.scheduler.start(tid, force_search=force_search)
        log = getattr(self.scheduler, "_emit_log", None)
        if callable(log):
            log(f"任务#{tid} 已继续")
        return {"task_id": tid, "resumed": True, "force_search": force_search}

    def _open_task_folder(self, task_id: int) -> dict[str, Any]:
        tid = int(task_id)
        report = self.scheduler.status_report()
        tasks = report.get("tasks") or {}
        task = tasks.get(tid) or tasks.get(str(tid))
        if not task:
            raise ValueError(f"任务不存在: {tid}")
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(self.scheduler.db_path)))
        base_dir = str(task.get("output_dir") or "").strip()
        if not base_dir:
            base_dir = os.path.join(project_root, "data", "exports")
        elif not os.path.isabs(base_dir):
            base_dir = os.path.join(project_root, base_dir)
        path = os.path.abspath(os.path.join(
            base_dir, self._export_task_label(task, tid)
        ))
        os.makedirs(path, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(path)
        else:  # pragma: no cover - 仅兼容非 Windows 预览环境
            import webbrowser
            webbrowser.open(path)
        return {"task_id": tid, "path": path}

    def dispatch(self, message: Mapping[str, Any], *, client=None) -> dict[str, Any]:
        request_id = str(message["request_id"])
        if not self._authorized(message):
            return make_response(
                request_id, ok=False,
                error={"code": "unauthorized", "message": "后台服务令牌无效"},
            )
        command = str(message["command"])
        args = message.get("args") or {}
        if client is not None and command not in _AUTH_COMMANDS:
            auth_user = getattr(client, "auth_user", None)
            if not isinstance(auth_user, Mapping):
                if command == "status":
                    return make_response(
                        request_id, ok=True,
                        result={"auth_required": True, "_auth_scope": self._auth_scope(client)},
                    )
                return make_response(
                    request_id, ok=False,
                    error={"code": "auth_required", "message": "请先登录"},
                )
            current_user = self._auth_store.get(int(auth_user.get("id") or 0))
            if not current_user or current_user.get("status") != "approved":
                client.auth_user = None
                return make_response(
                    request_id, ok=False,
                    error={"code": "auth_required", "message": "登录已失效，请重新登录"},
                )
            client.auth_user = current_user
        started = time.perf_counter()
        actor = self._client_user(client) or {}
        actor_details = {
            "user_id": int(actor.get("id") or 0) if str(actor.get("id") or "").isdigit() else 0,
            "username": str(actor.get("username") or ""),
            "role": str(actor.get("role") or ""),
        }
        try:
            result = self._dispatch_command(command, args, client=client)
            record = self._operation_log.append(
                f"命令完成：{command}", source="backend", event="command_completed",
                action=command, outcome="success",
                duration_ms=(time.perf_counter() - started) * 1000,
                details={"request_id": request_id, "actor": actor_details,
                         "args": self._safe_operation_args(args)},
            )
            self._emit_event("log", record)
            return make_response(request_id, ok=True, result=result)
        except Exception as exc:  # 不能把后台线程异常泄漏成连接断开
            record = self._operation_log.append(
                f"命令失败：{command}：{type(exc).__name__}: {exc}",
                level="error", source="backend", event="command_failed",
                action=command, outcome="failed",
                duration_ms=(time.perf_counter() - started) * 1000,
                details={"request_id": request_id, "actor": actor_details,
                         "args": self._safe_operation_args(args)},
            )
            self._emit_event("log", record)
            return make_response(
                request_id, ok=False,
                error={
                    "code": str(getattr(exc, "code", "") or type(exc).__name__),
                    "message": str(getattr(exc, "message", "") or str(exc)),
                },
            )

    @staticmethod
    def _client_user(client) -> dict[str, Any] | None:
        value = getattr(client, "auth_user", None) if client is not None else None
        return dict(value) if isinstance(value, Mapping) else None

    @classmethod
    def _is_admin_client(cls, client) -> bool:
        user = cls._client_user(client)
        return bool(user and str(user.get("role") or "") == "admin")

    @classmethod
    def _auth_scope(cls, client) -> dict[str, Any]:
        """返回状态快照所属的认证范围，供前端丢弃过期快照。"""
        user = cls._client_user(client) or {}
        try:
            user_id = int(user.get("id") or 0)
        except (TypeError, ValueError):
            user_id = 0
        return {
            "user_id": user_id if user_id > 0 else 0,
            "role": str(user.get("role") or ""),
        }

    @classmethod
    def _owner_user_id(cls, client) -> int | None:
        """返回当前员工的数据归属 ID；管理员/内部调用返回 None。"""
        user = cls._client_user(client)
        if not user or str(user.get("role") or "") == "admin":
            return None
        try:
            value = int(user.get("id") or 0)
        except (TypeError, ValueError):
            value = 0
        return value if value > 0 else None

    def _touch_device(self, user: Mapping[str, Any], client=None,
                      *, device_name: str | None = None) -> dict[str, Any] | None:
        """登记当前登录终端，并在管理员撤销后阻断继续使用。"""
        try:
            uid = int(user.get("id") or 0)
        except (TypeError, ValueError):
            uid = 0
        if uid <= 0 or str(user.get("role") or "") == "admin":
            return None
        device_id = str(getattr(client, "device_id", "") or "").strip()
        if not device_id:
            device_id = secrets.token_hex(16)
            if client is not None:
                client.device_id = device_id
        conn = self._lead_connection()
        try:
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            current = conn.execute(
                "SELECT * FROM employee_devices WHERE user_id = ? AND device_id = ?",
                (uid, device_id),
            ).fetchone()
            if current is not None and str(current["status"] or "active") == "disabled":
                raise self._auth_error_type("device_disabled", "当前设备已被管理员停用")
            name = str(device_name or getattr(client, "device_name", "") or "").strip()
            if not name:
                name = "未命名设备"
            if current is None:
                conn.execute(
                    "INSERT INTO employee_devices "
                    "(user_id, device_id, device_name, client_version, status, "
                    "first_seen_at, last_seen_at, last_ip) VALUES (?, ?, ?, ?, 'active', ?, ?, '')",
                    (uid, device_id, name, "2.0", stamp, stamp),
                )
            else:
                conn.execute(
                    "UPDATE employee_devices SET device_name = ?, client_version = ?, "
                    "status = 'active', last_seen_at = ? WHERE id = ?",
                    (name, "2.0", stamp, int(current["id"])),
                )
            state = conn.execute(
                "SELECT user_id FROM sync_state WHERE user_id = ?", (uid,)
            ).fetchone()
            if state is None:
                conn.execute(
                    "INSERT INTO sync_state (user_id, device_id, updated_at) VALUES (?, ?, ?)",
                    (uid, device_id, stamp),
                )
            else:
                conn.execute(
                    "UPDATE sync_state SET device_id = ?, updated_at = ? WHERE user_id = ?",
                    (device_id, stamp, uid),
                )
            conn.commit()
            return {
                "id": int(current["id"]) if current is not None else int(conn.execute(
                    "SELECT id FROM employee_devices WHERE user_id = ? AND device_id = ?",
                    (uid, device_id),
                ).fetchone()[0]),
                "user_id": uid, "device_id": device_id, "device_name": name,
                "status": "active", "last_seen_at": stamp,
            }
        finally:
            conn.close()

    def _scoped_status_report(self, client=None) -> dict[str, Any]:
        """按登录用户裁剪状态快照；管理员仍获得全量视图。"""
        scope = self._auth_scope(client)
        if client is not None and not self._client_user(client):
            return {"auth_required": True, "_auth_scope": scope}
        snapshot = self.scheduler.status_report()
        owner_id = self._owner_user_id(client)
        if owner_id is None:
            return {**snapshot, "_auth_scope": scope}

        def owned(item: Mapping[str, Any]) -> bool:
            try:
                return int(item.get("owner_user_id") or 0) == owner_id
            except (TypeError, ValueError):
                return False

        tasks = {
            str(key): value for key, value in (snapshot.get("tasks") or {}).items()
            if isinstance(value, Mapping) and owned(value)
        }
        accounts = {
            str(key): value for key, value in (snapshot.get("accounts") or {}).items()
            if isinstance(value, Mapping) and owned(value)
        }
        totals = dict(snapshot.get("totals") or {})
        totals.update({
            "tasks": len(tasks),
            "tasks_running": sum(1 for item in tasks.values()
                                  if item.get("status") in ("phase_a_search", "phase_b_comments")),
            "tasks_done": sum(1 for item in tasks.values() if item.get("status") == "done"),
            "videos_total": sum(int(item.get("videos_total") or 0) for item in tasks.values()),
            "videos_done": sum(int(item.get("video_done") or 0) for item in tasks.values()),
            "comments": sum(int(item.get("comments") or 0) for item in tasks.values()),
            "accounts": len(accounts),
            "waiting_human": [item.get("name") for item in accounts.values()
                              if item.get("status") == "waiting_human"],
        })
        try:
            conn = self._lead_connection()
            try:
                totals["leads"] = int(conn.execute(
                    "SELECT COUNT(*) FROM leads WHERE data_owner_user_id = ?",
                    (owner_id,),
                ).fetchone()[0])
                totals["interactions"] = int(conn.execute(
                    "SELECT COUNT(*) FROM interaction_drafts d JOIN leads l ON l.id = d.lead_id "
                    "WHERE l.data_owner_user_id = ? AND d.status IN "
                    "('draft','pending_review','approved','queued')",
                    (owner_id,),
                ).fetchone()[0])
            finally:
                conn.close()
        except sqlite3.Error:
            totals["leads"] = 0
            totals["interactions"] = 0
        return {**snapshot, "tasks": tasks, "accounts": accounts, "totals": totals,
                "paused": bool(snapshot.get("paused")) and bool(tasks),
                "_auth_scope": scope}

    def _require_owned_row(self, table: str, record_id: int, owner_column: str,
                           client, label: str) -> dict[str, Any]:
        """校验一条记录属于当前员工；表名来自内部白名单。"""
        allowed = {
            "tasks": "owner_user_id", "accounts": "owner_user_id",
            "keyword_groups": "owner_user_id", "leads": "data_owner_user_id",
            "generated_contents": "owner_user_id", "publish_drafts": "owner_user_id",
        }
        if table not in allowed or allowed[table] != owner_column:
            raise ValueError("内部数据归属字段无效")
        conn = self._lead_connection()
        try:
            row = conn.execute(
                f"SELECT * FROM {table} WHERE id = ?", (int(record_id),)
            ).fetchone()
            if row is None:
                raise ValueError(f"{label}不存在: {int(record_id)}")
            item = dict(row)
        finally:
            conn.close()
        owner_id = self._owner_user_id(client)
        if owner_id is not None and int(item.get(owner_column) or 0) != owner_id:
            raise self._auth_error_type("forbidden", f"无权访问该{label}")
        return item

    def _require_lead_ids(self, lead_ids: list[int], client) -> None:
        for lead_id in lead_ids:
            self._require_owned_row("leads", lead_id, "data_owner_user_id", client, "线索")

    def _set_owner_and_queue(self, table: str, record_id: int, client,
                             entity_type: str | None = None) -> None:
        """把新建记录绑定当前员工，并写入一次待同步事件。"""
        owner_id = self._owner_user_id(client)
        if owner_id is None:
            return
        allowed = {
            "tasks": "owner_user_id", "accounts": "owner_user_id",
            "keyword_groups": "owner_user_id", "leads": "data_owner_user_id",
            "generated_contents": "owner_user_id", "publish_drafts": "owner_user_id",
        }
        owner_column = allowed.get(table)
        if not owner_column:
            raise ValueError("不支持的数据归属表")
        conn = self._lead_connection()
        try:
            conn.execute(
                f"UPDATE {table} SET {owner_column} = ? WHERE id = ? "
                f"AND ({owner_column} IS NULL OR {owner_column} = ?)",
                (owner_id, int(record_id), owner_id),
            )
            row = conn.execute(
                f"SELECT * FROM {table} WHERE id = ? AND {owner_column} = ?",
                (int(record_id), owner_id),
            ).fetchone()
            if row is None:
                raise self._auth_error_type("forbidden", f"无法绑定该{table}记录")
            try:
                from .data_scope import SyncStore  # type: ignore
            except ImportError:  # pragma: no cover
                from data_scope import SyncStore  # type: ignore
            SyncStore(conn).enqueue(
                owner_id, entity_type or table.rstrip("s"), int(record_id), dict(row)
            )
        finally:
            conn.close()

    def _queue_entity(self, table: str, record_id: int, client,
                      entity_type: str | None = None) -> None:
        owner_id = self._owner_user_id(client)
        if owner_id is None:
            return
        self._require_owned_row(table, record_id, {
            "tasks": "owner_user_id", "accounts": "owner_user_id",
            "keyword_groups": "owner_user_id", "leads": "data_owner_user_id",
            "generated_contents": "owner_user_id", "publish_drafts": "owner_user_id",
        }[table], client, table)
        conn = self._lead_connection()
        try:
            row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (int(record_id),)).fetchone()
            if row is None:
                return
            try:
                from .data_scope import SyncStore  # type: ignore
            except ImportError:  # pragma: no cover
                from data_scope import SyncStore  # type: ignore
            SyncStore(conn).enqueue(owner_id, entity_type or table.rstrip("s"),
                                    int(record_id), dict(row))
        finally:
            conn.close()

    def _queue_deleted_snapshot(self, table: str, record_id: int, client,
                                entity_type: str, snapshot: Mapping[str, Any]) -> None:
        """删除后用删除前快照写入 outbox，避免同步端保留幽灵数据。"""
        owner_id = self._owner_user_id(client)
        if owner_id is None:
            return
        owner_column = {
            "tasks": "owner_user_id", "accounts": "owner_user_id",
            "keyword_groups": "owner_user_id", "leads": "data_owner_user_id",
            "generated_contents": "owner_user_id", "publish_drafts": "owner_user_id",
        }.get(table)
        if not owner_column or int(snapshot.get(owner_column) or 0) != owner_id:
            raise self._auth_error_type("forbidden", f"无权删除该{table}记录")
        payload = dict(snapshot)
        payload["deleted"] = True
        conn = self._lead_connection()
        try:
            try:
                from .data_scope import SyncStore  # type: ignore
            except ImportError:  # pragma: no cover
                from data_scope import SyncStore  # type: ignore
            SyncStore(conn).enqueue(
                owner_id, entity_type, int(record_id), payload, operation="delete"
            )
        finally:
            conn.close()

    def _sync_store(self):
        conn = self._lead_connection()
        try:
            try:
                from .data_scope import SyncStore  # type: ignore
            except ImportError:  # pragma: no cover
                from data_scope import SyncStore  # type: ignore
            return conn, SyncStore(conn)
        except Exception:
            conn.close()
            raise

    def _authorize_command_scope(self, command: str, args: Mapping[str, Any], client) -> None:
        """在进入业务服务前校验跨员工资源 ID，防止“先读后过滤”。"""
        if self._owner_user_id(client) is None:
            return
        task_commands = {
            "get_task", "start", "pause", "resume", "resume_task", "stop_task",
            "delete_task", "export_task", "open_task_folder", "waiting_accounts",
            "configure_monitoring",
        }
        if command in task_commands:
            task_id = self._int_arg(args, "task_id", required=False)
            if task_id is None or task_id <= 0:
                raise self._auth_error_type("forbidden", "员工操作必须指定自己的任务")
            self._require_owned_row("tasks", task_id, "owner_user_id", client, "任务")

        account_commands = {
            "open_account_browser", "bind_account", "remove_account", "resolve_human",
            "list_account_contents", "sync_account_contents", "list_published_messages",
            "preview_publish_draft", "real_publish_draft", "schedule_publish",
        }
        if command in account_commands:
            account_id = self._int_arg(args, "account_id", required=False)
            if account_id is not None and account_id > 0:
                self._require_owned_row("accounts", account_id, "owner_user_id", client, "账号")
            elif command not in {"list_published_messages"}:
                raise self._auth_error_type("forbidden", "员工操作必须指定自己的账号")

        lead_commands = {
            "add_leads_to_interaction", "add_leads_to_private_message", "export_leads",
            "tag_leads",
        }
        if command in lead_commands:
            self._require_lead_ids(self._lead_ids_arg(args), client)

        if command == "export_leads_by_task":
            raw_ids = args.get("lead_ids")
            if bool(args.get("all_filtered")) or not isinstance(raw_ids, list) or not raw_ids:
                # _lead_ids_for_export_filters 再次带员工条件查询。
                return
            self._require_lead_ids(self._lead_ids_arg(args), client)

        draft_commands = {
            "update_publish_variant", "publish_draft_action", "delete_publish_draft",
            "add_publish_assets", "delete_publish_asset", "preview_publish_draft",
            "real_publish_draft", "schedule_publish",
        }
        if command in draft_commands:
            draft_id = self._int_arg(args, "draft_id")
            self._require_owned_row("publish_drafts", draft_id, "owner_user_id", client, "发布草稿")

        if command in {"interaction_action"}:
            draft_id = self._int_arg(args, "draft_id")
            conn = self._lead_connection()
            try:
                row = conn.execute(
                    "SELECT l.id FROM interaction_drafts d JOIN leads l ON l.id = d.lead_id "
                    "WHERE d.id = ?", (draft_id,)
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                raise ValueError(f"互动草稿不存在: {draft_id}")
            self._require_owned_row("leads", int(row["id"]), "data_owner_user_id", client, "线索")
            if str(args.get("action") or "").strip().lower() == "assign_account":
                account_id = self._int_arg(args, "account_id")
                self._require_owned_row("accounts", account_id, "owner_user_id", client, "账号")

        if command == "send_interactions":
            raw_ids = args.get("draft_ids")
            if isinstance(raw_ids, list):
                conn = self._lead_connection()
                try:
                    for draft_id in self._lead_ids_arg({"lead_ids": raw_ids}):
                        row = conn.execute(
                            "SELECT l.id FROM interaction_drafts d JOIN leads l ON l.id = d.lead_id "
                            "WHERE d.id = ?", (draft_id,)
                        ).fetchone()
                        if row is None:
                            raise ValueError(f"互动草稿不存在: {draft_id}")
                        self._require_owned_row("leads", int(row["id"]), "data_owner_user_id", client, "线索")
                finally:
                    conn.close()
            account_id = self._int_arg(args, "account_id", required=False)
            if account_id is not None and account_id > 0:
                self._require_owned_row("accounts", account_id, "owner_user_id", client, "账号")

        if command == "list_content_comments":
            content_id = self._int_arg(args, "account_content_id")
            conn = self._lead_connection()
            try:
                row = conn.execute(
                    "SELECT account_id FROM account_contents WHERE id = ?", (content_id,)
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                raise ValueError(f"账号内容不存在: {content_id}")
            self._require_owned_row("accounts", int(row["account_id"]), "owner_user_id", client, "账号")

        if command == "mark_published_message":
            message_id = self._int_arg(args, "message_id")
            conn = self._lead_connection()
            try:
                row = conn.execute(
                    "SELECT account_id FROM published_messages WHERE id = ?", (message_id,)
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                raise ValueError(f"消息不存在: {message_id}")
            self._require_owned_row("accounts", int(row["account_id"]), "owner_user_id", client, "账号")

        if command == "import_generated_content":
            generated_id = self._int_arg(args, "generated_id")
            self._require_owned_row("generated_contents", generated_id, "owner_user_id", client, "生成内容")

    def _require_admin(self, client) -> dict[str, Any] | None:
        user = self._client_user(client)
        if client is not None and not user:
            raise self._auth_error_type("auth_required", "请先登录")
        if client is not None and user.get("role") != "admin":
            raise self._auth_error_type("forbidden", "只有管理员可以执行此操作")
        return user

    def _dispatch_command(self, command: str, args: Mapping[str, Any], *, client=None) -> Any:
        scheduler = self.scheduler
        if command == "auth_register":
            return self._auth_store.register(
                args.get("username"), args.get("password"), args.get("employee_name")
            )
        if command == "auth_login":
            # 重新登录先清空旧会话；如果新账号密码错误，不能继续沿用
            # 之前账号的管理员/员工权限。
            if client is not None:
                client.auth_user = None
            user = self._auth_store.authenticate(args.get("username"), args.get("password"))
            if client is not None:
                supplied_device_id = str(args.get("device_id") or "").strip()
                if supplied_device_id:
                    client.device_id = supplied_device_id[:128]
                self._touch_device(user, client)
                client.auth_user = user
            return {"authenticated": True, "user": user}
        if command == "auth_logout":
            if client is not None:
                client.auth_user = None
            return {"authenticated": False}
        if command == "auth_me":
            user = self._client_user(client)
            if not user:
                return {"authenticated": False, "user": None}
            current = self._auth_store.get(int(user.get("id") or 0))
            if not current or current.get("status") != "approved":
                if client is not None:
                    client.auth_user = None
                return {"authenticated": False, "user": None}
            if client is not None:
                client.auth_user = current
            return {"authenticated": True, "user": current}
        if command == "auth_list_users":
            self._require_admin(client)
            return {"items": self._auth_store.list_users()}
        if command in {"auth_approve_user", "auth_reject_user", "auth_disable_user"}:
            self._require_admin(client)
            user_id = self._int_arg(args, "user_id")
            action = {
                "auth_approve_user": self._auth_store.approve,
                "auth_reject_user": self._auth_store.reject,
                "auth_disable_user": self._auth_store.disable,
            }[command]
            return {"user": action(user_id)}
        if command == "auth_reset_password":
            self._require_admin(client)
            user_id = self._int_arg(args, "user_id")
            return {"user": self._auth_store.reset_password(user_id, args.get("password"))}
        self._authorize_command_scope(command, args, client)
        if command == "status":
            return self._scoped_status_report(client)
        if command == "get_task":
            return scheduler.get_task(self._int_arg(args, "task_id"))
        if command == "create_task":
            keyword = str(args.get("keyword") or "").strip()
            if not keyword:
                raise ValueError("keyword 不能为空")
            allowed = {
                "platform", "batch_size", "cooldown_seconds", "collect_mode",
                "target_count", "collect_types", "task_accounts", "only_with_comments",
                "output_dir", "search_sort", "execution_mode", "keyword_group_id",
                "monitor_interval_seconds",
            }
            kwargs = {key: args[key] for key in allowed if key in args}
            task_id = scheduler.create_task(keyword, **kwargs)
            self._set_owner_and_queue("tasks", int(task_id), client, "task")
            return {"task_id": int(task_id)}
        if command == "start":
            task_id = self._int_arg(args, "task_id")
            return {"task_id": int(scheduler.start(
                task_id, force_search=bool(args.get("force_search", False))
            ))}
        if command == "pause":
            task_id = self._int_arg(args, "task_id", required=False)
            scheduler.pause(task_id)
            return {"task_id": task_id, "paused": True}
        if command == "resume":
            task_id = self._int_arg(args, "task_id", required=False)
            scheduler.resume(task_id)
            return {"task_id": task_id, "paused": False}
        if command == "resume_task":
            return self._resume_task(self._int_arg(args, "task_id"))
        if command == "stop_task":
            task_id = self._int_arg(args, "task_id")
            scheduler.stop_task(task_id)
            return {"task_id": task_id, "stopped": True}
        if command == "delete_task":
            task_id = self._int_arg(args, "task_id")
            task_snapshot = None
            if self._owner_user_id(client) is not None:
                task_snapshot = self._require_owned_row(
                    "tasks", task_id, "owner_user_id", client, "任务"
                )
            with self._task_delete_lock:
                if task_id in self._task_deletions_inflight:
                    return {
                        "task_id": task_id,
                        "deleted": False,
                        "already_in_progress": True,
                    }
                self._task_deletions_inflight.add(task_id)
            try:
                deleted = bool(scheduler.delete_task(task_id))
                if deleted and task_snapshot is not None:
                    self._queue_deleted_snapshot(
                        "tasks", task_id, client, "task", task_snapshot
                    )
                return {"task_id": task_id, "deleted": deleted}
            finally:
                with self._task_delete_lock:
                    self._task_deletions_inflight.discard(task_id)
        if command == "export_task":
            return self._export_task(self._int_arg(args, "task_id"))
        if command == "open_task_folder":
            return self._open_task_folder(self._int_arg(args, "task_id"))
        if command == "create_browser_window":
            return self._create_browser_window(args)
        if command == "delete_browser_window":
            return self._delete_browser_window(args)
        if command == "add_account":
            name = str(args.get("name") or "").strip()
            if not name:
                raise ValueError("name 不能为空")
            window_id = str(args.get("bb_window_id") or "").strip()
            if not window_id:
                raise ValueError("请先在 BitBrowser 创建并打开窗口，再选择窗口 ID")
            owner_id = self._owner_user_id(client)
            if owner_id is not None:
                platform = str(args.get("platform") or "douyin").strip().lower()
                conn = self._lead_connection()
                try:
                    existing = conn.execute(
                        "SELECT id, owner_user_id FROM accounts WHERE platform = ? "
                        "AND (name = ? OR bb_window_id = ?) ORDER BY id LIMIT 1",
                        (platform, name, window_id),
                    ).fetchone()
                finally:
                    conn.close()
                if existing and int(existing["owner_user_id"] or 0) != owner_id:
                    raise self._auth_error_type("forbidden", "该账号或窗口已归属其他员工")
            account_id = scheduler.add_account(
                name, bb_window_id=window_id,
                platform=str(args.get("platform") or "douyin"),
            )
            self._set_owner_and_queue("accounts", int(account_id), client, "account")
            return {"account_id": int(account_id)}
        if command == "open_account_browser":
            return self._open_account_browser(args)
        if command == "bind_account":
            return self._bind_account(args)
        if command == "remove_account":
            account_id = self._int_arg(args, "account_id", required=False)
            account_snapshot = None
            if account_id is not None and self._owner_user_id(client) is not None:
                account_snapshot = self._require_owned_row(
                    "accounts", account_id, "owner_user_id", client, "账号"
                )
            removed = bool(scheduler.remove_account(
                name=args.get("name"), account_id=account_id,
                platform=args.get("platform"),
            ))
            if removed and account_snapshot is not None:
                self._queue_deleted_snapshot(
                    "accounts", account_id, client, "account", account_snapshot
                )
            return {"removed": removed}
        if command == "resolve_human":
            account_id = self._int_arg(args, "account_id")
            return {"account_id": account_id, "resolved": bool(scheduler.resolve_human(account_id))}
        if command == "waiting_accounts":
            task_id = self._int_arg(args, "task_id")
            return {"account_ids": sorted(scheduler.waiting_accounts_for_task(task_id))}
        if command == "configure_monitoring":
            task_id = self._int_arg(args, "task_id")
            interval = self._int_arg(args, "interval_seconds")
            return scheduler.configure_monitoring(
                task_id, interval_seconds=interval,
                enabled=bool(args.get("enabled", True)),
                search_sort=args.get("search_sort"),
            )
        if command == "start_monitoring":
            return {"started": bool(scheduler.start_monitoring(
                poll_seconds=float(args.get("poll_seconds", 15.0))
            ))}
        if command == "stop_monitoring":
            scheduler.stop_monitoring()
            return {"stopped": True}
        if command == "run_monitoring_once":
            return {"task_ids": list(scheduler.run_monitoring_once())}
        if command == "list_leads":
            return self._list_leads(args, client)
        if command == "list_keyword_groups":
            return self._list_keyword_groups(args, client)
        if command == "create_keyword_group":
            return self._create_keyword_group(args, client)
        if command == "update_keyword_group":
            return self._update_keyword_group(args, client)
        if command == "add_leads_to_interaction":
            return self._add_leads_to_interaction(args)
        if command == "add_leads_to_private_message":
            return self._add_leads_to_private_message(args)
        if command == "export_leads":
            return self._export_leads(args)
        if command == "export_leads_by_task":
            return self._export_leads_by_task(args, client)
        if command == "tag_leads":
            return self._tag_leads(args)
        if command == "list_interactions":
            return self._list_interactions(args, client)
        if command == "export_interactions_by_task":
            return self._export_interactions_by_task(args, client)
        if command == "interaction_action":
            return self._interaction_action(args)
        if command == "send_interactions":
            return self._send_interactions(args)
        if command == "list_publish_drafts":
            return self._list_publish_drafts(args, client)
        if command == "create_publish_draft":
            return self._create_publish_draft(args, client)
        if command == "update_publish_variant":
            return self._update_publish_variant(args, client)
        if command == "publish_draft_action":
            return self._publish_draft_action(args, client)
        if command == "delete_publish_draft":
            return self._delete_publish_draft(args, client)
        if command == "add_publish_assets":
            return self._add_publish_assets(args, client)
        if command == "delete_publish_asset":
            return self._delete_publish_asset(args, client)
        if command == "preview_publish_draft":
            return self._preview_publish_draft(args)
        if command == "real_publish_draft":
            return self._real_publish_draft(args)
        if command == "list_account_contents":
            return self._list_account_contents(args)
        if command == "sync_account_contents":
            return self._sync_account_contents(args)
        if command == "list_content_comments":
            return self._list_content_comments(args)
        if command == "list_generated_contents":
            return self._list_generated_contents(args, client)
        if command == "generate_content":
            return self._generate_content(args, client)
        if command == "import_generated_content":
            return self._import_generated_content(args, client)
        if command == "schedule_publish":
            return self._schedule_publish(args, client)
        if command == "list_published_messages":
            return self._list_published_messages(args, client)
        if command == "sync_published_messages":
            return self._sync_published_messages(args, client)
        if command == "mark_published_message":
            return self._mark_published_message(args)
        if command == "test_llm_api":
            return self._test_llm_api()
        if command == "save_template":
            return self._save_template(args)
        if command == "diagnostics_snapshot":
            return self._diagnostics_snapshot()
        if command == "run_platform_health":
            return self._run_platform_health()
        if command == "save_settings":
            return self._save_settings(args)
        if command == "inspect_bitbrowser":
            return self._inspect_bitbrowser()
        if command == "run_live_diagnostics":
            return self._run_live_diagnostics()
        if command == "export_operation_log":
            return self._export_operation_log()
        if command == "sync_status":
            return self._sync_status(client)
        if command == "save_sync_settings":
            return self._save_sync_settings(args, client)
        if command == "sync_now":
            return self._sync_now(client)
        if command == "admin_dashboard":
            return self._admin_dashboard(client)
        if command == "admin_audit_logs":
            return self._admin_audit_logs(args, client)
        if command == "admin_create_backup":
            return self._admin_create_backup(client)
        if command == "admin_list_backups":
            return self._admin_list_backups(client)
        if command == "admin_validate_backup":
            return self._admin_validate_backup(args, client)
        if command == "admin_restore_backup":
            return self._admin_restore_backup(args, client)
        if command == "admin_set_device_status":
            return self._admin_set_device_status(args, client)
        raise ProtocolError(f"未实现的命令：{command}")


__all__ = ["BackendService"]
