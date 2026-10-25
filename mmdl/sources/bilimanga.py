"""BiliManga protocols adapted from Keiyoushi (Apache-2.0)."""
import ast
from html.parser import HTMLParser
from http.cookiejar import Cookie
from io import BytesIO
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

from curl_cffi import requests
from PIL import Image

from mmdl.core.model import Chapter, Page, Title
from .base import BaseSource


SITE = "https://www.bilimanga.net"
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36")


class _Document(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = ET.Element("document")
        self.stack = [self.root]
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        element = ET.SubElement(self.stack[-1], tag, {key: value or "" for key, value in attrs})
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        parent = self.stack[-1]
        if len(parent):
            parent[-1].tail = (parent[-1].tail or "") + data
        else:
            parent.text = (parent.text or "") + data


def _nodes(root, class_name):
    return (node for node in root.iter() if class_name in node.get("class", "").split())


def _text(node):
    return re.sub(r"\s+", " ", "".join(node.itertext())).strip()


def _parameter(root, name):
    script = "\n".join(node.text or "" for node in root.iter("script"))
    match = re.search(r"\b" + re.escape(name) + r"\s*:\s*('(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")", script)
    if match is None:
        raise ValueError(f"BiliManga reader parameter missing: {name}")
    return ast.literal_eval(match[1]).replace("\\/", "/")


class BiliManga(BaseSource):
    name = "bilimanga"
    display_name = "嗶哩漫畫"
    lang_choices = ("zh-TW",)
    capabilities = frozenset({"crawl"})

    def __init__(self, throttle=0.0, lang="zh-TW"):
        super().__init__(throttle=throttle, lang=lang)
        self.browser_session = None
        self._snapshot = None
        self._documents = {}
        self._volumes = {}
        self._referers = {}

    @staticmethod
    def normalize(value):
        value = str(value).strip()
        if re.fullmatch(r"[0-9]+", value):
            return f"{SITE}/detail/{value}.html"
        address = urlsplit(value)
        paths = r"/(?:detail/[0-9]+(?:/vol_[0-9]+)?\.html|read/[0-9]+/(?:catalog|[0-9]+(?:_[0-9]+)?\.html))"
        if address.scheme != "https" or address.hostname not in {"www.bilimanga.net", "bilimanga.net"} or re.fullmatch(paths, address.path) is None:
            raise ValueError("嗶哩漫畫需要作品 ID、详情、目录、卷或阅读页链接")
        return SITE + address.path

    def make_client(self, throttle=0.0):
        session = requests.Session(impersonate="chrome_android", timeout=60, headers={
            "User-Agent": MOBILE_UA, "Accept-Language": "zh", "Accept": "*/*", "Referer": SITE + "/",
        })
        session.cookies.set("night", "1", domain=".bilimanga.net", path="/")
        return session

    def close(self):
        if self._client is not None:
            self._client.cookies.clear()
            self._client.close()
            self._client = None
        self.browser_session = self._snapshot = None
        self._documents.clear()
        self._volumes.clear()
        self._referers.clear()

    def _get(self, url, *, image=False):
        session = self.ensure_client()
        if self.browser_session is not None:
            snapshot = self.browser_session.session()
            if snapshot is not self._snapshot:
                for item in snapshot["cookies"]:
                    if item["name"] == "night":
                        continue
                    domain, expires = item["domain"], item["expires"]
                    session.cookies.jar.set_cookie(Cookie(
                        version=0, name=item["name"], value=item["value"], port=None, port_specified=False,
                        domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
                        path=item["path"], path_specified=True, secure=item["secure"],
                        expires=int(expires) if expires > 0 else None, discard=expires <= 0,
                        comment=None, comment_url=None, rest={},
                    ))
                self._snapshot = snapshot
        headers = {}
        if image:
            headers = {"Referer": self._referers[url],
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                "Sec-Fetch-Dest": "image", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Site": "cross-site"}
        for attempt in range(3):
            try:
                response = session.get(url, headers=headers)
                break
            except requests.RequestsError as error:
                if error.code != 35 or attempt == 2:
                    raise RuntimeError(f"BiliManga request failed ({urlsplit(url).hostname}, curl {error.code})") from None
                print(f"[bilimanga] TLS handshake failed; retry {attempt + 1}/2", flush=True)
                time.sleep(attempt + 1)
        if response.status_code == 403:
            raise RuntimeError("BiliManga HTTP 403；请在可正常阅读的浏览器页面打开扩展重试")
        if response.status_code != 200:
            raise RuntimeError(f"BiliManga HTTP {response.status_code}: {urlsplit(url).path}")
        return response

    def _document(self, url):
        if url not in self._documents:
            root = _Document(self._get(url).text).root
            verification = next(_nodes(root, "aui-ver-form"), None)
            if verification is not None:
                raise RuntimeError(_text(verification))
            self._documents[url] = root
        return self._documents[url]

    def get_title(self, title_id, **kw):
        url = self.normalize(title_id)
        identifier = re.search(r"/(?:detail|read)/([0-9]+)", urlsplit(url).path)[1]
        root = self._document(f"{SITE}/detail/{identifier}.html")
        summary = next(node for node in root.iter() if node.get("id") == "bookSummary")
        authors = [_text(node) for node in root.iter() if set(node.get("class", "").split()) & {"authorname", "illname"}]
        return Title(self.name, identifier, _text(next(_nodes(root, "book-title"))),
            author="、".join(authors), cover_url=urljoin(SITE, next(_nodes(root, "book-cover")).get("src")),
            description=_text(next(summary.iter("content"))))

    def get_chapters(self, title_id, **kw):
        url = self.normalize(title_id)
        path = urlsplit(url).path
        if re.fullmatch(r"/read/[0-9]+/[0-9]+(?:_[0-9]+)?\.html", path):
            root = self._document(url)
            volume = re.search(r"/vol_([0-9]+)\.html", _parameter(root, "url_mangainfo"))
            number = volume[1] if volume else "1"
            name = unicodedata.normalize("NFKC", _parameter(root, "chaptername"))
            return [Chapter(url, number, name)]
        identifier = re.search(r"/(?:detail|read)/([0-9]+)", path)[1]
        root = self._document(f"{SITE}/read/{identifier}/catalog")
        groups = list(_nodes(root, "catalog-volume"))
        chapters = []
        for group in groups or [root]:
            if groups:
                bar = next(_nodes(group, "chapter-bar"))
                volume_url = urljoin(SITE, next(bar.iter("a")).get("href"))
                number = re.search(r"/vol_([0-9]+)\.html", volume_url)[1]
            else:
                volume_url, number = f"{SITE}/detail/{identifier}.html", "1"
            self._volumes[volume_url] = [None if node.get("href") == "javascript:cid(1)"
                else self.normalize(urljoin(SITE, node.get("href"))) for node in _nodes(group, "chapter-li-a")]
            if not self._volumes[volume_url]:
                raise RuntimeError("嗶哩漫畫目录没有可用章节")
            chapters.append(Chapter(volume_url, number, f"第{number}卷"))
        if "/vol_" in path:
            chapters = [chapter for chapter in chapters if chapter.id == url]
        if not chapters:
            raise ValueError("嗶哩漫畫目录中未找到该卷")
        return chapters

    def _reader_url(self, urls, index):
        if urls[index] is not None:
            return urls[index]
        known = [position for position, url in enumerate(urls) if url is not None]
        if not known:
            raise RuntimeError("目录隐藏了全部章节链接；请从可阅读的具体话页面发起下载")
        previous = [position for position in known if position < index]
        position = previous[-1] if previous else known[0]
        step = 1 if position < index else -1
        while position != index:
            root = self._document(urls[position])
            target = _parameter(root, "url_next" if step == 1 else "url_previous")
            position += step
            urls[position] = self.normalize(urljoin(SITE, target))
        return urls[index]

    def get_pages(self, chapter, **kw):
        urls = self._volumes[chapter.id] if chapter.id in self._volumes else [chapter.id]
        pages = []
        for index in range(len(urls)):
            reader = self._reader_url(urls, index)
            root = self._document(reader)
            images = list(_nodes(root, "imagecontent"))
            if not images:
                raise RuntimeError("嗶哩漫畫未取得图片；请确认该话在移动端可读且拥有阅读权限")
            for node in images:
                image_url = urljoin(reader, node.attrib["data-src"])
                self._referers[image_url] = reader
                pages.append(Page(url=image_url, ext="webp", mime="image/webp"))
        return pages

    def download_page(self, page, chapter, **kw):
        data = self._get(page.url, image=True).content
        # AVIF is converted losslessly for the existing archive and EPUB readers.
        with Image.open(BytesIO(data)) as image:
            output = BytesIO()
            image.save(output, format="WEBP", lossless=True)
        return output.getvalue()
