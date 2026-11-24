# DocLens 的容器镜像。
#
# 为什么需要它：Glama 等 MCP 目录站会在 Firecracker 沙箱里从这个 Dockerfile
# 构建并实际跑一遍，构建失败就不会出现在搜索结果里。所以这个文件必须
# 真的能构建、真的能启动并响应 tools/list——不是装饰。
#
# 用 uv 而不是 pip 装依赖：它按 pyproject 解析出一份确定的依赖图，
# 比 pip 逐包装快一个数量级，沙箱构建超时的风险更低。
#
# 注意 https://docs.astral.sh/uv/guides/integration/docker/ 提到的取舍：
# uv 在容器里默认不建虚拟环境，用 --system 装到系统 Python 是刻意的，
# 这样 ENTRYPOINT 直接是 doclens 命令，不用再激活任何环境。

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 先装依赖层：只依赖 pyproject，源码改了也不会让这层缓存失效。
# doclens/ 先建一个空包，让 `pip install .` 能读到包结构而不报错。
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p doclens && touch doclens/__init__.py

RUN pip install --no-cache-dir uv \
 && uv pip install --system \
      "mcp>=2.0" "pdfplumber>=0.11" "openpyxl>=3.1" "python-docx>=1.2" "pydantic>=2.0"

# 再拷真实源码并装包本身（依赖已就位，--no-deps 避免重复解析）
COPY doclens/ doclens/
RUN uv pip install --system --no-deps .

# 以非 root 运行。MCP 服务器只需要读用户指定的文档，不需要写系统目录。
RUN useradd --create-home --uid 10001 doclens
USER doclens

# MCP 走 stdin/stdout，不能有额外输出污染管道。
ENTRYPOINT ["doclens"]
CMD []
