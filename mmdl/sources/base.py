"""Source 抽象基类。

三种能力轨：
- crawl 轨：list_titles / get_title / get_chapters / get_pages / download_page
  覆盖 MangaMillion、东立这类"按 title_id 遍历 title→chapter→pages"的源。
- capture 轨：capture_from_url(url) -> CaptureResult
  覆盖 BookWalker 这类"给一个 reader URL 直接抓整卷字节"的源。
- book 轨：get_book(book_id) -> CaptureResult
  覆盖 Kobo 这类"整本带 DRM 下载 + 解密 + 抽页图"的源（产物与 capture 同构）。

capabilities 声明源支持哪些能力，供 CLI 门控。
"""
import abc
from pathlib import Path

from mmdl.core.http import HttpClient, HttpConfig
from mmdl.core.model import Title, Chapter, Page, CaptureResult


class BaseSource(abc.ABC):
    name: str = ""
    display_name: str = ""

    # None = 忽略 --lang / --quality（源无该维度）
    lang_choices: tuple[str, ...] | None = None
    quality_choices: tuple[str, ...] | None = None

    # 能力集：{"list", "crawl", "capture"}
    capabilities: frozenset[str] = frozenset({"crawl"})

    def __init__(self, throttle: float = 0.0, lang: str = "en"):
        self.throttle = throttle
        self.lang = lang
        self._client: HttpClient | None = None

    @property
    def default_output(self) -> str:
        return str(Path("manga_download") / self.name)

    # ---- HTTP 配置（仅 crawl 源需要；浏览器辅助源如 bilibili/bookwalker 不适用，可忽略） ----
    def http_config(self) -> HttpConfig:
        """crawl 源返回 HttpConfig；浏览器源不适用。"""
        raise NotImplementedError(f"{self.name} is not an HTTP source")

    def make_client(self, throttle: float = 0.0) -> HttpClient:
        """crawl 源基于 http_config 构造 client；浏览器源不适用。"""
        raise NotImplementedError(f"{self.name} is not an HTTP source")

    def ensure_client(self) -> HttpClient:
        """懒加载共享 HttpClient（crawl 源内部所有请求走同一实例，保证 token 复用）。"""
        if self._client is None:
            self._client = self.make_client(self.throttle)
        return self._client

    # ---- crawl 轨 ----
    def list_titles(self, *, lang=None, **kw) -> list[Title]:
        raise NotImplementedError

    def get_title(self, title_id, *, lang=None, quality=None, **kw) -> Title:
        raise NotImplementedError

    def get_chapters(self, title_id, *, lang=None, quality=None, **kw) -> list[Chapter]:
        raise NotImplementedError

    def get_pages(self, chapter: Chapter, *, lang=None, quality=None, **kw) -> list[Page]:
        """默认实现：chapter.pages 已预取则直接返回（capture 轨用）。"""
        if chapter.pages is not None:
            return chapter.pages
        raise NotImplementedError

    def download_page(self, page: Page, chapter: Chapter, *, lang=None, quality=None,
                      client=None, **kw) -> bytes:
        """默认实现：对 page.url 做一次普通 GET（东立直链即走这里）。"""
        raise NotImplementedError

    # ---- capture 轨 ----
    def capture_from_url(self, url, *, lang=None, quality=None, **kw) -> CaptureResult:
        raise NotImplementedError

    # ---- book 轨 ----
    def get_book(self, book_id, *, lang=None, quality=None, **kw) -> CaptureResult:
        """下载整本带 DRM 的书并抽成页图 → CaptureResult（与 capture 轨同构）。

        仅 book 能力源（如 Kobo）实现；资源少从 server 整体拉取时用。
        """
        raise NotImplementedError
