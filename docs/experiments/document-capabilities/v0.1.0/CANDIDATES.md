# 候选清单

版本、许可证和维护迹象来自下列官方来源。本表只定义调查角色；未把任何候选接入生产 runtime。

| 格式 / 候选 | 官方来源与许可证 | 本地与网络边界 | 结构与 locator 能力 | 建议角色 |
| --- | --- | --- | --- | --- |
| `markdown-it-py` 4.2.0 | [官方仓库](https://github.com/executablebooks/markdown-it-py)、[v4.2.0](https://github.com/executablebooks/markdown-it-py/releases/tag/v4.2.0)，MIT；近期仍维护 | 纯 Python，无模型或网络路径 | CommonMark token；block token `map` 可给行范围，不提供列或字节范围 | Markdown primary |
| Microsoft `MarkItDown` 0.1.7 | [官方仓库](https://github.com/microsoft/markitdown)、[v0.1.7](https://github.com/microsoft/markitdown/releases/tag/v0.1.7)，MIT | 内置 converter 可本地；Azure、LLM image/OCR、YouTube 与插件可联网或计费 | headings、lists、tables、links 的轻量 Markdown；无稳定 page/cell/bbox locator | Agent preview |
| Apache Tika 3.3.2 | [下载](https://tika.apache.org/download.html)、[安全模型](https://tika.apache.org/security-model.html)，Apache-2.0 | Java 11+；server 必须本地隔离，禁 NetworkParser / pipes / 外部服务 | MIME、metadata、text/XHTML/recursive JSON；locator 不足以承担细粒度 Evidence | detection / fallback |
| Docling 2.119.0 | [官方仓库](https://github.com/docling-project/docling)、[格式](https://docling-project.github.io/docling/usage/supported_formats/)，MIT | macOS arm64 可用；首次模型使用可能下载，需预取、锁 artifacts、禁网 | hierarchy、text、table、picture、provenance、bbox、lossless JSON | 复杂格式候选 / preview |
| `pdfplumber` 0.11.10 | [官方仓库](https://github.com/jsvine/pdfplumber)、[许可证](https://github.com/jsvine/pdfplumber/blob/stable/LICENSE.txt)，MIT | 本地 Python；主要针对 machine-generated PDF | page、char、word、line、rect 与 table bbox | 文本型 PDF primary |
| PyMuPDF 1.27.1 | [官方仓库](https://github.com/pymupdf/PyMuPDF)、[许可](https://pymupdf.readthedocs.io/en/latest/about.html#license)，AGPL-3.0 或商业许可 | 本地；引入前须解决许可证边界 | 高性能文本、block、word、render 与 page locator | 有条件 fallback |
| MinerU | [官方仓库](https://github.com/opendatalab/MinerU)、[许可](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md) | 模型与服务行为需隔离验证 | PDF / image / Office 到 Markdown / JSON，多栏、表格与公式能力 | 实验 / preview；附加条款，非默认依赖 |
| Tesseract 5.5.3 | [官方仓库](https://github.com/tesseract-ocr/tesseract)、[CLI 文档](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html)，Apache-2.0 | 本地语言包已安装时可离线；需显式预置中文 traineddata | txt、PDF、hOCR、TSV、ALTO、box，可给 bbox | 扫描件和图片 OCR fallback |
| PaddleOCR 3.x | [官方仓库](https://github.com/PaddlePaddle/PaddleOCR)、[OCR pipeline](https://paddlepaddle.github.io/PaddleOCR/main/en/version3.x/pipeline_usage/OCR.html)，Apache-2.0 | 未指定本地模型目录时可能下载；须锁模型目录、禁网、禁云 fallback | polygon/bbox、score、方向与文档布局候选 | 中文 OCR 有条件 primary |
| Unstructured 0.24.1 | [发布](https://github.com/Unstructured-IO/unstructured/releases/tag/0.24.1)、[PDF 实现](https://github.com/Unstructured-IO/unstructured/blob/main/unstructured/partition/pdf.py)，Apache-2.0 | hosted API 与本地包必须分路；hi_res / OCR 需模型依赖 | fast 模式 page+coordinates；复杂模式依赖布局模型 | 通用 fallback / preview |
| `openpyxl` 3.1.5 | [官方文档](https://openpyxl.readthedocs.io/en/stable/)、[官方仓库](https://foss.heptapod.net/openpyxl/openpyxl)，MIT | 纯本地；严格只读 extraction，不可 load-save 原文件 | workbook / sheet / cell / range、formula、cached value、merge、hidden、drawing | XLSX structured primary |
| Apache POI 5.5.1 | [spreadsheet](https://poi.apache.org/components/spreadsheet/)、[slideshow](https://poi.apache.org/components/slideshow/)，Apache-2.0 | JDK；本机 Java runtime 未验证 | `.xls` / `.xlsx` / event model、部分公式计算、PPT | Excel / Office fallback |
| LibreOffice headless 26.x | [release notes](https://www.libreoffice.org/release-notes/)、[启动参数](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html)，MPL-2.0 | 本地 CLI；字体替代、重排与重算风险 | render / repair / 转换，不能可靠回指原 cell / shape | preview / conversion fallback |
| `python-pptx` 1.0.2 | [官方仓库](https://github.com/scanny/python-pptx)、[slides](https://python-pptx.readthedocs.io/en/latest/api/slides.html)，MIT | 本地 Python；读取 notes 前必须检查 `has_notes_slide` | slide、shape、bbox、text、table、picture、notes locator | PPTX structured primary |
| libmagic / `file` | [官方项目](https://www.darwinsys.com/file/)，BSD-like | 本地 magic-byte 检测 | MIME 初筛；不是安全或语义分类 | intake precheck |
| Exiv2 0.28.8 | [官方项目](https://exiv2.org/)、[许可证](https://github.com/Exiv2/exiv2/blob/main/LICENSE.txt)，GPL-2.0-or-later | CLI 可接受 URL，Adapter 必须只接受本地受管 bytes/path | EXIF/IPTC/XMP/ICC；原 metadata 可能敏感 | 实验 / 隔离 CLI，非嵌入默认 |
| ImageMagick | [identify](https://imagemagick.org/identify/)、[security policy](https://imagemagick.org/security-policy/)、[许可](https://github.com/ImageMagick/ImageMagick/blob/main/LICENSE) | 默认 policy 过宽且可能调用 delegate / URL；需 secure allowlist | 图像格式、尺寸、损坏迹象 | 仅受限 fallback |
| `llama.cpp` / Ollama | [llama.cpp](https://github.com/ggml-org/llama.cpp)、[Ollama FAQ](https://github.com/ollama/ollama/blob/main/docs/faq.mdx)，MIT | 模型 URL、`-hf`、cloud model、web search 都可能出站；必须禁云与锁定本地模型 | 视觉描述属于语义 preview，不是原图 locator | 后置实验，不进入首批 Adapter |

## 共同约束

候选的内部类型、ID、置信语义和输出格式不能进入 ArcheOS Core。每个未来 Adapter 均需显式记录工具、版本、模型、配置、网络策略、warning 与 completeness；第三方 Markdown、CSV 或 OCR 文本都只能作为可替换派生表示。
