"""Local download service restricted to the registered BW extension."""
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import secrets
import sys
import threading
from urllib.parse import parse_qs, urlsplit

from mmdl.core.archive import build_archives
from mmdl.core.driver import write_capture
from mmdl.core.naming import clean_name


PROFILE = Path.home() / ".mmdl" / "bookwalker_extension.json"


def register_extension(extension_id):
    if re.fullmatch(r"[a-p]{32}", extension_id) is None:
        raise ValueError("BW extension ID must contain 32 letters from a to p")
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE.write_text(json.dumps({"extension_id": extension_id}), encoding="utf-8")
    print("[bw] 扩展已注册；运行 --source bookwalker --bw-serve 后，打开扩展并点击“开始下载”")


def _reader(url):
    address = urlsplit(url)
    return address.hostname, parse_qs(address.query)["cid"][0]


class DownloadJob:
    def __init__(self, payload):
        address = urlsplit(payload["readerUrl"])
        if address.scheme != "https" or address.hostname not in {
            "viewer.bookwalker.jp", "viewer-trial.bookwalker.jp", "viewer-df.bookwalker.jp"
        }:
            raise ValueError("请从 BW 日本版漫画阅读器启动下载")
        self.reader = _reader(payload["readerUrl"])
        self.payload = payload
        self.state = "downloading"
        self.message = "正在读取漫画配置…"
        self.output = ""
        self.console = sys.stdout
        self.buffer = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.payload = None

    def session(self):
        if "error" in self.payload:
            raise RuntimeError(self.payload["error"])
        return self.payload

    def update(self, payload):
        if _reader(payload["readerUrl"]) != self.reader:
            raise ValueError("阅读器已切换到其他书籍，请保持原阅读器打开")
        self.payload = payload

    def status(self, job_id):
        return {"job": job_id, "state": self.state, "message": self.message, "output": self.output}

    def write(self, text):
        self.console.write(text)
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                self.message = line.strip()
        return len(text)

    def flush(self):
        self.console.flush()

    def run(self, source, out_dir, epub, archive_formats):
        try:
            with redirect_stdout(self):
                result = source.capture_from_url(self.payload["readerUrl"], browser_session=self)
                self.state = "writing"
                result.title.name = clean_name(result.title.name)
                write_capture(source, result, out_dir, epub=epub, lang="ja")
                title_dir = Path(out_dir) / result.title.name
                for extension in archive_formats:
                    for archive in build_archives(title_dir, extension=extension):
                        print(f"[{extension}] {archive}")
                self.output = str(title_dir.resolve())
                self.message = "下载完成：" + self.output
                self.state = "done"
        except Exception as error:
            # Worker failures must be returned to the extension instead of terminating the service.
            self.message = str(error)
            self.state = "error"
        finally:
            self.payload = None


def serve_downloads(source, out_dir, *, epub=False, archive_formats=("cbz",)):
    if not PROFILE.is_file():
        raise RuntimeError("Run --source bookwalker --setup to register the BW extension first")
    extension_id = json.loads(PROFILE.read_text(encoding="utf-8"))["extension_id"]
    allowed_origin = "chrome-extension://" + extension_id
    jobs = {}
    active_thread = None

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, result):
            data = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            if self.headers.get("Origin") == allowed_origin:
                self.send_header("Access-Control-Allow-Origin", allowed_origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "POST")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            if self.headers.get("Origin") != allowed_origin:
                self.respond(403, {"error": "请使用已注册的 Ezmanga BW 扩展"})
                return False
            return True

        def do_OPTIONS(self):
            if self.authorized():
                self.respond(200, {})

        def do_POST(self):
            nonlocal active_thread
            if not self.authorized():
                return
            try:
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/download":
                    if active_thread is not None and active_thread.is_alive():
                        self.respond(409, {"error": "已有下载正在进行，请等待完成"})
                        return
                    job = DownloadJob(payload)
                    job_id = secrets.token_hex(8)
                    jobs[job_id] = job
                    active_thread = threading.Thread(target=job.run,
                        args=(source, out_dir, epub, archive_formats), daemon=True)
                    active_thread.start()
                elif self.path.startswith("/jobs/"):
                    job_id = self.path.removeprefix("/jobs/")
                    job = jobs[job_id]
                    if payload["session"] is not None and job.state == "downloading":
                        job.update(payload["session"])
                else:
                    self.respond(404, {})
                    return
                self.respond(200, job.status(job_id))
            except (ValueError, KeyError, TypeError) as error:
                self.respond(400, {"error": str(error)})

        def log_message(self, *args):
            # Cookie-bearing requests must not reach access logs.
            pass

    with HTTPServer(("127.0.0.1", source.bw_port), Handler) as server:
        print(f"[bw] 下载服务已启动：127.0.0.1:{server.server_port}；打开 Ezmanga BW 后点击“开始下载”", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
