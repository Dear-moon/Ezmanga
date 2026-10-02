"""BookWalker Japan: native Publus image restoration with optional Canvas capture."""
from mmdl.core.model import Title, Chapter, Page, CaptureResult
from .base import BaseSource


class BookWalker(BaseSource):
    name = "bookwalker"
    display_name = "BookWalker"
    lang_choices = None
    quality_choices = None
    capabilities = frozenset({"capture"})

    def __init__(self, throttle=0.0, lang="ja", cdp_url="http://127.0.0.1:9222"):
        super().__init__(throttle=throttle, lang=lang)
        self.cdp_url = cdp_url
        self.bw_mode = "native"
        self.bw_port = 19225

    def setup(self):
        from .bookwalker_browser import register_extension
        register_extension(input("Ezmanga BW 扩展 ID：").strip())

    def http_config(self):
        raise NotImplementedError("BookWalker is browser-based; no HTTP config.")

    def _cdp(self):
        from mmdl.core.cdp import CdpClient
        return CdpClient(cdp_url=self.cdp_url, target_url_substr="bookwalker")

    # ---- 翻页 API 定位（对象名随版本变化，扫 NFBR 找可用菜单位） ----
    def _menu(self, client):
        """返回可用的 BW 菜单位（含 options.a6l 翻页函数）。"""
        val = client.eval(r"""(()=>{
          const V=window.NFBR&&window.NFBR.a6G&&window.NFBR.a6G.Initializer;
          if(!V) return null;
          for(const k of Object.keys(V)){
            const m=V[k]&&V[k].menu;
            if(m && m.options && m.options.a6l && typeof m.options.a6l.moveToNext==='function')
              return 'NFBR.a6G.Initializer.'+k+'.menu';
          }
          return null;
        })()""")
        return val

    def _move_to(self, client, menu_path, page_n):
        """跳转到第 page_n 页（1-based）。menu_path 形如 'NFBR.a6G.Initializer.T1V.menu'。"""
        client.eval(f"(()=>{{const m={menu_path}; if(m&&m.options&&m.options.a6l&&m.options.a6l.moveToPage) m.options.a6l.moveToPage({page_n}); return 1}})()")

    # ---- capture 轨 ----
    def capture_from_url(self, url, *, lang=None, quality=None, **kw):
        if self.bw_mode == "canvas":
            return self._capture_canvas(url, lang=lang, quality=quality, **kw)
        bridge = kw.get("browser_session")
        if bridge is None:
            raise RuntimeError("Start --source bookwalker --bw-serve, then click Start download in the Ezmanga BW extension")
        return self._capture_native(url, bridge)

    def _capture_native(self, url, bridge):
        import time
        from http.cookiejar import Cookie
        from http.cookies import SimpleCookie
        from urllib.parse import parse_qs, urlsplit
        from urllib.request import Request

        from curl_cffi import requests
        from .bookwalker_publus import decode_config, image_filename, restore_image

        address = urlsplit(url)
        viewers = {"viewer.bookwalker.jp", "viewer-trial.bookwalker.jp", "viewer-df.bookwalker.jp"}
        if address.scheme != "https" or address.hostname not in viewers:
            raise ValueError("Native BW requires a Japanese BookWalker reader URL; use --bw-mode canvas for other readers")
        with bridge:
            snapshot = bridge.session()
            user_agent = snapshot["userAgent"]
            with requests.Session(impersonate="chrome", headers={"User-Agent": user_agent, "Referer": url}, timeout=60) as session:
                def import_cookies():
                    for item in snapshot["cookies"]:
                        domain = item["domain"]
                        expires = item.get("expires", -1)
                        session.cookies.jar.set_cookie(Cookie(
                            version=0, name=item["name"], value=item["value"], port=None, port_specified=False,
                            domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
                            path=item["path"], path_specified=True, secure=item["secure"],
                            expires=int(expires) if expires > 0 else None, discard=expires <= 0,
                            comment=None, comment_url=None, rest={},
                        ))

                def get(target_url, params=None):
                    for attempt in range(3):
                        try:
                            response = session.get(target_url, params=params)
                            break
                        except requests.RequestsError as error:
                            if error.code != 35:
                                raise
                            host = urlsplit(target_url).hostname
                            if attempt == 2:
                                raise RuntimeError(f"BW TLS handshake failed ({host}, curl 35); try again") from error
                            print(f"[bw] TLS handshake failed ({host}); retry {attempt + 1}/2")
                            time.sleep(attempt + 1)
                    if response.status_code != 200:
                        raise RuntimeError(f"BW request failed (HTTP {response.status_code}); refresh the browser reading session")
                    return response

                import_cookies()
                reader = get(url)
                address = urlsplit(reader.url)
                if address.hostname not in viewers:
                    raise RuntimeError("BW reader redirected to login; log in and open the reader again")
                query = parse_qs(address.query)
                cid = query["cid"][0]
                if "cty" in query and query["cty"][0] not in ("1", "2"):
                    raise ValueError("Native BW supports comics, not novels")
                trial = address.hostname == "viewer-trial.bookwalker.jp"
                rental = address.hostname == "viewer-df.bookwalker.jp"
                origin = f"https://{address.hostname}"
                if trial:
                    content_api = origin + "/trial-page/c"
                elif rental:
                    content_api = origin + "/browserWebApi4/c"
                else:
                    content_api = origin + "/browserWebApi/c"
                parameters = {"cid": cid, "BID": "0"}

                def content():
                    request = Request(content_api)
                    session.cookies.jar.add_cookie_header(request)
                    cookies = SimpleCookie(request.get_header("Cookie", ""))
                    query = dict(parameters)
                    if not trial and snapshot["cr"] is not None:
                        query["cr"] = snapshot["cr"]
                    for name in ("u1", "u2"):
                        if name in cookies:
                            query[name] = cookies[name].value
                    result = get(content_api, query).json()
                    if not result.get("url"):
                        raise RuntimeError("BW content is unavailable; log in and open a purchased, rented or trial volume")
                    if result.get("cty") not in (1, 2):
                        raise ValueError("Native BW supports comics, not novels")
                    info = result.get("auth_info") or {}
                    fields = ("pfCd", "Policy", "Signature", "Key-Pair-Id")
                    if not trial:
                        fields += ("hti", "cfg", "uuid")
                    auth = {key: info[key] for key in fields if info.get(key) is not None}
                    if not trial and info:
                        auth["BID"] = "0"
                    return result["url"].rstrip("/"), auth

                base, auth = content()
                renewed = time.monotonic()
                root = get(base + "/configuration_pack.json", auth).json()
                config, keys = decode_config(root)
                entries = sorted(config["configuration"]["contents"], key=lambda item: item["index"])
                pages = []
                for index, entry in enumerate(entries, 1):
                    # Purchased-book signatures expire after 60 seconds.
                    if not trial and time.monotonic() - renewed >= 45:
                        snapshot = bridge.session()
                        import_cookies()
                        base, auth = content()
                        renewed = time.monotonic()
                    page_id = entry["file"]
                    detail = config[page_id]["FileLinkInfo"]["PageLinkInfoList"][0]["Page"]
                    image_url = base + "/" + image_filename(page_id, keys, detail.get("No", 0))
                    data = restore_image(get(image_url, auth).content, page_id, detail, keys)
                    pages.append(Page(data=data, ext="jpg", mime="image/jpeg"))
                    print(f"[bw] page {index}/{len(entries)} restored")
                    if self.throttle:
                        time.sleep(self.throttle)
                name = self._guess_name(snapshot["title"])
                return CaptureResult(
                    title=Title(source=self.name, id=url, name=name),
                    chapters=[Chapter(id=url, number="1", name="", pages=pages)],
                )

    def _capture_canvas(self, url, *, lang=None, quality=None, **kw):
        client = self._cdp()
        try:
            client.connect()
            menu = self._menu(client)
            if not menu:
                raise RuntimeError("BookWalker reader API not found (NFBR.a6G.Initializer.*.menu)")
            title_name = self._guess_name(client.eval("document.title || ''"))
            total = self._total_pages(client)
            pages = self._gather_pages(client, menu, total)
            # Chapter 名不设书名(避免目录嵌套为 书/书/); 让 _write_capture fallback 到 chapter_001
            chap = Chapter(id=url, number=str(len(pages)), name="", pages=pages)
            return CaptureResult(
                title=Title(source=self.name, id=url, name=title_name or "BookWalker"),
                chapters=[chap],
            )
        finally:
            client.close()

    def _guess_name(self, val):
        if val:
            for sep in (" - ", " -", " | "):
                if sep in val:
                    return val.split(sep)[0].strip()
        return val or "BookWalker"

    def _total_pages(self, client):
        """从页面 `N/169` 读总页数。"""
        import re
        val = client.eval("(document.body.innerText||'').match(/\\d+\\s*\\/\\s*(\\d+)/)?document.body.innerText.match(/\\d+\\s*\\/\\s*(\\d+)/)[1]:''")
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0

    def _gather_pages(self, client, menu, total=0):
        """整章遍历: moveToPage(k) 逐跨页提取正文 canvas, 去重相邻重叠。

        BW 页码 0-based(显示 N/169 时 moveToPage(N-1) 在当前页), 每页有 2 个 1289×1398
        正文 canvas + 1 个封面占位(大而内容平). 翻页时相邻跨页共享边界页, 按 hash 去重.
        若 total 未知, 则循环直到页码不再前进(到最后一页).
        """
        import hashlib
        import time

        def h(data):
            return hashlib.sha256(data).hexdigest()[:16]

        pages = []
        seen = set()
        last_page = client.eval("document.body.innerText.match(/\\d+\\s*\\/\\s*(\\d+)/)?document.body.innerText.match(/\\d+\\s*\\/\\s*(\\d+)/)[1]:''")
        try:
            last_page = int(last_page)
        except (TypeError, ValueError):
            last_page = 0
        if last_page:
            total = last_page  # 兜底: 用页面读到的总页数

        # 从第 0 页开始(0-based)
        for idx in range(total):
            self._move_to(client, menu, idx)
            time.sleep(1.2)   # 等 canvas 绘制
            for data, w, hh, i in client.extract_canvas_png():
                # 只收正文页 canvas(近似等宽的漫画页)。BW 正文多是非 1:1 的竖版(如 1289×1398),
                # 封面/占位通常是不同尺寸(如 1350×1920)或超小。用宽高落在漫画页尺寸带过滤。
                if w < 500 or hh < 500:
                    continue   # 过小图标
                if w > 1350 or hh > 1500:
                    continue   # 封面/大占位(约 1350×1920)
                key = h(data)
                if key in seen:
                    continue
                seen.add(key)
                pages.append(Page(data=data, ext="png", mime="image/png"))
        return pages
