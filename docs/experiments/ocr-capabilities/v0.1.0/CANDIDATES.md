# 候选、许可证与 artifact 门禁

本表的链接只指向官方项目或官方包索引；它不是法务意见，也不把任何第三方类型、ID、置信语义或生命周期引入 ArcheOS Core。

| 候选 | runtime 与许可证 | 模型 / traineddata 来源与许可证 | 固定与网络结论 | 实测状态 |
| --- | --- | --- | --- | --- |
| Tesseract 5.5.3 | [官方 5.5.3 源码](https://github.com/tesseract-ocr/tesseract/tree/5.5.3) 与 [Apache-2.0 LICENSE](https://github.com/tesseract-ocr/tesseract/blob/5.5.3/LICENSE)；本机通过 Homebrew 5.5.3 arm64 bottle 安装 | [官方 tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) 的 `eng` / `chi_sim` traineddata；仓库 [Apache-2.0 LICENSE](https://github.com/tesseract-ocr/tessdata_fast/blob/main/LICENSE)；本机语言包 formula 为 4.1.0。观察大小：`eng` 4,113,088 bytes，`chi_sim` 2,469,156 bytes | 可通过受控 `--tessdata-dir` 指向预置目录；缺失目录的试跑失败，没有自动取得 traineddata。公式提供的语言包版本不是 traineddata 的内容 hash 或单文件版本，未记录为可发布 artifact lock | runtime、语言包与 synthetic PNG OCR 成功；直接 PDF 不支持，见结果 |
| PaddleOCR 3.7.0 | [官方 3.7.0 tag](https://github.com/PaddlePaddle/PaddleOCR/tree/v3.7.0) 与 [Apache-2.0 LICENSE](https://github.com/PaddlePaddle/PaddleOCR/blob/v3.7.0/LICENSE)；包索引解析到 3.7.0 | [官方 OCR pipeline 文档](https://paddlepaddle.github.io/PaddleOCR/main/en/version3.x/pipeline_usage/OCR.html) 说明模型目录与下载路径；本轮未取得可审计的本地权重，因此不能声称任何权重许可证、大小或 artifact 标识 | 未指定并固定本地模型目录时存在下载风险；干净临时环境的 `--offline` 安装因缺少 `opencv-contrib-python 4.10.0.84` arm64 wheel 而失败，不能进入断网模型试跑 | `blocked_by_incomplete_prefetch` |

## 已审计的局限

- Tesseract 二进制构建信息显示链接了 `libcurl`；这不等于本次观察到联网，也不构成“永不联网”的证明。未来 Adapter 必须把模型目录、语言、输入类型和网络隔离作为显式配置。
- Homebrew bottle checksum 与 formula 元数据只能证明本次 runtime 分发来源；它们不替代锁定每个 traineddata 或 Paddle 模型 artifact 的版本、大小、许可证和可验证摘要。
- PaddleOCR 依赖解析与下载经本机代理发生；由于安装未完成，不能将缓存内容当作已预取、可离线使用的模型。
