# 基准计划

## 原则

基准只判断候选是否能在未来被隔离在 Adapter 后使用，不把第三方输出升级为 Source、Evidence 或 ArcheOS Core。每次运行只读取少量已选样本；原文件不移动、不复制、不修改，临时输出必须位于系统临时目录并清理。

## 样本与脱敏记录规则

本轮从产品负责人授权的本地样本根目录只读抽取代表类别。Git 中只记录下列聚合范围：

| 类别 | 计划 | 本轮状态 |
| --- | --- | --- |
| Markdown | 2 个 | not available in sample |
| 文本型 PDF | 2 个 | 已做只读结构基准 |
| 扫描或混合 PDF | 1 个 | not available in sample |
| XLSX | 2 个 | 已做只读结构基准 |
| PPTX | 1 个 | 已做只读结构基准 |
| 图片 | 场景与文档或 restricted 样本 | 已做 3 个图片结构元数据检查；未 OCR |

不得记录真实路径、名称、正文、图片、EXIF 值、hash 或业务实体。

## 命令模板

命令仅用于受控临时目录；执行前必须确认工具不会联网，或在禁网环境中运行。

```bash
# 结构初筛；不读取业务正文
file --brief <managed-local-file>
sips -g format -g pixelWidth -g pixelHeight <managed-local-image>

# 文本 PDF 的 page / word / bbox 聚合（Adapter 原型的临时实验）
python -m pdfplumber <managed-local-pdf>

# XLSX / PPTX 的只读结构盘点；不得 load-save
python -m openpyxl <managed-local-xlsx>
python -m pptx <managed-local-pptx>

# 仅在本地语言包与模型已锁定、禁网已验证时：
tesseract <managed-local-image> stdout tsv
```

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
