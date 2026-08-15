# Results

## 运行条件

- 日期：2026-08-15；
- 系统：macOS 27.0 / Apple Silicon arm64；
- External Agent：`codex-cli 0.147.0`；
- 输入：2 个公开合成 Analysis Units，包含 5 类唯一 synthetic sensitive value；
- transport：request 经 stdin；child argv 只有 CLI 参数和随机 protected temp path；
- observer：macOS `ps`，10ms interval；
- 单次 deadline：120 秒；无自动 retry / fallback；
- 未读取真实 Source / Representation / #60 样本或日志。

## Failure matrix

18 个 focused tests 全部通过。它们证明 harness 对 normal、timeout、runtime failure、no/empty result、invalid JSON、unknown/duplicate/incomplete coverage、wrong fingerprint、argv/env canary leak、permissions、cleanup、Readback、audit privacy、no-ingestion 与非 committed input 都按预期 fail closed。

## 实际 External Agent synthetic Gate

执行了两次独立 synthetic Processing Run，用于确认观测可重复；每个 run 都只有一次 External Agent 尝试。

| 指标 | Run 1 | Run 2 |
| --- | ---: | ---: |
| External structured result strict validation | passed | passed |
| eligible units | 2 | 2 |
| observed process count | 13 | 8 |
| process-tree snapshots | 339 | 405 |
| argv sensitive hits | 5 | 5 |
| environment sensitive hits | 0 | 0 |
| temp permission | verified | verified |
| temp cleanup | verified | verified |
| audit Readback | verified | verified |
| result published | false | false |
| package published | false | false |
| information ingested | false | false |
| execution status | failed | failed |
| failure category | `privacy_boundary_violation` | `privacy_boundary_violation` |

一次额外的只输出匿名定位标签的 diagnostic 观测确认：5 类命中都来自名为 `Codex` 的受控后代进程 argv，而不是 environment，也不是 harness 根命令。该 diagnostic 没有保存或打印 raw argv、environment 或 synthetic sensitive value。

## Gate 判断

External Agent 已返回 strict-valid structured result，但这不构成成功：runtime 的后代进程把 stdin 中的 5 类 synthetic sensitive value 全部展开到了 process argv。按 Issue #66 PASS Gate，任一命中都必须 route FAIL。

因此当前结论是：

> **External Agent Handoff transport not production viable.**

这不是语义质量结论，也不能通过保留 structured result、转写 Residue、自动重试或降低 process metadata Gate 来规避。

## 观测边界

- raw process metadata 未持久化；公共结果只保存匿名计数；
- observer 是采样而非内核 exec audit；本次泄漏在两次运行中都被捕获，但未来 route 若声称 PASS，Architecture Review 仍需评估短命子进程覆盖；
- 没有验证其他 External Agent runtime、direct model API 或 production integration；
- 没有运行真实资料，因此没有任何真实 semantic quality Evidence。
