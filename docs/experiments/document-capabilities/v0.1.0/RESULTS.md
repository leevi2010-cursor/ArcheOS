# 匿名本地基准结果

## 执行环境与边界

平台为 macOS / Apple Silicon。本轮对少量授权样本进行本地只读解析；工具为产生结构指标读取了真实文件字节。原文件未被写回、移动或复制；报告和 Git 不记录或提交正文、文件名、路径、hash、图像、EXIF 值或业务实体。除确认结构结果所必需的最小人工检查外，未进行业务内容分析。未运行会自动下载模型、访问云 API 或无法确认网络边界的候选。

状态定义：`actually_run` 表示已在本地只读完成；`not_run` 表示本轮未执行；`not_available` 表示本轮没有合适授权样本；`blocked_by_network_or_model_gate` 表示工具可能下载模型或联网，未满足门禁而未运行。

## 已完成的聚合结果

| 格式 | 样本聚合 | 工具与结果 | 结论 |
| --- | --- | --- | --- |
| 文本型 PDF | 2 份 | `pdfplumber 0.11.10`：34 / 9 pages、6411 / 523 words、1 / 9 tables，249 / 156 ms | `actually_run`；提供 page、word、table 聚合，未评估语义正确性 |
| XLSX | 2 份 | `openpyxl 3.1.5`：17 / 16 sheets、均无 hidden sheets、469 / 330 image parts、34 / 30 drawing parts，1010 / 61 ms | `actually_run`；未遍历 cell，不宣称公式、合并或 anchor 已验证 |
| PPTX | 1 份 | `python-pptx 1.0.2`：48 slides、212 recursive shapes、0 notes slides，452 ms | `actually_run`；shape 结构可定位，未做内容分析 |
| 图片 | 3 个 JPEG | `file` 与 `sips` 成功读取 MIME / 格式 / 尺寸 / alpha 结构字段；未 OCR、未输出 EXIF 或像素 | `actually_run`；不证明 scene / document / restricted 分类准确 |

## 未验证或不可用

- Markdown 为 `not_available`；本轮授权样本根未发现 Markdown。
- 扫描或混合 PDF为 `not_available`；OCR 为 `blocked_by_network_or_model_gate`，未实测。
- Tesseract、PaddleOCR、Docling、MinerU、Unstructured、Apache Tika/POI 在真实样本上的模型下载、断网运行、峰值内存与 Apple Silicon 吞吐为 `not_run` 或 `blocked_by_network_or_model_gate`。
- LibreOffice 转换、公式计算、合并单元格和图片 anchor 的完整准确率为 `not_run`。
- 图片 OCR、restricted 路由混淆矩阵与网络连接审计为 `not_run` 或 `blocked_by_network_or_model_gate`。

## 失败与完整性表达

后续 Adapter 不得把空输出表述为成功。缺页、密码、损坏、公式未计算、SmartArt、外部链接、宏、超大图片、解压比例异常、模型缺失或网络未禁用时，都必须保留 warning / completeness 并 fail closed 到人工或受控 fallback。
