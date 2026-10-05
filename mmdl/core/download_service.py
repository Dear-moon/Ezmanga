"""Local download service restricted to the registered browser extension."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from http.cookiejar import Cookie, CookieJar
import json
from pathlib import Path
import re
import secrets
import sys
import threading
from urllib.parse import parse_qs, urlsplit

from mmdl.core.archive import build_archives
from mmdl.core.driver import download_title, write_capture
from mmdl.core.epub import build_epub
from mmdl.core.naming import clean_name, parse_chapter_range
from mmdl.sources import get_source


PROFILE = Path.home() / ".mmdl" / "browser_extension.json"
SERVICE_SOURCES = ("mangamillion", "tongli", "bookwalker", "bilibili", "kobo", "lightnovel")
MAX_ACTIVE_JOBS = 2
_OUTPUT_LOCK = threading.Lock()


class _JobOutput:
    def __init__(self, console):
        self.console = console
        self.targets = threading.local()
        self.users = 0

    def write(self, text):
        return getattr(self.targets, "job", self.console).write(text)

    def flush(self):
        getattr(self.targets, "job", self.console).flush()

    def __getattr__(self, name):
        return getattr(self.console, name)


def register_extension(extension_id):
    if re.fullmatch(r"[a-p]{32}", extension_id) is None:
        raise ValueError("Extension ID must contain 32 letters from a to p")
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE.write_text(json.dumps({"extension_id": extension_id}), encoding="utf-8")
    print("[service] 扩展已注册；运行 --serve 后，打开扩展并点击“开始下载”")


def _reader(url):
    address = urlsplit(url)
    return address.hostname, parse_qs(address.query)["cid"][0]


def resolve_title(source, value):
    value = value.strip()
    if not value:
        raise ValueError("请填写作品 ID、链接或本地 ACSM 路径")
    address = urlsplit(value)
    if source.name == "bookwalker":
        if address.scheme != "https" or address.hostname not in {
            "viewer.bookwalker.jp", "viewer-trial.bookwalker.jp", "viewer-df.bookwalker.jp"
        }:
            raise ValueError("BW 下载需要当前日本版漫画阅读器链接")
        _reader(value)
    elif source.name == "bilibili":
        source._ids(value)
    elif source.name == "lightnovel":
        value = str(source._book_id(value))
    elif source.name == "tongli" and address.scheme:
        if address.scheme != "https" or address.hostname != "ebook.tongli.com.tw" or address.path != "/book":
            raise ValueError("东立需要作品 ID 或官网 /book 链接")
        value = parse_qs(address.query)["id"][0]
    elif source.name == "mangamillion":
        if address.scheme:
            match = re.fullmatch(r"/[^/]+/title/(\d+)(?:/chapter/\d+)?/?", address.path)
            if address.scheme != "https" or address.hostname != "mangamillion.shueisha.co.jp" or match is None:
                raise ValueError("MangaMillion 需要官网作品链接或数字 ID")
            value = match[1]
        if not value.isascii() or not value.isdigit():
            raise ValueError("MangaMillion 请填写作品的 original_title_id 数字 ID")
    elif source.name == "kobo" and address.scheme in ("https", "http"):
        raise ValueError("Kobo 请填写 content-id 或本地 ACSM 路径")
    return value


class DownloadJob:
    def __init__(self, payload):
        if payload["source"] not in SERVICE_SOURCES:
            raise ValueError("此服务不支持该来源；Keiyoushi 请使用 CLI")
        self.source = get_source(payload["source"])
        self.source_name = self.source.name
        self.title_id = resolve_title(self.source, payload["title"])
        options = payload["options"]
        self.lang = options["lang"] or self.source.lang
        self.quality = options["quality"] or None
        for label, value, choices in (("语言", self.lang, self.source.lang_choices),
                                     ("质量", self.quality, self.source.quality_choices)):
            if value is not None and choices is not None and value not in choices:
                raise ValueError(f"{label}可选值：{', '.join(choices)}")
        self.source.lang = self.lang
        self.chapter_range = parse_chapter_range(options["chapters"]) if options["chapters"] else None
        if self.chapter_range and (self.source.name == "bookwalker" or "book" in self.source.capabilities):
            raise ValueError("BW / Kobo 下载整卷，不支持章节范围")
        self.formats = options["formats"]
        if any(extension not in ("zip", "cbz", "epub") for extension in self.formats):
            raise ValueError("导出格式必须为 ZIP / CBZ / EPUB")
        self.chapter_ids = None
        if self.source.name == "tongli" and not self.chapter_range:
            query = parse_qs(urlsplit(payload["title"]).query)
            if query.get("isGroup", ["false"])[0].lower() != "true":
                self.chapter_ids = (self.title_id,)
        self.payload = None
        self.reader = None
        if self.source.name == "bookwalker":
            self.reader = _reader(self.title_id)
        if not (self.source.name == "kobo" and self.title_id.lower().endswith(".acsm")):
            self.update(payload["session"])
        self.state = "downloading"
        self.message = "正在读取作品信息…"
        self.output = ""
        console = sys.stdout
        self.console = console.console if isinstance(console, _JobOutput) else console
        self.buffer = ""
        self.failures = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.payload = None

    def session(self):
        if "error" in self.payload:
            raise RuntimeError(self.payload["error"])
        return self.payload

    def update(self, payload):
        if payload is None:
            raise ValueError("插件下载需要当前浏览器授权")
        if self.source_name == "bookwalker" and _reader(payload["readerUrl"]) != self.reader:
            raise ValueError("阅读器已切换到其他书籍，请保持原阅读器打开")
        if self.source_name in ("tongli", "lightnovel", "mangamillion"):
            if not isinstance(payload["token"], str) or not payload["token"].strip():
                raise ValueError("未取得网页授权，请先登录")
            if self.source_name == "mangamillion":
                self.source._token = payload["token"]
                self.source.browser_session = True
                if self.source._client is not None:
                    self.source._client.extra_headers["Access-Token"] = payload["token"]
            else:
                self.source.token = payload["token"]
                if self.source_name == "lightnovel" and self.source._client is not None:
                    self.source._client._token = payload["token"]
        self.payload = payload

    def authorize(self):
        if self.source_name == "bilibili":
            from mmdl.sources.bilibili_client import BilibiliClient
            cookies = CookieJar()
            for item in self.payload["cookies"]:
                domain = item["domain"]
                expires = item["expires"]
                cookies.set_cookie(Cookie(
                    version=0, name=item["name"], value=item["value"], port=None, port_specified=False,
                    domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
                    path=item["path"], path_specified=True, secure=item["secure"],
                    expires=int(expires) if expires > 0 else None, discard=expires <= 0,
                    comment=None, comment_url=None, rest={},
                ))
            self.source._client = BilibiliClient(cookies=cookies, user_agent=self.payload["userAgent"])
        elif self.source_name == "lightnovel":
            from mmdl.sources.lightnovel_client import LightnovelClient
            self.source._client = LightnovelClient(self.payload["token"])
        elif self.source_name == "kobo" and self.payload is not None:
            from mmdl.sources.kobo_api import activate
            self.source.tokens = activate(self.payload["userKey"], path=None)
            self.source.cred_file = None

    def status(self, job_id):
        return {"job": job_id, "source": self.source_name, "state": self.state,
                "message": self.message, "output": self.output}

    def write(self, text):
        self.console.write(text)
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                self.message = line.strip()
                if self.message.startswith("[fail]"):
                    self.failures += 1
        return len(text)

    def flush(self):
        self.console.flush()

    @contextmanager
    def capture_output(self):
        # stdout redirection is process-wide, so overlapping jobs share one thread-aware router.
        with _OUTPUT_LOCK:
            output = sys.stdout
            if not isinstance(output, _JobOutput):
                output = _JobOutput(output)
                sys.stdout = output
            output.users += 1
            output.targets.job = self
        try:
            yield
        finally:
            with _OUTPUT_LOCK:
                del output.targets.job
                output.users -= 1
                if output.users == 0:
                    sys.stdout = output.console

    def run(self, out_dir, throttle):
        source = self.source
        source.throttle = throttle
        out_dir = Path(out_dir) / source.name if out_dir is not None else source.default_output
        epub = "epub" in self.formats
        try:
            with self.capture_output():
                self.authorize()
                if source.name == "bookwalker":
                    result = source.capture_from_url(self.title_id, browser_session=self)
                elif "book" in source.capabilities:
                    result = source.get_book(self.title_id, lang=self.lang, quality=self.quality)
                else:
                    title_dir, title = download_title(source, self.title_id, out_dir, lang=self.lang,
                        quality=self.quality, chapter_range=self.chapter_range, throttle=throttle,
                        chapter_ids=self.chapter_ids)
                if source.name == "bookwalker" or "book" in source.capabilities:
                    self.state = "writing"
                    result.title.name = clean_name(result.title.name)
                    write_capture(source, result, out_dir, lang=self.lang)
                    title = result.title
                    title_dir = Path(out_dir) / result.title.name
                if self.failures:
                    raise RuntimeError(f"{self.failures} 页下载失败；点击开始下载可续传，完整下载后再导出")
                self.state = "writing"
                if epub:
                    epub_path = build_epub(title_dir, title=title.name, author=title.author, language=self.lang)
                    print(f"[epub] {epub_path}")
                for extension in self.formats:
                    if extension == "epub":
                        continue
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
            if hasattr(source, "close"):
                source.close()
            self.source = None


def serve_downloads(*, port=19225, out_dir=None, throttle=0.3):
    if not PROFILE.is_file():
        raise RuntimeError("Run --register-extension <ID> to register the browser extension first")
    extension_id = json.loads(PROFILE.read_text(encoding="utf-8"))["extension_id"]
    allowed_origin = "chrome-extension://" + extension_id
    jobs = {}
    active_threads = {}

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
                self.respond(403, {"error": "请使用已注册的 Ezmanga 扩展"})
                return False
            return True

        def do_OPTIONS(self):
            if self.authorized():
                self.respond(200, {})

        def do_POST(self):
            nonlocal active_threads
            if not self.authorized():
                return
            try:
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/download":
                    active_threads = {key: thread for key, thread in active_threads.items() if thread.is_alive()}
                    if len(active_threads) >= MAX_ACTIVE_JOBS:
                        self.respond(409, {"error": f"最多同时下载 {MAX_ACTIVE_JOBS} 个任务，请等待其中一个完成"})
                        return
                    job = DownloadJob(payload)
                    identity = (job.source_name, job.reader or job.title_id)
                    if any((jobs[key].source_name, jobs[key].reader or jobs[key].title_id) == identity
                           for key in active_threads):
                        self.respond(409, {"error": "该作品已有下载任务，请等待完成"})
                        return
                    job_id = secrets.token_hex(8)
                    jobs[job_id] = job
                    thread = threading.Thread(target=job.run, args=(out_dir, throttle), daemon=True)
                    active_threads[job_id] = thread
                    thread.start()
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

    with HTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"[service] 下载服务已启动：127.0.0.1:{server.server_port}；打开 Ezmanga 后点击“开始下载”", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
