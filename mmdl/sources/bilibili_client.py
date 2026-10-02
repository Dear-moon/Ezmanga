"""Bilibili's signed comic API and encrypted image transport."""
import base64
from http.cookiejar import LWPCookieJar
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from curl_cffi import requests

from .bilibili_wasm import GoWasm


ORIGIN = "https://manga.bilibili.com"
ASSET_BASE = "https://s1.hdslb.com/bfs/manga-static/manga-pc/"
READER = "static/js/reader.550c4c7ca4.js"
MODULES = {
    "sign": "efae82c96a7eef44bee5.wasm",
    "api": "e461bfa6b471a22c06fc.wasm",
    "image": "dda35c98742815151e46.wasm",
}
SIGN_QUERY = "device=pc&platform=web&nov=27&eot=812"


def _json(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class BilibiliClient:
    def __init__(self):
        self.profile = Path.home() / ".mmdl" / "bilibili_cookies.txt"
        cookies = LWPCookieJar(str(self.profile))
        if self.profile.is_file():
            cookies.load(ignore_discard=True)
        self.session = requests.Session(impersonate="chrome", cookies=cookies)
        self.cache = Path.home() / ".mmdl" / "bilibili"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.runtimes = {}
        reader = self._asset(READER).read_text(encoding="utf-8")
        self.header = re.search(r"['\"]([0-9A-Fa-f]{32})['\"]", reader).group(1)
        if not self.session.cookies.get("buvid3"):
            response = self.session.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=30)
            self._check_http(response, "device initialization")
            device = response.json()
            if device["code"] != 0:
                raise RuntimeError(f"Bilibili device initialization failed: {device['code']}")
            self.session.cookies.set("buvid3", device["data"]["b_3"], domain=".bilibili.com")
            self.session.cookies.set("buvid4", device["data"]["b_4"], domain=".bilibili.com")

    def login(self):
        import qrcode

        passport = "https://passport.bilibili.com/x/passport-login/web/qrcode/"
        response = self.session.get(passport + "generate",
            params={"source": "main_web", "go_url": ORIGIN + "/"}, timeout=30)
        self._check_http(response, "QR generation")
        generated = response.json()
        if generated["code"] != 0:
            raise RuntimeError(f"Bilibili QR generation failed: code={generated['code']}")
        qr = generated["data"]
        image_path = self.profile.with_name("bilibili_login.png")
        qrcode.make(qr["url"]).save(image_path)
        print(f"[bili] 请用哔哩哔哩 App 扫码并确认：{image_path}", flush=True)
        scanned = False
        try:
            while True:
                time.sleep(2)
                response = self.session.get(passport + "poll",
                    params={"qrcode_key": qr["qrcode_key"], "source": "main_web"}, timeout=30)
                self._check_http(response, "QR login")
                result = response.json()
                if result["code"] != 0:
                    raise RuntimeError(f"Bilibili QR login failed: code={result['code']}")
                code = result["data"]["code"]
                if code == 0:
                    response = self.session.get("https://api.bilibili.com/x/web-interface/nav", timeout=30)
                    self._check_http(response, "login verification")
                    account = response.json()
                    if account["code"] != 0 or not account["data"]["isLogin"]:
                        raise RuntimeError("Bilibili QR login did not create a valid account session")
                    temporary = self.profile.with_suffix(".tmp")
                    self.session.cookies.jar.save(str(temporary), ignore_discard=True)
                    os.chmod(temporary, 0o600)
                    temporary.replace(self.profile)
                    print(f"[bili] 登录成功，登录态已保存：{self.profile}", flush=True)
                    return
                if code == 86101:
                    continue
                if code == 86090:
                    if not scanned:
                        print("[bili] 已扫码，请在手机上确认登录", flush=True)
                        scanned = True
                    continue
                if code == 86038:
                    raise RuntimeError("Bilibili QR expired; run --source bilibili --setup again")
                raise RuntimeError(f"Bilibili QR login failed: code={code}")
        finally:
            image_path.unlink()

    @staticmethod
    def _check_http(response, operation):
        if response.status_code != 200:
            raise RuntimeError(f"Bilibili {operation}: HTTP {response.status_code}")

    def _asset(self, name):
        path = self.cache / name.rsplit("/", 1)[-1]
        if not path.is_file():
            response = self.session.get(ASSET_BASE + name, timeout=60)
            self._check_http(response, "protocol asset download")
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(response.content)
            temporary.replace(path)
        return path

    def _runtime(self, name):
        if name not in self.runtimes:
            self.runtimes[name] = GoWasm(self._asset(MODULES[name]), fetch=self._ignore_report)
        return self.runtimes[name]

    @staticmethod
    def _ignore_report(url, options):
        endpoint = urlsplit(url)
        if endpoint.hostname != "data.bilibili.com" or endpoint.path != "/log/web":
            raise NotImplementedError("Bilibili WASM requested an unsupported network endpoint")
        # Image decoding does not wait for the optional telemetry response.
        pending = {}
        pending["then"] = lambda *callbacks: pending
        pending["catch"] = lambda callback: pending
        return pending

    def post(self, method, body):
        api = "/twirp/comic.v1.Comic/" + method
        body_string = _json(body)
        signed = self._runtime("sign").global_object["y1_z2w2a3"](
            SIGN_QUERY, body_string, int(time.time() * 1000))
        if signed["error"]:
            raise RuntimeError("Bilibili request signing failed")
        params = {"device": "pc", "platform": "web", "ultra_sign": signed["sign"], "nov": 27, "a": 810}
        response = self.session.post(ORIGIN + api, params=params, data=body_string.encode("utf-8"),
            headers={"Content-Type": "application/json", "Referer": ORIGIN + "/", "Origin": ORIGIN,
                     "x-bili-data-sn": self.header}, timeout=30)
        self._check_http(response, method)
        payload = response.json()
        if payload["code"] != 0:
            raise RuntimeError(f"Bilibili {method} failed: code={payload['code']}")
        if "bytesData" not in payload:
            return payload["data"]
        decoded = self._runtime("api").global_object["c1_r9k2m7"](
            api + "?" + urlencode(params), payload["bytesData"],
            self.session.cookies.get("buvid3"), "web", body_string)
        if decoded["error"]:
            raise RuntimeError(f"Bilibili {method} response decryption failed")
        return json.loads(decoded["data"])

    def image_tokens(self, images):
        key = ec.generate_private_key(ec.SECP256R1())
        public = base64.b64encode(key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)).decode()
        numbers = key.private_numbers()
        def coordinate(value):
            return base64.urlsafe_b64encode(value.to_bytes(32, "big")).decode().rstrip("=")
        jwk = {"key_ops": ["deriveKey", "deriveBits"], "ext": True, "kty": "EC",
               "x": coordinate(numbers.public_numbers.x), "y": coordinate(numbers.public_numbers.y),
               "crv": "P-256", "d": coordinate(numbers.private_value)}
        private = base64.b64encode(_json(jwk).encode("utf-8")).decode()
        tokens = self.post("ImageToken", {"urls": _json([image["path"] for image in images]), "m1": public})
        urls = []
        for token in tokens:
            url = urlsplit(token["complete_url"])
            query = dict(parse_qsl(url.query))
            query["code"] = "DanmakuInfo"
            urls.append(urlunsplit(url._replace(query=urlencode(query))))
        return private, urls

    def download_image(self, url, private, index):
        response = self.session.get(url, headers={"Referer": ORIGIN + "/"}, timeout=60)
        self._check_http(response, "image download")
        data = response.content
        if data[:1] == b"\x08":
            result = self._runtime("image").global_object["a1_h17mj9"](private, bytearray(data), url, index)
            decoded = json.loads(bytes(result))
            if decoded["code"] != 0:
                raise RuntimeError(f"Bilibili image decryption failed: code={decoded['code']}")
            data = base64.b64decode(decoded["data"])
        return data

    def close(self):
        self.session.close()
        self.runtimes.clear()
