"""PDF 解析的行为测试。

这里固定的每一条都是踩过的坑：
- 无边框表格必须按坐标对齐还原，不能退化成空格拼凑的文本流；
- 两栏排版的正文绝不能被当成表格吞掉（曾经整页丢失）；
- 金额精度不能在链路中损失。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from doclens.core import parse


@pytest.fixture(scope="module")
def bordered_report(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """一份没有可见表格线的报表——真实世界里从网页打印的 PDF 长这样。"""
    path = tmp_path_factory.mktemp("pdf") / "report.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    _, h = A4
    y = h - 60

    c.setFont("Helvetica-Bold", 16)
    c.drawString(60, y, "Division Revenue Summary")
    y -= 40

    c.setFont("Helvetica-Bold", 11)
    for text, x in [("Division", 60), ("Revenue", 240), ("YoY", 360)]:
        c.drawString(x, y, text)
    y -= 18

    c.setFont("Helvetica", 11)
    for name, rev, yoy in [
        ("Enterprise", "8,450,000.00", "12.4%"),
        ("Consumer", "3,120,500.00", "-3.8%"),
        ("Overseas", "1,980,000.00", "24.1%"),
    ]:
        c.drawString(60, y, name)
        c.drawString(240, y, rev)
        c.drawString(360, y, yoy)
        y -= 18

    c.save()
    return path


@pytest.fixture(scope="module")
def two_column(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """两栏排版。每行都能被切成两格，但它是正文，不是表格。"""
    path = tmp_path_factory.mktemp("pdf") / "twocol.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    _, h = A4
    c.setFont("Helvetica", 10)
    y = h - 80
    for i in range(1, 6):
        c.drawString(60, y, f"L{i} left column sentence here.")
        c.drawString(320, y, f"R{i} right column sentence here.")
        y -= 16
    c.save()
    return path


def test_borderless_table_is_recovered(bordered_report: Path) -> None:
    doc = parse(bordered_report)
    assert len(doc.tables) == 1, "无边框表格没有被识别出来"

    table = doc.tables[0]
    assert table.n_rows == 4  # 表头 + 3 行数据
    assert table.n_cols == 3
    assert [c.text for c in table.rows[0]] == ["Division", "Revenue", "YoY"]


def test_amounts_keep_precision(bordered_report: Path) -> None:
    """金额必须逐字保留。任何 float 往返都会破坏财务对账。"""
    doc = parse(bordered_report)
    revenue_cells = [row[1] for row in doc.tables[0].rows[1:]]
    assert [c.text for c in revenue_cells] == [
        "8,450,000.00",
        "3,120,500.00",
        "1,980,000.00",
    ]
    assert [c.numeric for c in revenue_cells] == [
        "8450000.00",
        "3120500.00",
        "1980000.00",
    ]


def test_negative_percent(bordered_report: Path) -> None:
    doc = parse(bordered_report)
    negative = doc.tables[0].rows[2][2]
    assert negative.text == "-3.8%"
    assert negative.value_type.value == "percent"
    assert negative.numeric == "-3.8"


def test_two_column_text_is_not_swallowed(two_column: Path) -> None:
    """回归测试：两栏正文曾被整体丢弃，因为每行都被误判为表格行。"""
    doc = parse(two_column)
    assert doc.tables == [], "两栏正文不应被识别成表格"
    joined = "\n".join(b.text for b in doc.blocks)
    for i in range(1, 6):
        assert f"L{i} left column" in joined
        assert f"R{i} right column" in joined
    assert len(doc.blocks) == 5


def test_unsupported_extension_raises(tmp_path: Path) -> None:
    bogus = tmp_path / "x.exe"
    bogus.write_bytes(b"nope")
    with pytest.raises(ValueError, match="不支持的文件格式"):
        parse(bogus)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse(tmp_path / "不存在.pdf")
