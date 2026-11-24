"""数值类型识别的回归测试。

这些用例都是真实表格里出现过的形态。每一条都对应一次「agent 读错数字」的
具体失败：金额被当文本无法求和、日期被当数字推算、带单位的量被当可计算值。
"""

from __future__ import annotations

import pytest

from doclens.core.model import ValueType, classify


@pytest.mark.parametrize(
    "raw,expected_type,expected_numeric",
    [
        # 千分位金额：最常见的财务格式，精度必须完整保留
        ("12,000.00", ValueType.NUMBER, "12000.00"),
        ("1,234,567", ValueType.NUMBER, "1234567"),
        # 会计式负数：括号表示负，直接 str() 会丢掉负号
        ("(1,234.56)", ValueType.NUMBER, "-1234.56"),
        # 货币符号
        ("$1,234.56", ValueType.CURRENCY, "1234.56"),
        ("¥1234", ValueType.CURRENCY, "1234"),
        ("US$ 12.00", ValueType.CURRENCY, "12.00"),
        # 百分比
        ("12.5%", ValueType.PERCENT, "12.5"),
        ("-3%", ValueType.PERCENT, "-3"),
        # 裸数字与负号
        ("1234567.89", ValueType.NUMBER, "1234567.89"),
        ("-456789.12", ValueType.NUMBER, "-456789.12"),
        ("0.1523", ValueType.NUMBER, "0.1523"),
        # 科学计数法：指数不能丢，否则量级差几个数量级
        ("1.5e10", ValueType.NUMBER, "1.5e10"),
        ("2E-5", ValueType.NUMBER, "2E-5"),
        ("1.23e+8", ValueType.NUMBER, "1.23e+8"),
        # 前后空格应被忽略
        (" 42 ", ValueType.NUMBER, "42"),
        # 日期
        ("2026-11-24", ValueType.DATE, None),
        ("2026/11/24", ValueType.DATE, None),
        ("2026年11月24日", ValueType.DATE, None),
        # 空
        ("", ValueType.EMPTY, None),
        ("   ", ValueType.EMPTY, None),
    ],
)
def test_classify_values(raw: str, expected_type: ValueType, expected_numeric: str | None) -> None:
    kind, numeric = classify(raw)
    assert kind is expected_type, f"{raw!r} 被判定为 {kind}，应为 {expected_type}"
    assert numeric == expected_numeric


@pytest.mark.parametrize(
    "raw",
    [
        "12 个月",  # 带单位，不能当可求和数字
        "3 人",
        "毛利率",
        "第2季度",
        "v1.2.3",
        "1,2,3",  # 不合法的千分位
        "12.34.56",
        "1.5 亿",  # 中文数量单位
        "N/A",
        "-",
    ],
)
def test_classify_rejects_non_numbers(raw: str) -> None:
    """带单位或结构不合法的串必须判为文本，否则 agent 会把它们相加。"""
    kind, numeric = classify(raw)
    assert kind is ValueType.TEXT, f"{raw!r} 被误判为 {kind}"
    assert numeric is None
