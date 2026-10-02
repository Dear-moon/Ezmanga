"""Kobo manga source — `book` capability track: whole-book download → Obok decrypt → per-spine page extraction.

Unlike the other tracks it downloads the DRM'd fixed-layout .kepub, decrypts to plaintext epub,
then extracts page images; output is a CaptureResult (same shape as the BookWalker capture track),
persisted/packed by cli._write_capture.

Auth uses the `book`-only activation flow (kobo_api), requiring a one-time local debug-browser login;
download/decrypt/extract depend only on the .kepub itself. Cannot run in GitHub Actions (no local login state + needs real manga to calibrate endpoint params).
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
        self.adobe_account_dir = Path.home() / ".mmdl" / "adobe"   # anonymous (Adobe) activation state

    def http_config(self):
        raise NotImplementedError(f"{self.name} is a book source; HTTP handled by kobo_api.")

    def setup(self, cdp_url=None, **kw):
        """One-time activation: CDP login to Kobo → device registration → tokens saved to ~/.mmdl/kobo.json."""
        tokens = kobo_api.setup(cdp_url or self.cdp_url, path=self.cred_file)
        print(f"[kobo] activated: deviceId={tokens.get('deviceId', '')!r}")
        return tokens

    def adobe_setup(self, **kw):
        """Import the locally authorized ADE identity into ~/.mmdl/adobe (prerequisite for .acsm fulfillment).

        Uses adobe_import (registry + DPAPI + CPUID to recover master_key); only an ADE-verified binding
        passes Adobe signature checks. anonymous activation (adobe_auth.activate_anonymous) stalls at E_AUTH_USER_AUTH — not recommended.
        """
        from . import adobe_import
        info = adobe_import.import_ade_activation(str(self.adobe_account_dir))
        print(f"[kobo/adobe] imported user={info.get('user')!r} device={info.get('device')!r}")
        return info

    def get_book(self, book_id, *, lang=None, quality=None, **kw) -> CaptureResult:
        """Dual track: `.acsm` (Adobe ADE) → ADEPT fulfillment + decrypt; Kobo content-id (`.kepub`) → Obok.

        Dispatch by id suffix: `.acsm` goes to _get_acsm, everything else to _get_kepub by content-id.
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
