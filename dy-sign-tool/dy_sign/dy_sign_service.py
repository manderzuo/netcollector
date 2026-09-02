# -*- coding: utf-8 -*-
"""dy_sign_service.py — 抖音签名服务（独立 HTTP 服务）。

暴露三个接口（默认 127.0.0.1:8765，仅本机）：
  GET /sign?url=<完整URL>       → {"a_bogus": "..."}
  GET /mstoken?ttwid=<ttwid>    → {"ms_token": "..."}   （带缓存，失败返回空串）
  GET /sign_full?url=<URL>&ttwid=<ttwid>
                                → {"a_bogus": "...", "ms_token": "..."}
  GET /health                   → {"ok": true, "algorithm": "ab_pure",
                                   "bdms_version": "1.0.1.19-fix.01",
                                   "last_sign_ts": ..., "mstoken_cached": bool}

运行：
  python -m src.dy_sign.dy_sign_service --port 8765
  或
  python src/dy_sign/dy_sign_service.py --port 8765

依赖：requests（mstoken 上报用）。签名本体（ab_pure/sm3）零依赖。
"""

import argparse
import json
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from .ab_pure import ABogusPureSigner
    from .mstoken import get_mstoken
    from .fingerprint import get_profile
except ImportError:  # 直接运行 python dy_sign_service.py
    from ab_pure import ABogusPureSigner
    from mstoken import get_mstoken
    from fingerprint import get_profile

DEFAULT_PORT = 8765
BDMS_VERSION = "1.0.1.19-fix.01"


class DySignService:
    """签名服务核心逻辑（不依赖 HTTP，便于单元测试）。"""

    def __init__(self, fixed: bool = False):
        self._signer = ABogusPureSigner(fixed=fixed)
        self.last_sign_ts = 0
        self.sign_count = 0

    def sign_a_bogus(self, url: str) -> str:
        sig = self._signer.sign(url)
        self.last_sign_ts = int(time.time())
        self.sign_count += 1
        return sig

    def sign_a_bogus_for(self, api_path: str, query: str) -> str:
        """按接口路径 + query 拼 URL 后签名。"""
        url = "https://www.douyin.com" + api_path
        if query:
            url += "?" + query
        return self.sign_a_bogus(url)

    def get_mstoken(self, ttwid: str = "") -> str:
        try:
            return get_mstoken(ttwid=ttwid or None, use_cache=True) or ""
        except Exception:
            return ""

    def health(self) -> dict:
        return {
            "ok": True,
            "algorithm": "ab_pure",
            "bdms_version": BDMS_VERSION,
            "last_sign_ts": self.last_sign_ts,
            "sign_count": self.sign_count,
        }


_service = DySignService(fixed=False)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默，不刷屏
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
        q = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                return self._send_json(_service.health())
            if parsed.path == "/sign":
                url = (q.get("url") or [""])[0]
                if not url:
                    return self._send_json({"error": "missing url"}, 400)
                return self._send_json({"a_bogus": _service.sign_a_bogus(url)})
            if parsed.path == "/mstoken":
                ttwid = (q.get("ttwid") or [""])[0]
                return self._send_json({"ms_token": _service.get_mstoken(ttwid)})
            if parsed.path == "/sign_full":
                url = (q.get("url") or [""])[0]
                ttwid = (q.get("ttwid") or [""])[0]
                if not url:
                    return self._send_json({"error": "missing url"}, 400)
                return self._send_json({
                    "a_bogus": _service.sign_a_bogus(url),
                    "ms_token": _service.get_mstoken(ttwid),
                })
            return self._send_json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def main():
    ap = argparse.ArgumentParser(description="抖音签名服务")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"[dy_sign] 签名服务运行中 http://{args.host}:{args.port} "
          f"(bdms {BDMS_VERSION})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[dy_sign] 已停止")


if __name__ == "__main__":
    main()
