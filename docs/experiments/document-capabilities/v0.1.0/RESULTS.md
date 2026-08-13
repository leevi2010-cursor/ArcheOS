# 匿名本地基准结果

## 执行环境与边界

平台为 macOS / Apple Silicon。本轮使用少量代表文件进行只读检查，未写回原目录，未读取或记录业务正文、文件名、路径、hash、图像像素或 EXIF 值。未运行会自动下载模型、访问云 API 或无法确认网络边界的候选。

## 已完成的聚合结果

| 格式 | 样本聚合 | 工具与结果 | 结论 |
| --- | --- | --- | --- |
| 文本型 PDF | 2 份，页数为 1 / 4 | PyMuPDF：0.035s / 0.014s，得到 55 / 167 words、41 / 53 blocks；pdfplumber：1.014s / 0.068s，得到 67 / 193 words、5 / 0 tables；MarkItDown：0.707s / 0.067s，只生成 277 / 3484 chars preview | 结构提取与 preview 明显不同；Markdown preview 不足以定位 Evidence |
| XLSX | 2 份，大小约 102 / 139 MB | openpyxl read-only metadata：0.031s / 0.021s；17 / 16 sheets、均无 hidden sheet；OOXML 包含 468 / 329 image parts、16 / 14 drawing parts、无 table parts | 必须保留 workbook 结构；未全表遍历，不能宣称公式、合并或 anchor 已验证 |
| PPTX | 1 份，约 52 MB | python-pptx：0.023s，48 slides、212 shapes、98 text shapes、114 pictures；MarkItDown：0.040s、4619 preview chars | shape 结构可定位；Markdown 仅适合作 preview |
| 图片 | 3 个 JPEG | `file` 与 `sips` 均读取 MIME、格式、尺寸、色彩空间和 DPI；未 OCR、未读取 EXIF 值或像素 | 本机结构元数据路径可用；不证明 scene/document/restricted 分类准确 |

## 未验证或不可用

- Markdown 样本数为 0；本轮不伪造结果。
- 未发现适合本轮低文本扫描 PDF 样本；OCR 质量未实测。
- Tesseract、PaddleOCR、Docling、MinerU、Unstructured、Apache Tika/POI 在真实样本上的模型下载、断网运行、峰值内存与 Apple Silicon 吞吐未验证。
- LibreOffice 转换、公式计算、合并单元格和图片 anchor 的完整准确率未验证。
- 图片 OCR、restricted 路由混淆矩阵与网络连接审计未验证。

## 失败与完整性表达

后续 Adapter 不得把空输出表述为成功。缺页、密码、损坏、公式未计算、SmartArt、外部链接、宏、超大图片、解压比例异常、模型缺失或网络未禁用时，都必须保留 warning / completeness 并 fail closed 到人工或受控 fallback。
