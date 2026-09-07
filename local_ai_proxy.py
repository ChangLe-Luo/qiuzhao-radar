"""Backward-compatible bridge for older clients.

The real service is ``server.py`` on port 5500 and lets Codex/CCSwitch handle
credentials. This optional 8765 listener only forwards local requests to that
service; it never reads or stores an API key.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


TARGET = "http://127.0.0.1:5500"
MAX_BODY = 16 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status: int, payload: object) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/chat":
            self.send_json(405, {"error": "GET 不支持；请使用 POST /chat"})
            return
        if self.path not in ("/", "/health"):
            self.send_json(404, {"error": "not found"})
            return
        try:
            request = urllib.request.Request(TARGET + "/api/health", headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=4) as response:
                health = json.loads(response.read().decode("utf-8", "replace"))
            self.send_json(200, {
                "status": "ok",
                "service": "local-ai-proxy-compat",
                "model": health.get("model"),
                "chat_endpoint": "POST /chat",
                "api_key_configured": bool(health.get("ai_key_configured")),
                "backend": TARGET,
            })
        except Exception as exc:
            self.send_json(503, {"status": "unavailable", "error": f"主服务未启动：{exc}"})

    def do_POST(self) -> None:
        if self.path != "/chat":
            self.send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY:
                raise ValueError("请求体过大")
            body = self.rfile.read(length)
            request = urllib.request.Request(
                TARGET + "/api/chat",
                data=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=190) as response:
                payload = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            self.send_json(exc.code, {"error": "主服务返回错误", "detail": detail})
        except Exception as exc:
            self.send_json(503, {"error": f"主服务不可用：{exc}"})

    def log_message(self, *_args) -> None:
        return


class ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    ReusableHTTPServer(("0.0.0.0", 8765), Handler).serve_forever()
