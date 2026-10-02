"""東立 Firebase 认证：纯脚本登录 + refreshToken 自动刷新（无 DRM，密钥不硬编码）。

前端用 signInWithEmailAndPassword，这里直调 Firebase Auth REST API 拿 idToken（约 1h
过期）/ refreshToken（长期）。Firebase Web api key **不写死**：优先 TONG_LI_FIREBASE_API_KEY
环境变量，否则运行时从東立官网前端 JS 抓取（该 key 本就公开在官网）。resolve_access_token()：
静态 token → 缓存 refresh 刷新 → 邮箱登录。密码不落盘。
"""
import base64
import getpass
import json
import os
import re
import time
from pathlib import Path
from configparser import ConfigParser

from mmdl.core.http import HttpConfig, HttpClient, split_url

SIGNIN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
REFRESH_BASE = "https://securetoken.googleapis.com/v1/token"
FIREBASE_JS = "https://ebook.tongli.com.tw/js/firebase_token.js"
CRED_FILE = Path.home() / ".mmdl" / "tongli_refresh.json"
TOKEN_KEY = "refresh_token"
ID_TOKEN_KEY = "id_token"
ID_EXP_KEY = "id_exp"

# Reuse the cached idToken until it is within this window of expiry. Every refresh mints a new
# token, and the backend counts each unseen token as a new device against the account's quota.
_REFRESH_MARGIN = 300

_KEY_CACHE = None


def _http():
    return HttpClient(HttpConfig(content_type="application/json"))


def _api_key():
    """Firebase Web API key：env 优先，否则从東立官网前端抓取（缓存，避免重复请求）。"""
    global _KEY_CACHE
    if _KEY_CACHE:
        return _KEY_CACHE
    env = os.environ.get("TONG_LI_FIREBASE_API_KEY", "").strip()
    if env:
        _KEY_CACHE = env
        return env
    host, path = split_url(FIREBASE_JS)
    st, body = _http().request(host, "GET", path)
    if st == 200:
        m = re.search(rb'apiKey\s*:\s*"([^"]+)"', body)
        if m:
            _KEY_CACHE = m.group(1).decode("utf-8")
            return _KEY_CACHE
    raise RuntimeError("cannot fetch Tongli Firebase api key (set TONG_LI_FIREBASE_API_KEY?)")


def _raise_error(prefix, data):
    err = data.get("error") if isinstance(data, dict) else data
    msg = ""
    if isinstance(err, dict):
        msg = err.get("message") or ""
    elif isinstance(err, str):
        msg = err
    if not msg and isinstance(data, dict):
        msg = data.get("status") or data.get("message") or ""
    raise RuntimeError(f"{prefix}: {msg}" if msg else prefix)


def signin_password(email, password):
    """邮箱登录，返回 (idToken, refreshToken)。"""
    host, path = split_url(SIGNIN_URL + "?key=" + _api_key())
    body = json.dumps({"email": email, "password": password, "returnSecureToken": True}).encode()
    st, resp = _http().request(host, "POST", path, body=body, content_type="application/json")
    if st != 200:
        _raise_error(f"Firebase signin HTTP {st}", json.loads(resp.decode()))
    data = json.loads(resp.decode())
    if "error" in data:
        _raise_error("Firebase signin failed", data)
    return data["idToken"], data["refreshToken"]


def refresh_access_token(refresh_token):
    """refreshToken 换新 idToken，返回 (idToken, 新 refreshToken)。"""
    host, path = split_url(REFRESH_BASE + "?key=" + _api_key())
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": refresh_token}).encode()
    st, resp = _http().request(host, "POST", path, body=body, content_type="application/json")
    if st != 200:
        _raise_error(f"Firebase refresh HTTP {st}", json.loads(resp.decode()))
    data = json.loads(resp.decode())
    if "error" in data:
        _raise_error("Firebase refresh failed", data)
    return data["id_token"], data.get("refresh_token", refresh_token)


def load_refresh_token(path=CRED_FILE):
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")).get(TOKEN_KEY, "") or ""
    except Exception:
        pass
    return ""


def save_refresh_token(refresh_token, path=CRED_FILE):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({TOKEN_KEY: refresh_token}), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _jwt_exp(id_token):
    """Read the `exp` claim of an idToken; 0 when unparsable."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0


def load_cached_id_token(path=CRED_FILE):
    """Return the cached idToken while it stays valid beyond the refresh margin, else ""."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        tok = d.get(ID_TOKEN_KEY) or ""
        exp = int(d.get(ID_EXP_KEY) or 0)
    except Exception:
        return ""
    return tok if tok and exp - time.time() > _REFRESH_MARGIN else ""


def save_id_token(id_token, path=CRED_FILE):
    """Cache the idToken and its expiry, preserving the stored refresh token."""
    p = Path(path)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d[ID_TOKEN_KEY] = id_token
    d[ID_EXP_KEY] = _jwt_exp(id_token)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def static_token():
    env = os.environ.get("TONG_LI_TOKEN", "").strip()
    if env:
        return env
    try:
        cfg = Path.home() / ".mmdl" / "config.ini"
        if cfg.exists():
            cp = ConfigParser()
            cp.read(cfg, encoding="utf-8")
            return cp.get("tongli", "token", fallback="").strip()
    except Exception:
        pass
    return ""


def _login(email=None, password=None):
    email = (email or os.environ.get("TONG_LI_EMAIL", "")).strip()
    password = password or os.environ.get("TONG_LI_PASSWORD", "")
    if not email:
        email = input("東立 email: ").strip()
    if not password:
        password = getpass.getpass("東立 password: ")
    if not email or not password:
        raise RuntimeError("東立登录需要邮箱/密码：设 TONG_LI_EMAIL / TONG_LI_PASSWORD，或交互输入")
    return signin_password(email, password)


def resolve_access_token(cli_token=None, email=None, password=None, cred_file=CRED_FILE):
    """Return a usable idToken: static -> unexpired cache -> refresh -> email login (first run)."""
    static = cli_token or static_token()
    if static:
        return static.strip()
    cached = load_cached_id_token(cred_file)
    if cached:
        return cached
    rt = load_refresh_token(cred_file)
    if rt:
        try:
            idt, new_rt = refresh_access_token(rt)
            if new_rt != rt:
                save_refresh_token(new_rt, cred_file)
            save_id_token(idt, cred_file)
            return idt
        except Exception:
            pass   # refresh 失效，降级为交互登录
    idt, rt = _login(email=email, password=password)
    save_refresh_token(rt, cred_file)
    save_id_token(idt, cred_file)
    return idt
