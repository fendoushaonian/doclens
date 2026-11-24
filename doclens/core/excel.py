"""Excel / CSV 解析。

核心约束：公式不能丢，也不能只给结果。agent 经常需要知道「这个数是怎么算出来的」，
因为财务模型的错误往往在公式里，而不在显示值里。openpyxl 的 data_only=False 拿到公式，
data_only=True 拿到缓存结果，这里两份都保留。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from .model import BlockKind, Cell, Document, Table, TextBlock, ValueType, file_digest

# 单个 sheet 的行上限。超大的导出表先截断并告知，避免把整个 agent 上下文塞满。
MAX_ROWS_DEFAULT = 5000

# 连续多少个空行算作一张表的结束。报表里常有多张表上下堆叠。
BLANK_ROW_BREAK = 2


def _cell_from_value(value: Any) -> Cell:
    """把 openpyxl 的值转成 Cell。

    openpyxl 给出的是原生类型：int/float/datetime/str。float 在这里直接 str() 会变成
    '1234.5600000000001' 这类噪声，所以整数型 float 先转 int，其余按 repr 去尾零。
    但不论怎么转，都只是展示层处理，真正的精度由原始 bytes 决定。
    """
    if value is None:
        return Cell(text="", value_type=ValueType.EMPTY)
    if isinstance(value, bool):
        return Cell(text="TRUE" if value else "FALSE", value_type=ValueType.TEXT)
    if isinstance(value, int):
        return Cell(text=str(value), value_type=ValueType.NUMBER, numeric=str(value))
    if isinstance(value, float):
        if value.is_integer():
            text = str(int(value))
        else:
            # repr 给出最短可逆表示，比 str 更不容易带出浮点噪声
            text = repr(value)
        return Cell(text=text, value_type=ValueType.NUMBER, numeric=text)
    return Cell(text=str(value))


def _split_tables(rows: list[list[Cell]]) -> list[list[list[Cell]]]:
    """把一个 sheet 按连续空行切成多张表。

    这是真实场景的需要：一张「月度汇总」sheet 里常常上面是损益表、
    下面是比率表，混成一张表会让 agent 把不相干的列对齐。
    """
    chunks: list[list[list[Cell]]] = []
    current: list[list[Cell]] = []
    blanks = 0
    for row in rows:
        is_blank = all(c.text == "" for c in row)
        if is_blank:
            blanks += 1
            if blanks >= BLANK_ROW_BREAK and current:
                chunks.append(current)
                current = []
                blanks = 0
            continue
        blanks = 0
        current.append(row)
    if current:
        chunks.append(current)
    return chunks


def parse_excel(path: str | Path, max_rows: int = MAX_ROWS_DEFAULT) -> Document:
    """解析 .xlsx / .xlsm / .csv。"""
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in {".csv", ".tsv"}:
        return _parse_csv(path, max_rows)

    blocks: list[TextBlock] = []
    tables: list[Table] = []
    warnings: list[str] = []
    metadata: dict[str, Any] = {}

    # 两份工作簿：一份带公式，一份带缓存值。行对齐，按坐标取。
    wb_formula = load_workbook(str(path), data_only=False, read_only=True)
    try:
        wb_value = load_workbook(str(path), data_only=True, read_only=True)
    except Exception:
        wb_value = None
        warnings.append("无法读取公式缓存值，公式单元格只会显示公式文本。")

    metadata["sheets"] = list(wb_formula.sheetnames)

    for sheet_name in wb_formula.sheetnames:
        ws_f = wb_formula[sheet_name]
        # 图表页（Chartsheet）不是数据表，没有 iter_rows/max_row。跳过它，
        # 但要在 warnings 里说明，否则调用方会以为这个 sheet 是空的。
        if not hasattr(ws_f, "iter_rows"):
            warnings.append(f"工作表「{sheet_name}」是图表页，没有单元格数据，已跳过。")
            continue
        ws_v = wb_value[sheet_name] if wb_value else None
        if ws_v is not None and not hasattr(ws_v, "iter_rows"):
            ws_v = None
        rows: list[list[Cell]] = []
        truncated = False

        for i, row_f in enumerate(ws_f.iter_rows()):
            if i >= max_rows:
                truncated = True
                break
            row_v = None
            if ws_v is not None:
                row_v = next(ws_v.iter_rows(min_row=i + 1, max_row=i + 1), None)

            cells: list[Cell] = []
            for j, cell_f in enumerate(row_f):
                val = cell_f.value
                raw_v = row_v[j].value if row_v is not None and j < len(row_v) else None

                if isinstance(val, str) and val.startswith("="):
                    # 公式单元格：text 放缓存显示值，formula 单独保存。
                    # 若缓存值为空（openpyxl 生成的文件常如此），用公式本身占位，
                    # 否则 agent 看到的是一个空单元格，会以为那里没数据。
                    shown = _cell_from_value(raw_v) if raw_v is not None else Cell(text="")
                    text = shown.text if shown.text else val
                    cells.append(
                        Cell(
                            text=text,
                            value_type=shown.value_type if shown.text else ValueType.TEXT,
                            numeric=shown.numeric,
                            formula=val,
                        )
                    )
                else:
                    cells.append(_cell_from_value(val))
            rows.append(cells)

        if truncated:
            warnings.append(
                f"工作表「{sheet_name}」超过 {max_rows} 行，只读取了前 {max_rows} 行。"
            )

        # 公式单元格若没有缓存值，text 会等于公式原文。必须说出来，否则 agent
        # 可能把 "=B2+B3" 当作计算结果引用。
        uncached = sum(
            1
            for row in rows
            for c in row
            if c.formula is not None and c.text == c.formula
        )
        if uncached:
            warnings.append(
                f"工作表「{sheet_name}」有 {uncached} 个公式单元格没有缓存计算结果，"
                f"它们显示的是公式原文（见 formula 字段），不是计算结果。"
                f"如需数值请用 Excel 打开后重算并另存。"
            )

        chunks = _split_tables(rows)
        for idx, chunk in enumerate(chunks):
            # 去掉每行尾部的空列，让紧邻的几张表列宽一致（Excel 常留空白列）
            width = 0
            for r in chunk:
                for j, c in enumerate(r):
                    if c.text:
                        width = max(width, j + 1)
            trimmed = [r[:width] for r in chunk]
            tables.append(
                Table(
                    page_start=1,
                    page_end=1,
                    rows=trimmed,
                    header_rows=1,
                    caption=f"{sheet_name}" + (f" (表 {idx + 1})" if len(chunks) > 1 else ""),
                )
            )

        blocks.append(
            TextBlock(
                text=f"工作表「{sheet_name}」，{ws_f.max_row or 0} 行 × {ws_f.max_column or 0} 列",
                page=1,
                kind=BlockKind.HEADING,
            )
        )

    wb_formula.close()
    if wb_value:
        wb_value.close()

    return Document(
        path=str(path),
        kind="excel",
        sha256=file_digest(path),
        page_count=1,
        blocks=blocks,
        tables=tables,
        metadata=metadata,
        warnings=warnings,
    )


def _parse_csv(path: Path, max_rows: int) -> Document:
    """CSV/TSV：编码探测失败时退到 latin-1，并明确告知可能乱码。"""
    warnings: list[str] = []
    encoding = "utf-8-sig"
    try:
        with open(path, "r", encoding=encoding, newline="") as fh:
            sample = fh.read(8192)
            fh.seek(0)
            delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
                delimiter = dialect.delimiter
            except csv.Error:
                pass
            reader = csv.reader(fh, delimiter=delimiter)
            rows: list[list[Cell]] = []
            for i, row in enumerate(reader):
                if i >= max_rows:
                    warnings.append(f"CSV 超过 {max_rows} 行，只读取了前 {max_rows} 行。")
                    break
                rows.append([Cell(text=(v or "").strip()) for v in row])
    except UnicodeDecodeError:
        with open(path, "r", encoding="latin-1", newline="") as fh:
            rows = [[Cell(text=(v or "").strip()) for v in r] for r in csv.reader(fh)]
        warnings.append("该文件不是 UTF-8 编码，已按 latin-1 读取，非 ASCII 字符可能乱码。")

    tables = [
        Table(page_start=1, page_end=1, rows=chunk, header_rows=1)
        for chunk in _split_tables(rows)
    ]

    return Document(
        path=str(path),
        kind="csv",
        sha256=file_digest(path),
        page_count=1,
        blocks=[],
        tables=tables,
        metadata={"delimiter": delimiter},
        warnings=warnings,
    )
