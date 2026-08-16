# Dataset 与 Corpus Sufficiency Recheck

## 数据范围

| 指标 | 结果 |
| --- | ---: |
| Durable Atomic Information | 136 |
| conversation/source families | 11 |
| Conversation Representations | 11 |
| Evidence entries | 336 |
| 具有完整 Source locator 的 Evidence | 336 / 336 |
| 具有完整 Representation 与 unit reference 的 Evidence | 336 / 336 |
| 结构化 Claim | 0 |
| 已有 Object binding | 0 |
| 内容完全相同的 Source snapshot | 0 |
| 跨 Source 重复出现的外部资源指纹 | 1 组，涉及 2 个 Source |

全部业务正文、身份、路径、内部 ID、content hash 与 locator 均只在本机受控环境中读取，没有进入本报告。

## Sufficiency Recheck

复核结论：`CORPUS_SUFFICIENT`。

| 研究边界 | 结果 | 匿名 Evidence 概况 |
| --- | --- | --- |
| equivalent / derived | pass | 存在明确派生候选；多个表面近似案例证明整条 Information 不能直接判 equivalent |
| complementary | pass | 同一工作主题下存在范围扩展、条件补充和执行细化 |
| temporal_update | pass | 存在连续数值序列及跨日期状态/排期变化 |
| conflict / uncertain | pass | 存在责任、范围、完成状态及处理路径不一致，且部分无法安全消解 |
| unrelated negative controls | pass | 存在共享词语、数字或通用流程词但业务语境独立的案例 |
| same-source / derived Evidence | pass | 同一 Source/Representation 内存在多个独立 unit，并有跨 Source 的共同外部资源线索 |
| cross-conversation independent Evidence | pass | 同一 assertion family 在不同 Source/Representation 中出现，Evidence 保持独立 |
| clear chronological change | pass | 时间顺序可由 statement time 与 Evidence provenance 共同复核 |
| cannot-safely-fold | pass | 冲突、范围差异和不确定来源案例不能折叠为单一事实 |

上一次 40 条语料 checkpoint 的三个关键缺口均已解除：

- `equivalent_or_derived_provenance_pair = pass`
- `same_source_or_derived_evidence_chain = pass`
- `cross_conversation_independent_same_statement = pass_at_claim_scope`

最后一项刻意限定为 Claim 范围：跨 conversation 的陈述属于同一 assertion family，但 scope、时间或数量限定可能不同。若直接在整条 Atomic Information 上判断 equivalent，会把这些限定条件一起折叠。

## 人工审查样本

从 136 条 Information 中建立了 28 个本机只读 pair review：

| 审查结果 | 数量 |
| --- | ---: |
| derived | 2 |
| complementary | 6 |
| temporal_update | 8 |
| conflict | 3 |
| uncertain | 4 |
| unrelated negative control | 5 |
| confirmed whole-record equivalent | 0 |

另对 6 个表面高度近似的候选进行了 equivalence 边界复核；它们实际属于时间变化、互补或范围敏感案例，因此没有确认 whole-record equivalent。这个零值不是语料不足，而是对 production contract 的直接约束：第一版不能把文本相似度当作等价关系。

## 已知限制

1. 当前 136 条 Information 没有结构化 Claim，也没有 Object binding；关系判断只能从 statement、raw concerns、Evidence、Source/Representation provenance 和时间信息恢复 Claim 范围。
2. 同一 Source 的 content identity 能证明字节来源，但不能证明两条 Information 等价。
3. 跨 Source 的共同外部资源指纹是派生线索，不足以单独证明 lineage 或 equivalent。
4. corpus 中没有 confirmed whole-record equivalent，不能据此批准 equivalent 的自动 active record 写入。
5. #88 的 `anchor_coverage` failure 是独立 runtime reliability Evidence；本实验不修复它，也不把它用于否定已经 Durable 的 136 条 Information。
