# Hypothesis Concept Change Plan

> 临时架构变更计划。完成后应删除或归档；最终权威必须进入 `docs/architecture/CONCEPTS.md` 与 ADR-005。

## 目标

把 `Hypothesis` 正式纳入 canonical concept，但保持概念最小化：

- Hypothesis 属于 Information Layer；
- 复用 Atomic Information 的稳定身份、Revision、Evidence、context、provenance；
- 不建立 Hypothesis Object / 独立 Store / 第二套生命周期；
- 每次影响 Judgment / Decision 的工作假设必须可追溯记录；
- Feedback 用于支持、反对或保持不确定；
- Hypothesis 的现实支持程度不得复用 `Atomic Information.confidence`；
- 反复验证后的“知识化”优先沉淀到已有 `Pattern / Protocol / Policy / Principle`，不新增泛化 Knowledge Store。

## CONCEPTS.md 应修改

1. Information Layer 列表加入 `Hypothesis`。
2. 在 Atomic Information / Claim 附近新增 `Hypothesis` 正式定义，至少说明：
   - testable proposition；
   - 与 Observation / Claim / Judgment / Action / Decision 的区别；
   - supporting/challenging Evidence + Feedback；
   - verification/support 状态与 `Atomic Information.confidence` 分离；
   - 被支持后可以为 Pattern / Protocol / Policy / Principle 的新版本提供依据，但原 Hypothesis 历史保留。
3. Concept alias / convergence 表中加入 Hypothesis 的映射与禁止平行模型规则。
4. 不新增 `Knowledge` Core；“知识”作为产品/展示层统称时，映射到已有 canonical forms。

## 后续同步

- 修订 Issue #42 的 Concept Convergence Check；
- Roadmap M4 把“工作假设只在 Derived Artifact”改成 durable Hypothesis；
- Decision / Action / Feedback 的复盘要求能回到 relied-on Hypothesis；
- 后续前端增加 Hypothesis 验证历史与 Pattern / Protocol 来源追溯，但不启动 UI。
