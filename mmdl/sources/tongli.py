"""東立電子書城 source: public endpoints + free preview (no DRM).

/Book and /Book/BookVol need no login; /Comic/sas needs a Firebase Bearer token (see tongli_auth.py, auto
parse/refresh, password never persisted). Images are Azure signed direct links, fetched with a plain GET.
"""
from urllib.parse import urlparse

from mmdl.core.http import HttpClient, HttpConfig, split_url
from mmdl.core.model import Title, Chapter, Page
from .base import BaseSource
from .tongli_auth import resolve_access_token

API_HOST = "api.tongli.tw"
SITE = "https://ebook.tongli.com.tw"


class Tongli(BaseSource):
    name = "tongli"
    display_name = "東立電子書城"
    lang_choices = None           # no language dimension (already traditional Chinese)
    quality_choices = None
    capabilities = frozenset({"crawl"})   # no public list (search is limited); fetched by bookID

    def __init__(self, throttle=0.0, lang="zh-TW", book_group=None, token=None,
                 email=None, password=None):
        super().__init__(throttle=throttle, lang=lang)
        self.book_group = book_group   # optional BookGroupID; defaults to the one returned by /Book
        self.token = token             # explicit static idToken; None falls back to refresh/login
        self.email = email
        self.password = password
        self._fresh = None             # idToken cache resolved within this process

    # ---- HTTP ----
    def http_config(self) -> HttpConfig:
        return HttpConfig(
            origin=SITE,
            referer=SITE + "/",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"),
            content_type="application/json",
            verify_ssl=True,
            extra_headers={"Authorization": f"bearer {self.token}"} if self.token else {},
        )

    def make_client(self, throttle=0.0) -> HttpClient:
        return HttpClient(self.http_config(), throttle=throttle)

    # ---- auth ----
    def _access_token(self):
        """Return a usable idToken. Static token first; otherwise resolve (result cached in _fresh)."""
        if self.token:
            return self.token
        if self._fresh is None:
            self._fresh = resolve_access_token(email=self.email, password=self.password)
        return self._fresh

    def _auth_client(self):
        """Return the shared client with a fresh Authorization header (token refreshed into extra_headers)."""
        tok = self._access_token()
        client = self.ensure_client()
        client.extra_headers["Authorization"] = f"bearer {tok}"
        return client

    # ---- API ----
    def _get(self, client, path, params=None):
        """GET api.tongli.tw and parse JSON; raises on non-200."""
        st, body = client.request(API_HOST, "GET", path, params=params)
        if st != 200:
            raise RuntimeError(f"GET {path} HTTP {st}")
        import json
        return json.loads(body.decode("utf-8")) if isinstance(body, bytes) else json.loads(body)

    # ---- BaseSource implementation (crawl track) ----
    def get_title(self, title_id, *, lang=None, quality=None, **kw):
        """bookID -> Title. title_id is a single-volume or book-group GUID."""
        client = self.ensure_client()
        d = self._get(client, "/Book", params={"bookID": title_id})
        return Title(
            source=self.name,
            id=str(d.get("BookID", title_id)),
            name=d.get("Title") or str(title_id),
            author="、".join(a.get("Name", "") for a in d.get("Authors") or []),
            cover_url=d.get("CoverURL") or "",
            description=d.get("Introduction") or "",
        )

    def get_chapters(self, title_id, *, lang=None, quality=None, **kw):
        """Treat each volume of the series as a Chapter (number=Vol, id=single-volume BookID)."""
        client = self.ensure_client()
        d = self._get(client, "/Book", params={"bookID": title_id})
        vol_guid = d.get("BookGroupID")
        if not vol_guid:
            raise RuntimeError(f"no BookGroupID for {title_id}")
        vols = self._get(client, f"/Book/BookVol/{vol_guid}", params={"bookID": title_id})
        chapters = []
        for v in vols or []:
            chapters.append(Chapter(
                id=str(v.get("BookID")),
                number=v.get("Vol") or "",
                name=v.get("Vol") or "",
            ))
        return chapters

    def get_pages(self, chapter: Chapter, *, lang=None, quality=None, **kw):
        """Fetch each page ImageURL for this volume via Comic/sas (Azure SAS direct link); needs a token.

        WARNING: do NOT send the freeTrialToken query parameter -- measured to return HTTP 404 even with a valid
        token (the web reader omits it too). Paid/unpurchased (non-200) returns an empty list so the driver skips it.
        """
        path = f"/Comic/sas/{chapter.id}"
        client = self._auth_client()
        st, body = client.request(API_HOST, "GET", path)
        if st == 401:
            # token invalid (esp. expired idToken) -> clear cache, re-resolve, retry once
            self._fresh = None
            client = self._auth_client()
            st, body = client.request(API_HOST, "GET", path)
        if st != 200:
            # server errors (e.g. "超過使用裝置上限") and "no free preview" both land here; print the reason to avoid a silent 0 pages.
            import json
            try:
                msg = (json.loads(body.decode("utf-8")) or {}).get("Error") or ""
            except Exception:
                msg = ""
            print(f"[tongli] skip {chapter.name or chapter.id}: HTTP {st} {msg}".rstrip())
            return []
        import json
        data = json.loads(body.decode("utf-8")) if isinstance(body, bytes) else json.loads(body)
        pages = []
        for p in data.get("Pages") or []:
            url = p.get("ImageURL")
            if url:
                pages.append(Page(url=url, ext="jpg", mime="image/jpeg"))
        return pages

    def _fetch_sas(self, url, client):
        """GET a SAS direct link with Authorization removed (Azure rejects both auth schemes at once)."""
        host, path = split_url(url)
        auth = client.extra_headers.pop("Authorization", None)
        try:
            return client.request(host, "GET", path, img=True)
        finally:
            if auth is not None:
                client.extra_headers["Authorization"] = auth

    def _refresh_page_url(self, page, chapter):
        """Re-fetch the page list and return the fresh SAS URL for `page`, matched by page number."""
        number = urlparse(page.url).path.rsplit("/", 1)[-1]
        for p in self.get_pages(chapter):
            if urlparse(p.url).path.rsplit("/", 1)[-1] == number:
                return p.url
        return ""

    def download_page(self, page: Page, chapter: Chapter, *, lang=None, quality=None,
                      client=None, **kw):
        """Fetch raw bytes from the SAS direct link.

        A SAS lives only ~7 minutes, so a long download outlives it; on 403 re-fetch the page list
        and retry once with the freshly signed URL.
        """
        if client is None:
            client = self.ensure_client()
        st, body = self._fetch_sas(page.url, client)
        if st == 403:
            fresh = self._refresh_page_url(page, chapter)
            if fresh:
                st, body = self._fetch_sas(fresh, client)
        if st == 200 and body:
            return body
        raise RuntimeError(f"download page HTTP {st}")
