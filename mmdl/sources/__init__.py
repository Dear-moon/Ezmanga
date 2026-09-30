"""source 注册表。新源在此登记，CLI 与测试都从 get_source 取。"""
from . import mangamillion
from . import tongli
from . import bookwalker
from . import bilibili
from . import kobo
from . import lightnovel

SOURCES = {
    "mangamillion": mangamillion.MCMillion,
    "tongli": tongli.Tongli,
    "bookwalker": bookwalker.BookWalker,
    "bilibili": bilibili.Bilibili,
    "kobo": kobo.Kobo,
    "lightnovel": lightnovel.Lightnovel,
}


def get_source(name: str):
    cls = SOURCES.get(name)
    if cls is None:
        raise ValueError(f"unknown source: {name!r}")
    return cls()
