# 综合推荐

## 按格式的初步选择

| 输入 | primary | fallback / preview | 不作为 Evidence 权威的输出 |
| --- | --- | --- | --- |
| Markdown | `markdown-it-py`，固定插件配置与原始切片 | Docling 仅用于复杂跨格式结构；MarkItDown 为 Agent preview | 任何转换后的 Markdown |
| 文本型 PDF | `pdfplumber`，page + word / block / bbox | PyMuPDF 仅在许可获批后；Tika 为检测/基础文本 fallback | MarkItDown / 纯 text |
| 多栏、表格、公式 PDF | Docling 的结构化 JSON 候选，先完成离线模型验证 | MinerU 仅实验 / preview；Unstructured 为 fallback | Markdown preview |
| 扫描 PDF / 文档图片 | PaddleOCR 的本地锁模型候选 | Tesseract 的 TSV / hOCR / ALTO 本地 fallback | 无坐标 OCR text |
| XLSX | `openpyxl` 读取为自有结构 JSON | Apache POI 处理 `.xls`、流式及部分公式；LibreOffice 仅 render / repair | CSV / Markdown preview |
| PPTX | `python-pptx` 读取 slide / shape / notes | Docling 统一结构 preview；MarkItDown preview | 幻灯片 Markdown |
| 图片 | `file` / `sips` 做结构初筛；restricted policy 独立门禁 | PaddleOCR / Tesseract 仅受控 OCR；视觉理解后置 | 场景描述或 OCR 文字本身 |

## ArcheOS 只应负责的部分

第三方承担格式解析、文字识别和布局模型。ArcheOS 不重写这些基础能力，而是提供：

1. **Adapter 边界**：只读接收已验证 Managed Source；固定工具、模型、配置与网络策略；拒绝 URL、插件和 cloud fallback。
2. **Evidence locator**：让派生结果回到不可变 Source，而不是回到 preview 文本。
3. **完整性与安全**：统一 warning、completeness、资源限制、模型 provenance、临时文件清理与 fail-closed 行为。
4. **隐私路由**：把 restricted 保持在本地、无网络的路径，未知情况交由人工。

不得把第三方 document model、token、cell ID、置信语义或生命周期升级为 Core 模型。

## 下一张 Normalized Representation contract 应固定的最小字段

以下为 #29 的输入建议，不是本 Issue 新建的 runtime contract：

```text
source_id
source_content_hash
representation_kind
adapter_name
adapter_version
model_or_artifact_version (可选)
configuration_fingerprint
network_policy
created_at
status
warnings
completeness
locator
```

`locator` 按格式扩展但必须回到 Source：

- PDF：page（1-based）、bbox、坐标系、page 尺寸、block / word / table / cell ordinal；
- Markdown：line range、UTF-8 byte range、固定 parser/plugin configuration；
- XLSX：sheet name/index、A1 cell/range、formula 与 cached value、merge master、visibility、drawing anchor；
- PPTX：slide ID/index、shape ID/type、bbox、paragraph/run 或 table cell、notes flag；
- 图片：image/frame/page index、bbox/polygon、orientation/transform chain、text span、confidence。

## 第一批与后置 Adapter

优先进入下一轮验证：Markdown (`markdown-it-py`)、文本型 PDF (`pdfplumber`)、XLSX (`openpyxl`)、PPTX (`python-pptx`) 和图像 / PDF 的轻量本地结构 preflight。它们应先使用 synthetic fixtures 建立 locator、损坏输入、稳定性和 privacy 回归。

后置：Docling、PaddleOCR、Tesseract 的正式模型路线，直到完成模型许可、离线预取、Apple Silicon、资源上限、断网和 restricted 样本验证；复杂 PDF / 公式、手写、SmartArt、宏、外链、图片语义理解都必须保留 warning 或人工路径。MinerU、PyMuPDF、Exiv2、ImageMagick、Ollama / `llama.cpp` 不应在未完成专门许可证或安全评估前成为首批默认依赖。
