"""MCP 服务的端到端测试。

用官方 SDK 的 client 真实拉起 stdio 子进程并调用工具——不是 mock，
因为要验证的正是「宿主 agent 能不能真的用上」这件事。

所有测试样本都在这里现造（写到 pytest 的临时目录），不依赖仓库里的
fixtures/ 二进制文件：那些文件不进 git，陌生用户 clone 下来必须也能跑测试。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "doclens.server"],
        cwd=str(PROJECT_ROOT),
    )


async def _with_session(coro_fn):
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await coro_fn(session)


def _run(coro_fn):
    return asyncio.run(_with_session(coro_fn))


@pytest.fixture(scope="module")
def report_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """一份无边框的报表 PDF。测试需要什么就现画什么，不依赖仓库里的样本。"""
    path = tmp_path_factory.mktemp("mcp") / "q3.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    _, h = A4
    y = h - 60
    c.setFont("Helvetica-Bold", 13)
    c.drawString(60, y, "Q3 Revenue")
    y -= 30
    c.setFont("Helvetica-Bold", 11)
    for text, x in [("Division", 60), ("Revenue", 220), ("YoY", 360)]:
        c.drawString(x, y, text)
    y -= 18
    c.setFont("Helvetica", 11)
    for name, rev, yoy in [
        ("Enterprise", "8,450,000.00", "12.4%"),
        ("Consumer", "3,120,500.00", "-3.8%"),
        ("Overseas", "1,980,000.00", "24.1%"),
    ]:
        c.drawString(60, y, name)
        c.drawString(220, y, rev)
        c.drawString(360, y, yoy)
        y -= 18
    c.save()
    return path


@pytest.fixture(scope="module")
def sample_dir(
    tmp_path_factory: pytest.TempPathFactory, report_pdf: Path
) -> Path:
    """一个装着可读文档的目录，用于验证 list_documents。"""
    d = tmp_path_factory.mktemp("docs")
    (d / "a.md").write_text("# 报告\n\n正文。\n", encoding="utf-8")
    (d / "b.txt").write_text("纯文本内容\n", encoding="utf-8")
    # 顺带放一个不支持的格式，验证它被正确跳过
    (d / "c.exe").write_bytes(b"\x00\x01")
    return d


TOOL_NAMES = {
    "list_documents",
    "read_document",
    "search_document",
    "extract_table",
    "inspect_document",
    "compare_extraction",
}


def test_server_exposes_all_tools() -> None:
    async def check(session):
        tools = await session.list_tools()
        return {t.name for t in tools.tools}

    names = _run(check)
    assert names == TOOL_NAMES, f"工具清单不符：{names}"


def test_read_document_preserves_table(report_pdf: Path) -> None:
    async def call(session):
        res = await session.call_tool("read_document", {"path": str(report_pdf)})
        return res.content[0].text

    text = _run(call)
    # 表头与数据必须在同一行内，证明列关系没丢
    assert "| Division | Revenue | YoY |" in text
    assert "| Enterprise | 8,450,000.00 (`8450000.00`) | 12.4% (`12.4`) |" in text


def test_search_document_finds_table_cell(report_pdf: Path) -> None:
    async def call(session):
        res = await session.call_tool(
            "search_document", {"path": str(report_pdf), "query": "overseas"}
        )
        return res.content[0].text

    text = _run(call)
    assert "Overseas" in text


def test_inspect_flags_issues(report_pdf: Path) -> None:
    async def call(session):
        res = await session.call_tool("inspect_document", {"path": str(report_pdf)})
        return res.content[0].text

    text = _run(call)
    assert "解析诊断" in text
    assert "其中数值型" in text


def test_missing_file_returns_readable_error() -> None:
    async def call(session):
        res = await session.call_tool(
            "read_document", {"path": "/tmp/绝对不存在的文件xyz.pdf"}
        )
        return res.content[0].text

    text = _run(call)
    # 必须是可读提示，不能是堆栈
    assert "文件不存在" in text
    assert "Traceback" not in text


def test_list_documents_finds_supported_files(sample_dir: Path) -> None:
    async def call(session):
        res = await session.call_tool("list_documents", {"directory": str(sample_dir)})
        return res.content[0].text

    text = _run(call)
    assert "可读文档" in text
    # 支持的两个文件要出现，不支持的 .exe 要被跳过
    assert "a.md" in text and "b.txt" in text
    assert "c.exe" not in text
