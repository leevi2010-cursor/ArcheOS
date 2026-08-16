# Bounded Candidate Retrieval Findings

## 方法

对 136 条 Information 做本机离线检索。ground truth 为 23 个相关/歧义审查对和 5 个专门选择的 unrelated negative controls。检索只决定“是否进入比较候选”，不决定 relation。

基础策略均为 deterministic、可重放，并限制每条查询最多返回 5 个候选：

- exact normalized statement；
- 同一 Source provenance；
- raw concern overlap；
- number/date/key token overlap；
- bounded lexical overlap。

## 单策略结果

| 策略 | 相关对召回 | 负对照被召回 | 平均候选数 | 最大候选数 |
| --- | ---: | ---: | ---: | ---: |
| exact normalized | 0 / 23 | 0 / 5 | 0.00 | 0 |
| same Source | 16 / 23 | 0 / 5 | 4.93 | 5 |
| concern overlap | 15 / 23 | 0 / 5 | 4.14 | 5 |
| number/date/key token | 2 / 23 | 0 / 5 | 0.51 | 5 |
| lexical overlap | 16 / 23 | 4 / 5 | 4.45 | 5 |

主要观察：

1. exact normalized 在当前 corpus 中没有命中，不能作为唯一策略。
2. same Source 与 concern overlap 是最有效的高精度基础，但会漏掉跨 conversation 的 independent Evidence。
3. number/date/key token 单独召回很低，适合补充时间序列和硬 token，不适合主检索。
4. lexical overlap 能补召回，也最容易把共享通用词的 unrelated 案例带入。

## 组合策略

使用优先级 `exact → same Source → concern → number/date/key token → lexical` 去重合并：

| top-k | 相关对召回 | 负对照被召回 | 平均候选数 |
| ---: | ---: | ---: | ---: |
| 5 | 16 / 23（69.6%） | 0 / 5 | 5.00 |
| 8 | 19 / 23（82.6%） | 3 / 5 | 7.89 |
| 12 | 19 / 23（82.6%） | 5 / 5 | 10.02 |

`top-k=8` 是当前样本的最小有效边界：从 8 增加到 12 没有提高相关对召回，却把所有负对照带入候选。候选中出现 unrelated 是允许的，前提是 classification 与 retrieval 严格分离，且 `unrelated` 不持久化为 active relation。

## 未召回案例

组合策略仍漏掉 4 / 23 个相关或歧义 pair，集中在：

- 跨 conversation、scope 不完整的相似陈述；
- 需要共同 Source material 才能看出的 derived hint；
- 表达语言或摘要粒度不同；
- raw concerns 没有使用稳定共享词汇。

这些缺口不能通过无限提高 top-k 或全 Store 扫描后送入模型解决。第一版接口应允许调用方提供 bounded retrieval scope，例如已有 Object/concern、明确选中的 Source family 或 provenance hint；没有 scope 时保持较低召回并显式披露 completeness。

## 推荐 retrieval contract

```text
retrieve(query_revision, bounded_scope, top_k=8)
  -> candidates[]
```

每个 candidate 至少返回：

- comparison target revision；
- retrieval basis；
- Source/Representation independence summary；
- time/claimant availability；
- whether Evidence is same-family, derived-hint, or independent；
- deterministic rank 与 completeness disclosure。

不建议在第一版引入 embedding：当前 deterministic 组合已经足以验证 contract 风险，而主要缺口来自 provenance、Claim scope 和业务边界缺失，不是单纯语义相似度不足。
