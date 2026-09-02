# -*- coding: utf-8 -*-
"""生成真实浏览器健康诊断的脱敏导出包。

导出包只用于复现平台连接/登录/验证/滚轮问题，不包含 Cookie、Token、API Key、
CDP 地址或原始浏览器窗口标识。截图在写入前会再次模糊处理。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any


_SENSITIVE_KEY = re.compile(
    r"(?:cookie|token|secret|api[_-]?key|password|passwd|authorization|websocket|cdp|ws_url)",
    re.IGNORECASE,
)
_SENSITIVE_QUERY = re.compile(
    r"([?&](?:xsec_token|token|access_token|auth|signature|sign|key|password)=)[^&#\s]+",
    re.IGNORECASE,
)
_SENSITIVE_INLINE = re.compile(
    r"(\b(?:cookie|token|access[_-]?token|secret|api[_-]?key|password|authorization)\s*[:=]\s*)[^\s,;&]+",
    re.IGNORECASE,
)
_CDP_URL = re.compile(r"wss?://[^\s\"']+", re.IGNORECASE)


def _account_alias(account_id: Any) -> str:
    digest = hashlib.sha256(str(account_id).encode("utf-8")).hexdigest()[:8]
    return f"账号#{digest}"


def _tail_text(path: Path, max_lines: int = 300) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            lines = stream.readlines()
        return "".join(lines[-max_lines:])
    except OSError:
        return ""


def _redact_text(value: Any, names: dict[str, str], windows: dict[str, str]) -> str:
    text = str(value or "")
    text = _CDP_URL.sub("[CDP地址已脱敏]", text)
    text = _SENSITIVE_QUERY.sub(r"\1[已脱敏]", text)
    text = _SENSITIVE_INLINE.sub(r"\1[已脱敏]", text)
    for source, replacement in sorted(names.items(), key=lambda item: len(item[0]), reverse=True):
        if source:
            text = text.replace(source, replacement)
    for source, replacement in sorted(windows.items(), key=lambda item: len(item[0]), reverse=True):
        if source:
            text = text.replace(source, replacement)
    return text


def _redact(value: Any, names: dict[str, str], windows: dict[str, str], account_map: dict[int, str], key: str = "") -> Any:
    if _SENSITIVE_KEY.search(key or ""):
        return "[已脱敏]"
    if isinstance(value, dict):
        return {str(k): _redact(v, names, windows, account_map, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, names, windows, account_map, key) for item in value]
    if key.lower() == "account_id" and isinstance(value, (int, str)) and str(value).isdigit():
        return account_map.get(int(value), _account_alias(value))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _redact_text(value, names, windows) if isinstance(value, str) else value
    return _redact_text(value, names, windows)


def _blur_image(source: Path, target: Path) -> bool:
    """将截图模糊后写出；失败时不复制原图。"""
    try:
        from PIL import Image, ImageFilter

        with Image.open(source) as image:
            image = image.convert("RGB")
            radius = max(5, min(24, max(image.size) // 90))
            image.filter(ImageFilter.GaussianBlur(radius=radius)).save(target, format="PNG")
        return True
    except Exception:  # noqa: BLE001
        return False


class DiagnosticPackageExporter:
    """从本地数据库、日志和本次诊断截图生成可分享的 ZIP。"""

    def __init__(self, db_path: str, *, project_root: str | None = None):
        self.db_path = os.path.abspath(db_path)
        self.project_root = os.path.abspath(project_root or os.path.dirname(os.path.dirname(self.db_path)))

    def export(self, destination_dir: str | None = None, health_result: dict | None = None) -> dict:
        destination = Path(destination_dir or os.path.join(
            self.project_root, "data", "diagnostics", "packages"
        ))
        destination.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive = destination / f"diagnostic_{stamp}.zip"
        if archive.exists():
            archive = destination / f"diagnostic_{stamp}_{os.getpid()}.zip"

        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            accounts = [dict(row) for row in conn.execute(
                "SELECT id, name, bb_window_id FROM accounts ORDER BY id"
            ).fetchall()]
            events = [dict(row) for row in conn.execute(
                "SELECT id, platform, account_id, check_name, status, detail, run_id, metadata, observed_at "
                "FROM health_events ORDER BY id DESC LIMIT 300"
            ).fetchall()]
        finally:
            conn.close()

        names = {str(row.get("name") or ""): _account_alias(row.get("id")) for row in accounts}
        windows = {str(row.get("bb_window_id") or ""): "[窗口标识已脱敏]" for row in accounts}
        account_map = {int(row["id"]): _account_alias(row["id"]) for row in accounts}
        redacted_events = []
        for event in events:
            item = dict(event)
            try:
                metadata = json.loads(item.get("metadata") or "{}")
            except (TypeError, ValueError):
                metadata = {"raw": item.get("metadata") or ""}
            item["account_id"] = account_map.get(int(item["account_id"]), _account_alias(item["account_id"])) \
                if item.get("account_id") is not None else None
            item["detail"] = _redact_text(item.get("detail"), names, windows)
            item["run_id"] = _redact_text(item.get("run_id"), names, windows)
            item["metadata"] = _redact(metadata, names, windows, account_map)
            redacted_events.append(item)

        result = health_result or {}
        screenshot_paths = list(result.get("screenshot_paths") or [])
        files: list[str] = ["README.txt", "summary.json", "health_events.json", "health_events.csv"]
        with tempfile.TemporaryDirectory(prefix="diagnostic_export_", dir=str(destination)) as temp_name:
            temp = Path(temp_name)
            (temp / "README.txt").write_text(
                "多账号采集平台真实浏览器诊断包\n\n"
                "本包用于定位平台首页、登录状态、人工验证和滚轮响应问题。\n"
                "包内内容已脱敏：不包含 Cookie、Token、API Key、CDP 地址、原始窗口标识或原始截图。\n"
                "截图仅保留模糊版本；日志和健康事件仅保留最近记录。\n",
                encoding="utf-8",
            )
            summary = {
                "package_version": "1",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "diagnostic_mode": result.get("mode") or "unknown",
                "checked_at": result.get("checked_at") or "",
                "event_count": len(redacted_events),
                "screenshot_count": 0,
                "redaction": "account/window names hashed; secrets, cookies, tokens and CDP addresses removed",
            }
            (temp / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (temp / "health_events.json").write_text(
                json.dumps(redacted_events, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            with (temp / "health_events.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=(
                    "id", "platform", "account_id", "check_name", "status", "detail", "run_id", "observed_at"
                ))
                writer.writeheader()
                for event in redacted_events:
                    writer.writerow({key: event.get(key, "") for key in writer.fieldnames})

            log_dir = Path(self.project_root) / "data" / "logs"
            for log_path in sorted(log_dir.glob("*.log")) + sorted(log_dir.glob("*.jsonl")):
                content = _tail_text(log_path)
                if not content:
                    continue
                relative = Path("logs") / f"{log_path.stem}{log_path.suffix}.txt"
                output = temp / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(_redact_text(content, names, windows), encoding="utf-8")
                files.append(str(relative).replace("\\", "/"))

            screenshot_dir = temp / "screenshots"
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            for index, source_name in enumerate(screenshot_paths, start=1):
                source = Path(str(source_name))
                if not source.is_file():
                    continue
                target = screenshot_dir / f"page_{index:02d}.png"
                if _blur_image(source, target):
                    files.append(f"screenshots/{target.name}")
                    summary["screenshot_count"] += 1
            (temp / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for path in temp.rglob("*"):
                    if path.is_file():
                        bundle.write(path, path.relative_to(temp).as_posix())

        return {
            "path": str(archive),
            "size": archive.stat().st_size,
            "files": files,
            "screenshot_count": summary["screenshot_count"],
        }
