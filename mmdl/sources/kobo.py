"""Kobo 漫画源 —— `book` 能力轨：整本下载 → Obok 解密 → 按 spine 抽页图。

与前两轨不同，它不逐页走 HTTP 或浏览器 canvas，而是把整本带 DRM 的固定版式 .kepub
下载下来，解密成明文 epub，再抽出页图。产物是 CaptureResult（与 BookWalker capture 轨同构），
由 cli._write_capture 统一落盘 + 打包。

鉴权用 `book` 源专用的激活流程（kobo_api），需要本地 debug 浏览器一次性登录；下载/解密/抽页
其余部分只依赖 .kepub 本身。进不了 GitHub Actions（无本地登录态 + 需真实漫画校验端点参数）。
"""
from pathlib import Path

from mmdl.core.model import Title, Chapter, CaptureResult
from mmdl.core.cdp import DEFAULT_CDP_URL
from .base import BaseSource
from . import kobo_api
from . import kobo_drm
from . import kobo_acsm
from . import adept_drm
from .page_extract import extract_pages


class Kobo(BaseSource):
    name = "kobo"
    display_name = "Kobo"
    lang_choices = None
    quality_choices = None
    capabilities = frozenset({"book"})

    def __init__(self, throttle: float = 0.0, lang: str = "en",
                 cdp_url: str = DEFAULT_CDP_URL, cred_file=None):
        super().__init__(throttle=throttle, lang=lang)
        self.cdp_url = cdp_url
        self.cred_file = cred_file or kobo_api.CRED_FILE
        self.adobe_account_dir = Path.home() / ".mmdl" / "adobe"   # anonymous(Adobe) 激活态

    def http_config(self):
        raise NotImplementedError(f"{self.name} is a book source; HTTP handled by kobo_api.")

    def setup(self, cdp_url=None, **kw):
        """一次性激活：CDP 登录 Kobo → 设备注册 → 存 token 到 ~/.mmdl/kobo.json。"""
        tokens = kobo_api.setup(cdp_url or self.cdp_url, path=self.cred_file)
        print(f"[kobo] activated: deviceId={tokens.get('deviceId', '')!r}")
        return tokens

    def adobe_setup(self, **kw):
        """导入本机已授权的 ADE 身份到 ~/.mmdl/adobe（.acsm 兑现前置）。

        走 adobe_import（注册表+DPAPI+CPUID 解 master_key）；ADE 已验证的绑定才能过 Adobe 签名。
        anonymous 激活(adobe_auth.activate_anonymous)会卡 E_AUTH_USER_AUTH，不推荐。
        """
        from . import adobe_import
        info = adobe_import.import_ade_activation(str(self.adobe_account_dir))
        print(f"[kobo/adobe] imported user={info.get('user')!r} device={info.get('device')!r}")
        return info

    def get_book(self, book_id, *, lang=None, quality=None, **kw) -> CaptureResult:
        """双轨：`.acsm`（Adobe ADE）→ ADEPT 兑现+解密；Kobo content-id（`.kepub`）→ Obok。

        按 id 后缀分流：`.acsm` 走 _get_acsm，其余按 content-id 走 _get_kepub。
        """
        if str(book_id).lower().endswith(".acsm"):
            return self._get_acsm(book_id)
        return self._get_kepub(book_id, lang=lang, quality=quality)

    def _get_acsm(self, acsm_path):
        acsm_bytes = Path(acsm_path).read_bytes()
        try:
            epub_bytes, meta, res_id = kobo_acsm.fulfill_acsm(self.adobe_account_dir, acsm_bytes)
            user_key = kobo_acsm.export_user_key(self.adobe_account_dir)
            plain = adept_drm.decrypt_epub(epub_bytes, user_key)
        except Exception as e:
            raise RuntimeError(
                f"kobo .acsm 兑现失败（需先 ADE 激活，`--adobe-setup` 导入到 {self.adobe_account_dir}）：{e}")
        book_id = meta.get("resource") or res_id or acsm_path
        title, pages = extract_pages(plain, source=self.name, book_id=book_id)
        chap = Chapter(id=book_id, number=str(len(pages)), name="", pages=pages)
        return CaptureResult(title=title, chapters=[chap])

    def _get_kepub(self, book_id, *, lang=None, quality=None) -> CaptureResult:
        tokens = kobo_api.ensure_tokens(path=self.cred_file)
        kepub_bytes, content_keys = kobo_api.download_book(book_id, tokens=tokens, path=self.cred_file)
        device_id = kobo_drm.device_id(tokens.get("serial", ""), tokens.get("hashKey", kobo_drm.HASH_KEYS[0]))
        plain = kobo_drm.decrypt_kepub(kepub_bytes, device_id, tokens.get("userId", ""), content_keys)
        title, pages = extract_pages(plain, source=self.name, book_id=book_id)
        chap = Chapter(id=book_id, number=str(len(pages)), name="", pages=pages)
        return CaptureResult(title=title, chapters=[chap])
