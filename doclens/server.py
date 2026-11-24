"""DocLens 的 MCP 服务。

暴露给 AI 编程 agent（Claude Code、Codex、Cursor、Michael-IDE 等）的工具。
设计上刻意保持工具数少而正交：list / read / search / table / doctor。
工具名和能力太多的 MCP 服务会让模型选错工具，开销比它省下的还大。

传输：stdio。这是 MCP 桌面集成的事实标准，不需要端口、不需要鉴权，
进程由宿主 agent 拉起，用完即退。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .core import SUPPORTED_EXTENSIONS, parse
from .core.model import Document
from .render import render_document, render_table, render_table_only

server = MCPServer(
    name="doclens",
    version="0.1.0",
    instructions=(
        "DocLens 读取 PDF / Excel / Word / CSV / 纯文本，保留表格结构与数字精度。"
        "当任务涉及财报、合同、报价单、数据表这类文档时优先用它："
        "它给出的表格是行列对齐的，金额保留原始精度，公式单独标注。"
        "不要用 shell 的 cat / pdftotext 读这类文件——那会丢结构。"
    ),
)


# 解析结果缓存。同一个文件在一次会话里常被反复读（先概览再细看），
# 用 (路径, 修改时间, 大小) 做键，文件一改就自动失效。
_CACHE: dict[tuple[str, float, int], Document] = {}


def _load(path: str) -> Document:
    resolved = Path(path).expanduser()
    if not resolved.exists():
        raise FileNotFoundError(
            f"文件不存在：{resolved}。请确认路径；相对路径是相对于 MCP 宿主进程的工作目录。"
        )
    stat = resolved.stat()
    key = (str(resolved), stat.st_mtime, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    doc = parse(resolved)
    # 只留最近 32 份，避免长会话把内存吃掉
    if len(_CACHE) > 32:
        _CACHE.clear()
    _CACHE[key] = doc
    return doc


@server.tool(
    description=(
        "列出目录里 DocLens 能读的文件，附带大小和是否受支持。"
        "用于在批量处理前先摸清有哪些文档。"
    )
)
def list_documents(
    directory: Annotated[str, Field(description="要列出的目录路径")],
    recursive: Annotated[bool, Field(description="是否递归子目录")] = False,
) -> str:
    root = Path(directory).expanduser()
    if not root.is_dir():
        return f"不是目录：{root}"

    iterator = root.rglob("*") if recursive else root.iterdir()
    found: list[str] = []
    skipped = 0

    for item in iterator:
        if not item.is_file():
            continue
        if item.suffix.lower() in SUPPORTED_EXTENSIONS:
            size = item.stat().st_size
            size_text = f"{size / 1024:.0f} KB" if size < 1024**2 else f"{size / 1024**2:.1f} MB"
            found.append(f"- {item}（{size_text}）")
        else:
            skipped += 1

    if not found:
        return (
            f"目录里没有 DocLens 支持的文件。支持的格式：{', '.join(SUPPORTED_EXTENSIONS)}"
        )

    header = f"目录 {root} 下找到 {len(found)} 份可读文档"
    if skipped:
        header += f"（另有 {skipped} 个不支持的文件已跳过）"
    return header + "：\n" + "\n".join(sorted(found))


@server.tool(
    description=(
        "读取一份文档，返回结构化的 Markdown：表格按行列对齐、金额保留原始精度、"
        "公式单独标注、正文按页标注。这是主工具，处理财务/合同/报表时优先用它。"
    )
)
def read_document(
    path: Annotated[str, Field(description="文档的绝对路径或相对路径")],
    max_chars: Annotated[
        int, Field(description="返回内容的最大字符数，超出会截断并提示如何继续", ge=1000)
    ] = 40_000,
) -> str:
    try:
        doc = _load(path)
    except (FileNotFoundError, ValueError) as exc:
        return f"读取失败：{exc}"
    except Exception as exc:  # 解析器内部异常也要转成人能读的一句话
        return f"解析「{path}」时出错：{type(exc).__name__}: {exc}"

    return render_document(doc, max_chars=max_chars)


@server.tool(
    description=(
        "在文档里搜索文本，返回命中的页码和上下文。"
        "长文档不必整份读进来，先用它定位。"
    )
)
def search_document(
    path: Annotated[str, Field(description="文档路径")],
    query: Annotated[str, Field(description="要查找的文本，不区分大小写")],
    context_chars: Annotated[int, Field(description="命中处前后各保留多少字符", ge=0)] = 120,
    limit: Annotated[int, Field(description="最多返回多少条命中", ge=1)] = 20,
) -> str:
    try:
        doc = _load(path)
    except (FileNotFoundError, ValueError) as exc:
        return f"读取失败：{exc}"

    needle = query.lower()
    hits: list[str] = []

    for block in doc.blocks:
        hay = block.text.lower()
        start = 0
        while len(hits) < limit:
            pos = hay.find(needle, start)
            if pos < 0:
                break
            lo = max(0, pos - context_chars)
            hi = min(len(block.text), pos + len(query) + context_chars)
            snippet = block.text[lo:hi].replace("\n", " ")
            hits.append(f"- 第 {block.page} 页：…{snippet}…")
            start = pos + len(query)
        if len(hits) >= limit:
            break

    # 表格里的命中同样重要：agent 常常从表里找一个科目名
    if len(hits) < limit:
        for t_idx, table in enumerate(doc.tables, start=1):
            for r_idx, row in enumerate(table.rows):
                for cell in row:
                    if needle in cell.text.lower() and len(hits) < limit:
                        row_text = " | ".join(c.text for c in row)
                        hits.append(
                            f"- 第 {table.page_start} 页 表{t_idx} 第 {r_idx + 1} 行：{row_text}"
                        )

    if not hits:
        return f"在「{Path(path).name}」里没有找到「{query}」。"
    return f"在「{Path(path).name}」里找到 {len(hits)} 处「{query}」：\n" + "\n".join(hits)


@server.tool(
    description=(
        "只读取指定序号的那一张表，输出完整 Markdown 表格。"
        "文档里表格很多时用它，避免整份读进来。序号从 1 开始。"
    )
)
def extract_table(
    path: Annotated[str, Field(description="文档路径")],
    index: Annotated[int, Field(description="表序号，从 1 开始", ge=1)] = 1,
) -> str:
    try:
        doc = _load(path)
    except (FileNotFoundError, ValueError) as exc:
        return f"读取失败：{exc}"

    if not doc.tables:
        return f"「{Path(path).name}」里没有识别到表格。"

    listing = "\n".join(
        f"  {i}. {(t.caption or f'第 {t.page_start} 页')} — {t.n_rows} 行 × {t.n_cols} 列"
        for i, t in enumerate(doc.tables, start=1)
    )
    body = render_table_only(doc, index)
    return f"文档共 {len(doc.tables)} 张表：\n{listing}\n\n{body}"


@server.tool(
    description=(
        "诊断一份文档的解析质量：表格是否完整、有没有数字被降级成文本、"
        "有没有扫描页缺文字层、有没有公式缺缓存值。"
        "在把解析结果用于重要决策之前跑一次，能提前发现读错的风险。"
    )
)
def inspect_document(
    path: Annotated[str, Field(description="文档路径")],
) -> str:
    try:
        doc = _load(path)
    except (FileNotFoundError, ValueError) as exc:
        return f"读取失败：{exc}"
    except Exception as exc:
        return f"解析「{path}」时出错：{type(exc).__name__}: {exc}"

    lines = [f"# 解析诊断：{doc.name}", ""]
    lines.append(f"- 类型：{doc.kind}｜页数：{doc.page_count}")
    lines.append(f"- 正文段：{len(doc.blocks)}｜表格：{len(doc.tables)}")

    total_cells = sum(t.n_rows * t.n_cols for t in doc.tables)
    numeric = 0
    suspicious = 0

    for table in doc.tables:
        for row in table.rows:
            for cell in row:
                if cell.value_type.value in {"number", "percent", "currency"}:
                    numeric += 1
                # 含数字但被判为文本的单元格，是最容易出错的形态
                if (
                    cell.value_type.value == "text"
                    and re.search(r"\d", cell.text)
                    and cell.formula is None
                ):
                    suspicious += 1

    lines.append(f"- 表格单元格总数：{total_cells}｜其中数值型：{numeric}")

    issues: list[str] = []
    if suspicious:
        issues.append(
            f"{suspicious} 个含数字的单元格被判为文本（可能带单位或格式异常），"
            "求和/比较前请人工确认。"
        )
    # 公式缺缓存值的 warning 已经由解析器生成并附在 doc.warnings 里，
    # 这里不再重复统计——同一个问题报两遍会让人以为有两个问题。
    for warning in doc.warnings:
        issues.append(warning)

    lines.append("")
    if issues:
        # 解析器自己的 warnings 可能和上面统计出的问题重复（比如公式缺缓存值
        # 在两处都会被发现）。去重但保序，避免同一问题报两遍让人以为有两个问题。
        seen: set[str] = set()
        unique: list[str] = []
        for issue in issues:
            key = issue.strip()
            if key and key not in seen:
                seen.add(key)
                unique.append(issue)
        lines.append("## 需要注意")
        lines.append("")
        for issue in unique:
            lines.append(f"- {issue}")
    else:
        lines.append("## 需要注意")
        lines.append("")
        lines.append("- 未发现明显问题。")

    return "\n".join(lines)


@server.tool(
    description=(
        "把 DocLens 的表格还原结果与「朴素文本流」抽取的结果并排给出，"
        "用于判断这份文档如果不做结构还原会错在哪里。"
        "适合在向别人解释这份工具的必要性时用。"
    )
)
def compare_extraction(
    path: Annotated[str, Field(description="文档路径")],
    index: Annotated[int, Field(description="第几张表，从 1 开始", ge=1)] = 1,
) -> str:
    try:
        doc = _load(path)
    except (FileNotFoundError, ValueError) as exc:
        return f"读取失败：{exc}"

    if not doc.tables:
        return f"「{Path(path).name}」里没有识别到表格，无法对比。"
    if not 1 <= index <= len(doc.tables):
        return f"表序号超出范围：共 {len(doc.tables)} 张表。"

    table = doc.tables[index - 1]
    structured = render_table(table)

    # 朴素结果：把所有单元格按出现顺序拼成一段文本。这等价于用纯文本抽取器
    # 拿到的效果——列关系完全丢失。
    naive_parts = [c.text for row in table.rows for c in row if c.text]
    naive = " ".join(naive_parts)

    return (
        f"## 结构还原（DocLens）\n\n{structured}\n\n"
        f"## 朴素文本流（列关系丢失）\n\n{naive[:2000]}\n\n"
        f"## 差异\n\n"
        f"结构化结果保留了 {table.n_rows} 行 × {table.n_cols} 列的对应关系；"
        f"朴素结果只有 {len(naive_parts)} 个孤立的词，"
        f"无法判断哪个数字属于哪一行、哪一列。"
    )


def main() -> None:
    """stdio 入口。宿主 agent 会以子进程方式拉起它。

    Windows 控制台的默认编码是 cp1252，遇到中文会抛 UnicodeEncodeError，
    而 MCP 服务在 stdio 上一崩就等于整个工具不可用。这里把标准流重配成
    UTF-8，并允许编码失败时替换而不是抛异常——宁可少一个字符，也不能
    让进程直接死掉。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                # 流可能已被重定向且不支持重配；忽略即可，不影响协议本身。
                pass

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
