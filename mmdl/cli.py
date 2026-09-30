"""CLI 编排：--source 路由 + capability 门控 + 按 source 校验 lang/quality。"""
import argparse
import os
import sys
from pathlib import Path

from .sources import SOURCES, get_source
from .core.epub import build_epub
from .core.archive import build_archives
from .core.driver import download_title, write_capture


def build_parser():
    ap = argparse.ArgumentParser(
        prog="mmdl",
        description="MM-Downloader: 多 source 漫画下载器（个人离线阅读用）",
    )
    ap.add_argument("--source", default="mangamillion",
                    help="内容来源（默认 mangamillion；可用: " + ", ".join(SOURCES) + "）")
    ap.add_argument("--lang", default=None, help="语言代码，如 en / ja / zh-CN")
    ap.add_argument("--quality", default=None, help="图片质量档位（按 source 定义）")
    ap.add_argument("--list", action="store_true", help="列出该 source 的所有标题后退出")
    ap.add_argument("--title", help="标题 ID（按 source 语义，如 original_title_id）")
    ap.add_argument("--chapters", help="章节范围，如 '1-20'")
    ap.add_argument("--url", help="capture 模式 URL（如 BookWalker reader，延后）")
    ap.add_argument("--output", default=None, help="输出目录")
    ap.add_argument("--throttle", type=float, default=0.3, help="单页下载间隔秒数")
    ap.add_argument("--epub", action="store_true", help="下载后打包为 EPUB")
    ap.add_argument("--zip", action="store_true", help="下载后每章/卷打包为 ZIP")
    ap.add_argument("--cbz", action="store_true", help="下载后每章/卷打包为 CBZ")
    export_only = ap.add_mutually_exclusive_group()
    export_only.add_argument("--epub-only", help="只把已下载目录打包为 EPUB，不下载")
    export_only.add_argument("--zip-only", help="只把已下载漫画目录按章/卷打包为 ZIP，不下载")
    export_only.add_argument("--cbz-only", help="只把已下载漫画目录按章/卷打包为 CBZ，不下载")
    ap.add_argument("--token", help="source 的鉴权 token（如东立 Bearer 值）")
    ap.add_argument("--book-group", help="source 可选参数（如东立的 BookGroupID）")
    ap.add_argument("--setup", action="store_true",
                    help="一次性完成该 source 的账号激活/登录（如 --source kobo --setup）")
    ap.add_argument("--adobe-setup", action="store_true",
                    help="一次性 Adobe anonymous 激活（.acsm 兑现前置，仅 kobo）")
    return ap


def _validate(source, args):
    """按 source 校验 lang/quality 值域 + capability 门控。"""
    if source.lang_choices is not None and args.lang is not None:
        if args.lang not in source.lang_choices:
            raise SystemExit(f"[error] --lang 可选值: {', '.join(source.lang_choices)}")
    if source.quality_choices is not None and args.quality is not None:
        if args.quality not in source.quality_choices:
            raise SystemExit(f"[error] --quality 可选值: {', '.join(source.quality_choices)}")

    caps = source.capabilities
    if args.setup and not hasattr(source, "setup"):
        raise SystemExit(f"[error] {source.name} 不支持 --setup")
    if args.adobe_setup and not hasattr(source, "adobe_setup"):
        raise SystemExit(f"[error] {source.name} 不支持 --adobe-setup")
    if args.list and "list" not in caps:
        raise SystemExit(f"[error] {source.name} 不支持 --list")
    if args.url and "capture" not in caps:
        raise SystemExit(f"[error] {source.name} 不支持 --url（非 capture 源）")
    if args.title and "book" not in caps and "crawl" not in caps:
        raise SystemExit(f"[error] {source.name} 不支持 --title")
    if args.title and "book" in caps and (args.url or args.list):
        raise SystemExit("[error] book 源不能用 --url/--list（其只吃 --title）")
    if (args.epub_only or args.zip_only or args.cbz_only) and args.title:
        raise SystemExit("[error] 导出已有目录的参数与 --title 不能同时使用")
    if (args.zip_only or args.cbz_only) and (args.url or args.list or args.setup or args.adobe_setup):
        raise SystemExit("[error] --zip-only/--cbz-only 不能与下载、列表或账号配置参数同时使用")


def main(argv=None):
    args = build_parser().parse_args(argv)
    source = get_source(args.source)
    # 把 CLI 通用参数注入 source（东立等需要 token/book_group 的源）
    if args.token is not None and hasattr(source, "token"):
        source.token = args.token
        if source._client is not None:
            source._client.extra_headers["Authorization"] = f"bearer {args.token}"
    if args.book_group is not None and hasattr(source, "book_group"):
        source.book_group = args.book_group
    _validate(source, args)

    export_dir = args.epub_only or args.zip_only or args.cbz_only
    if export_dir:
        if args.epub_only or args.epub:
            epub_path = build_epub(export_dir, language=args.lang or "en")
            print(f"[epub] {epub_path}")
        _export_archives(export_dir, args)
        raise SystemExit(0)

    # --setup：一次性激活（如 Kobo）
    if args.setup:
        source.setup()
        print("[done]")
        return

    # --adobe-setup：Adobe anonymous 激活（kobo .acsm 兑现前置）
    if args.adobe_setup:
        source.adobe_setup()
        print("[done]")
        return

    out_dir = args.output or source.default_output

    # capture 轨（延后，BookWalker）
    if args.url:
        result = source.capture_from_url(args.url, lang=args.lang, quality=args.quality)
        _write_capture(source, result, out_dir, args)
        return

    # book 轨（Kobo）：整本下载+解密+抽页 → 落盘同 capture
    if args.title and "book" in source.capabilities:
        result = source.get_book(args.title, lang=args.lang, quality=args.quality)
        _write_capture(source, result, out_dir, args)
        return

    if args.list:
        titles = source.list_titles(lang=args.lang)
        print(f"[list] {len(titles)} titles:")
        for t in titles:
            print(f"  {t.id:>6}  {t.name}  ({t.author})")
        return

    if args.title:
        chap_range = None
        if args.chapters and "-" in args.chapters:
            a, b = args.chapters.split("-", 1)
            chap_range = (int(a), int(b))
        title_dir, title = download_title(
            source, args.title, out_dir,
            lang=args.lang, quality=args.quality,
            chapter_range=chap_range, throttle=args.throttle, epub=args.epub,
        )
        _export_archives(title_dir, args)
        print("[done]")
        return

    build_parser().print_help()


def _export_archives(title_dir, args):
    for extension, enabled in (("zip", args.zip or args.zip_only), ("cbz", args.cbz or args.cbz_only)):
        if enabled:
            for archive_path in build_archives(title_dir, extension=extension):
                print(f"[{extension}] {archive_path}")


def _write_capture(source, result, out_dir, args):
    """Write captured pages and requested export formats."""
    saved = write_capture(source, result, out_dir, epub=args.epub, lang=args.lang)
    _export_archives(Path(out_dir) / (result.title.name or "captured"), args)
    return saved


if __name__ == "__main__":
    main()
