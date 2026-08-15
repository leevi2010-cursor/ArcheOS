# Codex CLI 执行诊断 v0.1.0

这是 Issue #78 的公开 synthetic-only 诊断。它只验证当前 `codex exec` route 是否能返回严格结构化结果，用来定位 #76 的 `provider_nonzero_exit`；不读取任何 Managed Source、Representation、#76 request/raw result/marker 或其他业务资料，也不写入 `Atomic Information`、World Model 或 production runtime。

## 固定矩阵

最多 6 次正式 Codex model call，按 A–F 顺序执行：基础 runtime/auth、最小 schema、#66 同目录形态、#76 双目录形态、修正后 #76 小 contract、修正后 19-anchor 近真实规模 contract。前置失败已能定位时，harness 会停止不再调用无意义 case。

每个 case 仅使用已提交 public synthetic fixture 或脚本内公开 synthetic 文本。诊断记录包含 version、命令形态、exit code、经凭据脱敏并截断的 stdout/stderr、result file、JSON/strict validator、耗时和失败分类。`local-diagnostics/` 永远不提交。

## 执行

```bash
python3 docs/experiments/codex-cli-diagnosis/v0.1.0/run_diagnosis.py \
  --report /tmp/archeos-issue78-report.json \
  --timeout 120
```

运行后将 `/tmp` 中完整 public-synthetic report 审核为可提交的匿名/公开结论，再更新 `RESULTS.md`。禁止把环境变量、token 或本机私人路径写入 GitHub。
