"""下载驱动层：统一遍历 title→chapters→pages，负责续传 / 进度 / 落盘 / 调 source。

crawl 轨的 download_title 与 capture 轨的 capture_url（延后）都复用 _save_chapter。
"""
import os
import time
import tempfile
from pathlib import Path

from .epub import build_epub
from .naming import clean_name, _num
from .resume import page_already_downloaded, chapter_already_downloaded


def _write_page(path, data):
    temporary = None
    try:
        # Interrupted writes must not become resumable pages.
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".mmdl-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_captured_chapter(ch_dir, pages):
    """直接把 capture 轨已提取好的 Page.data 逐页落盘（BW/B站）。返回落盘页数。

    BW 的 capture_from_url 已把每页字节装在 page.data 里，不再走 download_page。
    """
    ch_dir = Path(ch_dir)
    ch_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for pno, page in enumerate(pages, 1):
        if not page.data:
            continue
        ext = page.ext or "png"
        (ch_dir / f"{pno:03d}.{ext}").write_bytes(page.data)
        # 进度：仅打印每 10 页或首尾，避免刷屏
        if n % 10 == 0 or n < 2:
            print(f"    page {pno} ok ({len(page.data)} bytes)")
        n += 1
    return n


def _save_chapter(source, chapter, ch_dir, pages, *, lang=None, quality=None,
                  throttle=0.3, client=None):
    """逐页下载并落盘到 ch_dir。返回失败页数或 None。"""
    ch_dir = Path(ch_dir)
    ch_dir.mkdir(parents=True, exist_ok=True)
    fails = 0
    for pno, page in enumerate(pages, 1):
        ext = page.ext or "webp"
        fname = ch_dir / f"{pno:03d}.{ext}"
        if page_already_downloaded(fname):
            continue
        try:
            data = source.download_page(page, chapter, lang=lang, quality=quality, client=client)
        except Exception as e:
            print(f"    [fail] page {pno}: {e}")
            fails += 1
            continue
        if data:
            fname.write_bytes(data)
            print(f"    page {pno} ok ({len(data)} bytes)")
        else:
            fails += 1
        time.sleep(throttle)
    return fails


def write_capture(source, result, out_dir, *, epub=False, lang=None):
    """capture/book 轨落盘：直接写已提取的 Page.data（BookWalker/B站/Kobo 整本）。

    由 cli._write_capture 委托，GUI 也直接调这里，避免依赖 cli 的 argparse。
    """
    out_dir = Path(out_dir)
    title_dir = out_dir / (result.title.name or "captured")
    title_dir.mkdir(parents=True, exist_ok=True)
    print(f"[title] {result.title.name}")

    for idx, ch in enumerate(result.chapters, 1):
        ch_dir = title_dir / (ch.name or f"chapter_{idx:03d}")
        ch_dir.mkdir(parents=True, exist_ok=True)
        n = save_captured_chapter(ch_dir, ch.pages or [])
        print(f"  [{idx}/{len(result.chapters)}] {ch.name} ({n} pages)")

    if epub:
        epub_path = build_epub(title_dir, title=result.title.name,
                               author=result.title.author, language=lang or "en")
        print(f"[epub] {epub_path}")
    print("[done]")


def download_title(source, title_id, out_dir, *, lang=None, quality=None,
                   chapter_range=None, throttle=0.3, epub=False):
    """crawl 轨主入口：按 title_id 下载整部。返回 (title_dir, title)。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    title = source.get_title(title_id, lang=lang, quality=quality)
    title_dir = out_dir / clean_name(title.name or f"title_{title_id}")
    title_dir.mkdir(exist_ok=True)
    print(f"[title] {title.name} by {title.author or 'Unknown'}")

    chapters = source.get_chapters(title_id, lang=lang, quality=quality)
    if chapter_range:
        lo, hi = chapter_range
        chapters = [c for c in chapters if lo <= _num(c.number) <= hi]
    print(f"[chapters] {len(chapters)} to download")

    client = source.ensure_client()
    for idx, ch in enumerate(chapters, 1):
        ch_name = clean_name(f"{ch.number} {ch.name}".strip())
        ch_dir = title_dir / ch_name
        ch_dir.mkdir(parents=True, exist_ok=True)
        pages = source.get_pages(ch, lang=lang, quality=quality)
        if not pages:
            print(f"  [skip] {ch_name}: no pages")
            continue
        if chapter_already_downloaded(ch_dir, len(pages), ext=pages[0].ext or "webp"):
            print(f"  [skip] {ch_name}: already downloaded")
            continue
        print(f"  [{idx}/{len(chapters)}] {ch.number} ({len(pages)} pages)")
        for pno, page in enumerate(pages, 1):
            ext = page.ext or "webp"
            fname = ch_dir / f"{pno:03d}.{ext}"
            extensions = getattr(source, "page_extensions", (ext,))
            if any(page_already_downloaded(ch_dir / f"{pno:03d}.{candidate}") for candidate in extensions):
                continue
            data = source.download_page(page, ch, lang=lang, quality=quality, client=client)
            if data:
                fname = ch_dir / f"{pno:03d}.{page.ext or ext}"
                _write_page(fname, data)
            else:
                print(f"    [fail] page {pno}")
            time.sleep(throttle)

    if epub:
        epub_path = build_epub(
            title_dir,
            title=title.name or title_dir.name,
            author=title.author or "Unknown",
            language=lang or "en",
        )
        print(f"[epub] {epub_path}")
    return title_dir, title
