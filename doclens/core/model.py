"""DocLens 的统一文档模型。

设计原则（这是产品存在的理由，改动前请先读）：
1. 数字永不经过 float 往返。原文始终保存在 Cell.text，numeric 只是附带的解析结果。
   财务/发票场景里 0.1+0.2 这类误差会直接变成对不上的账。
2. 每个单元格和段落都带来源坐标（page, row, col），agent 引用时能指回原处，
   而不是编造一个看起来合理的数字。
3. 表格结构优先于纯文本流。多栏排版、跨页续表、合并单元格都必须显式表达，
   否则 agent 会把两列拼成一句话。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class BlockKind(str, Enum):
    """文本块的语义类别。用枚举而不是裸字符串，避免下游拼错。"""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    CAPTION = "caption"
    FOOTNOTE = "footnote"


class ValueType(str, Enum):
    """单元格的值类型。NUMBER 只说明它长得像数字，原文仍在 text 里。"""

    EMPTY = "empty"
    NUMBER = "number"
    PERCENT = "percent"
    CURRENCY = "currency"
    DATE = "date"
    TEXT = "text"


# 数字的组成部分，复用在下面几个模式里。不捕获分组，避免模式之间捕获组数量不同
# 导致调用方按下标取值时越界——那类 bug 在「12,000.00」这种最常见的金额上就会崩。
#
# 结构必须是「整数部分 + 可选小数」或「纯小数」，且整数部分要么是规整的千分位
# （1,234,567）要么是不带逗号的数字。这样「1,2,3」和单独一个「-」都不会被当成数字，
# 它们只会让 agent 算错。
_NUM_BODY = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|-?\.\d+"

# 会计式负数： (1,234.56) 表示 -1234.56
_NEG_ACCOUNTING = re.compile(rf"^\(({_NUM_BODY})\)$")
# 百分比： 12.5% / -3%
_PERCENT = re.compile(rf"^({_NUM_BODY})\s?%$")
# 货币： $1,234.56 / ¥1234 / US$ 12.00
_CURRENCY = re.compile(rf"^(?:US)?[$€£¥￥]\s?({_NUM_BODY})$")
# 裸数字，含千分位和科学计数法。指数必须在捕获组内，否则 \"1.5e10\" 被截成 \"1.5\"。
# 内层用非捕获组包住 \"整数或小数\" 这个二选一，否则 | 会让 ^ 只作用于前半段。
_PLAIN_NUMBER = re.compile(rf"^((?:{_NUM_BODY})(?:[eE][-+]?\d+)?)$")

# 常见日期形态。只用于标注类型，不做时区/格式归一化——归一化会丢信息。
_DATE_PATTERN = re.compile(
    r"^(?:\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})$"
)


def classify(raw: str) -> tuple[ValueType, str | None]:
    """判断一个单元格原文的数值类型。

    返回 (类型, 裸数值串)。裸数值串保留全部有效数字，不做 float 转换，
    调用方需要精确计算时用 decimal.Decimal 解析它。

    用全匹配（不是 match）是刻意的：「12 个月」「3 人」这类带单位的文本不能算数字，
    否则 agent 会把它们当可求和的量。
    """
    text = (raw or "").strip()
    if not text:
        return ValueType.EMPTY, None

    m = _NEG_ACCOUNTING.match(text)
    if m:
        return ValueType.NUMBER, f"-{m.group(1).replace(',', '')}"

    m = _PERCENT.match(text)
    if m:
        return ValueType.PERCENT, m.group(1).replace(",", "")

    m = _CURRENCY.match(text)
    if m:
        return ValueType.CURRENCY, m.group(1).replace(",", "")

    m = _PLAIN_NUMBER.match(text)
    if m:
        return ValueType.NUMBER, m.group(1).replace(",", "")

    if _DATE_PATTERN.match(text):
        return ValueType.DATE, None

    return ValueType.TEXT, None


@dataclass
class Cell:
    """表格里的一个单元格。

    text 是原文，永远不丢。numeric 只在 ValueType 是数值类时有值，且以字符串形式
    保存，避免 float 精度损失。merged 标记该格是否被合并覆盖。
    formula 保存 Excel 里的原始公式（如 "=B2/1000"），它比显示值更重要——
    财务模型的错误通常在公式里，agent 拿不到公式就只能盲猜。
    """

    text: str
    value_type: ValueType = ValueType.TEXT
    numeric: str | None = None
    row_span: int = 1
    col_span: int = 1
    merged: bool = False
    formula: str | None = None

    def __post_init__(self) -> None:
        if self.value_type is ValueType.TEXT and self.numeric is None:
            self.value_type, self.numeric = classify(self.text)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"text": self.text, "type": self.value_type.value}
        if self.numeric is not None:
            out["numeric"] = self.numeric
        if self.formula is not None:
            out["formula"] = self.formula
        if self.row_span != 1 or self.col_span != 1:
            out["span"] = [self.row_span, self.col_span]
        if self.merged:
            out["merged"] = True
        return out


@dataclass
class Table:
    """一张表。跨页续表会被合并成同一个 Table 对象并记下页码范围。"""

    page_start: int
    page_end: int
    rows: list[list[Cell]]
    # 表头行数。read_document 用它决定是否在每一段输出里重复表头，
    # 这样 agent 分块读取时不会忘记列名。
    header_rows: int = 1
    caption: str | None = None
    bbox: tuple[float, float, float, float] | None = None

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_start": self.page_start,
            "page_end": self.page_end,
            "header_rows": self.header_rows,
            "rows": [len(self.rows), self.n_cols],
            "caption": self.caption,
            "cells": [[c.as_dict() for c in row] for row in self.rows],
        }


@dataclass
class TextBlock:
    """一段正文。bbox 用于让 agent 理解版面（例如判断是不是页眉页脚）。"""

    text: str
    page: int
    kind: BlockKind = BlockKind.PARAGRAPH
    bbox: tuple[float, float, float, float] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"page": self.page, "kind": self.kind.value, "text": self.text}


@dataclass
class Document:
    """解析结果。warnings 必须透传给调用方：解析是有损的，遮掩损失等于撒谎。"""

    path: str
    kind: str
    sha256: str
    page_count: int
    blocks: list[TextBlock] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return Path(self.path).name

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "kind": self.kind,
            "sha256": self.sha256,
            "page_count": self.page_count,
            "metadata": self.metadata,
            "warnings": self.warnings,
            "blocks": [b.as_dict() for b in self.blocks],
            "tables": [t.as_dict() for t in self.tables],
        }


def file_digest(path: str | Path) -> str:
    """算文件指纹，用于缓存和「这份文档有没有变过」的判断。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
