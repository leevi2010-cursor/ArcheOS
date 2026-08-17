# Issue #17 Pilot Results

日期：2026-08-17

Product Stage：Stage 1 — 证明“长期认知”真实成立

结果：端到端技术链 `PASS`；Product alignment `PARTIAL`；Stage 1 Gate 建议 `review`

## 1. 执行结论

真实、多来源 Pilot 已完成：

```text
privacy gate
→ Managed Source
→ Representation
→ Semantic Handoff
→ Durable Atomic Information
→ read-only Consolidation
→ bounded Identity Gate
→ Product Owner Context Review
```

最终没有未解决 P0 / P1，也没有 privacy breach、false bind、false create、duplicate Object、false consolidation 或不可恢复部分写入。

本 Pilot 不支持直接宣告 Stage 1 PASS：Consolidation 的有界真实候选没有安全形成 equivalent / derived / temporal / conflict 判断，全部保持 `uncertain`；最终业务 Context 只验证了一个 Object。该缺口应由 Lead 判断是扩大验证样本、设计更有针对性的真实实验，还是接受为当前 corpus 的边界。

## 2. 必报指标

| 指标 | 结果 |
| --- | --- |
| source_total | 20 |
| representation_complete / partial / failed | 17 / 3 / 0 |
| representation_warning_count | 7 |
| analysis_units_total / eligible / excluded | 750 / 633 / 117 |
| unaccounted_units | 0 |
| atomic_information_created / existing | 275 / 0 |
| residue_items | 24 |
| consolidation_equivalent | 0 |
| consolidation_derived | 0 |
| consolidation_complementary | 0 |
| consolidation_temporal | 0 |
| consolidation_conflict | 0 |
| consolidation_uncertain | 100（有界候选对） |
| false_merge / false_split | `not_measurable_yet` / `not_measurable_yet` |
| auto_bind / false_bind | 1 / 0 |
| auto_create_minimal / false_create / duplicate_created | 1 / 0 / 0 |
| accumulate / human_review / no_object | 1 / 1 / 1 |
| context_information_total / included / grouped | 2 / 2 / `not_measurable_yet` |
| context_pending_judgments | 1（Product Owner 决定 `defer`） |
| context_truncated | 0 |
| manual_missed_information | `not_measurable_yet` |
| manual_false_structuralization | 0（本次审阅范围内） |

`not_measurable_yet` 表示本 Pilot 没有足够人工标注或对照 truth，不用 0 代替未知。

## 3. Source / Representation

- 20 / 20 Sources admitted、verified；原资料保持只读；
- 17 complete、3 partial；partial 来自真实格式能力边界，未静默包装为 complete；
- 7 个 warning 全部进入 Representation readback；
- 3 个视觉 Source 因 privacy 无法确定而保持 `local_only`，Provider 调用为 0；
- 未扫描其他目录，未从 Preflight 之外重新 discovery。

## 4. Semantic Handoff / Atomic Information

- 17 个 privacy-approved Sources 完成 25 个 distinct semantic batches；
- 25 / 25 batches 的 schema、protocol/input binding、anchor coverage、Evidence 与 audit readback 均 strict PASS；
- Provider calls 总计 26：原 120 秒 timeout 1 次，加同一 batch 的已授权 300 秒 recovery 1 次，再加其余 distinct batches；
- 原 timeout Processing Run 保留为独立 Evidence，没有覆盖或删除；
- recovery 保持相同 Source、Representation、anchor IDs、batch boundary、request fingerprint、Provider route/version、Prompt、schema 与 Evidence contract；
- 275 条 Atomic Information 与 24 条 Residue 成功 package/readback；633 eligible Units 的 `unaccounted = 0`；
- 没有 retry、fallback 或 Provider switch；production 默认 timeout 未修改。

## 5. Read-only Consolidation

对完整 275 条当前 Information 使用 bounded retrieval，最多检查 100 个唯一候选对：

- 100 个候选对全部因确定性证据不足而标记 `uncertain`；
- 没有把 lexical overlap 当作 equivalent / derived / temporal / conflict truth；
- 275 / 275 原 Atomic Information 保留；
- durable relation writes = 0；
- durable Information rewrites = 0。

这是安全结果，但不是 relation quality 的充分证据。`false_merge / false_split` 仍需带人工 truth 的专门验证。

## 6. Identity Gate / World Model

### 最终有效 bounded run

先完成 read-only assessment / apply plan，再 sequential apply：

```text
create_minimal = 1 / cap 3
bind_existing  = 1 / cap 5
accumulate     = 1
human_review   = 1
no_object      = 1
```

durable cap 由每次 apply 前后重新读取的 apply receipts、Change Journal、current Information bindings 与 active World Model Objects 共同计算；unique logical changes 才计数，进程 counter 不再构成 authority。

验证结果：

- 2 receipts、2 Identity Journal changes、2 append-only binding revisions、1 active Object 完全收敛；
- exact create replay / bind replay 均无新 Object、Revision、Receipt、Journal；
- stale revision 与 identity collision 均 fail closed，durable state 无变化；
- duplicate Object = 0；
- automatic Role / Relationship / Lifecycle = 0 / 0 / 0；
- merge / delete / split = 0。

### Product Owner Context Decision

- Product Owner 批准为已确认 Object 增加 `business_line` Role；
- Role 通过 Change Proposal → `human_approved` apply 写入，保留 supporting Atomic Information、apply receipt 与 Change Journal；
- exact approval replay 为 no-op；
- pending identity 决定为 `deferred`，重复 defer 为 no-op；
- 最终仍无 Relationship、Lifecycle、merge、delete、split。

## 7. Cap accounting deviation 与 recovery

第一次本地 Identity evaluation harness 只使用单进程 counter。两次末端只读校验接口错误后重跑，累计产生了无效的 5 次 create / 9 次 bind，超过批准的 3 / 5 cap。

安全恢复：

1. 立即停止并报告 `LEAD_DECISION_REQUIRED`；
2. 确认无重复 primary Name、Role、Relationship、Lifecycle、merge、delete、split；
3. 将整套无效 Identity / Context 产物移入本地私有隔离区并保留，不删除、不伪装；
4. 活动 Store 恢复为 275 条 revision 1、0 binding，活动 World Model / Proposal / Journal 为空；
5. Lead 授权从 clean baseline 重跑；
6. harness 改为 durable logical change accounting，并完成上述最终有效 bounded run。

该偏差是 Pilot harness 的执行治理缺陷，不是 production Identity Gate contract change。最终 PR 不把配额机制写入 Core。

## 8. Context final readback

| 项目 | 结果 |
| --- | --- |
| Context roots | 1 |
| root Roles | `business_line` |
| Atomic Information total / included | 2 / 2 |
| recent changes | 3 |
| pending judgments | 1，状态 `deferred` |
| relationships | 0 |
| truncated | 0 |
| Product Owner review | accepted |

真实 Context 与 Evidence 只保留在本地私有 workspace，没有进入 GitHub。

## 9. Severity 与 Gate

| Gate | 结果 |
| --- | --- |
| P0 Information loss | none observed |
| P1 Source / Evidence | none unresolved |
| P1 Consolidation | none observed；relation coverage insufficient |
| P1 Identity / World Model | none unresolved；harness deviation recovered |
| P2 Context | one-Object coverage only |
| Privacy | PASS |
| Product alignment | PARTIAL |
| Expected evidence obtained | partial |

## 10. Roadmap Feedback

Observation:

- 40-anchor mitigation 在 25 个成功真实 batches 上保持 strict coverage；
- Source → Context 端到端链路可运行并保持 Evidence；
- durable idempotency / fail-closed 能力有效，但实验配额必须从 durable state 计算；
- 当前 corpus 没有提供足以确定 relation truth 的真实候选，Context 业务覆盖也偏窄。

Affected Stage / Assumption:

Stage 1 关于长期变化、冲突、重复与多 Object Context 的证据仍不完整。

Suggested Change:

`review`。不新增无关 Core feature；Lead 可选择一个小型、带人工 truth 的 Consolidation / 多 Object Context 实验，或接受本 Pilot 作为当前 Stage 的部分证据。

Decision:

`review`
