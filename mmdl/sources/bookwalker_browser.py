"""Receive BW browser-extension sessions for the lifetime of one download."""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
import threading
from urllib.parse import parse_qs, urlsplit


def _reader(url):
    address = urlsplit(url)
    return address.hostname, parse_qs(address.query)["cid"][0]


class BrowserBridge:
    def __init__(self, port, reader_url):
        self.port = port
        self.reader = _reader(reader_url)
        self.token = secrets.token_urlsafe(18)
        self.ready = threading.Event()
        self.payload = None

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def respond(self, status, result):
                data = json.dumps(result).encode("utf-8")
                self.send_response(status)
                origin = self.headers.get("Origin", "")
                if origin.startswith("chrome-extension://"):
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST")
                    self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_OPTIONS(self):
                if not self.headers.get("Origin", "").startswith("chrome-extension://"):
                    self.respond(403, {"error": "仅支持浏览器辅助扩展连接"})
                    return
                self.respond(200, {})

            def do_GET(self):
                if self.path != "/pair":
                    self.respond(404, {})
                    return
                proof = hmac.new(owner.token.encode(), b"ezmanga-bookwalker", hashlib.sha256).hexdigest()
                self.respond(200, {"proof": proof})

            def do_POST(self):
                if self.path != "/session":
                    self.respond(404, {})
                    return
                if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + owner.token):
                    self.respond(403, {"error": "配对码不正确"})
                    return
                try:
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    reader = _reader(payload["readerUrl"])
                except (ValueError, KeyError, TypeError):
                    self.respond(400, {"error": "无效的阅读器会话"})
                    return
                if reader != owner.reader:
                    self.respond(409, {"error": "阅读器与下载命令中的书籍不一致"})
                    return
                owner.payload = payload
                owner.ready.set()
                self.respond(200, {"accepted": True})

            def log_message(self, *args):
                # Cookie-bearing requests must not reach access logs.
                pass

        self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        print(f"[bw] 在普通浏览器的 BW 阅读器标签页点击 Ezmanga BW 扩展，连接端口 {self.port}")
        print(f"[bw] 本次配对码：{self.token}", flush=True)
        return self

    def session(self):
        if not self.ready.wait(120):
            raise RuntimeError("BW 辅助扩展未连接；请打开阅读器并填写本次配对码")
        payload = self.payload
        if "error" in payload:
            raise RuntimeError(payload["error"])
        return payload

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
