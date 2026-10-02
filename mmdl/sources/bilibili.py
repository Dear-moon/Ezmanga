"""Bilibili comics through HTTP/WASM, with optional Canvas capture."""
from io import BytesIO
import re
import time
from urllib.parse import urlsplit

from PIL import Image
from mmdl.core.model import Title, Chapter, Page, CaptureResult
from .base import BaseSource


class Bilibili(BaseSource):
    name = "bilibili"
    display_name = "哔哩哔哩漫画"
    lang_choices = None
    quality_choices = None
    capabilities = frozenset({"crawl", "capture"})
    page_extensions = ("jpg", "png", "webp", "avif", "gif")
    default_output = "manga_million"

    def __init__(self, throttle=0.0, lang="zh-CN", cdp_url="http://127.0.0.1:9222",
                 manga_name="", page_coords=None, bili_mode="http"):
        super().__init__(throttle=throttle, lang=lang)
        self.cdp_url = cdp_url
        self.manga_name = manga_name
        self.page_coords = page_coords or {"next": (258, 600), "prev": (773, 600)}   # 点击翻页(左1/3下页, 右1/3上页)
        self.bili_mode = bili_mode
        self._books = {}
        self._page_keys = {}

    def make_client(self, throttle=0.0):
        from .bilibili_client import BilibiliClient
        return BilibiliClient()

    def setup(self):
        self.ensure_client().login()

    @staticmethod
    def _ids(value):
        value = str(value).strip()
        if value.isascii() and value.isdigit():
            return int(value), None
        url = urlsplit(value)
        match = re.fullmatch(r"/(?:detail/)?mc(\d+)(?:/(\d+))?/?", url.path)
        if url.scheme != "https" or url.hostname != "manga.bilibili.com" or match is None:
            raise ValueError("Bilibili requires a comic ID, comic URL, or reader URL")
        return int(match[1]), int(match[2]) if match[2] else None

    def _book(self, value):
        comic_id, _ = self._ids(value)
        if comic_id not in self._books:
            self._books[comic_id] = self.ensure_client().post("ComicDetail", {"comic_id": comic_id})
        return self._books[comic_id]

    def get_title(self, title_id, *, lang=None, quality=None, **kw):
        book = self._book(title_id)
        return Title(source=self.name, id=str(book["id"]), name=book["title"],
                     author=", ".join(book["author_name"]), cover_url=book["vertical_cover"],
                     description=book["evaluate"])

    def get_chapters(self, title_id, *, lang=None, quality=None, **kw):
        _, episode_id = self._ids(title_id)
        episodes = sorted(self._book(title_id)["ep_list"], key=lambda episode: episode["ord"])
        if episode_id is not None:
            episodes = [episode for episode in episodes if episode["id"] == episode_id]
            if not episodes:
                raise ValueError("Bilibili reader chapter does not belong to this comic")
        return [Chapter(id=str(episode["id"]), number=str(episode["ord"]), name=episode["title"])
                for episode in episodes]

    def get_pages(self, chapter, *, lang=None, quality=None, **kw):
        client = self.ensure_client()
        images = client.post("GetImageIndex", {"ep_id": int(chapter.id)})["images"]
        private, urls = client.image_tokens(images)
        self._page_keys = {url: (private, index) for index, url in enumerate(urls)}
        return [Page(url=url, ext="jpg", mime="image/jpeg") for url in urls]

    def download_page(self, page, chapter, *, lang=None, quality=None, client=None, **kw):
        private, index = self._page_keys[page.url]
        data = (client or self.ensure_client()).download_image(page.url, private, index)
        image = Image.open(BytesIO(data))
        image.load()
        page.ext, page.mime = {
            "JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"),
            "WEBP": ("webp", "image/webp"), "AVIF": ("avif", "image/avif"),
            "GIF": ("gif", "image/gif"),
        }[image.format]
        return data

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None
        self._page_keys.clear()

    def http_config(self):
        raise NotImplementedError("Bilibili uses a signed API client; no generic HTTP config.")

    def _cdp(self):
        from mmdl.core.cdp import CdpClient
        return CdpClient(cdp_url=self.cdp_url, target_url_substr="manga.bilibili")

    def capture_from_url(self, url, *, lang=None, quality=None, **kw):
        """Capture one reader chapter using the selected download mode."""
        if self.bili_mode == "http":
            if self._ids(url)[1] is None:
                raise ValueError("Bilibili capture requires a reader URL with a chapter ID")
            title = self.get_title(url)
            chapters = self.get_chapters(url)
            for chapter in chapters:
                chapter.pages = self.get_pages(chapter)
                for page in chapter.pages:
                    page.data = self.download_page(page, chapter)
                    page.url = None
                    time.sleep(self.throttle)
            return CaptureResult(title=title, chapters=chapters)
        client = self._cdn_open()
        client.connect()
        try:
            title_name = self.manga_name or self._guess_name(client)
            total = self._total_pages(client)
            pages = self._gather_pages(client, total=total)
            chapters = []
            chap = Chapter(id=url, number="", name=title_name or "reader", pages=pages)
            chapters.append(chap)
            return CaptureResult(
                title=Title(source=self.name, id=url, name=title_name or "bilibili"),
                chapters=chapters,
            )
        finally:
            client.close()

    def _cdn_open(self):
        return self._cdp()

    def _guess_name(self, client):
        val = client.eval("document.title || ''")
        part = (val or "").split("-")   # 形如 "48 - Unnamed Memory - 哔哩哔哩漫画"
        return part[1].strip() if len(part) > 1 else (val or "bilibili")

    def _extract_all(self, client):
        """提取当前页的全部漫画 canvas。返回 [Page(data=...)]。"""
        import time
        time.sleep(1.5)
        return [Page(data=d, ext="png", mime="image/png") for d, w, h, _ in client.extract_canvas_png()]

    def _pagenum(self, client):
        """当前跨页页码文本（形如 '1 2'），读不到返回 ''。"""
        val = client.eval(r"((document.body.innerText||'').match(/\d+\s*\n\s*\d+\s*\d+P?/)||[''])[0]")
        return (val or "").strip()

    def _total_pages(self, client):
        """从 '…70P' 读总页数；读不到返回 0。"""
        m = client.eval(r"((document.body.innerText||'').match(/(\d+)P\b/)||[])")
        try:
            return int(m[1])
        except (TypeError, ValueError, IndexError):
            return 0

    def _ep_id(self, client):
        """从 location.href 提取当前 ep 编号（如 1226681）；失败返回 ''。"""
        m = client.eval(r"((location.href||'').match(/mc\d+\/(\d+)/)||[])")
        try:
            return m[1]
        except (TypeError, IndexError):
            return ""

    def _gather_pages(self, client, total=0):
        """整话遍历：Home 回开头 → 逐跨页提取 → ArrowDown 慢速翻页 → 去重。

        收集满 total / 页码不前进 / ep 变化（翻进下一话）即停，防越过本话边界。
        """
        import hashlib, time

        def h(data):
            return hashlib.sha256(data).hexdigest()[:16]

        if not total:
            total = self._total_pages(client)
        client.key("Home", "Home", 36)   # 回到本话第 1 跨页(避开记住的阅读位置停在中间/末尾)
        time.sleep(2.0)
        pages, seen = [], set()
        step, guard = 0, max(40, total or 40)
        ep0 = self._ep_id(client)
        while len(pages) < total and step < guard:
            step += 1
            for pg in self._extract_all(client):
                key = h(pg.data)
                if key in seen:
                    continue
                seen.add(key)
                pages.append(pg)
            if len(pages) >= total:      # 收集满即停，不翻越去下一话
                break
            page_str = self._pagenum(client)
            client.key("ArrowDown", "ArrowDown", 40)
            time.sleep(2.0)   # ≥ 风控阈值(1.5s)，慢速逐跨页
            if self._ep_id(client) != ep0:   # 翻进了下一话（跨话）→ 停
                break
            nxt = self._pagenum(client)
            if nxt == page_str:          # 翻不动 => 到底
                break
        return pages
