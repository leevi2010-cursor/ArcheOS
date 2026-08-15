# 微信语义质量门禁 v0.1.0

这是 Issue #76 的 experiment-only harness。它复用 #31 的 canonical Analysis Unit、batch 与 validator；不调用 `extract`、package publish、durable ingestion、Atomic Information 或 World Model。

默认运行仅输出 synthetic 状态，绝不读取 Representation 或启动 Provider：

```sh
python docs/experiments/semantic-quality-wechat/v0.1.0/run_quality_gate.py --synthetic
```

真实流程仅可在 Reviewer 的 `REAL_CALL_APPROVED` 后使用受控本地适配器执行。one-shot marker 会在 `codex exec` 前以 0600 原子落盘；timeout、非零退出或无效输出均消耗调用。真实 request/result/review packet 仅保存于 0700 临时目录及用户本地位置，不能提交。
