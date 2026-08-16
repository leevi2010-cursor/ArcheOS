# Information Consolidation 真实语料实验 v0.1.0

本目录记录 Issue #32 在真实、匿名化语料上的只读实验结果。实验使用已进入 Durable Atomic Information lifecycle 的 136 条 Information，覆盖 11 个 conversation/source family；没有调用 Provider，没有发现或接纳新 Source，也没有写入 Atomic Information、Object、Relationship、Lifecycle 或 World Model。

## 结论

Corpus Sufficiency Recheck 结果为 `CORPUS_SUFFICIENT`。现有语料足以研究来源边界、派生链、互补、时间变化、冲突、不确定以及负对照，也足以证明“相似候选”不能自动成为 consolidation truth。

最终建议为：

```text
#33 = rewrite
```

原因不是 consolidation 不需要，而是当前 #33 同时引入整条 Atomic Information 的单一关系、远程分类 Provider、多个持久化 Store、人工决策流和 Context 接入，超过了真实语料已经证明的最小安全边界。第一版应收敛为：

1. deterministic、bounded 的候选检索；
2. Claim 范围或 statement projection 范围的关系判断；
3. 不改变 Atomic Information 的只读 grouped view；
4. Evidence、来源独立性、时间与不确定性完整披露；
5. 不把 `unrelated` 持久化为 active relation，不自动折叠 conflict、temporal update 或 uncertain。

## 报告导航

- [DATASET.md](DATASET.md)：语料范围、充分性复核与隐私边界。
- [RELATIONSHIP_FINDINGS.md](RELATIONSHIP_FINDINGS.md)：关系词汇、provenance 与自动/人工边界。
- [RETRIEVAL_FINDINGS.md](RETRIEVAL_FINDINGS.md)：bounded Candidate Retrieval 结果。
- [CONTEXT_IMPACT.md](CONTEXT_IMPACT.md)：离线 Context A/B 对照。
- [RECOMMENDATION.md](RECOMMENDATION.md)：对 #33 的路线建议。
- [manifest.json](manifest.json)：机器可读匿名指标。

## 解释边界

本实验中的 `equivalent`、`derived`、`complementary`、`temporal_update`、`conflict`、`uncertain` 和 `unrelated` 仅是 Information Layer 实验标签，不是 World Model `Relationship`，也不是新增 canonical concept。候选检索结果只表示“值得比较”，不表示关系成立或内容为真。
