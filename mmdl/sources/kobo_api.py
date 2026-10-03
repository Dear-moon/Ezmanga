"""Kobo activation and download API: web activation flow → device registration → token persistence → whole-.kepub download.

Auth (web activation flow, no direct password):
  1. GET auth.kobobooks.com/ActivateOnWeb —— browser authorization (user login; email only)
  2. POST storeapi.kobo.com/v1/auth/device   —— body carries UserKey → returns deviceId/userId/token
  3. POST storeapi.kobo.com/v1/auth/refresh  —— accessToken renewal

Persists accessToken / userKey / deviceId / userId / hashKey to ~/.mmdl/kobo.json
(mirrors tongli_auth's tongli_refresh.json; URL secrets and password never touch disk).

⚠️ Store API host/path, header params and content_keys structure are server-supplied; calibrate against a real manga purchase.
   Configurable via KOBO_API_BASE / KOBO_API_KEY env vars; failures raise readable errors with status/response excerpt.
"""
import getpass
import json
import os
import re
import time
from pathlib import Path

from mmdl.core.http import HttpClient, HttpConfig, split_url
from mmdl.core.cdp import CdpClient, DEFAULT_CDP_URL

AUTH_PAGE = "https://auth.kobobooks.com/ActivateOnWeb"
STORE_API = os.environ.get("KOBO_API_BASE", "https://storeapi.kobo.com")
API_KEY = os.environ.get("KOBO_API_KEY", "")
CRED_FILE = Path.home() / ".mmdl" / "kobo.json"

_TOKEN_FIELDS = ("accessToken", "refreshToken", "userKey", "deviceId", "userId", "hashKey", "expiresAt")


def _http() -> HttpClient:
    return HttpClient(HttpConfig(content_type="application/json"))


def _store() -> tuple[str, str]:
    return split_url(STORE_API)


# ---- token persistence (mirrors tongli_refresh.json) ----
def load_tokens(path=CRED_FILE) -> dict:
    try:
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def save_tokens(tokens: dict, path=CRED_FILE):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tokens), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# ---- device activation ----
def _wait_for_userkey(client, timeout=180):
    """Poll the activation page; capture the userkey from the post-auth URL or page text."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        href = client.eval("location.href") or ""
        for m in re.finditer(r"userkey=([A-Za-z0-9+/=%_-]+)", href, re.I):
            return m.group(1)
        txt = client.eval("(document.body.innerText||'')") or ""
        m = re.search(r"userkey\s*[:=]\s*([A-Za-z0-9+/=%_-]+)", txt, re.I)
        if m:
            return m.group(1)
        time.sleep(2)
    raise RuntimeError("Kobo activate: 超时未捕获到 userkey（需在激活页完成登录/授权）")


def _register_device(userkey, serial=None):
    """Device registration: POST /v1/auth/device. payload/headers are Kobo reverse-engineering defaults; calibrate by testing."""
    serial = serial or os.environ.get("KOBO_SERIAL", f"MMDL-{int(time.time())}")
    payload = {
        "ApiKey": API_KEY,
        "AppId": "KoboForPC",
        "AppVersion": "4.38.21971",
        "SerialNumber": serial,
        "UserKey": userkey,
    }
    host, base = _store()
    body = json.dumps(payload).encode()
    st, resp = _http().request(host, "POST", base + "/v1/auth/device",
                               body=body, content_type="application/json")
    if st != 200:
        raise RuntimeError(f"Kobo device auth HTTP {st}: {resp.decode('utf-8', 'ignore')[:300]}")
    data = json.loads(resp.decode())
    return data


def setup(cdp_url=DEFAULT_CDP_URL, path=CRED_FILE) -> dict:
    """One-time activation: CDP opens ActivateOnWeb → user logs in → capture userkey → device registration → save tokens.

    Requires a local debug browser (with websocket-client). Returns and persists the token dict.
    """
    client = CdpClient(cdp_url=cdp_url, target_url_substr="")
    try:
        client.connect()
        client.eval(f"location.href='{AUTH_PAGE}'")
        userkey = _wait_for_userkey(client)
    finally:
        client.close()
    return activate(userkey, path=path)


def activate(userkey, path=CRED_FILE):
    serial = os.environ.get("KOBO_SERIAL", f"MMDL-{int(time.time())}")
    data = _register_device(userkey, serial)
    tokens = {
        "accessToken": data.get("AccessToken") or data.get("accessToken") or "",
        "refreshToken": data.get("RefreshToken") or data.get("refreshToken") or "",
        "userKey": userkey,
        # serial feeds Obok deviceid derivation (SHA256(hash_key+serial)); server-returned DeviceId is stored separately
        "serial": serial,
        "deviceId": data.get("DeviceId") or data.get("deviceId") or data.get("deviceID") or "",
        "userId": data.get("UserId") or data.get("userId") or data.get("userID") or "",
        "hashKey": os.environ.get("KOBO_HASH_KEY", "88b3a2e13"),
        "expiresAt": data.get("AccessTokenExpiry")
        or data.get("accessTokenExpiry")
        or data.get("expiresIn", "") or "",
    }
    if path is not None:
        save_tokens(tokens, path)
    return tokens


# ---- auth ----
def _headers(tokens, extra=None):
    h = {}
    if tokens.get("accessToken"):
        h["Authorization"] = f"Bearer {tokens['accessToken']}"
    if API_KEY:
        h["x-api-key"] = API_KEY
    if extra:
        h.update(extra)
    return h


def _expired(tokens):
    exp = tokens.get("expiresAt")
    # Treat as unknown only when the field is missing; 0 is a real expiry and must not take this path
    if exp is None or exp == "":
        return not tokens.get("accessToken")
    try:
        return time.time() > float(exp)
    except (ValueError, TypeError):
        return False


def ensure_tokens(tokens=None, path=CRED_FILE) -> dict:
    """Ensure a usable accessToken: load → /auth/refresh if expired → write back."""
    if tokens is None:
        tokens = load_tokens(path)
    if tokens.get("accessToken") and not _expired(tokens):
        return tokens
    if not tokens.get("refreshToken"):
        raise RuntimeError("Kobo 未激活：请先 `--source kobo --setup`")
    body = json.dumps({"refresh_token": tokens["refreshToken"]}).encode()
    host, base = _store()
    st, resp = _http().request(host, "POST", base + "/v1/auth/refresh",
                               body=body, content_type="application/json",
                               headers=_headers(tokens))
    if st != 200:
        raise RuntimeError(f"Kobo refresh HTTP {st}: {resp.decode('utf-8', 'ignore')[:300]}")
    data = json.loads(resp.decode())
    tokens["accessToken"] = data.get("AccessToken") or data.get("accessToken") or tokens["accessToken"]
    tokens["refreshToken"] = data.get("RefreshToken") or data.get("refreshToken") or tokens["refreshToken"]
    tokens["expiresAt"] = data.get("AccessTokenExpiry") or data.get("accessTokenExpiry") or ""
    if path is not None:
        save_tokens(tokens, path)
    return tokens


# ---- download ----
def download_book(book_id: str, tokens=None, path=CRED_FILE):
    """Download the whole .kepub. Returns (kepub_bytes, content_keys: dict) — elementkeys for kobo_drm.

    The endpoint is supplied by /v1/initialization Resources (library_sync / content_access_book templates);
    real path and content_keys structure need live testing; here we build the URL from the resolved template and raise readable errors.
    """
    tokens = ensure_tokens(tokens, path)
    host, _ = _store()
    return _download_from_api(host, book_id, tokens)


def _download_from_api(host, book_id, tokens):
    st, resp = _http().request(host, "GET", "/v1/initialization", headers=_headers(tokens))
    if st != 200:
        raise RuntimeError(f"Kobo initialization HTTP {st}: {resp.decode('utf-8', 'ignore')[:300]}")
    init = json.loads(resp.decode())

    resource = None
    resources = init.get("Resources") or init.get("resources") or {}
    for key in resources:
        if "content_access" in key.lower() or "download" in key.lower():
            resource = resources[key]
            break
    if resource is None:
        raise RuntimeError("Kobo initialization 缺少 content_access_book / download 模板（需实测）")

    template = resource
    if isinstance(resource, dict):
        template = next((v for v in resource.values() if isinstance(v, str)), "")

    book_url = template.format(ContentId=book_id, contentId=book_id) if "{" in template else template
    if book_url.startswith("http"):
        dl_host, url = split_url(book_url)
    else:  # relative path → join onto storeapi host
        dl_host, url = host, book_url

    st, body = _http().request(dl_host, "GET", url, headers=_headers(tokens))
    if st != 200:
        raise RuntimeError(f"Kobo download HTTP {st}: {body.decode('utf-8', 'ignore')[:300]}")

    content_keys = _extract_content_keys(init, book_id)
    return body, content_keys


def _extract_content_keys(init, book_id):
    """Try to extract content_keys (elementkey set) from the init response; structure needs live testing, empty dict if absent."""
    ck = init.get("contentKeys") or init.get("ContentKeys")
    if isinstance(ck, dict):
        return ck
    if isinstance(ck, list):
        out = {}
        for item in ck:
            if isinstance(item, dict):
                name = item.get("ContentId") or item.get("contentId") or item.get("File") or ""
                key = item.get("ContentKey") or item.get("contentKey") or item.get("elementKey") or ""
                if name and key:
                    out[name] = key
        return out
    return {}
