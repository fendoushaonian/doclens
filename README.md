# DocLens

**给 AI 编程 agent 的高保真文档读取器。**

Claude Code、Codex、Cursor 这类 agent 读 PDF 和 Excel 时会出错，而且错得很安静：表格被压成一条文本流，列关系丢失；金额经过 float 往返，精度掉了；多栏排版的左右栏交错读出；扫描件被当成空文档。

DocLens 是一个 MCP 服务，把这些读完再交给 agent——**表格按行列对齐，金额保留原始精度，公式单独标出，有损的地方明确告诉你**。

---

## 为什么需要它

用 `pdftotext` 读同一份财务报表，你得到的是：

```
Enterprise 8,450,000.00 12.4% Consumer 3,120,500.00 -3.8% Overseas 1,980,000.00 24.1%
```

用 DocLens：

```
| Division | Revenue | YoY |
|---|---|---|
| Enterprise | 8,450,000.00 (`8450000.00`) | 12.4% (`12.4`) |
| Consumer | 3,120,500.00 (`3120500.00`) | -3.8% (`-3.8`) |
| Overseas | 1,980,000.00 (`1980000.00`) | 24.1% (`24.1`) |
```

前者让 agent 猜哪个数字属于哪一行，后者直接告诉它。

---

## 安装

```bash
pip install doclens-mcp
```

或者直接从 GitHub 安装（PyPI 未发布时用这条）：

```bash
pip install git+https://github.com/fendoushaonian/doclens
```

从源码开发：

```bash
git clone https://github.com/fendoushaonian/doclens
cd doclens
pip install -e ".[dev]"
```

## 接入 agent

### Claude Code

```bash
claude mcp add doclens -- doclens
```

### Cursor

在 `~/.cursor/mcp.json` 里加：

```json
{
  "mcpServers": {
    "doclens": {
      "command": "doclens"
    }
  }
}
```

### 其他 MCP 宿主

任何支持 stdio 传输的 MCP 客户端都能用它——启动命令就是 `doclens`，没有端口，没有 API key。

---

## 工具

| 工具 | 用途 |
|---|---|
| `read_document` | 读取整份文档，返回结构化 Markdown。主工具。 |
| `search_document` | 在文档里定位文本，返回页码和上下文。长文档先用它。 |
| `extract_table` | 只读某一张表，表格多的文档用它能省上下文。 |
| `inspect_document` | 诊断解析质量：有没有数字被降级成文本、公式缺缓存值、扫描页缺文字层。 |
| `list_documents` | 列目录里能读的文件。 |
| `compare_extraction` | 并排展示「结构还原」与「朴素文本流」的差异。 |

## 支持的格式

PDF、XLSX、XLSM、CSV、TSV、DOCX、MD、TXT、JSON、YAML、LOG。

## 独有之处

- **无边框表格**：不依赖可见表格线，按字形坐标聚类还原行列。真实世界里从网页打印的 PDF、排版软件导出的报表大多没有线。
- **数字不经过 float**：`12,000.00` 全程是字符串，同时给出可计算的裸值 `12000.00`，精度不丢。
- **识别会计式负数**：`(1,234.56)` 正确解析为 `-1234.56`。
- **不静默丢内容**：两栏排版的正文不会被误判成表格吞掉；截断、扫描页、公式缺值都会明说。
- **Excel 公式保留**：`=B2+B3` 和它的计算结果分别可见，模型的错误通常在公式里。

---

## 已知限制

- **扫描件需要 OCR**：没有文字层的 PDF 会明确报告，但不会自动 OCR。
- **跨页续表是启发式判断**：列数一致且页码相邻时才合并，会在输出里说明，需要人工确认。
- **超长文档分段**：默认 PDF 读前 200 页、Excel 每表前 5000 行，超出会提示而不是静默截断。
- **复杂合并单元格**：docx 的合并单元格会被展开成重复文本，不保留 span 语义。

---

## 许可

MIT
