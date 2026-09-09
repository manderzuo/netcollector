# -*- coding: utf-8 -*-
"""统一账号服务客户端。

桌面端仍保留本机认证作为离线兼容路径；当登录/注册界面传入统一账号
服务地址时，注册申请和审批状态走 HTTPS 服务端，不再只写入当前电脑的
``data/platform_gui.db``。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping


class RemoteAuthError(RuntimeError):
    """可安全展示给界面的统一账号服务错误。"""

    def __init__(self, code: str, message: str):
        self.code = str(code or "remote_auth_error")
        self.message = str(message or "统一账号服务请求失败")
        super().__init__(self.message)


class RemoteAuthClient:
    """调用统一账号服务；只传输账号认证字段，不传浏览器和业务数据。"""

    def __init__(self, base_url: str, *, timeout: float = 8.0):
        self.base_url = str(base_url or "").strip().rstrip("/")
        if not self.base_url:
            raise RemoteAuthError("not_configured", "尚未配置统一账号服务地址")
        if not self.base_url.lower().startswith(("http://", "https://")):
            raise RemoteAuthError("invalid_url", "统一账号服务地址必须以 http:// 或 https:// 开头")
        self.timeout = max(3.0, min(30.0, float(timeout or 8.0)))

    def _request(self, action: str, payload: Mapping[str, Any] | None = None,
                 token: str = "") -> dict[str, Any]:
        body = json.dumps(dict(payload or {}), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/api/collector/auth/" + str(action).strip("/"),
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                **({"Authorization": "Bearer " + str(token).strip()} if str(token).strip() else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(8192).decode("utf-8", errors="replace")
                parsed = json.loads(raw or "{}")
            except (OSError, UnicodeError, ValueError):
                parsed = {}
            if isinstance(parsed, Mapping):
                raise RemoteAuthError(
                    str(parsed.get("code") or ("unauthorized" if exc.code in {401, 403} else "remote_http_error")),
                    str(parsed.get("message") or f"统一账号服务返回 HTTP {exc.code}"),
                ) from exc
            raise RemoteAuthError("remote_http_error", f"统一账号服务返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RemoteAuthError("connection_failed", f"无法连接统一账号服务：{type(exc).__name__}") from exc
        try:
            result = json.loads(raw or "{}")
        except ValueError as exc:
            raise RemoteAuthError("invalid_response", "统一账号服务返回的内容不是有效 JSON") from exc
        if not isinstance(result, Mapping):
            raise RemoteAuthError("invalid_response", "统一账号服务返回格式无效")
        if result.get("ok") is False:
            raise RemoteAuthError(
                str(result.get("code") or "remote_auth_error"),
                str(result.get("message") or "统一账号服务拒绝请求"),
            )
        return dict(result)

    def register(self, username: str, password: str, employee_name: str) -> dict[str, Any]:
        return self._request("register", {
            "username": str(username or ""),
            "password": str(password or ""),
            "employee_name": str(employee_name or ""),
        })

    def login(self, username: str, password: str) -> dict[str, Any]:
        return self._request("login", {
            "username": str(username or ""),
            "password": str(password or ""),
        })

    def me(self, token: str) -> dict[str, Any]:
        return self._request("me", {}, token)

    def logout(self, token: str) -> dict[str, Any]:
        return self._request("logout", {}, token)

    def list_users(self, token: str) -> dict[str, Any]:
        return self._request("list_users", {}, token)

    def set_status(self, action: str, user_id: int, token: str) -> dict[str, Any]:
        return self._request(action, {"user_id": int(user_id)}, token)

    def reset_password(self, user_id: int, password: str, token: str) -> dict[str, Any]:
        return self._request("reset_password", {
            "user_id": int(user_id), "password": str(password or ""),
        }, token)


__all__ = ["RemoteAuthClient", "RemoteAuthError"]
