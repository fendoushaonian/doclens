"""解析入口：按扩展名派发到具体解析器。

新增格式只在这里改一行，调用方无需知道有几种解析器。
"""

from __future__ import annotations

from pathlib import Path

from .docx import parse_docx, parse_text
from .excel import parse_excel
from .model import Document
from .pdf import parse_pdf

# 扩展名 → 解析函数。值是可以直接调用的 (path) -> Document。
_PARSERS = {
    ".pdf": parse_pdf,
    ".xlsx": parse_excel,
    ".xlsm": parse_excel,
    ".csv": parse_excel,
    ".tsv": parse_excel,
    ".docx": parse_docx,
    ".md": parse_text,
    ".markdown": parse_text,
    ".txt": parse_text,
    ".text": parse_text,
    ".log": parse_text,
    ".json": parse_text,
    ".yaml": parse_text,
    ".yml": parse_text,
}

SUPPORTED_EXTENSIONS = tuple(sorted(_PARSERS))


class UnsupportedFormat(ValueError):
    """扩展名不在支持列表里。带上支持列表，方便调用方直接修正调用。"""

    def __init__(self, suffix: str) -> None:
        self.suffix = suffix
        super().__init__(
            f"不支持的文件格式「{suffix or '(无扩展名)'}」。"
            f"当前支持：{', '.join(SUPPORTED_EXTENSIONS)}"
        )


def parse(path: str | Path) -> Document:
    """解析任意受支持的文档。

    文件不存在或格式不支持时抛 ValueError 子类，调用方据此给 agent 明确错误，
    而不是返回一个空文档让 agent 猜。
    """
    resolved = Path(path).expanduser()
    if not resolved.exists():
        raise FileNotFoundError(f"文件不存在：{resolved}")
    if not resolved.is_file():
        raise ValueError(f"不是文件：{resolved}")

    parser = _PARSERS.get(resolved.suffix.lower())
    if parser is None:
        raise UnsupportedFormat(resolved.suffix.lower())
    return parser(resolved)
