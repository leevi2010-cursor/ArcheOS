# Results

## 运行条件

- 日期：2026-08-14；
- 输入：13 个公开匿名 unit，其中 5 个长文本 unit 被确定性重复 24 次，形成超过 10,000 个中文字符的长上下文；另外包含 short、table 和三个独立 batch 标记；
- 每条路线：1 次正式运行，120 秒 deadline，无自动 retry；
- `p50/p95`：每路线仅 `n=1`，数值相同但不具有统计意义；
- 不保存模型原始输出；临时输入和输出均已清理。

## 匿名聚合指标

| 指标 | pinned SDK | latest SDK（实际同版） | external Agent handoff |
| --- | ---: | ---: | ---: |
| provider version / runtime | `0.144.4` | `0.144.4` | `codex-cli 0.147.0` |
| completed rate | 1/1 | 1/1 | 1/1 |
| structured output valid rate | 1/1 | 1/1 | 1/1 |
| latency p50 / p95 | 43.021s / 43.021s | 16.942s / 16.942s | 29.113s / 29.113s |
| timeout rate | 0/1 | 0/1 | 0/1 |
| schema failure rate（正式运行） | 0/1 | 0/1 | 0/1 |
| runtime failure rate（正式运行） | 0/1 | 0/1 | 0/1 |
| units total / eligible / excluded | 13 / 13 / 0 | 13 / 13 / 0 | 13 / 13 / 0 |
| candidate entry count | 9 | 11 | 9 |
| residue entry count | 4 | 2 | 4 |
| candidate + residue coverage | 13 | 13 | 13 |
| unaccounted units | 0 | 0 | 0 |
| fixture disposition mismatches | 1 | 3 | 1 |
| package strict verify | pass | pass | pass |
| temporary artifact cleanup | verified | verified | verified |

## 语义人工检查指标的边界

本轮未获真实资料验证授权，因此：

- `manual_missed_information`：**不适用（真实资料未运行）**；合成 coverage 为 13/13，不可替代真实遗漏率。
- `manual_false_information`：**不适用（真实资料未运行）**；作为安全代理，fixture 的不确定/冲突分类偏差分别为 1、3、1 个 unit。

这意味着三条路线都没有 P0 coverage 丢失或 P1 locator/schema 失败的合成证据，但仍存在不应被自动吸收的语义分类风险。

## Failure observations

1. 初始 preflight 中，app-server 拒绝 schema 的 `uniqueItems` keyword，并返回明确的 `invalid_json_schema`。harness 已把唯一性检查移到本地 strict validator，随后正式运行通过；没有真实输入参与该过程。
2. 先前真实 text-PDF 未完成的原因仍不可确定。当前合成长输入成功，最多说明它不是所有长输入都会失败；不能将原因锁定为 SDK、runtime、模型、schema 或 document shape。
3. 外部 handoff 的临时目录需要显式跳过 Git-repository 检查；若该前置条件缺失，命令会快速明确失败而非无限等待。
