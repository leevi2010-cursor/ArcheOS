# Relation Contract Findings

## Provenance / Source family boundary

实验确认以下边界必须同时成立：

1. `Source` identity、content identity、Representation lineage 与 Information relation 是不同问题。
2. 相同 Source/Representation 能提高“值得比较”的优先级，但不能自动推出 equivalent。
3. 不同 Source/Representation 下相同 assertion family 应保留为 independent Evidence，除非有明确 derived lineage。
4. 共同外部资源、文件名、链接或主题词只能作为 source-family hint；没有可核验 lineage 时应保持 `uncertain`。
5. `derived` 不能替代 Source-level `derived_from`；Information Layer 只能记录本次解释看到的派生依据。

## 推荐实验词汇

这些标签只描述 Information Layer 的比较结果：

| 标签 | 最小成立条件 | 展示影响 | 禁止行为 |
| --- | --- | --- | --- |
| `equivalent` | 同一 Claim scope、限定条件兼容、时间与 claimant 不造成实质差异 | 可同组展示 | 不删除、覆盖或减少独立 Evidence |
| `derived` | 有可复核的共同材料或 lineage，后者是前者的转述、摘要或展开 | 同组并标明派生链 | 不把派生项计为新增独立来源 |
| `complementary` | scope 兼容，信息分别增加非重叠细节 | 并列展示 | 不折叠成单一 statement |
| `temporal_update` | 同一 scope 且具有明确先后顺序 | 时间序列展示 | 不用新状态覆盖旧状态 |
| `conflict` | 在相容 scope/time 下存在不能同时成立的陈述 | 并列、突出差异 | 不自动选“真值” |
| `uncertain` | Evidence 不足以区分上述关系，或 scope/lineage 不清 | 明确待确认 | 不伪装为低置信度 truth |
| `unrelated` | 候选复核后不属于同一 consolidation case | 从候选集中排除 | 不创建 active relation record |

## Whole-record relation 不成立

真实样本反复出现“一条 statement 完整包含另一条 statement 的核心结论，同时增加范围、数量、条件或执行要求”。如果 pair 只能有一个 whole-record relation：

- 标为 `equivalent` 会隐藏额外限定；
- 标为 `complementary` 又无法表达其中一个 Claim 确实重复；
- 标为 `derived` 可能错误暗示 Source lineage 已确认。

因此 relation target 应是可复核的 Claim projection 或 statement span，并引用当时看到的 Atomic Information revision。当前没有结构化 Claim 时，可先生成 read-only projection；不得反写或重写原 Atomic Information。

## 自动与人工边界

### 可自动执行

- 生成 bounded candidates；
- 标记 exact normalized duplicate candidate；
- 检查 revision、Source、Representation、Evidence 和时间字段是否完整；
- 构建不改变事实状态的预览视图；
- 在没有 relation records 时保持现有读取行为。

### 第一版不可自动生效

- 跨 claimant 或跨独立 Source 的 equivalent；
- conflict 与 temporal_update 的选择；
- complementary 与 equivalent 的选择；
- 没有显式 lineage 的 derived；
- 任何会减少独立 Evidence 计数的折叠；
- scope、时间或来源不明确的 pair；
- Provider confidence 驱动的自动 active record。

corpus 中 confirmed whole-record equivalent 为 0，因此不能用本实验为 equivalent 自动写入提供 real-data acceptance evidence。

## 不属于本实验的概念

这些标签不是 World Model `Relationship`，不解决 claimant credibility、truth probability 或 Object conflict，也不创建第二条 Information lifecycle。若未来需要持久化 relation judgment，必须先由 Architecture Review 固定其技术记录边界；本实验不新增 canonical concept。
