# Architecture Recommendation

## Recommendation：C

**需要 domain-level schema / mapping，但 ArcheOS Core 不新增 canonical concept。**

实验表明：

- 现有 Source 与 XLSX Normalized Representation 已足以保存 observed source truth；
- `Derived Artifact / Projection` 足以承载可修订的 structure discovery、mapping、normalization 与 data-quality assessment；
- Evidence 已能定位到 sheet / row / column / cell；
- 少量值得长期认知化的结论仍可进入 Atomic Information；
- source key 继续只作为 Identity Gate Evidence；
- structured state、Atomic Information 与 warnings 可以通过 bounded Projection / Context preview 清楚分层。

因此当前没有证据支持新增 canonical `Dataset / Table / Record / Schema / StructuredState`，也不需要新的 Store 或 Information lifecycle。

## 后续实现边界

若后续产品化供应商报价吸收，应在具体 Domain / Workspace 保存版本化 mapping rule 与 domain schema，并继续生成可替换 Projection。它们不是 Core schema registry，也不得覆盖历史 Source / Representation。

只有当多个真实 Domain 反复证明 `Projection` 无法表达“需要长期身份、跨 Source 合并与独立治理的结构化 current state”时，才向 Lead 提交新的 Concept Convergence Check；本实验不预先批准该扩张。

## Roadmap Feedback

Observation:

脏结构化数据的主要风险不是 XLSX 读取，而是把 observed header/key 误当 canonical identity/schema，以及在版本关系不明确时静默覆盖冲突。

Evidence:

三版高保真 synthetic XLSX 中，faithful Representation 与 Projection 可稳定分离；全部 safe normalization 均保持 locator；identity、价格冲突与 free-text 解释可以 fail closed。

Affected Stage / Assumption:

Stage 1 “真实、异构、持续变化的信息仍保持 provenance、冲突和 identity 安全”的假设得到 synthetic 支持，但尚缺真实报价 smoke。

Suggested Change:

继续使用现有 Core；未来有明确真实业务需求时做一个小型 domain mapping implementation / real smoke，不先建设通用 ETL、MDM 或新的 canonical structured-state concept。

Decision:

keep
