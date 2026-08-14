# C. External Agent Handoff

## 最小边界

```text
versioned analysis input package
→ external Agent execution
→ strict structured result
→ ArcheOS validation / Candidate + Residue package
```

ArcheOS 在此路线中只拥有 package、schema、coverage 验证与失败记录；不拥有外部 Agent 的运行时、推理策略或凭据。本实验的调用使用 `codex exec`，read-only sandbox、ephemeral 模式和明确的“不调用工具”指令。临时工作目录不是 Git 仓库，因此还需要 `--skip-git-repo-check`；这只是启动条件，不能降低 sandbox 或扩大输入范围。

## 结果

本机 `codex-cli 0.147.0` 对 5 个独立 package 总计 145.537 秒完成（p50 27.915 秒，p95 43.334 秒）。每个 package 都返回可验证 structured output，跨调用 13/13 单元被完整且不重复地覆盖，`unaccounted_eligible_units=0`，没有 timeout/runtime failure。fixture oracle 中有 2 个不确定/冲突单元的分类不符合预期。

## 产品边界判断

该路线在结构上符合“ArcheOS 作为长期记忆、认知和 Context 增强层，而非自建 Agent runtime”的方向：执行由外部 Agent 承担，ArcheOS 保持统一输入、Evidence unit、Candidate/Residue 与 strict validation contract。

但 5 次合成成功不等于自动化可靠性。它仍依赖本机 Agent 安装、登录态和运行环境，尚无真实样本、并发、成本或审计保持期证据。因此不能直接成为 v1 production default。

## 最小失败规则

一份 package 只尝试一次；120 秒未完成即终止，异常则 fail closed。禁止自动 fallback/retry 和输出修补。若未来要对失败包人工指定另一条 Provider，必须保留相同 package identity、版本、失败原因与新的 Processing Run，而不是把失败改写成 Residue。
