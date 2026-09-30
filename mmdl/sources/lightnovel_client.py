"""SignalR LongPolling transport for the Light Novel Shelf comic API."""
import gzip
import json
import time
from urllib.parse import urlencode, urlsplit


API_BASES = ("https://api.lightnovel.life", "https://cf-api.lightnovel.life")
HTTP_TIMEOUT = 30
HUB_TIMEOUT = 45
INVOKE_DEADLINE = 90


class SignalRError(RuntimeError):
    pass


class TransportError(SignalRError):
    pass


class LightnovelClient:
    def __init__(self, refresh_token, api_base=None):
        try:
            import msgpack
            from curl_cffi import requests
        except ImportError:
            raise RuntimeError("lightnovel requires curl_cffi and msgpack (pip install curl_cffi msgpack)") from None
        self._msgpack = msgpack
        self._curl = requests
        self._http = requests.Session(impersonate="chrome124")
        self._http.headers.update({
            "Origin": "https://www.lightnovel.app",
            "Referer": "https://www.lightnovel.app/",
        })
        self._token = refresh_token
        self._bearer = None
        self._hub_url = None
        self._buffer = b""
        self._invocation = 0
        self._base_index = 0
        self._bases = (api_base.rstrip("/"),) if api_base else API_BASES
        for base in self._bases:
            parts = urlsplit(base)
            if parts.scheme != "https" or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
                raise ValueError("LIGHTNOVEL_API_BASE must be an HTTPS base URL")

    def _request(self, method, url, **kw):
        try:
            response = self._http.request(method, url, timeout=kw.pop("timeout", HTTP_TIMEOUT), **kw)
        except self._curl.RequestsError as error:
            raise TransportError(f"lightnovel network error (curl {error.code})") from None
        if response.status_code >= 500 or response.status_code in (408, 429):
            raise TransportError(f"lightnovel HTTP {response.status_code}")
        if url == self._hub_url and response.status_code in (401, 404, 410):
            raise TransportError("lightnovel session expired or connection closed")
        if response.status_code >= 400:
            raise SignalRError(f"lightnovel HTTP {response.status_code}")
        return response

    def _message(self, value):
        text = str(value or "lightnovel API request failed")
        for secret in (self._token, self._bearer):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text

    def _connect(self):
        base = self._bases[self._base_index]
        response = self._request("POST", base + "/api/user/refresh_token", json={"token": self._token}).json()
        self._bearer = response.get("Response")
        if not isinstance(self._bearer, str) or not self._bearer:
            raise SignalRError("lightnovel refresh token is invalid or expired; log in again and update LIGHTNOVEL_REFRESH_TOKEN")
        response = self._request("POST", base + "/hub/api/negotiate?negotiateVersion=0", json={}).json()
        cid = response.get("connectionId")
        if not cid:
            raise TransportError("lightnovel negotiate returned no connection ID")
        self._hub_url = base + "/hub/api?" + urlencode({"id": cid, "access_token": self._bearer})
        self._buffer = b""
        self._request("POST", self._hub_url,
                      data=b'{"protocol":"messagepack","version":1}\x1e',
                      headers={"Content-Type": "text/plain;charset=UTF-8"}, timeout=HUB_TIMEOUT)

    def _records(self, data):
        self._buffer += data
        while self._buffer:
            if self._buffer.startswith(b"{"):
                end = self._buffer.find(b"\x1e")
                if end < 0:
                    return
                record = json.loads(self._buffer[:end])
                self._buffer = self._buffer[end + 1:]
                if "error" in record:
                    raise SignalRError(self._message(record["error"]))
                continue
            length = 0
            for index, byte in enumerate(self._buffer[:5]):
                length |= (byte & 0x7f) << (7 * index)
                if byte < 128:
                    break
            else:
                if len(self._buffer) < 5:
                    return
                raise SignalRError("invalid SignalR frame length")
            if length <= 0 or length > 32 * 1024 * 1024:
                raise SignalRError("invalid SignalR frame length")
            start = index + 1
            end = start + length
            if len(self._buffer) < end:
                return
            record = self._msgpack.unpackb(self._buffer[start:end], raw=False, strict_map_key=False)
            self._buffer = self._buffer[end:]
            yield record

    def _unwrap(self, result):
        if not isinstance(result, dict) or "Success" not in result:
            raise SignalRError("invalid lightnovel response envelope")
        if not result["Success"]:
            raise SignalRError(self._message(result.get("Msg") or result.get("Message")))
        value = result.get("Response")
        if isinstance(value, bytes):
            if value.startswith(b"\x1f\x8b"):
                value = gzip.decompress(value)
            value = json.loads(value)
        return value

    def _invoke(self, method, params):
        if not self._hub_url:
            self._connect()
        self._invocation += 1
        invocation_id = str(self._invocation)
        payload = self._msgpack.packb([1, {}, invocation_id, method, [params, {"UseGzip": True}], []], use_bin_type=True)
        size = len(payload)
        prefix = bytearray()
        while size >= 128:
            prefix.append((size & 0x7f) | 0x80)
            size >>= 7
        prefix.append(size)
        self._request("POST", self._hub_url, data=bytes(prefix) + payload,
                      headers={"Content-Type": "application/octet-stream"}, timeout=HUB_TIMEOUT)
        deadline = time.monotonic() + INVOKE_DEADLINE
        while time.monotonic() < deadline:
            response = self._request("GET", self._hub_url, timeout=max(0.1, min(HUB_TIMEOUT, deadline - time.monotonic())))
            if response.status_code == 204:
                raise TransportError("lightnovel connection closed")
            for record in self._records(response.content):
                if not isinstance(record, list) or not record:
                    raise SignalRError("invalid SignalR record")
                kind = record[0]
                if kind == 7:
                    raise TransportError("lightnovel connection closed")
                if kind == 2 and len(record) > 3 and str(record[2]) == invocation_id:
                    return self._unwrap(record[3])
                if kind == 3 and len(record) > 3 and str(record[2]) == invocation_id:
                    if record[3] == 3 and len(record) > 4:
                        return self._unwrap(record[4])
                    error = record[4] if len(record) > 4 else "no result"
                    if isinstance(error, dict):
                        error = error.get("message") or error.get("Message")
                    raise SignalRError(self._message(error))
        raise TransportError(f"timeout waiting for {method}")

    def invoke(self, method, params):
        for attempt in range(3):
            try:
                return self._invoke(method, params)
            except TransportError:
                self._disconnect()
                if attempt == 2:
                    raise
                self._base_index = (self._base_index + 1) % len(self._bases)
                time.sleep(attempt + 1)

    def download_image(self, url):
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
            raise SignalRError("invalid lightnovel image URL")
        for attempt in range(3):
            try:
                # The image CDN converts .jpg paths to WebP; the suffix is not the format.
                response = self._request("GET", url, headers={"Accept": "image/webp"}, timeout=60)
                data = response.content
                if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
                    raise SignalRError("lightnovel image CDN returned non-WebP data")
                if int.from_bytes(data[4:8], "little") + 8 != len(data):
                    raise TransportError("lightnovel image download is incomplete")
                return data
            except TransportError:
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)

    def _disconnect(self):
        if self._hub_url:
            try:
                self._request("DELETE", self._hub_url, timeout=5)
            except SignalRError:
                pass
        self._hub_url = None
        self._buffer = b""

    def close(self):
        self._disconnect()
        self._http.close()
