"""文件名规范化工具。"""
from decimal import Decimal
import re


def clean_name(s):
    """把非法文件名字符替换为空格，收尾去空白，空则 'unknown'。"""
    s = re.sub(r'[\\/:*?"<>|\r\n]', " ", s)
    return re.sub(r"\s+", " ", s).strip() or "unknown"


def natural_sort_key(value):
    """自然排序 key（数字段按数值，字母段按小写）。"""
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(value))]


def _num(s):
    """Extract a chapter number without truncating fractional chapters."""
    try:
        value = Decimal(str(s).strip())
        if value.is_finite():
            return value
    except ArithmeticError:
        pass
    match = re.search(r"[0-9]+(?:\.[0-9]+)?", str(s))
    return Decimal(match.group()) if match else Decimal(0)
