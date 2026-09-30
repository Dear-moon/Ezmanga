"""Light Novel Shelf comics through its authenticated reading API."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from mmdl.core.model import Title, Chapter, Page
from .base import BaseSource
from .lightnovel_client import LightnovelClient, SignalRError


class Lightnovel(BaseSource):
    name = "lightnovel"
    display_name = "轻书架漫画"
    capabilities = frozenset({"list", "crawl"})

    def __init__(self, throttle=0.0, lang="zh-CN", token=None):
        super().__init__(throttle=throttle, lang=lang)
        self.token = token
        self._books = {}

    def make_client(self, throttle=0.0):
        path = Path(os.environ["LIGHTNOVEL_CONFIG"]).expanduser() if os.environ.get("LIGHTNOVEL_CONFIG") else Path.home() / ".mmdl" / "lightnovel.json"
        config = {}
        if path.is_file():
            config = json.loads(path.read_text(encoding="utf-8"))
            config = config.get("lightnovel", config)
        elif os.environ.get("LIGHTNOVEL_CONFIG"):
            raise RuntimeError("LIGHTNOVEL_CONFIG file does not exist")
        token = self.token or os.environ.get("LIGHTNOVEL_REFRESH_TOKEN") or config.get("refresh_token")
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError("lightnovel requires a refresh token; set LIGHTNOVEL_REFRESH_TOKEN or LIGHTNOVEL_CONFIG")
        base = os.environ.get("LIGHTNOVEL_API_BASE") or config.get("api_base")
        return LightnovelClient(token.strip(), api_base=base)

    @staticmethod
    def _book_id(value):
        value = str(value).strip()
        if not value.isascii() or not value.isdigit():
            parts = urlsplit(value)
            match = re.fullmatch(r"/manga/(\d+)/?", parts.path)
            if parts.scheme != "https" or parts.hostname not in ("www.lightnovel.app", "lightnovel.app") or not match:
                raise ValueError("lightnovel --title must be a manga ID or https://www.lightnovel.app/manga/<id> URL")
            value = match.group(1)
        book_id = int(value)
        if book_id <= 0:
            raise ValueError("lightnovel manga ID must be positive")
        return book_id

    def _book(self, title_id):
        book_id = self._book_id(title_id)
        if book_id not in self._books:
            result = self.ensure_client().invoke("GetBookInfo", {"Id": book_id})
            book = result.get("Book") if isinstance(result, dict) else None
            if not isinstance(book, dict) or book.get("Id") != book_id:
                raise SignalRError("lightnovel returned invalid book metadata")
            if book.get("Type") != "Comic":
                raise ValueError("the supplied lightnovel ID is not a comic")
            self._books[book_id] = book
        return self._books[book_id]

    def _title(self, book):
        return Title(source=self.name, id=str(book["Id"]), name=book.get("Title") or str(book["Id"]),
                     author=book.get("Author") or "", cover_url=book.get("Cover") or "",
                     description=book.get("Introduction") or "")

    def list_titles(self, *, lang=None, **kw):
        client = self.ensure_client()
        titles = []
        seen = set()
        page = 1
        while True:
            result = client.invoke("GetComicList", {"Page": page, "Size": 24, "Order": "latest"})
            if not isinstance(result, dict) or not isinstance(result.get("Data"), list):
                raise SignalRError("lightnovel returned an invalid comic list")
            if result.get("Page") != page:
                raise SignalRError("lightnovel returned an unexpected comic list page")
            for book in result["Data"]:
                if book["Id"] not in seen:
                    seen.add(book["Id"])
                    titles.append(self._title(book))
            if not result.get("HasMore"):
                return titles
            if not result["Data"]:
                raise SignalRError("lightnovel comic list pagination made no progress")
            page += 1

    def get_title(self, title_id, *, lang=None, quality=None, **kw):
        return self._title(self._book(title_id))

    def get_chapters(self, title_id, *, lang=None, quality=None, **kw):
        book = self._book(title_id)
        chapters = book.get("Chapters")
        if not isinstance(chapters, list):
            raise SignalRError("lightnovel returned an invalid chapter list")
        return [Chapter(id=str(chapter["Id"]), number=str(chapter["SortNum"]), name=chapter.get("Title") or "")
                for chapter in sorted(chapters, key=lambda item: item["SortNum"])]

    def get_pages(self, chapter: Chapter, *, lang=None, quality=None, **kw):
        client = self.ensure_client()
        pages = []
        total = None
        skip = 0
        while total is None or skip < total:
            result = client.invoke("GetComicContent", {"Cid": int(chapter.id), "Skip": skip, "Take": 12})
            data = result.get("Chapter") if isinstance(result, dict) else None
            if not isinstance(data, dict) or str(data.get("Id")) != chapter.id or data.get("Skip") != skip:
                raise SignalRError("lightnovel returned an unexpected comic page batch")
            images = data.get("Images")
            count = data.get("Total")
            if not isinstance(images, list) or not isinstance(count, int) or count < 0:
                raise SignalRError("lightnovel returned invalid comic page metadata")
            if total is not None and total != count:
                raise SignalRError("lightnovel chapter changed during pagination; retry the download")
            total = count
            if skip + len(images) > total:
                raise SignalRError("lightnovel returned too many comic pages")
            if not images and skip < total:
                raise SignalRError("lightnovel comic pagination stopped before all pages were returned")
            for url in images:
                if not isinstance(url, str) or not url:
                    raise SignalRError("lightnovel returned an invalid comic image URL")
                pages.append(Page(url=url, ext="webp", mime="image/webp"))
            skip += len(images)
        return pages

    def download_page(self, page: Page, chapter: Chapter, *, lang=None, quality=None, client=None, **kw):
        return (client or self.ensure_client()).download_image(page.url)
