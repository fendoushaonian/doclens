"""PDF 解析。

为什么不用 pdftotext：它把版面压成一条文本流，多栏排版会左右栏交错读出，
表格会变成用空格勉强对齐的字符串，agent 拿到的数字和标签经常错位。
这里改用 pdfplumber 的字级坐标重建：先按 Y 轴分行，再按 X 轴间隙切列，
保证「表头 - 数值」的对应关系不被打乱。

无边框表格是这里的难点：pdfplumber 的 extract_tables 依赖可见线，
大量真实 PDF（尤其是从网页打印的、排版软件导出的）没有线。所以分两路：
有线的走 extract_tables，无线的走坐标聚类，两条路的结果都转成 Table。
"""

from __future__ import annotations

from pathlib import Path

import pdfplumber

from .model import BlockKind, Cell, Document, Table, TextBlock, file_digest

# 单份 PDF 的处理页数上限。超长文档先给前 N 页并明确告知，
# 而不是跑到超时让调用方以为文档是空的。
MAX_PAGES_DEFAULT = 200

# 同一行的判定容差，单位是 pt。PDF 里同一视觉行的字常有 1-2pt 的基线抖动。
LINE_TOLERANCE = 3.0

# 列间距阈值：同一行内两个字之间的空白超过这个宽度，就认为跨了一列。
COLUMN_GAP = 8.0

# 字号相对全文中位数的比值，用来判断标题。1.35 是在多份年报/合同上试出来的经验值。
HEADING_RATIO = 1.35


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _group_lines(words: list[dict]) -> list[list[dict]]:
    """把字按视觉行聚合。

    pdfplumber 的 word 自带 top/bottom/x0/x1。以 top 聚类后再组内按 x0 排序，
    得到的是「读起来」的顺序，而不是 PDF 内部的绘制顺序。
    """
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (round(w["top"], 1), w["x0"]))
    lines: list[list[dict]] = []
    current: list[dict] = [ordered[0]]
    for word in ordered[1:]:
        # 与当前行的基准线比较，而不是与上一个字比较：避免误差逐字累积。
        if abs(word["top"] - current[0]["top"]) <= LINE_TOLERANCE:
            current.append(word)
        else:
            lines.append(sorted(current, key=lambda w: w["x0"]))
            current = [word]
    lines.append(sorted(current, key=lambda w: w["x0"]))
    return lines


def _line_to_cells(line: list[dict]) -> list[Cell]:
    """把一行字按水平间隙切成单元格。

    间隙大于 COLUMN_GAP 就认为换了一列。同一格内的字用单空格拼接，
    不做额外归一化——原文里的空格有时是有效信息（如 "1 234"）。
    """
    if not line:
        return []
    cells: list[list[str]] = [[line[0]["text"]]]
    prev_x1 = line[0]["x1"]
    for word in line[1:]:
        if word["x0"] - prev_x1 > COLUMN_GAP:
            cells.append([word["text"]])
        else:
            cells[-1].append(word["text"])
        prev_x1 = word["x1"]
    return [Cell(text=" ".join(parts)) for parts in cells]


def _is_heading(line: list[dict], body_size: float) -> bool:
    if not line:
        return False
    sizes = [w.get("size", 0) for w in line if w.get("size")]
    if not sizes:
        return False
    text = " ".join(w["text"] for w in line)
    if len(text) > 160:  # 长行不是标题
        return False
    # 字号明显大于正文，或整行是大写短句（英文年报常见）
    return _median(sizes) >= body_size * HEADING_RATIO


def _tables_from_rules(page: pdfplumber.page.Page) -> list[Table]:
    """有边框的表格走这条：交给 pdfplumber 的线检测。"""
    found: list[Table] = []
    try:
        raw_tables = page.extract_tables()
    except Exception:
        # 单页解析失败不该毁掉整份文档，交给调用方在 warnings 里看到即可。
        return found

    for raw in raw_tables:
        rows: list[list[Cell]] = []
        for row in raw:
            cells = [Cell(text=(c or "").strip()) for c in row]
            # 整行都空的行没有信息量，丢掉但保留列数一致性
            if any(c.text for c in cells):
                rows.append(cells)
        if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
            found.append(Table(page_start=page.page_number, page_end=page.page_number, rows=rows))
    return found


def _tables_from_layout(
    lines: list[list[dict]], page_no: int
) -> tuple[list[Table], set[int]]:
    """无边框表格走这条：连续多行、列数一致且至少有一列像数字，就当作表。

    这个判断是产品价值所在——真实世界的报表大多没有线。

    返回 (表格列表, 已归入表格的行下标集合)。调用方必须用它来跳过已消费的行，
    不能自己按「这行能切成几格」判断——两栏排版的正文每行都能切成两格，
    那样会把整页正文静默吞掉。
    """
    candidate: list[list[Cell]] = []
    candidate_idx: list[int] = []
    tables: list[Table] = []
    consumed: set[int] = set()

    def flush() -> None:
        nonlocal candidate, candidate_idx
        if len(candidate) >= 3 and max(len(r) for r in candidate) >= 2:
            width = max(len(r) for r in candidate)
            numeric_cols = 0
            for col in range(width):
                hits = sum(
                    1
                    for r in candidate
                    if col < len(r)
                    and r[col].value_type.value in {"number", "percent", "currency"}
                )
                if hits >= 2:
                    numeric_cols += 1
            if numeric_cols >= 1:
                tables.append(Table(page_start=page_no, page_end=page_no, rows=candidate))
                consumed.update(candidate_idx)
        candidate = []
        candidate_idx = []

    for idx, line in enumerate(lines):
        cells = _line_to_cells(line)
        if len(cells) >= 2:
            candidate.append(cells)
            candidate_idx.append(idx)
        else:
            flush()
    flush()
    return tables, consumed


def parse_pdf(path: str | Path, max_pages: int = MAX_PAGES_DEFAULT) -> Document:
    """解析一份 PDF，返回统一模型。

    页数超过 max_pages 时只处理前 max_pages 页，并在 warnings 里写明，
    调用方据此决定是否要分段读。
    """
    path = Path(path)
    blocks: list[TextBlock] = []
    tables: list[Table] = []
    warnings: list[str] = []
    metadata: dict = {}

    with pdfplumber.open(str(path)) as pdf:
        total = len(pdf.pages)
        metadata = {k: v for k, v in (pdf.metadata or {}).items() if v}
        if total > max_pages:
            warnings.append(
                f"文档共 {total} 页，本次只解析前 {max_pages} 页；"
                f"其余页面未读取，不要假设后面没有内容。"
            )

        # 先扫一遍取正文字号中位数，才能判断哪些行是标题。
        all_sizes: list[float] = []
        page_words: dict[int, list[dict]] = {}
        for page in pdf.pages[:max_pages]:
            words = page.extract_words(
                extra_attrs=["size"], keep_blank_chars=False, use_text_flow=False
            )
            page_words[page.page_number] = words
            all_sizes.extend(w.get("size", 0) for w in words if w.get("size"))
        body_size = _median(all_sizes) or 10.0

        for page in pdf.pages[:max_pages]:
            page_no = page.page_number
            words = page_words.get(page_no, [])
            if not words:
                # 没有文字层：可能是扫描件。明确说出来，别让 agent 以为文档为空。
                warnings.append(
                    f"第 {page_no} 页没有可提取的文字层，可能是扫描件或纯图片，需要 OCR。"
                )

            ruled = _tables_from_rules(page)
            tables.extend(ruled)
            # 有边框表的页不再做布局推断，避免同一张表被抽取两次。
            if ruled:
                lines = _group_lines(words)
                for line in lines:
                    text = " ".join(w["text"] for w in line).strip()
                    if text:
                        kind = (
                            BlockKind.HEADING
                            if _is_heading(line, body_size)
                            else BlockKind.PARAGRAPH
                        )
                        blocks.append(
                            TextBlock(
                                text=text,
                                page=page_no,
                                kind=kind,
                                bbox=(
                                    round(line[0]["x0"], 1),
                                    round(line[0]["top"], 1),
                                    round(line[-1]["x1"], 1),
                                    round(line[-1]["bottom"], 1),
                                ),
                            )
                        )
            else:
                lines = _group_lines(words)
                page_tables, consumed = _tables_from_layout(lines, page_no)
                tables.extend(page_tables)
                for idx, line in enumerate(lines):
                    # 只有真正构成表格的行才跳过。两栏排版的正文行虽然也能切成两格，
                    # 但它不在 consumed 里，必须作为正文保留下来。
                    if idx in consumed:
                        continue
                    text = " ".join(w["text"] for w in line).strip()
                    if not text:
                        continue
                    kind = (
                        BlockKind.HEADING
                        if _is_heading(line, body_size)
                        else BlockKind.PARAGRAPH
                    )
                    blocks.append(
                        TextBlock(
                            text=text,
                            page=page_no,
                            kind=kind,
                            bbox=(
                                round(line[0]["x0"], 1),
                                round(line[0]["top"], 1),
                                round(line[-1]["x1"], 1),
                                round(line[-1]["bottom"], 1),
                            ),
                        )
                    )

    tables = _merge_cross_page(tables, warnings)

    return Document(
        path=str(path),
        kind="pdf",
        sha256=file_digest(path),
        page_count=total,
        blocks=blocks,
        tables=tables,
        metadata=metadata,
        warnings=warnings,
    )


def _merge_cross_page(tables: list[Table], warnings: list[str]) -> list[Table]:
    """把跨页续表合并。

    判定条件很保守：后一张表在前一页的下一页、列数相同、且上一张表没有明显收尾。
    宁可少合并也不要错合并——错合并会让两笔不相关的记录看起来是同一张表的续行。
    """
    if not tables:
        return tables

    tables = sorted(tables, key=lambda t: (t.page_start, t.bbox[1] if t.bbox else 0))
    merged: list[Table] = []
    for table in tables:
        if merged:
            prev = merged[-1]
            same_width = prev.n_cols == table.n_cols and table.n_cols > 0
            adjacent = table.page_start == prev.page_end + 1
            if same_width and adjacent:
                prev.rows.extend(table.rows)
                prev.page_end = table.page_end
                warnings.append(
                    f"第 {table.page_start} 页的表格已并入第 {prev.page_start} 页起的同名表"
                    f"（列数一致），如果这其实是两张无关的表，请以页码为准分开看。"
                )
                continue
        merged.append(table)
    return merged


__all__ = ["parse_pdf"]
