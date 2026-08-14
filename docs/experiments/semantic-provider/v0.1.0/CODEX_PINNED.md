# A. Pinned Codex Python SDK / app-server

## 设置

- SDK：`openai-codex==0.144.4`；
- 独立 Python 3.13 环境；
- `ApprovalMode.deny_all`、`Sandbox.read_only`、ephemeral thread；
- 120 秒硬 deadline；
- 5 次独立合成 package 调用：short/table、long、multi-a、multi-b、multi-c；不读取 Source 或任何工作区业务数据。

## 结果

5 次正式合成调用总计 69.063 秒完成（p50 12.874 秒，p95 20.771 秒）。每次输出都通过严格 JSON 验证，跨调用 coverage 为 13/13，`unaccounted_eligible_units=0`，没有 timeout 或 runtime failure。fixture oracle 中有 3 个刻意不确定/冲突单元被归入不符合预期的一侧。

这证明当前 pinned SDK 能在本环境完成本实验的合成 structured output；它**不**证明真实 Representation package 的语义质量，也不能推翻此前真实 text-PDF 未完成的观察。

## 失败与恢复规则

SDK 调用超时或异常时，harness 会结束整个子进程组、清理临时目录并返回 `timeout` 或 `runtime_failure`。失败不产生 Candidate、Residue、Atomic Information 或任何 World Model 写入；本实验不执行自动 retry。

## 结论

保留为实验路线。它目前没有足够真实资料证据成为正式 production default。
