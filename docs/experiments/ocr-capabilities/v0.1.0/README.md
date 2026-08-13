# 本地 OCR 能力门禁实验 v0.1.0

本目录是 Issue #39 的脱敏实验记录。它只验证 `PaddleOCR 3.7.0` 和 `Tesseract 5.5.3` 的本地门禁；不含 production OCR Adapter、Adapter registry、Atomic Information、World Model 或真实资料。

## 结论级别

当前结论为 **blocked，不建议创建 production OCR Adapter Issue**。Tesseract 已完成 arm64 安装、合成 PNG 试跑与显式 TSV bbox/confidence 文件验证；若进程 exit=0 但 TSV 缺失或无效，脚本会 fail closed。它仍缺少强制断网审计、artifact lock、资源与 PDF rasterization 门禁；PaddleOCR 虽解析到固定包版本，但未能完成可复现的离线 runtime 安装、模型预取或断网运行。两者均未达到最低门禁，仍按本 Issue 限制不扩展候选。

- [候选与许可](CANDIDATES.md)
- [离线与 restricted 路线](OFFLINE_SECURITY.md)
- [实际结果](RESULTS.md)
- [推荐](RECOMMENDATION.md)
- [实验 manifest](manifest.json)
- [合成用例](fixtures/synthetic-ocr-cases.json)
- [可重复门禁脚本](run_synthetic_ocr_gate.sh)

脚本要求调用者通过 `TESSDATA_DIR` 显式传入已经审计的本地 traineddata 目录，避免把机器路径、自动下载或默认 cache 固化到仓库。

所有可运行输入仅在临时目录生成；Git 不保存真实资料、真实路径、文件名、正文、图像、hash、业务实体或凭证。
