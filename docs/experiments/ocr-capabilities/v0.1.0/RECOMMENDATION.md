# Recommendation

## 决定

**不创建 production OCR Adapter Issue。** 两个指定候选均未通过最低门禁：Tesseract 5.5.3 已通过本机合成 PNG 与 TSV 字段可用性基线，但没有强制断网审计、固定 traineddata artifact 记录、资源测量或 PDF rasterization 安全路线；PaddleOCR 3.7.0 未完成本机 runtime、固定模型或离线试跑。依照 Issue #39，不扩展到其他 OCR 或 cloud 候选。

因此 production primary / fallback 均为 `none`，不是 “PaddleOCR primary、Tesseract fallback”。Tesseract 可以保留为下一次受控实验的优先修复候选，但 #28 的候选建议不能越过本实验结果。

## 下一次受控实验必须补齐

1. 记录 **Tesseract 5.5.3** arm64 runtime 与 `eng` / `chi_sim` traineddata 的精确 artifact 版本、大小、许可证和摘要；正常 OCR 与缺失模型两条路径都在强制断网环境运行。
2. 完成 **PaddleOCR 3.7.0** 及其兼容 `paddlepaddle` arm64 runtime 的干净安装；从官方来源预取明确的检测、识别和方向模型，锁定 artifact 与许可证后在禁网环境运行。
3. 对五类合成用例取得重复结果：中英文、orientation、低对比度、简单表格、无文字和损坏输入；仅记录聚合准确性、时延、内存和字段存在性，不记录 OCR 正文。
4. 明确 PDF rasterization 的独立安全预检、像素/页数/时间/内存/临时空间上限，并通过输入读取、失败清理和不出网审计。
5. 只有至少一个候选能在上述条件下稳定输出 `text`、page/image index、bbox/polygon、OCR score、engine/runtime version、model artifact version、orientation/transform（可得时）后，Architect 才能决定是否创建一个单独 production Adapter Issue。

## 已知风险

- 本机 Tesseract 构建链接 `libcurl`；未来必须用真实网络隔离证据而非依赖“CLI 通常本地运行”的假设。
- OCR 的 `confidence` / `score` 仅是识别引擎信号，不是来源真实性、人物身份或业务事实的概率。
- 本实验未授权真实 restricted 样本；这不是缺陷，真实资料进入任何路径前仍需满足 `OFFLINE_SECURITY.md` 的条件。
