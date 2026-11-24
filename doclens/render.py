"""把统一文档模型渲染成给 agent 读的文本。

为什么单独一层：agent 拿到的不是 JSON schema，而是一段能直接理解的文本。
JSON 会让模型把注意力花在括号和字段名上，而它真正需要的是「哪一列是哪一列」。
这里用 Markdown 表格 + 显式页码/坐标，让引用可以回溯到原文件。

关键设计：表格永远重复表头。agent 分块读取长表时最容易犯的错就是忘记列名，
然后按错的列求和。
"""

from __future__ import annotations

from .core.model import Cell, Document, Table


def _cell_text(cell: Cell) -> str:
    """单元格在 Markdown 里的显示形式。

    公式单元格要标出来：agent 看到 "$1,234.56" 和 "=SUM(B2:B9)" 的反应完全不同，
    后者意味着数值由别处决定，改动它之前得先看被引用的单元格。
    """
    if cell.formula is not None and cell.text == cell.formula:
        return f"`{cell.text}`"
    text = cell.text.replace("|", "\\|")
    if cell.numeric is not None and cell.numeric != cell.text:
        # 原文与裸值不同（如带千分位、货币符号）时给出可计算的值，
        # 避免 agent 自己去做字符串清洗而出错。
        return f"{text} (`{cell.numeric}`)"
    return text


def render_table(table: Table, include_header: bool = True) -> str:
    """把一张表渲染成 Markdown。列数不一致的行补空单元格，保持表格合法。"""
    if not table.rows:
        return "(空表)"

    width = table.n_cols
    lines: list[str] = []

    caption = table.caption or f"表格（第 {table.page_start} 页）"
    if table.page_end != table.page_start:
        caption += f"，跨第 {table.page_start}-{table.page_end} 页"
    lines.append(f"**{caption}** — {table.n_rows} 行 × {width} 列")
    lines.append("")

    for i, row in enumerate(table.rows):
        padded = row + [Cell(text="")] * (width - len(row))
        lines.append("| " + " | ".join(_cell_text(c) for c in padded) + " |")
        if i == 0:
            lines.append("|" + "|".join(["---"] * width) + "|")

    return "\n".join(lines)


def render_document(doc: Document, max_chars: int = 40_000) -> str:
    """渲染整份文档。

    超过 max_chars 时截断，并明确告诉调用方截在哪里、怎么继续读——
    静默截断会让 agent 以为文档就这么短。
    """
    parts: list[str] = []
    parts.append(f"# {doc.name}")
    parts.append("")
    parts.append(
        f"- 类型：{doc.kind}｜页数：{doc.page_count}｜"
        f"正文段：{len(doc.blocks)}｜表格：{len(doc.tables)}"
    )
    if doc.metadata:
        meta = "，".join(f"{k}={v}" for k, v in list(doc.metadata.items())[:6])
        parts.append(f"- 文档属性：{meta}")
    parts.append("")

    if doc.warnings:
        parts.append("## 解析提示")
        parts.append("")
        for w in doc.warnings:
            parts.append(f"- {w}")
        parts.append("")

    if doc.tables:
        parts.append("## 表格")
        parts.append("")
        for table in doc.tables:
            parts.append(render_table(table))
            parts.append("")

    if doc.blocks:
        parts.append("## 正文")
        parts.append("")
        current_page = None
        for block in doc.blocks:
            if block.page != current_page:
                parts.append(f"### 第 {block.page} 页")
                parts.append("")
                current_page = block.page
            text = block.text
            if block.kind.value == "heading":
                parts.append(f"**{text}**")
            else:
                parts.append(text)
            parts.append("")

    rendered = "\n".join(parts)
    if len(rendered) > max_chars:
        truncated = rendered[:max_chars]
        # 在最后一个完整行处断开，避免切碎一个表格单元格
        cut = truncated.rfind("\n")
        if cut > 0:
            truncated = truncated[:cut]
        return (
            truncated
            + f"\n\n---\n内容已截断：只显示前约 {max_chars} 字符，"
            + f"全文约 {len(rendered)} 字符。用 search_document 定位具体内容，"
            + "或指定页码/表序号分段读取。"
        )
    return rendered


def render_table_only(doc: Document, index: int) -> str:
    """只渲染指定序号的表。序号从 1 开始，与 render_document 里的出现顺序一致。"""
    if not doc.tables:
        return "该文档没有识别到表格。"
    if not 1 <= index <= len(doc.tables):
        return f"表序号超出范围：文档共 {len(doc.tables)} 张表，请求的是第 {index} 张。"
    return render_table(doc.tables[index - 1])
