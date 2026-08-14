# Results

## 运行条件

- 日期：2026-08-14；
- 输入：13 个公开匿名 unit，其中 5 个长文本 unit 被确定性重复 24 次，形成超过 10,000 个中文字符的长上下文；
- 每条路线：5 次实际独立调用，固定为 short/table、long、multi-a、multi-b、multi-c；每次 120 秒 deadline，无自动 retry；
- `p50/p95`：每路线 `n=5`，仅用于本次兼容性观察，不代表生产延迟承诺；
- 不保存模型原始输出；临时输入和输出均已清理。

## 匿名聚合指标

| 指标 | pinned SDK | latest SDK（实际同版） | external Agent handoff |
| --- | ---: | ---: | ---: |
| provider version / runtime | `0.144.4` | `0.144.4` | `codex-cli 0.147.0` |
| completed rate | 5/5 | 5/5 | 5/5 |
| structured output valid rate | 5/5 | 5/5 | 5/5 |
| latency p50 / p95 | 12.874s / 20.771s | 15.384s / 25.330s | 27.915s / 43.334s |
| route elapsed total | 69.063s | 84.236s | 145.537s |
| timeout rate | 0/5 | 0/5 | 0/5 |
| schema failure rate（正式运行） | 0/5 | 0/5 | 0/5 |
| runtime failure rate（正式运行） | 0/5 | 0/5 | 0/5 |
| units total / eligible / excluded | 13 / 13 / 0 | 13 / 13 / 0 | 13 / 13 / 0 |
| candidate entry count | 11 | 11 | 10 |
| residue entry count | 2 | 2 | 3 |
| candidate + residue coverage | 13 | 13 | 13 |
| unaccounted units | 0 | 0 | 0 |
| fixture disposition mismatches | 3 | 3 | 2 |
| package strict verify | pass | pass | pass |
| temporary artifact cleanup | verified | verified | verified |

## 语义人工检查指标的边界

本轮未获真实资料验证授权，因此：

- `manual_missed_information`：**不适用（真实资料未运行）**；合成 coverage 为 13/13，不可替代真实遗漏率。
- `manual_false_information`：**不适用（真实资料未运行）**；作为安全代理，fixture 的不确定/冲突分类偏差分别为 3、3、2 个 unit。

这意味着三条路线都没有 P0 coverage 丢失或 P1 locator/schema 失败的合成证据，但仍存在不应被自动吸收的语义分类风险。

## Failure observations

1. 初始 preflight 中，app-server 拒绝 schema 的 `uniqueItems` keyword，并返回明确的 `invalid_json_schema`。harness 已把唯一性检查移到本地 strict validator，随后正式运行通过；没有真实输入参与该过程。
2. 先前真实 text-PDF 未完成的原因仍不可确定。当前合成长输入成功，最多说明它不是所有长输入都会失败；不能将原因锁定为 SDK、runtime、模型、schema 或 document shape。
3. 外部 handoff 的临时目录需要显式跳过 Git-repository 检查；若该前置条件缺失，命令会快速明确失败而非无限等待。
4. 本次已实际跨 5 个独立调用聚合 coverage；每个 batch 都单独完成 strict validation，任一调用失败都会保留其 failure category 并使 route 结果 fail closed。
