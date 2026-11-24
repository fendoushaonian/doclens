"""Word (.docx) 与纯文本解析。

docx 的重点是保留表格和文档结构（标题层级），而不是拍平成一段字符串。
python-docx 的 document.element.body 按文档真实顺序遍历段落和表格，
用它可以还原「表在哪段话之后」这个上下文——这对合同、报告类文档很关键。
"""

from __future__ import annotations

from pathlib import Path

from docx import Document as DocxDocument
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph

from .model import BlockKind, Cell, Document, Table, TextBlock, file_digest

# 纯文本单次读取的字符上限。超过就截断并告知，避免把上下文一次撑爆。
MAX_TEXT_CHARS = 400_000


def _iter_block_items(doc: DocxDocument):
    """按文档真实顺序产出段落和表格。

    python-docx 没有官方 API，只能走底层 XML。段落和表格的 tag 名不同，
    这里靠 cls 映射回高层对象。
    """
    from docx.oxml.ns import qn

    body = doc.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield DocxTable(child, doc)


def _para_kind(para: Paragraph) -> BlockKind:
    """根据样式名判断段落性质。样式名随语言变化，所以用前缀匹配。"""
    style = (para.style.name or "").lower()
    if style.startswith("heading") or style.startswith("标题"):
        return BlockKind.HEADING
    if style in {"caption", "题注"}:
        return BlockKind.CAPTION
    return BlockKind.PARAGRAPH


def parse_docx(path: str | Path) -> Document:
    path = Path(path)
    doc = DocxDocument(str(path))
    blocks: list[TextBlock] = []
    tables: list[Table] = []
    warnings: list[str] = []

    for item in _iter_block_items(doc):
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if text:
                blocks.append(TextBlock(text=text, page=1, kind=_para_kind(item)))
        else:
            rows: list[list[Cell]] = []
            for row in item.rows:
                cells = [Cell(text=p.text.strip()) for p in row.cells]
                if any(c.text for c in cells):
                    rows.append(cells)
            if len(rows) >= 2:
                # docx 表格没有页码概念，统一记第 1 页
                tables.append(
                    Table(
                        page_start=1,
                        page_end=1,
                        rows=rows,
                        header_rows=1,
                        caption="Word 表格",
                    )
                )

    metadata: dict = {}
    try:
        props = doc.core_properties
        for key in ("title", "author", "created", "modified", "subject"):
            value = getattr(props, key, None)
            if value:
                metadata[key] = str(value)
    except Exception:
        warnings.append("无法读取文档属性，不影响正文解析。")

    return Document(
        path=str(path),
        kind="docx",
        sha256=file_digest(path),
        page_count=1,
        blocks=blocks,
        tables=tables,
        metadata=metadata,
        warnings=warnings,
    )


def parse_text(path: str | Path) -> Document:
    """纯文本 / Markdown。按空行切段，Markdown 的 # 开头行识别为标题。"""
    path = Path(path)
    warnings: list[str] = []
    raw = ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            raw = path.read_text(encoding=encoding)
            if encoding != "utf-8-sig":
                warnings.append(f"该文件不是 UTF-8 编码，已按 {encoding} 读取。")
            break
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            raise RuntimeError(f"无法读取 {path.name}：{exc}") from exc

    if len(raw) > MAX_TEXT_CHARS:
        raw = raw[:MAX_TEXT_CHARS]
        warnings.append(
            f"文本长度超过 {MAX_TEXT_CHARS} 字符，只返回了前 {MAX_TEXT_CHARS} 字符。"
        )

    blocks: list[TextBlock] = []
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        if buffer:
            blocks.append(
                TextBlock(text="\n".join(buffer).strip(), page=1, kind=BlockKind.PARAGRAPH)
            )
            buffer = []

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if stripped.startswith("#"):
            flush()
            level_marks = len(stripped) - len(stripped.lstrip("#"))
            blocks.append(
                TextBlock(
                    text=stripped.lstrip("#").strip(),
                    page=1,
                    kind=BlockKind.HEADING,
                )
            )
            _ = level_marks  # 标题层级暂不入模型，保留原文更重要
            continue
        buffer.append(line.rstrip())
    flush()

    return Document(
        path=str(path),
        kind="text",
        sha256=file_digest(path),
        page_count=1,
        blocks=blocks,
        tables=[],
        metadata={"encoding": encoding},
        warnings=warnings,
    )
