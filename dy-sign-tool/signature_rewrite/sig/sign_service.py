# -*- coding: utf-8 -*-
"""sign_service.py — 签名 HTTP 服务（clean-room 重写版）。

接口（默认 127.0.0.1:8766，避免与旧服务 8765 冲突）：
  GET /health                   → 健康状态
  GET /sign?url=<完整URL>       → {"a_bogus": "..."}
  GET /ms_token?ttwid=<ttwid>   → {"ms_token": "..."}
  GET /sign_full?url=&ttwid=    → {"a_bogus": "...", "ms_token": "..."}

运行：
  python -m sig.sign_service --port 8766
"""

import argparse
import json
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .a_bogus import ABogusSigner
from .ms_token import fetch_ms_token

_DEFAULT_PORT = 8766
_ALGORITHM_VERSION = "a_bogus"


class SignatureService:
    """签名服务核心逻辑（与 HTTP 解耦，便于单测）。"""

    def __init__(self, fixed: bool = False):
        self._signer = ABogusSigner(fixed=fixed)
        self.last_sign_ts = 0
        self.sign_count = 0

    def sign_a_bogus(self, url: str) -> str:
        signature = self._signer.sign(url)
        self.last_sign_ts = int(time.time())
        self.sign_count += 1
        return signature

    def sign_for_path(self, api_path: str, query: str) -> str:
        """按接口路径 + query 拼 URL 后签名。"""
        url = "https://www.douyin.com" + api_path
        if query:
            url += "?" + query
        return self.sign_a_bogus(url)

    def get_ms_token(self, ttwid: str = "") -> str:
        try:
            return fetch_ms_token(ttwid=ttwid or None, use_cache=True) or ""
        except Exception:  # noqa: BLE001
            return ""

    def health(self) -> dict:
        return {
            "ok": True,
            "algorithm": _ALGORITHM_VERSION,
            "last_sign_ts": self.last_sign_ts,
            "sign_count": self.sign_count,
        }


_service = SignatureService(fixed=False)


class _RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                return self._send_json(_service.health())
            if parsed.path == "/sign":
                url = (query.get("url") or [""])[0]
                if not url:
                    return self._send_json({"error": "missing url"}, 400)
                return self._send_json({"a_bogus": _service.sign_a_bogus(url)})
            if parsed.path == "/ms_token":
                ttwid = (query.get("ttwid") or [""])[0]
                return self._send_json({"ms_token": _service.get_ms_token(ttwid)})
            if parsed.path == "/sign_full":
                url = (query.get("url") or [""])[0]
                ttwid = (query.get("ttwid") or [""])[0]
                if not url:
                    return self._send_json({"error": "missing url"}, 400)
                return self._send_json({
                    "a_bogus": _service.sign_a_bogus(url),
                    "ms_token": _service.get_ms_token(ttwid),
                })
            return self._send_json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def main():
    parser = argparse.ArgumentParser(description="签名 HTTP 服务（重写版）")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), _RequestHandler)
    print(f"[sig] 签名服务运行中 http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[sig] 已停止")


if __name__ == "__main__":
    main()
