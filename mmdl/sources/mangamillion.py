"""MangaMillion source：protobuf 协议 + AES-256-CBC 图片解密。

后端 api.mangamillion.shueisha.co.jp，响应全为 protobuf。网页为 Next.js SPA。
流程：register 拿设备 token -> manga_list/title_detail/chapter_list -> viewer 拿页面URL+密钥 -> 下载解密。
"""
import time

from Crypto.Cipher import AES

from mmdl.core.http import HttpClient, HttpConfig
from mmdl.core.model import Title, Chapter, Page
from .base import BaseSource

API_HOST = "api.mangamillion.shueisha.co.jp"
SITE = "https://mangamillion.shueisha.co.jp"


# ---------------- protobuf 手动解析 ----------------

def _varint(b, i):
    v = 0
    s = 0
    while True:
        x = b[i]
        i += 1
        v |= (x & 0x7F) << s
        if not x & 0x80:
            return v, i
        s += 7


def pb_fields(b):
    """解析 protobuf bytes -> {field: [(kind, value)]}; kind: v=varint / b=length-delimited。"""
    out = {}
    i = 0
    end = len(b)
    while i < end:
        tag, i = _varint(b, i)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _varint(b, i)
            out.setdefault(fn, []).append(("v", v))
        elif wt == 2:
            ln, i = _varint(b, i)
            out.setdefault(fn, []).append(("b", b[i:i + ln]))
            i += ln
        elif wt == 1:
            out.setdefault(fn, []).append(("b", b[i:i + 8]))
            i += 8
        elif wt == 5:
            out.setdefault(fn, []).append(("b", b[i:i + 4]))
            i += 4
        else:
            raise ValueError(f"unexpected wire type {wt} at field {fn}")
    return out


def pb_str(v):
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)


def pb_int(v):
    return v if isinstance(v, int) else int.from_bytes(v, "little")


# ---------------- protobuf 字段 -> 结构化 ----------------

def parse_manga_list(body):
    """manga_list 响应 -> [{id,name,author}]。field22 -> field1(MangaListItem) -> field1(OriginalTitleSummary)。"""
    resp = pb_fields(body)
    ml = resp.get(22, [("b", b"")])[0][1]
    inner = pb_fields(ml)
    out = []
    for kind, val in inner.get(1, []):
        if kind != "b":
            continue
        item = pb_fields(val)
        o = pb_fields(item.get(1, [("b", b"")])[0][1])
        oid = o.get(1, [("v", 0)])[0][1]
        out.append({
            "id": pb_int(oid),
            "name": pb_str(o.get(3, [("b", b"")])[0][1]) if 3 in o else "",
            "author": pb_str(o.get(4, [("b", b"")])[0][1]) if 4 in o else "",
        })
    return out


def parse_title_detail(body):
    """title_detail 响应 -> {name, author, description, coverUrl}。field50 -> field1(ServiceTitle)。"""
    resp = pb_fields(body)
    td = resp.get(50, [("b", b"")])[0][1]
    inner = pb_fields(td)
    st = pb_fields(inner.get(1, [("b", b"")])[0][1])
    return {
        "coverUrl": pb_str(st.get(1, [("b", b"")])[0][1]) if 1 in st else "",
        "name": pb_str(st.get(2, [("b", b"")])[0][1]) if 2 in st else "",
        "author": pb_str(st.get(3, [("b", b"")])[0][1]) if 3 in st else "",
        "description": pb_str(st.get(7, [("b", b"")])[0][1]) if 7 in st else "",
    }


def parse_chapter_list(body):
    """chapter_list 响应 -> [{number,name,id}]。field60 -> field2(ChapterGroup) -> field2(ChapterInfo)。"""
    resp = pb_fields(body)
    cl = resp.get(60, [("b", b"")])[0][1]
    inner = pb_fields(cl)
    chapters = []
    for gkind, gval in inner.get(2, []):
        if gkind != "b":
            continue
        g = pb_fields(gval)
        for ckind, cval in g.get(2, []):
            if ckind != "b":
                continue
            c = pb_fields(cval)
            chapters.append({
                "number": pb_str(c.get(1, [("b", "")])[0][1]) if 1 in c else "",
                "name": pb_str(c.get(2, [("b", "")])[0][1]) if 2 in c else "",
                "id": pb_int(c.get(3, [("v", 0)])[0][1]),
            })
        if not g.get(2) and g.get(3):
            chapters.append({
                "number": pb_str(g.get(1, [("b", "")])[0][1]) if 1 in g else "",
                "name": pb_str(g.get(2, [("b", "")])[0][1]) if 2 in g else "",
                "id": pb_int(g.get(3, [("v", 0)])[0][1]),
            })
    if not chapters:
        for ckind, cval in inner.get(2, []):
            c = pb_fields(cval)
            if c.get(3):
                chapters.append({
                    "number": pb_str(c.get(1, [("b", "")])[0][1]) if 1 in c else "",
                    "name": pb_str(c.get(2, [("b", "")])[0][1]) if 2 in c else "",
                    "id": pb_int(c.get(3, [("v", 0)])[0][1]),
                })
    return chapters


def parse_viewer(body):
    """viewer 响应 -> {pages:[url], aesKey, aesIv}。field70 -> field1(repeated page), 7 key, 8 iv。"""
    resp = pb_fields(body)
    vw = resp.get(70, [("b", b"")])[0][1]
    v = pb_fields(vw)
    pages = []
    for pkind, pval in v.get(1, []):
        if pkind != "b":
            continue
        p = pb_fields(pval)
        if 1 in p:
            pages.append(pb_str(p[1][0][1]))
    key = pb_str(v.get(7, [("b", b"")])[0][1]) if 7 in v else ""
    iv = pb_str(v.get(8, [("b", b"")])[0][1]) if 8 in v else ""
    return {"pages": pages, "aesKey": key, "aesIv": iv}


def aes_decrypt(data, key_hex, iv_hex):
    """AES-256-CBC + PKCS7 去填充。"""
    key = bytes.fromhex(key_hex)
    iv = bytes.fromhex(iv_hex)
    c = AES.new(key, AES.MODE_CBC, iv)
    pt = c.decrypt(data)
    if pt:
        pad = pt[-1]
        if 1 <= pad <= 16 and pt[-pad:] == bytes([pad]) * pad:
            pt = pt[:-pad]
    return pt


# ---------------- source 实现 ----------------

class MCMillion(BaseSource):
    name = "mangamillion"
    display_name = "MANGA MILLION"
    lang_choices = ("en", "ja", "zh-CN", "zh-TW", "ko-KR", "fr", "de", "es", "pt-BR", "ru", "th", "vi", "id")
    quality_choices = ("middle", "low")
    capabilities = frozenset({"list", "crawl"})

    def __init__(self, lang="en", throttle=0.3, token=None):
        super().__init__(throttle=throttle, lang=lang)
        self._token = token
        self.browser_session = False
        self._view_meta = {}  # chapter_id -> {pages, aesKey, aesIv} 缓存（viewer 拉取结果）

    def http_config(self) -> HttpConfig:
        return HttpConfig(
            origin=SITE,
            referer=SITE + "/",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            content_type="application/x-protobuf",
            verify_ssl=False,  # 保持现状：证书链异常仍能抓
            extra_headers={"Access-Token": self._token or ""} if self._token else {},
            img_headers={
                "Sec-Fetch-Dest": "image",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site",
                "Referer": SITE + "/en/title/1/chapter/1",
            },
        )

    def make_client(self, throttle=0.0) -> HttpClient:
        return HttpClient(self.http_config(), throttle=throttle)

    # ---- 鉴权 ----
    def _register(self, client: HttpClient):
        """POST /api/register -> device token，写回 self._token 并更新 extra_headers。"""
        st, body = client.request(API_HOST, "POST", "/api/register")
        if st != 200:
            raise RuntimeError(f"register failed HTTP {st}")
        resp = pb_fields(body)
        reg = pb_fields(resp.get(170, [("b", b"")])[0][1])
        tok = reg.get(1, [("b", b"")])[0][1]
        if not isinstance(tok, bytes):
            raise RuntimeError("no token in register response")
        self._token = tok.decode()
        # 更新 client 的鉴权头
        client.extra_headers["Access-Token"] = self._token

    def _api(self, client: HttpClient, path, params=None, method="GET"):
        """调 API；403(token 失效) 自动重新 register 重试一次。"""
        if not self._token:
            self._register(client)
        for attempt in range(2):
            st, body = client.request(API_HOST, method, path, params=params)
            if st == 403 and attempt == 0 and not self.browser_session:
                time.sleep(0.5)
                self._register(client)
                continue
            if st != 200:
                raise RuntimeError(f"{path} HTTP {st}")
            return body

    def _ensure_params(self, params):
        p = dict(params or {})
        p.setdefault("service_language", self.lang)
        return p

    # ---- 鉴权后 API（crawl 轨实现）----
    def list_titles(self, *, lang=None, **kw):
        client = self.ensure_client()
        body = self._api(client, "/api/manga_list", self._ensure_params({"service_language": lang or self.lang}))
        return [Title(source=self.name, id=str(t["id"]), name=t["name"], author=t["author"])
                for t in parse_manga_list(body)]

    def get_title(self, title_id, *, lang=None, quality=None, **kw):
        client = self.ensure_client()
        body = self._api(client, "/api/title_detail",
                         self._ensure_params({"original_title_id": title_id}))
        d = parse_title_detail(body)
        return Title(source=self.name, id=str(title_id), name=d["name"],
                     author=d["author"], cover_url=d["coverUrl"], description=d["description"])

    def get_chapters(self, title_id, *, lang=None, quality=None, **kw):
        client = self.ensure_client()
        body = self._api(client, "/api/chapter_list",
                         {"original_title_id": title_id, "translated_language": lang or self.lang,
                          "service_language": lang or self.lang})
        return [Chapter(id=str(c["id"]), number=c["number"], name=c["name"])
                for c in parse_chapter_list(body)]

    def get_pages(self, chapter, *, lang=None, quality=None, **kw):
        client = self.ensure_client()
        return self._viewer_pages(client, chapter, quality or "middle", lang or self.lang)

    def _viewer_pages(self, client, chapter, quality, lang):
        """拉 viewer，返回 [Page(url=...)]（AES 密钥缓存到 self._view_meta）。"""
        body = self._api(client, "/api/viewer",
                         {"translated_chapter_id": chapter.id, "quality": quality,
                          "service_language": lang})
        v = parse_viewer(body)
        mid = chapter.id
        self._view_meta[mid] = {"pages": v["pages"], "aesKey": v["aesKey"], "aesIv": v["aesIv"]}
        return [Page(url=u, ext="webp", mime="image/webp") for u in v["pages"]]

    def download_page(self, page, chapter, *, lang=None, quality=None, client=None, **kw):
        """下载单页 -> 用缓存密钥 AES 解密 -> 校验 RIFF 魔数返回字节。失败时重拉 viewer。"""
        if client is None:
            client = self.ensure_client()
        mid = chapter.id
        meta = self._view_meta.get(mid)
        if meta is None:
            # 首次无缓存：拉一次
            pages = self._viewer_pages(client, chapter, quality or "middle", lang or self.lang)
            page_index = pages.index(page) if page in pages else 0
        else:
            page_index = meta["pages"].index(page.url) if page.url in meta["pages"] else 0

        for _ in range(3):
            st, enc = client.request("img.mangamillion.shueisha.co.jp", "GET",
                                     _path_of(page.url), img=True)
            if st == 200 and len(enc) > 32:
                try:
                    pt = aes_decrypt(enc, meta["aesKey"], meta["aesIv"])
                except Exception:
                    st = 403
                else:
                    if pt[:4] == b"RIFF":
                        return pt
            time.sleep(0.6)
            # 签名过期：重拉 viewer
            pages = self._viewer_pages(client, chapter, quality or "middle", lang or self.lang)
            meta = self._view_meta[mid]
            if page_index < len(pages):
                page.url = pages[page_index].url
        return b""


def _path_of(url):
    from mmdl.core.http import split_url
    return split_url(url)[1]
