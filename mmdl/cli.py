"""CLI 编排：--source 路由 + capability 门控 + 按 source 校验 lang/quality。"""
import argparse
from decimal import Decimal
import json
import os
import re
import sys
from pathlib import Path

from .sources import SOURCES, get_source
from .core.epub import build_epub
from .core.archive import build_archives
from .core.driver import download_title, write_capture


def _parse_chapter_range(value):
    number = r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)"
    match = re.fullmatch(r"\s*(" + number + r")(?:\s*-\s*(" + number + r"))?\s*", value)
    if match is None:
        raise argparse.ArgumentTypeError("章节范围应为 1、1-20 或 10.5-12.5")
    start = Decimal(match.group(1))
    end = Decimal(match.group(2)) if match.group(2) is not None else start
    if start > end:
        raise argparse.ArgumentTypeError("章节范围起点不能大于终点")
    return start, end


def build_parser():
    ap = argparse.ArgumentParser(
        prog="mmdl",
        description="MM-Downloader: 多 source 漫画下载器（个人离线阅读用）",
    )
    ap.add_argument("--source", default="mangamillion",
                    help="内容来源（默认 mangamillion；可用: " + ", ".join(SOURCES) + "）")
    ap.add_argument("--lang", default=None, help="语言代码，如 en / ja / zh-CN")
    ap.add_argument("--quality", default=None, help="图片质量档位（按 source 定义）")
    ap.add_argument("--list", action="store_true", help="列出标题后退出")
    ap.add_argument("--title", help="标题 ID（按 source 语义，如 original_title_id）")
    ap.add_argument("--chapters", type=_parse_chapter_range, help="单章或章节范围，如 1 / 1-20 / 10.5-12.5")
    ap.add_argument("--url", help="阅读器 URL（如 BookWalker reader）")
    ap.add_argument("--bw-mode", choices=("native", "canvas"), help="BookWalker 下载方式（默认 native 本地还原；canvas 提取已绘制页面）")
    ap.add_argument("--bw-port", type=int, help="BW 辅助扩展连接端口（默认 19225）")
    ap.add_argument("--bw-serve", action="store_true", help="启动 BW 本机下载服务，供浏览器扩展一键下载")
    ap.add_argument("--bili-mode", choices=("http", "canvas"), help="B 漫下载方式（默认 http；canvas 使用调试浏览器）")
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
    ap.add_argument("--source-preferences", action="store_true", help="查看扩展设置项（不显示凭据值）")
    ap.add_argument("--preferences-file", help="从私有 JSON 文件导入所选扩展源的设置")
    ap.add_argument("--source-filters", action="store_true", help="查看扩展搜索筛选器及位置路径")
    ap.add_argument("--filters-file", help="搜索筛选器 JSON 文件（配合 --search，可传空查询）")
    ap.add_argument("--extension", help="Keiyoushi 扩展包名或简称")
    ap.add_argument("--extension-source", help="Keiyoushi 扩展内源 ID（用于多源扩展）")
    ap.add_argument("--extensions", action="store_true", help="列出可用的 Keiyoushi 扩展")
    ap.add_argument("--installed-extensions", action="store_true", help="列出本机已安装的 Keiyoushi 扩展")
    ap.add_argument("--extension-sources", action="store_true", help="列出所选扩展内的源")
    ap.add_argument("--install-extension", help="安装一个官方 Keiyoushi JAR 扩展")
    ap.add_argument("--update-extensions", action="store_true", help="更新已安装扩展（可用 --extension 限定一个扩展）")
    ap.add_argument("--search", help="搜索漫画（配合 --extensions 时筛选扩展）")
    ap.add_argument("--page", type=int, default=1, help="Keiyoushi 搜索页码（默认 1）")
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

    extension_options = (args.extension or args.extension_source or args.extensions or args.installed_extensions or args.extension_sources
                         or args.install_extension or args.update_extensions or args.search is not None or args.page != 1
                         or args.source_preferences or args.preferences_file or args.source_filters or args.filters_file)
    if extension_options and source.name != "keiyoushi":
        raise SystemExit("[error] 扩展/搜索参数需要 --source keiyoushi")
    if args.page < 1:
        raise SystemExit("[error] --page 必须大于 0")
    management = (args.extensions or args.installed_extensions or args.extension_sources or args.install_extension or args.update_extensions
                  or args.source_preferences or args.source_filters)
    if sum(bool(value) for value in (args.extensions, args.installed_extensions, args.extension_sources, args.install_extension, args.update_extensions, args.source_preferences, args.source_filters)) > 1:
        raise SystemExit("[error] 每次请选择一个扩展管理操作")
    if management and (args.title or args.url or args.list or args.setup or args.epub_only or args.zip_only or args.cbz_only):
        raise SystemExit("[error] 扩展管理不能与下载、初始化或离线导出同时使用")
    if args.search is not None and not (args.extensions or args.installed_extensions) and (args.title or args.url or args.list or args.setup or management
                                              or args.epub_only or args.zip_only or args.cbz_only):
        raise SystemExit("[error] 搜索不能与其他操作同时使用")

    if args.filters_file and args.search is None:
        raise SystemExit("[error] --filters-file 需要 --search（可传空查询）")
    if args.preferences_file and (args.setup or args.extensions or args.installed_extensions or args.install_extension or args.update_extensions
                                 or args.epub_only or args.zip_only or args.cbz_only):
        raise SystemExit("[error] 设置导入不能与安装、初始化或离线导出同时使用")

    if (args.bw_mode is not None or args.bw_port is not None or args.bw_serve) and source.name != "bookwalker":
        raise SystemExit("[error] --bw-mode/--bw-port/--bw-serve 需要 --source bookwalker")
    if args.bw_serve and (args.setup or args.url or args.title or args.list or args.epub_only or args.zip_only or args.cbz_only or args.bw_mode == "canvas"):
        raise SystemExit("[error] --bw-serve 不能与下载、初始化、离线导出或 canvas 模式同时使用")
    if args.bili_mode is not None and source.name != "bilibili":
        raise SystemExit("[error] --bili-mode 需要 --source bilibili")
    if source.name == "bilibili" and source.bili_mode == "canvas" and args.title:
        raise SystemExit("[error] B 漫 canvas 模式需要 --url")

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
    try:
        return _run(source, args)
    finally:
        if hasattr(source, "close"):
            source.close()


def _run(source, args):
    if source.name == "bilibili":
        source.throttle = args.throttle
        if args.bili_mode is not None:
            source.bili_mode = args.bili_mode
    if source.name == "bookwalker":
        source.throttle = args.throttle
        if args.bw_mode is not None:
            source.bw_mode = args.bw_mode
        if args.bw_port is not None:
            source.bw_port = args.bw_port
    if source.name == "keiyoushi":
        source.extension = args.extension
        source.extension_source = args.extension_source
        source.page = args.page
    # 把 CLI 通用参数注入 source（东立等需要 token/book_group 的源）
    if args.token is not None and hasattr(source, "token"):
        source.token = args.token
        if source._client is not None:
            source._client.extra_headers["Authorization"] = f"bearer {args.token}"
    if args.book_group is not None and hasattr(source, "book_group"):
        source.book_group = args.book_group
    _validate(source, args)

    if source.name == "keiyoushi":
        from .sources.keiyoushi_client import load_filters, load_preferences
        if args.filters_file:
            source.search_filters = load_filters(args.filters_file)
        if args.preferences_file:
            source.set_preferences(load_preferences(args.preferences_file), lang=args.lang)
            print("[preferences] 已保存源设置")
            if not (args.title or args.url or args.list or args.search is not None
                    or args.source_preferences or args.source_filters or args.extension_sources):
                return

    if source.name == "keiyoushi" and _manage_extensions(source, args):
        return

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

    if args.bw_serve:
        from .sources.bookwalker_browser import serve_downloads
        formats = tuple(extension for extension, enabled in (("zip", args.zip), ("cbz", args.cbz)) if enabled)
        serve_downloads(source, out_dir, epub=args.epub, archive_formats=formats or ("cbz",))
        return

    # capture 轨（延后，BookWalker）
    if args.url and not (source.name == "bilibili" and source.bili_mode == "http"):
        result = source.capture_from_url(args.url, lang=args.lang, quality=args.quality)
        _write_capture(source, result, out_dir, args)
        return

    # book 轨（Kobo）：整本下载+解密+抽页 → 落盘同 capture
    if args.title and "book" in source.capabilities:
        result = source.get_book(args.title, lang=args.lang, quality=args.quality)
        _write_capture(source, result, out_dir, args)
        return

    if args.list or args.search is not None:
        titles = source.list_titles(lang=args.lang, **({"query": args.search} if args.search is not None else {}))
        print(f"[list] {len(titles)} titles:")
        for t in titles:
            print(f"  {t.id:>6}  {t.name}  ({t.author})")
        return

    if args.title or (args.url and source.name == "bilibili" and source.bili_mode == "http"):
        title_dir, title = download_title(
            source, args.title or args.url, out_dir,
            lang=args.lang, quality=args.quality,
            chapter_range=args.chapters, throttle=args.throttle, epub=args.epub,
        )
        _export_archives(title_dir, args)
        print("[done]")
        return

    build_parser().print_help()


def _manage_extensions(source, args):
    from .sources.keiyoushi_client import catalog, install_extension, installed_extensions, resolve_extension

    if args.source_preferences or args.source_filters:
        values = source.source_preferences(lang=args.lang) if args.source_preferences else source.source_filters(lang=args.lang)
        print(json.dumps(values, ensure_ascii=False, indent=2))
        return True
    if args.extension_sources:
        for item in source.extension_sources():
            print(f"{item['id']}  {item['lang']}  {item['name']}")
        return True
    if args.extensions or args.installed_extensions:
        entries = installed_extensions() if args.installed_extensions else catalog()
        if not entries:
            print("[extensions] No installed extensions")
        for entry in entries:
            languages = sorted({item["language"] for item in entry["sources"]})
            if args.lang and args.lang not in languages:
                continue
            if args.search and args.search.casefold() not in (entry["name"] + entry["packageName"]).casefold():
                continue
            print(f"{entry['packageName']}  {entry['versionName']}  {entry['name']}  ({', '.join(languages)})")
        return True
    if args.install_extension:
        install_extension(resolve_extension(args.install_extension, catalog()))
        return True
    if args.update_extensions:
        installed = installed_extensions()
        if args.extension:
            installed = [resolve_extension(args.extension, installed)]
        if not installed:
            print("[extensions] No installed extensions")
            return True
        available = catalog()
        for entry in installed:
            install_extension(resolve_extension(entry["packageName"], available))
        return True
    return False


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
