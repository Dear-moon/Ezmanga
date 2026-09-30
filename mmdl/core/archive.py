"""Export each downloaded chapter as a ZIP or CBZ image archive."""
import tempfile
import zipfile
from pathlib import Path

from .epub import IMG_EXTS
from .naming import natural_sort_key


def build_archives(title_dir, extension="cbz"):
    if extension not in ("zip", "cbz"):
        raise ValueError("archive extension must be zip or cbz")
    title_dir = Path(title_dir)
    if not title_dir.is_dir():
        raise FileNotFoundError(f"title directory does not exist: {title_dir}")
    chapters = []
    for chapter_dir in sorted((p for p in title_dir.iterdir() if p.is_dir()),
                              key=lambda p: natural_sort_key(p.name)):
        pages = sorted((p for p in chapter_dir.iterdir()
                        if p.is_file() and p.suffix.lower() in IMG_EXTS),
                       key=lambda p: natural_sort_key(p.name))
        if pages:
            chapters.append((chapter_dir, pages))
    if not chapters:
        raise RuntimeError(f"no downloaded chapter images found in {title_dir}")
    archives = []
    for chapter_dir, pages in chapters:
        archive_path = title_dir / f"{chapter_dir.name}.{extension}"
        with tempfile.NamedTemporaryFile(dir=title_dir, prefix=".mmdl-", suffix=".tmp", delete=False) as temp:
            temp_path = Path(temp.name)
        try:
            # Already compressed images gain little from another compression pass.
            with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_STORED) as archive:
                for page in pages:
                    archive.write(page, arcname=page.name)
            temp_path.replace(archive_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        archives.append(archive_path)
    return archives
