"""kepub page extraction: decrypted epub container → page images in spine→XHTML→img order.

Platform-agnostic (shared by Kobo / Readmoo). Page order follows OPF spine itemref (Kobo image filename numbering ≠ page order,
worked around here); if the spine has `page-progression-direction="rtl"` or contains `primary-writing-mode:horizontal-rl`
the whole document is reversed. Two-page spreads (one XHTML with multiple <img>) are collected page by page; cover placeholders (svg/css and other non-raster) are filtered out.
"""
import gzip
import io
import posixpath
import zipfile
import xml.etree.ElementTree as ET

from mmdl.core.model import Title, Page

_LN = "http://www.w3.org/1999/xlink"


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _maybe_gunzip(data):
    if data[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(data)
        except OSError:
            return data
    return data


def _join(base, href):
    return posixpath.normpath(posixpath.join(base, href))


def _detect(data):
    """Detect raster format by magic bytes; returns (ext, mime), or ("", "") for non-raster."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return "", ""


def extract_pages(epub_bytes, *, source="kepub", book_id=None):
    """Unpack a decrypted epub and extract page images in spine→XHTML→img order.

    Returns (Title, list[Page]). Title.source comes from the caller (kobo/readmoo), id is book_id or title name.
    """
    with zipfile.ZipFile(io.BytesIO(epub_bytes)) as zp:
        names = set(zp.namelist())

        container = ET.fromstring(_read(zp, "META-INF/container.xml"))
        rootfile = None
        for rf in container.iter():
            if _local(rf.tag) == "rootfile":
                rootfile = rf.get("full-path")
                break
        if not rootfile or rootfile not in names:
            raise RuntimeError("kepub: container.xml 无有效 rootfile")

        opf_raw = _read(zp, rootfile)
        opf = ET.fromstring(opf_raw)
        opf_dir = posixpath.dirname(rootfile)

        title_name, author, spine_pd = "", "", "ltr"
        manifest, spine_ids, spine_el = {}, [], None
        for el in opf.iter():
            tag = _local(el.tag)
            if tag == "title" and not title_name:
                title_name = (el.text or "").strip()
            elif tag == "creator" and not author:
                author = (el.text or "").strip()
            elif tag == "item":
                manifest[el.get("id")] = el.get("href", "")
            elif tag == "spine":
                spine_el = el
            elif tag == "itemref":
                spine_ids.append(el.get("idref", ""))
        if spine_el is not None:
            spine_pd = spine_el.get("page-progression-direction") or "ltr"
        if spine_pd not in ("rtl", "r-l"):
            opf_text = opf_raw.decode("utf-8", "ignore")
            if "primary-writing-mode" in opf_text and ("horizontal-rl" in opf_text or "vertical-rl" in opf_text):
                spine_pd = "rtl"
        if spine_pd == "rtl":
            spine_ids = list(reversed(spine_ids))

        pages = []
        for idref in spine_ids:
            href = manifest.get(idref)
            if not href:
                continue
            spine_path = _join(opf_dir, href)
            xhtml_data = _read(zp, spine_path)
            if not xhtml_data:
                continue
            try:
                xroot = ET.fromstring(xhtml_data)
            except ET.ParseError:
                continue
            xhtml_dir = posixpath.dirname(spine_path)
            for img in xroot.iter():
                if _local(img.tag) != "img":
                    continue
                src = img.get("src") or img.get(f"{{{_LN}}}href")
                if not src:
                    continue
                data = _read(zp, _join(xhtml_dir, src))
                if not data:
                    continue
                ext, mime = _detect(data)
                if not ext:
                    continue  # skip non-raster such as svg/css placeholders
                pages.append(Page(data=data, ext=ext, mime=mime))

    if not title_name:
        title_name = book_id or source
    return Title(source=source, id=book_id or title_name, name=title_name, author=author), pages


def _read(zp, name):
    try:
        data = zp.read(name)
    except KeyError:
        return b""
    return _maybe_gunzip(data)
