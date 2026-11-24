"""验证已安装的 DocLens 能完成 MCP 握手并列出全部工具。

为什么是独立文件而不是 CI 里的 heredoc：Windows runner 用 PowerShell，
`python - <<'PY'` 这种 POSIX heredoc 会直接语法错误。写成文件后三种平台
都用同一条命令 `python scripts/smoke_test.py`，没有 shell 方言问题。

也能本地直接跑：
    .venv/bin/python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys

from mcp import Client, StdioServerParameters

EXPECTED = {
    "list_documents",
    "read_document",
    "search_document",
    "extract_table",
    "inspect_document",
    "compare_extraction",
}


async def main() -> int:
    # 用 PATH 里的 doclens：CI 刚 pip 装完，这是用户真实拿到的入口。
    # 在 Windows 上 py 启动器装出来的可能是 doclens.exe，shutil.which 两者都能找到。
    binary = shutil.which("doclens")
    if binary is None:
        print("找不到 doclens 命令，安装可能没成功", file=sys.stderr)
        return 1
    print(f"使用入口：{binary}")

    async with Client(StdioServerParameters(command=binary, args=[])) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        print(f"发现 {len(names)} 个工具：{sorted(names)}")

        missing = EXPECTED - names
        if missing:
            print(f"缺少工具：{sorted(missing)}", file=sys.stderr)
            return 1

    print("握手成功，全部工具齐全")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
