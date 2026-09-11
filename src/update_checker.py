# -*- coding: utf-8 -*-
"""应用更新检查的纯 Python 实现。

启动器和设置页共用同一套规则：版本号用于发现正式升级，构建标识用于
发现“版本号不变但代码已经更新”的修复包。网络请求只读取更新清单，
不会上传本地业务数据、账号信息或 API 密钥。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_MANIFEST_URL = "https://www.gemstory.cn/release/latest.json"
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class UpdateCheckError(RuntimeError):
    """更新清单不可用或不符合发布格式。"""


def _version_key(value: Any) -> tuple[int, ...]:
    """将 2、2.1、2.1.1 等版本转换成可比较的数字元组。"""

    text = str(value or "").strip()
    numbers = re.findall(r"\d+", text)
    if not numbers:
        raise UpdateCheckError(f"版本号无效：{text or '空值'}")
    return tuple(int(item) for item in numbers)


def compare_versions(left: Any, right: Any) -> int:
    """比较两个版本号，返回 -1、0、1。"""

    a = _version_key(left)
    b = _version_key(right)
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return (a > b) - (a < b)


def normalize_manifest(payload: Mapping[str, Any]) -> dict[str, str]:
    """校验线上清单并返回只包含更新所需字段的安全副本。"""

    if not isinstance(payload, Mapping):
        raise UpdateCheckError("更新清单不是 JSON 对象")
    version = str(payload.get("version") or "").strip()
    _version_key(version)
    download_url = str(payload.get("download_url") or "").strip()
    parsed = urlsplit(download_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise UpdateCheckError("更新下载地址无效")
    sha256 = str(payload.get("sha256") or "").strip().upper()
    if not _SHA256_RE.fullmatch(sha256):
        raise UpdateCheckError("更新包 SHA256 无效")
    build_id = str(payload.get("build_id") or payload.get("release_id") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    return {
        "version": version,
        "download_url": download_url,
        "sha256": sha256,
        "build_id": build_id,
        "notes": notes,
    }


def add_cache_buster(url: str, value: str) -> str:
    """给清单请求加查询参数，避免 CDN/代理返回旧清单。"""

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["_client_check"] = str(value or "1")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def fetch_manifest(url: str = DEFAULT_MANIFEST_URL, timeout: float = 15.0) -> dict[str, str]:
    """从线上读取并校验更新清单。"""

    target = str(url or DEFAULT_MANIFEST_URL).strip() or DEFAULT_MANIFEST_URL
    target = add_cache_buster(target, str(int(time.time())))
    request = Request(
        target,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "CollectorWorkbench-UpdateChecker/2.2.4",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=max(3.0, float(timeout))) as response:
            raw = response.read(1024 * 1024 + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise UpdateCheckError(f"无法连接更新服务：{type(exc).__name__}") from exc
    if len(raw) > 1024 * 1024:
        raise UpdateCheckError("更新清单超过允许大小")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateCheckError("更新清单不是有效 JSON") from exc
    return normalize_manifest(payload)


def read_local_build_id(app_root: str | Path) -> str:
    """读取安装包构建标识；旧包没有该文件时返回空字符串。"""

    path = Path(app_root) / "BUILD_ID.txt"
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return ""


def is_update_available(
    current_version: Any,
    current_build_id: Any,
    manifest: Mapping[str, Any],
) -> tuple[bool, str]:
    """判断是否需要更新，并返回适合显示的原因。"""

    remote = normalize_manifest(manifest)
    version_result = compare_versions(remote["version"], current_version)
    if version_result > 0:
        return True, "发现新版本"
    if version_result < 0:
        return False, "本地版本较新"
    remote_build = remote.get("build_id", "")
    local_build = str(current_build_id or "").strip()
    if remote_build and remote_build != local_build:
        return True, "发现同版本修复包"
    return False, "已是最新版本"


__all__ = [
    "DEFAULT_MANIFEST_URL",
    "UpdateCheckError",
    "add_cache_buster",
    "compare_versions",
    "fetch_manifest",
    "is_update_available",
    "normalize_manifest",
    "read_local_build_id",
]
