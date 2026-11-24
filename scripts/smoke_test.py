"""验证已安装的 DocLens 能完成 MCP 握手并列出全部工具。

为什么是独立文件而不是 CI 里的 heredoc：Windows runner 用 PowerShell，
`python - <<'PY'` 这种 POSIX heredoc 会直接语法错误。写成文件后三种平台
都用同一条命令 `python scripts/smoke_test.py`，没有 shell 方言问题。

输出刻意只用 ASCII：Windows 控制台默认 cp1252，打印中文会抛
UnicodeEncodeError 直接让进程失败——那会让一次正常的检查变成假故障。
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
    # Windows 上装出来的是 doclens.exe，shutil.which 会自动处理后缀。
    binary = shutil.which("doclens")
    if binary is None:
        print("FAIL: doclens command not found on PATH", file=sys.stderr)
        return 1
    # 用 ascii 安全的方式打印路径：路径本身可能含非 ASCII（比如中文用户名）
    print("entry:", binary.encode("ascii", "backslashreplace").decode())

    async with Client(StdioServerParameters(command=binary, args=[])) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        print(f"tools found: {len(names)}")
        for name in sorted(names):
            print("  -", name)

        missing = EXPECTED - names
        if missing:
            print(f"FAIL: missing tools {sorted(missing)}", file=sys.stderr)
            return 1

    print("OK: handshake succeeded, all tools present")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
