"""東立電子書城 source：公开接口 + 免费试读（无 DRM）。

/Book、/Book/BookVol 免登录；/Comic/sas 需要 Firebase Bearer token（见 tongli_auth.py，
自动解析/刷新，密码不落盘）。图片是 Azure 签名直链，直接 GET。
"""
from mmdl.core.http import HttpClient, HttpConfig, split_url
from mmdl.core.model import Title, Chapter, Page
from .base import BaseSource
from .tongli_auth import resolve_access_token

API_HOST = "api.tongli.tw"
SITE = "https://ebook.tongli.com.tw"

# 免费试读 token（Comic/sas 索取试读页数据时附带）
FREE_TRIAL = "free"


class Tongli(BaseSource):
    name = "tongli"
    display_name = "東立電子書城"
    lang_choices = None           # 无语言维度（本身是繁体中文）
    quality_choices = None
    capabilities = frozenset({"crawl"})   # 不对外 list（public 检索有限），按 bookID 抓取

    def __init__(self, throttle=0.0, lang="zh-TW", book_group=None, token=None,
                 email=None, password=None):
        super().__init__(throttle=throttle, lang=lang)
        self.book_group = book_group   # 可选 BookGroupID；缺省从 /Book 返回取
        self.token = token             # 显式静态 idToken；None 走 refresh/登录
        self.email = email
        self.password = password
        self._fresh = None             # 本进程内解析好的 idToken 缓存

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

    # ---- 认证 ----
    def _access_token(self):
        """返回可用 idToken。静态 token 优先；否则 resolve（结果缓存在 _fresh）。"""
        if self.token:
            return self.token
        if self._fresh is None:
            self._fresh = resolve_access_token(email=self.email, password=self.password)
        return self._fresh

    def _auth_client(self):
        """返回带最新 Authorization 的共享 client（每次刷新 token 到 extra_headers）。"""
        tok = self._access_token()
        client = self.ensure_client()
        client.extra_headers["Authorization"] = f"bearer {tok}"
        return client

    # ---- API ----
    def _get(self, client, path, params=None):
        """GET api.tongli.tw 并解析 JSON；非 200 抛错。"""
        st, body = client.request(API_HOST, "GET", path, params=params)
        if st != 200:
            raise RuntimeError(f"GET {path} HTTP {st}")
        import json
        return json.loads(body.decode("utf-8")) if isinstance(body, bytes) else json.loads(body)

    # ---- BaseSource 实现（crawl 轨）----
    def get_title(self, title_id, *, lang=None, quality=None, **kw):
        """bookID -> Title。title_id 是单集或书组的 GUID。"""
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
        """把该系列的各集当作 Chapter（number=Vol，id=单集 BookID）。"""
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
        """Comic/sas 拿该集每页 ImageURL（Azure SAS 直链），需 token。

        付费/无免费试读（非 200）返回空列表 → driver 跳过该集。
        """
        path = f"/Comic/sas/{chapter.id}"
        client = self._auth_client()
        st, body = client.request(API_HOST, "GET", path, params={"freeTrialToken": FREE_TRIAL})
        if st == 401:
            # token 失效（尤其 idToken 过期）→ 清缓存重解析后重试一次
            self._fresh = None
            client = self._auth_client()
            st, body = client.request(API_HOST, "GET", path, params={"freeTrialToken": FREE_TRIAL})
        if st != 200:
            return []   # 该集无免费试读/不可访问，跳过（driver 会打印 skip）
        import json
        data = json.loads(body.decode("utf-8")) if isinstance(body, bytes) else json.loads(body)
        pages = []
        for p in data.get("Pages") or []:
            url = p.get("ImageURL")
            if url:
                pages.append(Page(url=url, ext="jpg", mime="image/jpeg"))
        return pages

    def download_page(self, page: Page, chapter: Chapter, *, lang=None, quality=None,
                      client=None, **kw):
        """直链直接 GET 原始字节。SAS 直链不需 token，共享 client 若带 Authorization
        会触发 Azure 400 "Both authorizations"，故下载前临时移除。
        """
        if client is None:
            client = self.ensure_client()
        host, path = split_url(page.url)
        auth = client.extra_headers.pop("Authorization", None)
        try:
            st, body = client.request(host, "GET", path, img=True)
        finally:
            if auth is not None:
                client.extra_headers["Authorization"] = auth
        if st == 200 and body:
            return body
        raise RuntimeError(f"download page HTTP {st}")
