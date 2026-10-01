"""Run user-selected Keiyoushi HTTP extensions in an on-demand JVM."""
import base64
import io
from pathlib import PurePosixPath
from urllib.parse import urlparse

from .base import BaseSource
from .keiyoushi_client import KeiyoushiClient, setup_runtime
from mmdl.core.model import Title, Chapter, Page


class Keiyoushi(BaseSource):
    name = "keiyoushi"
    display_name = "Keiyoushi extensions"
    default_output = "manga_million"
    capabilities = frozenset({"crawl"})
    page_extensions = ("jpg", "png", "webp", "gif")

    def __init__(self, throttle=0.0, lang="en"):
        super().__init__(throttle=throttle, lang=lang)
        self.extension = None
        self.extension_source = None
        self.page = 1
        self.search_filters = {}
        self._runtime = None
        self._selected = False

    def setup(self):
        setup_runtime()

    def close(self):
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None
        self._selected = False

    def extension_sources(self):
        if not self.extension:
            raise ValueError("Specify --extension after installing a Keiyoushi extension")
        if self._runtime is None:
            self._runtime = KeiyoushiClient(self.extension)
        return self._runtime.call("sources")

    def _call(self, op, *, lang=None, **params):
        if not self._selected:
            sources = self.extension_sources()
            if self.extension_source:
                matches = [source for source in sources if source["id"] == self.extension_source]
            elif lang:
                matches = [source for source in sources if source["lang"] == lang]
            elif len(sources) == 1:
                matches = sources
            else:
                matches = [source for source in sources if source["lang"] == self.lang]
            if len(matches) != 1:
                raise ValueError("Select one source using --lang or --extension-source; inspect IDs with --extension-sources")
            self._runtime.call("select", source=matches[0]["id"])
            self._selected = True
        try:
            return self._runtime.call(op, **params)
        except RuntimeError:
            self.close()
            raise

    def source_preferences(self, *, lang=None):
        return self._call("preferences", lang=lang)

    def set_preferences(self, values, *, lang=None):
        self._call("set_preferences", lang=lang, values=values)
        # Recreate extension clients because authentication headers may have been cached.
        self.close()

    def source_filters(self, *, lang=None):
        return self._call("filters", lang=lang)

    def ensure_client(self):
        return None

    def _title(self, value):
        return Title(self.name, value["url"], value["title"], value.get("author") or "",
                     value.get("cover") or "", value.get("description") or "")

    def list_titles(self, *, query, lang=None, **kw):
        result = self._call("search", lang=lang, page=self.page, query=query, filters=self.search_filters)
        if result["has_more"]:
            print(f"[more] More results available with --page {self.page + 1}")
        return [self._title(value) for value in result["items"]]

    def get_title(self, title_id, *, lang=None, **kw):
        return self._title(self._call("title", lang=lang, url=str(title_id)))

    def get_chapters(self, title_id, *, lang=None, **kw):
        values = self._call("chapters", lang=lang, url=str(title_id))
        if all(value["number"] >= 0 for value in values):
            values.sort(key=lambda value: value["number"])
        else:
            values.reverse()
        return [Chapter(value["handle"], format(value["number"], "g") if value["number"] >= 0 else str(index),
                        value["name"] + (f" [{value['scanlator']}]" if value.get("scanlator") else ""))
                for index, value in enumerate(values, 1)]

    def get_pages(self, chapter, *, lang=None, **kw):
        values = self._call("pages", lang=lang, chapter=chapter.id)
        values.sort(key=lambda value: value["index"])
        result = []
        for value in values:
            suffix = PurePosixPath(urlparse(value.get("image_url") or "").path).suffix.lstrip(".").lower()
            extension = "jpg" if suffix == "jpeg" else suffix
            if extension not in self.page_extensions:
                extension = "jpg"
            result.append(Page(url=value["handle"], ext=extension))
        return result

    def download_page(self, page, chapter, *, lang=None, **kw):
        reply = self._call("image", lang=lang, page=page.url)
        data = base64.b64decode(reply["data"], validate=True)
        if data.startswith(b"\xff\xd8\xff"):
            page.ext, page.mime = "jpg", "image/jpeg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            page.ext, page.mime = "png", "image/png"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            page.ext, page.mime = "gif", "image/gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            page.ext, page.mime = "webp", "image/webp"
        elif data[4:8] == b"ftyp" and b"avif" in data[8:64]:
            from PIL import Image, features
            if not features.check("avif"):
                raise RuntimeError("AVIF images require Pillow 11.3+ with AVIF support")
            with Image.open(io.BytesIO(data)) as image:
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Animated AVIF pages are not supported")
                output = io.BytesIO()
                # PNG keeps decoded AVIF pixels compatible with existing EPUB readers.
                image.save(output, format="PNG")
                data = output.getvalue()
            page.ext, page.mime = "png", "image/png"
        else:
            raise ValueError("Extension returned unsupported image bytes (expected JPEG, PNG, WebP, GIF, or static AVIF)")
        return data
