# 实际实验结果

## 环境

| 项目 | 结果 |
| --- | --- |
| 平台 | macOS 27.0，arm64，Apple M5 |
| Tesseract | Homebrew `tesseract 5.5.3`；构建输出包含 `Found NEON`，未使用 Rosetta |
| 语言包 | Homebrew `tesseract-lang 4.1.0`；本机可列出 `eng` 与 `chi_sim` |
| PaddleOCR | 目标包 `paddleocr==3.7.0`；CPython 3.12 arm64 临时环境完成依赖解析，但公开包预取未完成。干净临时环境的 `--offline` 重试因缺少 `opencv-contrib-python 4.10.0.84` arm64 wheel 失败，未安装可运行 package 或权重 |

## 合成基线

合成用例规格见 `fixtures/synthetic-ocr-cases.json`。实际生成并试跑了包含中英文普通印刷体与简单表格的无敏感图像，并从同一图像生成一页 scan PDF；没有保存其像素、文本、路径或摘要。

| 候选 | 成功路径 | bbox / polygon + confidence | orientation、低对比度、无文字、损坏输入 | 资源与失败 |
| --- | --- | --- | --- | --- |
| Tesseract 5.5.3 | `partial_pass`：合成 PNG 的普通印刷、旋转、低对比度和无文字路径均完成；一页 scan PDF 不能由 Tesseract 直接读取 | TSV header 与 word rows 实测存在 bbox 和 `conf`；普通 / 旋转 / 低对比度的 word rows 均为 10，空白图仍为 1，说明无文字误识别必须保留 warning | 合成普通印刷、orientation、低对比度、无文字和损坏输入均已实际试跑；未评估文字正确率，因此不报告虚假的准确率 | 损坏输入、模型缺失与直接 PDF 输入均以非零状态失败；未取得可信峰值内存，后续仍需设限 |
| PaddleOCR 3.7.0 | `blocked_by_incomplete_prefetch`：未完成 runtime 安装与模型预取；离线安装以非零状态失败 | `not_run` | `not_run` | 未取得资源、首次启动或 Apple Silicon accelerator 证据 |

## 离线与模型缺失

- Tesseract 的空 `--tessdata-dir` 试跑退出失败；没有自动下载语言包的可见行为。此项仅覆盖该本地 CLI 路径，不能证明整个主机网络已隔离。
- Tesseract 的预取语言包在正常合成 PNG 上成功；本轮没有独立可审计的强制禁网证据，因此仍未满足“预取后、禁网下正常 OCR 成功”。
- PaddleOCR 的 runtime / model prefetch 与禁网运行均未完成。未把临时下载缓存当作模型固定证据。

## 输出 contract 可行性

两者的公开接口在原则上可映射到未来 Representation locator：Tesseract TSV 可提供 image/page 顺序、bbox、text 与 `conf`；PaddleOCR API 设计可提供 polygon/bbox、text、score 和方向信息。还必须由未来 Adapter 添加 engine、runtime version、固定 model artifact、配置、坐标系 / transform 和 warning/completeness。

这不是 production contract 变更，也不是对稳定输出的通过结论；本机没有任何候选产出可验证的完整字段集。
