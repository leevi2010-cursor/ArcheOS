# 匿名本地基准结果

## 执行环境与边界

平台为 macOS / Apple Silicon。本轮未固定或选择可复现的授权样本集，因此没有真实样本 benchmark 结果。未来本地受控工具若对真实样本做只读解析，会为产生结构指标读取文件字节；原文件不得被写回、移动或复制。报告和 Git 不记录或提交正文、文件名、路径、hash、图像、EXIF 值或业务实体；除确认结构结果所必需的最小人工检查外，不进行业务内容分析。未运行会自动下载模型、访问云 API 或无法确认网络边界的候选。

## 已完成的聚合结果

| 格式 | 样本聚合 | 工具与结果 | 结论 |
| --- | --- | --- | --- |
| 所有类别 | 未固定可复现授权样本集 | 未运行真实样本解析；仅保留 planned command 与最小脚本 | 不得将候选能力写成已实测支持 |

## 未验证或不可用

- Markdown、PDF、XLSX、PPTX 与图片均未运行；本轮不伪造结果。
- 扫描或混合 PDF、OCR 质量均未实测。
- Tesseract、PaddleOCR、Docling、MinerU、Unstructured、Apache Tika/POI 在真实样本上的模型下载、断网运行、峰值内存与 Apple Silicon 吞吐未验证。
- LibreOffice 转换、公式计算、合并单元格和图片 anchor 的完整准确率未验证。
- 图片结构预检、OCR、restricted 路由混淆矩阵与网络连接审计未验证。

## 失败与完整性表达

后续 Adapter 不得把空输出表述为成功。缺页、密码、损坏、公式未计算、SmartArt、外部链接、宏、超大图片、解压比例异常、模型缺失或网络未禁用时，都必须保留 warning / completeness 并 fail closed 到人工或受控 fallback。
