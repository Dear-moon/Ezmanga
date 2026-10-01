"""EPUB 打包（纯磁盘布局驱动，与站点无关）。"""
import html
import mimetypes
import time
import uuid
import zipfile
from pathlib import Path

from .naming import natural_sort_key

IMG_EXTS = {".webp", ".jpg", ".jpeg", ".png", ".gif"}


def _epub_href(value):
    import urllib.parse
    return urllib.parse.quote(value, safe="/._-")


def _media_type(path):
    if path.suffix.lower() == ".webp":
        return "image/webp"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _downloaded_chapters(title_dir):
    """扫描磁盘，返回 [{name, pages:[Path]}]。只收图片。"""
    title_dir = Path(title_dir)
    chapters = []
    for chapter_dir in sorted((p for p in title_dir.iterdir() if p.is_dir()),
                              key=lambda p: natural_sort_key(p.name)):
        pages = sorted(
            (p for p in chapter_dir.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXTS),
            key=lambda p: natural_sort_key(p.name),
        )
        if pages:
            chapters.append({"name": chapter_dir.name, "pages": pages})
    return chapters


def _missing_title_dir_message(title_dir):
    title_dir = Path(title_dir)
    message = f"title directory does not exist: {title_dir}"
    parent = title_dir.parent
    if parent.is_dir():
        candidates = sorted((p.name for p in parent.iterdir() if p.is_dir()), key=natural_sort_key)
        if candidates:
            message += "\nAvailable title directories:"
            message += "".join(f"\n  {parent / name}" for name in candidates)
    return message


def build_epub(title_dir, epub_path=None, title=None, author=None, language="en"):
    """把已下载的 title 目录打包成 EPUB 3。返回 epub 路径。"""
    title_dir = Path(title_dir)
    if not title_dir.is_dir():
        raise FileNotFoundError(_missing_title_dir_message(title_dir))

    chapters = _downloaded_chapters(title_dir)
    if not chapters:
        raise RuntimeError(f"no downloaded chapter images found in {title_dir}")

    title = title or title_dir.name
    author = author or "Unknown"
    language = language or "en"
    epub_path = Path(epub_path) if epub_path else title_dir.with_suffix(".epub")
    epub_path.parent.mkdir(parents=True, exist_ok=True)

    identifier = f"urn:uuid:{uuid.uuid4()}"
    manifest_items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    ]
    spine_items = []
    nav_items = []
    chapter_docs = []
    image_entries = []

    for chapter_index, chapter in enumerate(chapters, 1):
        chapter_id = f"chapter_{chapter_index:03d}"
        chapter_href = f"chapters/{chapter_id}.xhtml"
        manifest_items.append(
            f'<item id="{chapter_id}" href="{chapter_href}" media-type="application/xhtml+xml"/>'
        )
        spine_items.append(f'<itemref idref="{chapter_id}"/>')
        nav_items.append(
            f'<li><a href="{_epub_href(chapter_href)}">{html.escape(chapter["name"])}</a></li>'
        )

        image_tags = []
        for page_index, page in enumerate(chapter["pages"], 1):
            image_id = f"img_{chapter_index:03d}_{page_index:03d}"
            image_href = f"images/{chapter_id}/{page.name}"
            manifest_items.append(
                f'<item id="{image_id}" href="{_epub_href(image_href)}" media-type="{_media_type(page)}"/>'
            )
            image_entries.append((page, f"EPUB/{image_href}"))
            image_tags.append(
                f'<img src="../{_epub_href(image_href)}" alt="{html.escape(chapter["name"])} page {page_index}"/>'
            )

        chapter_docs.append((
            f"EPUB/{chapter_href}",
            f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{html.escape(language)}" lang="{html.escape(language)}">
<head>
  <title>{html.escape(chapter["name"])}</title>
  <style>
    body {{ margin: 0; padding: 0; background: #111; }}
    section {{ margin: 0 auto; max-width: 100%; }}
    img {{ display: block; width: 100%; height: auto; margin: 0 auto; }}
  </style>
</head>
<body>
  <section>
    <h1>{html.escape(chapter["name"])}</h1>
    {chr(10).join(image_tags)}
  </section>
</body>
</html>
''',
        ))

    package_doc = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{identifier}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:creator>{html.escape(author)}</dc:creator>
    <dc:language>{html.escape(language)}</dc:language>
    <meta property="dcterms:modified">{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}</meta>
  </metadata>
  <manifest>
    {chr(10).join(manifest_items)}
  </manifest>
  <spine>
    {chr(10).join(spine_items)}
  </spine>
</package>
'''
    nav_doc = f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{html.escape(language)}" lang="{html.escape(language)}">
<head>
  <title>{html.escape(title)}</title>
</head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>{html.escape(title)}</h1>
    <ol>
      {chr(10).join(nav_items)}
    </ol>
  </nav>
</body>
</html>
'''
    container_doc = '''<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
'''

    with zipfile.ZipFile(epub_path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container_doc, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("EPUB/package.opf", package_doc, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("EPUB/nav.xhtml", nav_doc, compress_type=zipfile.ZIP_DEFLATED)
        for chapter_name, chapter_doc in chapter_docs:
            archive.writestr(chapter_name, chapter_doc, compress_type=zipfile.ZIP_DEFLATED)
        for source_path, archive_name in image_entries:
            archive.write(source_path, archive_name, compress_type=zipfile.ZIP_DEFLATED)

    return epub_path
