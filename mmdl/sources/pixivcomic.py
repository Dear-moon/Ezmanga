"""Pixiv Comic protocols adapted from Keiyoushi (Apache-2.0)."""
from datetime import datetime
import hashlib
from http.cookiejar import Cookie
import json
from pathlib import PurePosixPath
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from mmdl.core.model import Title, Chapter, Page
from .base import BaseSource
from .bookwalker_publus import decode_config, image_filename, restore_image


SITE = "https://comic.pixiv.net"
API = SITE + "/api/app"
VIEWER = "https://comic-store-viewer.pixiv.net"


class PixivComic(BaseSource):
    name = "pixivcomic"
    display_name = "Pixiv Comic"
    capabilities = frozenset({"crawl"})
    lang_choices = ("ja",)

    def __init__(self, throttle=0.0, lang="ja"):
        super().__init__(throttle=throttle, lang=lang)
        self.browser_session = None
        self._snapshot = None
        self._works = {}
        self._products = {}
        self._readers = {}
        self._readings = {}
        self._volumes = {}
        self._page_details = {}

    @staticmethod
    def target(value):
        value = str(value).strip()
        if value.isascii() and value.isdigit():
            return "works", value
        address = urlsplit(value)
        if address.scheme != "https":
            raise ValueError("Pixiv Comic requires a work ID or an official HTTPS link")
        if address.hostname == "comic-store-viewer.pixiv.net":
            return "volume", parse_qs(address.query)["cid"][0]
        if address.hostname != "comic.pixiv.net":
            raise ValueError("Pixiv Comic requires an official comic.pixiv.net link")
        for pattern, kind in (
            (r"/works/(\d+)/?", "works"),
            (r"/viewer/stories/(\d+)/?", "episode"),
            (r"/store/products/([A-Za-z0-9_-]+)/?", "product"),
            (r"/store/viewers/([A-Za-z0-9_-]+)/master/?", "volume"),
        ):
            match = re.fullmatch(pattern, address.path)
            if match:
                return kind, match[1]
        raise ValueError("Pixiv Comic requires a work, episode reader or store link")

    @classmethod
    def normalize(cls, value):
        kind, key = cls.target(value)
        paths = {"works": "/works/", "episode": "/viewer/stories/",
                 "product": "/store/products/", "volume": "/store/viewers/"}
        return SITE + paths[kind] + key + ("/master" if kind == "volume" else "")

    def make_client(self, throttle=0.0):
        from curl_cffi import requests
        return requests.Session(impersonate="chrome",
            headers={"X-Requested-With": "pixivcomic", "Referer": SITE + "/"}, timeout=60)

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None
        self.browser_session = None
        self._snapshot = None
        self._volumes.clear()
        self._page_details.clear()

    def _get(self, url, **kwargs):
        client = self.ensure_client()
        if self.browser_session is not None:
            snapshot = self.browser_session.session()
            if snapshot is not self._snapshot:
                client.headers["User-Agent"] = snapshot["userAgent"]
                for item in snapshot["cookies"]:
                    domain, expires = item["domain"], item["expires"]
                    client.cookies.jar.set_cookie(Cookie(
                        version=0, name=item["name"], value=item["value"], port=None, port_specified=False,
                        domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
                        path=item["path"], path_specified=True, secure=item["secure"],
                        expires=int(expires) if expires > 0 else None, discard=expires <= 0,
                        comment=None, comment_url=None, rest={},
                    ))
                self._snapshot = snapshot
        from curl_cffi import requests
        for attempt in range(3):
            try:
                response = client.get(url, **kwargs)
                break
            except requests.RequestsError as error:
                if error.code != 35 or attempt == 2:
                    raise RuntimeError(f"Pixiv Comic network error ({urlsplit(url).hostname}, curl {error.code})") from None
                print(f"[pixivcomic] TLS handshake failed; retry {attempt + 1}/2")
                time.sleep(attempt + 1)
        if response.status_code >= 400:
            if response.status_code in (401, 403) or (
                response.status_code == 400 and urlsplit(url).path.endswith("/master")
            ):
                raise RuntimeError("Pixiv Comic: log in and open a purchased/readable chapter in the browser")
            raise RuntimeError(f"Pixiv Comic HTTP {response.status_code}: {urlsplit(url).path}")
        return response

    def _api(self, path, **kwargs):
        return self._get(API + path, **kwargs).json()["data"]

    def _work(self, key):
        if key not in self._works:
            self._works[key] = self._api("/works/v5/" + key)["official_work"]
        return self._works[key]

    def _product(self, key):
        if key not in self._products:
            self._products[key] = self._api("/store/products/v2/" + key)
        return self._products[key]

    def _reader(self, key):
        if key not in self._readers:
            html = self._get(SITE + "/viewer/stories/" + key).text
            match = re.search(r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.S)
            if match is None:
                raise RuntimeError("Pixiv Comic reader did not return Next.js data")
            self._readers[key] = json.loads(match[1])["props"]["pageProps"]
        return self._readers[key]

    def _reading(self, key):
        if key not in self._readings:
            salt = self._reader(key)["salt"]
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            digest = hashlib.sha256((stamp + salt).encode("utf-8")).hexdigest()
            reading = self._api("/episodes/" + key + "/read_v4",
                headers={"X-Client-Time": stamp, "X-Client-Hash": digest})["reading_episode"]
            if not reading["pages"]:
                raise RuntimeError("Pixiv Comic: log in and purchase this chapter to read")
            self._readings[key] = reading
        return self._readings[key]

    @staticmethod
    def _number(value, index):
        match = re.search(r"\d+(?:\.\d+)?", value)
        return match[0] if match else str(index)

    def _episode(self, entry, index):
        label = entry["numbering_title"]
        subtitle = entry.get("sub_title")
        return Chapter("episode:" + str(entry["id"]), self._number(label, index),
            label + (": " + subtitle if subtitle else ""))

    def get_title(self, title_id, **kw):
        kind, key = self.target(title_id)
        if kind == "volume":
            return Title(self.name, self.normalize(title_id), "Pixiv Comic " + key)
        if kind == "product":
            product = self._product(key)["product"]
            return Title(self.name, self.normalize(title_id), product["title"],
                product.get("author_name") or "", product.get("image_url") or "",
                product.get("explanation") or "")
        if kind == "episode":
            key = str(self._reader(key)["workId"])
        work = self._work(key)
        return Title(self.name, SITE + "/works/" + key, work["name"],
            work.get("author") or "", (work.get("image") or {}).get("main_big") or "",
            work.get("description") or "")

    def get_chapters(self, title_id, **kw):
        kind, key = self.target(title_id)
        if kind == "episode":
            return [self._episode(self._reading(key), 1)]
        if kind == "volume":
            return [Chapter("volume:" + key, "1", key)]
        if kind == "product":
            readable = [entry for entry in self._product(key)["variants"]
                if entry.get("purchased_at") is not None or (entry.get("price") or {}).get("value") == 0]
            if not readable:
                raise RuntimeError("Pixiv Comic: no purchased/free volumes; use the extension in a logged-in browser")
            return [Chapter("volume:" + entry["sku"], self._number(entry["name"], index), entry["name"])
                for index, entry in enumerate(reversed(readable), 1)]
        entries = self._api("/works/" + key + "/episodes/v2", params={"order": "desc"})["episodes"]
        episodes = [entry["episode"] for entry in reversed(entries)
            if entry.get("episode") is not None and entry["episode"]["state"] == "readable"]
        if not episodes:
            raise RuntimeError("Pixiv Comic: this work has no currently readable episodes")
        return [self._episode(entry, index) for index, entry in enumerate(episodes, 1)]

    def _volume(self, key):
        if key not in self._volumes:
            address = urlsplit(self.browser_session.session()["readerUrl"] if self.browser_session is not None else "")
            query = parse_qs(address.query)
            if address.hostname != "comic-store-viewer.pixiv.net" or query.get("cid") != [key]:
                target = SITE + "/store/viewers/" + key + "/master"
                while True:
                    response = self._get(target, allow_redirects=False)
                    redirect = response.status_code in (301, 302, 303, 307, 308)
                    target = urljoin(response.url, response.headers["Location"]) if redirect else response.url
                    address = urlsplit(target)
                    query = parse_qs(address.query)
                    if address.hostname == "comic-store-viewer.pixiv.net" and "cid" in query:
                        break
                    if not redirect:
                        raise RuntimeError("Pixiv Comic store did not return a purchased-volume authorization")
            parameters = {name: query[name][0] for name in ("cid", "u1", "u2") if name in query}
            content = self._get(VIEWER + "/api/c", params=parameters).json()
            if not content.get("url"):
                raise RuntimeError("Pixiv Comic: log in and purchase this volume to read")
            if content["cty"] not in (1, 2):
                raise ValueError("Pixiv Comic supports comics, not novels")
            info = content.get("auth_info") or {}
            auth = {name: info[name] for name in
                ("hti", "cfg", "uuid", "pfCd", "Policy", "Signature", "Key-Pair-Id")
                if info.get(name) is not None}
            base = content["url"].rstrip("/") + "/"
            config, keys = decode_config(self._get(base + "configuration_pack.json", params=auth).json())
            self._volumes[key] = {"base": base, "auth": auth, "config": config, "keys": keys}
        return self._volumes[key]

    def get_pages(self, chapter, **kw):
        kind, key = chapter.id.split(":", 1)
        if kind == "episode":
            pages = []
            for entry in self._reading(key)["pages"]:
                address = urlsplit(entry["url"])
                # The untransformed CDN path returns the original, unscrambled image.
                path = "/" + "/".join(address.path.split("/")[3:])
                url = urlunsplit((address.scheme, address.netloc, path, address.query, ""))
                extension = PurePosixPath(path).suffix.lstrip(".").lower()
                extension = "jpg" if extension == "jpeg" else extension
                mime = "image/jpeg" if extension == "jpg" else "image/" + extension
                pages.append(Page(url=url, ext=extension, mime=mime))
            return pages
        volume = self._volume(key)
        pages = []
        for entry in sorted(volume["config"]["configuration"]["contents"], key=lambda entry: entry["index"]):
            file = entry["file"]
            detail = volume["config"][file]["FileLinkInfo"]["PageLinkInfoList"][0]["Page"]
            # Pixiv encrypts configurations but keeps store image filenames unhashed.
            url = urljoin(volume["base"], image_filename(file, [], detail.get("No", 0)))
            self._page_details[(chapter.id, url)] = (file, detail)
            pages.append(Page(url=url, ext="jpg", mime="image/jpeg"))
        return pages

    def download_page(self, page, chapter, **kw):
        kind, key = chapter.id.split(":", 1)
        if kind == "episode":
            return self._get(page.url).content
        volume = self._volume(key)
        data = self._get(page.url, params=volume["auth"]).content
        file, detail = self._page_details[(chapter.id, page.url)]
        return restore_image(data, file, detail, volume["keys"])
