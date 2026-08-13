# 基准计划

## 原则

基准只判断候选是否能在未来被隔离在 Adapter 后使用，不把第三方输出升级为 Source、Evidence 或 ArcheOS Core。每次运行只读取少量已选样本；原文件不移动、不复制、不修改，临时输出必须位于系统临时目录并清理。

## 样本与脱敏记录规则

本轮从产品负责人授权的本地样本根目录只读抽取代表类别。Git 中只记录下列聚合范围：

| 类别 | 计划 | 本轮状态 |
| --- | --- | --- |
| Markdown | 2 个 | not_available；授权样本根无 Markdown |
| 文本型 PDF | 2 个 | actually_run；`pdfplumber` 只读聚合 |
| 扫描或混合 PDF | 1 个 | not_available；未选择低文本扫描样本 |
| XLSX | 2 个 | actually_run；`openpyxl` 只读聚合 |
| PPTX | 1 个 | actually_run；`python-pptx` 只读聚合 |
| 图片 | 场景与文档或 restricted 样本 | actually_run；3 个 JPEG 的结构检查，未 OCR |

不得记录真实路径、名称、正文、图片、EXIF 值、hash 或业务实体。

## 命令模板

命令仅用于受控临时目录；执行前必须确认工具不会联网，或在禁网环境中运行。

```bash
# actually_run：仅输出匿名聚合，不输出输入路径或内容。
python3 docs/experiments/document-capabilities/v0.1.0/structural_benchmark.py \
  --format pdf <managed-local-pdf>
python3 docs/experiments/document-capabilities/v0.1.0/structural_benchmark.py \
  --format xlsx <managed-local-xlsx>
python3 docs/experiments/document-capabilities/v0.1.0/structural_benchmark.py \
  --format pptx <managed-local-pptx>
python3 docs/experiments/document-capabilities/v0.1.0/structural_benchmark.py \
  --format image <managed-local-image>
```

`structural_benchmark.py` 是本次保存的最小、只读 benchmark script；它延迟导入候选库、只向 stdout 输出聚合计数与耗时、不写入输入目录。本轮以临时虚拟环境中的固定版本实际运行其 PDF / XLSX / PPTX / 图片分支；图片分支受控调用本地 `file` 与 `sips`，只保留 MIME、格式、尺寸和 alpha 结构字段，不把临时环境或真实输入写入仓库。Tesseract、PaddleOCR、Docling、MinerU、Unstructured、Tika、POI、LibreOffice 与 MarkItDown 的真实样本命令均为 `not_run` 或 `blocked_by_network_or_model_gate`，必须先完成各自的离线、模型和许可证门禁。

## 指标

- 安装成功、首次启动、模型准备或缓存体积；
- 单文件耗时与可测的峰值内存；
- 断网成功率、实际连接数和任何模型下载；
- 结构保留、页/行/cell/bbox locator 可用率；
- 表格、图片、公式、合并单元格、notes 的保留情况；
- warning、completeness、重复运行稳定性与失败清理；
- 损坏、密码、宏、外部链接、巨图或解压炸弹的 fail-closed 行为。

不使用单一总分；每种格式分别给出推荐、有条件推荐、不推荐或未验证。

## 隐私与网络门禁

真实或 restricted Source 前，必须固定依赖、模型和配置。模型下载、远程 API、第三方插件、URL 输入和 cloud fallback 默认关闭；工具无法确认离线时不运行敏感样本。OCR 与视觉输出是可替换派生表示，不能替代原始字节或独立成为 Evidence 权威。
