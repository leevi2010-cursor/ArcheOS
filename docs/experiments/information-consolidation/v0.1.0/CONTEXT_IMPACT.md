# Offline Context Impact

## 对照设计

从 corpus 中选择 30 条真实 Information，覆盖时间序列、流程分歧、范围敏感缺陷、整改变化、文档控制分歧和排期演进。样本来自 5 个独立 Source/Representation，共有 120 条 Evidence entries。

对照只在内存中构建：

- A：现有 raw information list，共 30 个顶层条目；
- B：relation-aware grouped view，共 6 个顶层 group，原 Information 仍可展开读取。

没有修改 Context Builder、Atomic Information Store 或 World Model。

## 结果

| 指标 | A：raw list | B：grouped view |
| --- | ---: | ---: |
| 顶层展示行 | 30 | 6 |
| 可访问 Atomic Information | 30 / 30 | 30 / 30 |
| 可访问 Evidence | 120 / 120 | 120 / 120 |
| 独立 Source/Representation | 5 / 5 | 5 / 5 |
| conflict 显式展示 | 否 | 是 |
| temporal order 显式展示 | 部分依赖读者自行排序 | 是 |
| uncertain scope 显式展示 | 否 | 是 |

顶层展示减少 80%，但这不是信息删除：B 中每一组都披露 included Information 数量、Evidence 数量、独立来源数量和 relation 状态，并允许展开原始条目。

## 质量影响

正向影响：

- 连续数值或状态变化可作为时间序列阅读；
- 互补信息不再被误认为重复；
- 冲突和 uncertain case 在同一视野内并列；
- 同源派生与跨来源 independent Evidence 可以分开计数；
- 读者不必在 30 条平铺内容中手动重建主题和先后关系。

风险：

- 若 group title 写成“最终事实”，会把 conflict 或 uncertain 隐藏；
- 若 whole-record equivalent 自动生效，会丢掉额外限定条件；
- 若 derived hint 被当作 lineage truth，会错误减少 independent Evidence；
- 若只显示 6 个 group 而不披露 30 条 included Information 和 120 条 Evidence，会制造虚假的完整性。

## Context contract 建议

第一版只提供 opt-in、read-only grouped projection，并至少披露：

```text
total_information
included_information
group_count
relation_state
independent_source_count
evidence_count
pending_or_uncertain_count
retrieval_completeness
```

没有 consolidation judgment 时，输出必须与当前 Context 等价。`conflict`、`temporal_update`、`complementary` 和 `uncertain` 不得折叠为单一 statement；`equivalent` 或 `derived` group 也必须保留所有原 Information 与 Evidence 入口。
