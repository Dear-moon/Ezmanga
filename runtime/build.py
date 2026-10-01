"""Build the stdio host using a local JDK and a pinned compatibility library."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

BASE_URL = "https://github.com/576576/Suwayomi-ext-runtime/releases/download/36.0.66-alpha.36683078947/ext-runtime-36.0.66.jar"
BASE_SHA256 = "dadec0957e5c78ca39e6717cb2e379977bbd04085af49ada8c2c1ff3a9b316a9"


def java_tool(name):
    override = os.environ.get("EZMANGA_" + name.upper())
    if override:
        return override
    if os.environ.get("JAVA_HOME"):
        candidate = Path(os.environ["JAVA_HOME"]) / "bin" / (name + (".exe" if os.name == "nt" else ""))
        if candidate.is_file():
            return str(candidate)
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} not found; install JDK 25+ or set JAVA_HOME")
    return path


def require_java25(tool):
    result = subprocess.run([tool, "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    version = re.search(r'(?:version\s+"|javac\s+)(\d+)', result.stdout + result.stderr)
    if result.returncode or not version or int(version.group(1)) < 25:
        raise RuntimeError("The pinned compatibility library requires JDK 25+")


def build(output):
    output = Path(output).expanduser().resolve()
    java, javac = java_tool("java"), java_tool("javac")
    require_java25(java)
    require_java25(javac)
    output.mkdir(parents=True, exist_ok=True)
    base = output / "runtime-base.jar"
    if not base.exists():
        from curl_cffi import requests
        response = requests.get(BASE_URL, impersonate="chrome", timeout=120)
        response.raise_for_status()
        if hashlib.sha256(response.content).hexdigest() != BASE_SHA256:
            raise RuntimeError("Compatibility library SHA-256 mismatch")
        with tempfile.NamedTemporaryFile(dir=output, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(response.content)
        try:
            temporary.replace(base)
        finally:
            temporary.unlink(missing_ok=True)
    if hashlib.sha256(base.read_bytes()).hexdigest() != BASE_SHA256:
        raise RuntimeError("Compatibility library SHA-256 mismatch; restore the pinned artifact")
    sources = sorted((Path(__file__).parent / "src").glob("*.java"))
    digest = hashlib.sha256(BASE_SHA256.encode() + b"".join(source.name.encode() + b"\0" + source.read_bytes() for source in sources)).hexdigest()[:16]
    classes = output / ("classes-" + digest)
    marker = classes / ".built"
    if not marker.exists():
        classes.mkdir(exist_ok=True)
        subprocess.run([javac, "--release", "25", "-encoding", "UTF-8", "-cp", str(base), "-d", str(classes), *map(str, sources)], check=True)
        marker.write_text(digest, encoding="ascii")
    return base, classes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(os.environ.get("EZMANGA_KEIYOUSHI_HOME", Path.home() / ".mmdl" / "keiyoushi")) / "runtime")
    args = parser.parse_args()
    base, classes = build(args.output)
    print(f"[runtime] {base} ({base.stat().st_size / 1024 / 1024:.1f} MiB)")
    print(f"[host] {classes}")
