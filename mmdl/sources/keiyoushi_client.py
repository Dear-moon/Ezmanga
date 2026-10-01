"""Keiyoushi extension installation and the local JVM stdio transport."""
import atexit
from collections import deque
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse


def state_dir():
    return Path(os.environ.get("EZMANGA_KEIYOUSHI_HOME", Path.home() / ".mmdl" / "keiyoushi")).expanduser().resolve()


def _build_module():
    path = Path(__file__).resolve().parents[2] / "runtime" / "build.py"
    spec = importlib.util.spec_from_file_location("ezmanga_runtime_build", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup_runtime():
    module = _build_module()
    base, classes = module.build(state_dir() / "runtime")
    print(f"[runtime] {base} ({base.stat().st_size / 1024 / 1024:.1f} MiB)")
    return base, classes


def _get(url):
    from curl_cffi import requests
    for attempt in range(3):
        try:
            response = requests.get(url, impersonate="chrome", timeout=90, headers={"User-Agent": "Ezmanga"})
            response.raise_for_status()
            return response.content
        except requests.exceptions.RequestException:
            if attempt == 2:
                raise RuntimeError("Could not fetch the official extension repository") from None
            time.sleep(attempt + 1)


def catalog():
    commit = json.loads(_get("https://api.github.com/repos/keiyoushi/extensions/commits/repo"))["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Invalid extension repository revision")
    root = f"https://raw.githubusercontent.com/keiyoushi/extensions/{commit}/"
    index = json.loads(_get(root + "index.json"))["extensionList"]["extensions"]
    assets = json.loads(_get(root + "release-assets.json"))
    for extension in index:
        extension["jar_asset"] = assets.get(extension["packageName"], {}).get("jar")
    return index


def resolve_extension(name, entries):
    matches = [entry for entry in entries if entry["packageName"] == name or entry["packageName"].rsplit(".", 1)[-1] == name]
    if len(matches) != 1:
        raise ValueError(f"Extension name must match one package: {name!r}")
    return matches[0]


def _write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def install_extension(extension):
    pkg = extension["packageName"]
    if not re.fullmatch(r"[a-zA-Z0-9_.]+", pkg) or extension["extensionLib"] not in {"1.4", "1.5", "1.6"}:
        raise ValueError("Supported extension APIs are 1.4, 1.5, and 1.6 JARs")
    url = extension["resources"].get("jarUrl", "")
    parsed = urlparse(url)
    asset = extension.get("jar_asset")
    if (parsed.scheme != "https" or parsed.netloc != "github.com"
            or not parsed.path.startswith("/keiyoushi/extensions/releases/download/")
            or not asset or parsed.path.rsplit("/", 1)[-1] != asset["name"]
            or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])):
        raise RuntimeError("No matching official JAR and checksum in the extension index")
    jar = state_dir() / "extensions" / (pkg + ".jar")
    receipt = jar.with_suffix(".json")
    if jar.exists() and hashlib.sha256(jar.read_bytes()).hexdigest() == asset["sha256"]:
        data = jar.read_bytes()
    else:
        data = _get(url)
    if hashlib.sha256(data).hexdigest() != asset["sha256"]:
        raise RuntimeError("Extension JAR SHA-256 mismatch")
    _write_atomic(jar, data)
    _write_atomic(receipt, json.dumps(extension, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"[extension] {extension['name']} {extension['versionName']} ({pkg})")


def installed_extensions():
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted((state_dir() / "extensions").glob("*.json"))]


def load_preferences(path):
    values = json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    if not isinstance(values, dict) or set(values) != {"preferences"} or not isinstance(values["preferences"], list):
        raise ValueError("Preferences file must contain a preferences array")
    keys = set()
    for item in values["preferences"]:
        if not isinstance(item, dict) or set(item) != {"key", "type", "value"}:
            raise ValueError("Each preference needs key, type, and value")
        key, kind, value = item["key"], item["type"], item["value"]
        if not isinstance(key, str) or not key or key in keys:
            raise ValueError("Preference keys must be nonempty and unique")
        keys.add(key)
        valid = ((kind == "String" and isinstance(value, str))
                 or (kind == "Boolean" and isinstance(value, bool))
                 or (kind == "Int" and type(value) is int and -(2 ** 31) <= value < 2 ** 31)
                 or (kind == "Long" and type(value) is int and -(2 ** 63) <= value < 2 ** 63)
                 or (kind == "Float" and type(value) in (int, float) and abs(value) <= 3.4028234663852886e38 and math.isfinite(value))
                 or (kind == "StringSet" and isinstance(value, list) and all(isinstance(part, str) for part in value)))
        if not valid:
            raise ValueError(f"Invalid preference type/value for {key!r}")
    return values


def load_filters(path):
    values = json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    if not isinstance(values, dict) or any(not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*", key) for key in values):
        raise ValueError("Filters file must map numeric filter paths to states")
    return values


class KeiyoushiClient:
    def __init__(self, extension):
        self.extension = extension
        self._process = None
        self._replies = queue.Queue()
        self._errors = deque(maxlen=12)
        self._serial = 0
        self._lock = threading.Lock()

    def _start(self):
        extension = resolve_extension(self.extension, installed_extensions())
        pkg = extension["packageName"]
        jar = state_dir() / "extensions" / (pkg + ".jar")
        if hashlib.sha256(jar.read_bytes()).hexdigest() != extension["jar_asset"]["sha256"]:
            raise RuntimeError("Installed extension SHA-256 mismatch; reinstall this extension")
        module = _build_module()
        base = state_dir() / "runtime" / "runtime-base.jar"
        if not base.exists():
            raise RuntimeError("Run --source keiyoushi --setup before using extensions")
        base, classes = module.build(base.parent)
        self._replies = queue.Queue()
        self._errors.clear()
        cache = state_dir() / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        self._process = subprocess.Popen(
            [module.java_tool("java"), "-Xms16m", "-Xmx192m", "-XX:+UseSerialGC", "-Dfile.encoding=UTF-8", "-Djava.io.tmpdir=" + str(cache),
             "-cp", os.pathsep.join((str(classes), str(base))), "ezmanga.EzmangaHost", str(jar), str(state_dir() / "settings"), extension["extensionLib"]],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=env, cwd=state_dir(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        process = self._process
        replies = self._replies

        def read_replies():
            try:
                for line in process.stdout:
                    replies.put(line)
            finally:
                replies.put(None)

        def read_errors():
            for line in process.stderr:
                self._errors.append(line.rstrip())

        threading.Thread(target=read_replies, daemon=True).start()
        threading.Thread(target=read_errors, daemon=True).start()
        atexit.register(self.close)

    def call(self, op, **params):
        with self._lock:
            if self._process is None:
                self._start()
            self._serial += 1
            request = {"id": self._serial, "op": op, **params}
            try:
                self._process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self._process.stdin.flush()
                line = self._replies.get(timeout=120)
                if line is None:
                    self.close()
                    raise RuntimeError("Extension host exited; check Java and extension compatibility")
                reply = json.loads(line)
                if reply.get("id") != self._serial:
                    raise RuntimeError("Extension host returned an invalid response ID")
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                return reply["result"]
            except (queue.Empty, BrokenPipeError, OSError, json.JSONDecodeError) as error:
                self.close()
                raise RuntimeError(f"Extension host communication failed ({type(error).__name__}); the JVM has been closed") from None

    def close(self):
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.stdin.close()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        finally:
            process.stdout.close()
            process.stderr.close()
            atexit.unregister(self.close)
