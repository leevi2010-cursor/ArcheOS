# ADR-005：把 Hypothesis 作为 Atomic Information 的正式语义形态

## 状态

Proposed / Architect + Product Owner agreed direction

## 背景

ArcheOS 已经用 `Atomic Information` 表达可独立理解、可追溯的长期信息，并明确区分 `Claim`、`Evidence`、`Judgment`、`Decision` 等语义。

在 M4 决策增强设计中，曾尝试把“工作假设”仅作为 Protocol 阶段 `Derived Artifact` 的字段，以避免增加概念。但真实产品需求表明这不足以表达假设的长期业务意义：

- 每次系统性思考都需要明确记录其关键假设；
- 后续行动和 Feedback 的复盘应围绕这些假设展开，而不只看 Decision 本身成功或失败；
- 同一假设可以被多次真实结果支持、反对或修订；
- 某些假设经过跨场景、反复验证后，可以成为更稳定、可复用的知识结构；
- 假设即使被否定，其历史仍然有长期学习价值，不能被删除或覆盖。

因此 `Hypothesis` 不是为了代码方便新增的名词，而具有独立且稳定的认知语义。

## Decision

### 1. Hypothesis 是正式 canonical concept

`Hypothesis` 表示：**一个尚未被系统当作稳定知识接受、但可以通过后续 Evidence / Feedback 被支持、反对、修订或淘汰的可检验命题。**

Hypothesis 属于 Information Layer。

第一版不建立独立 `HypothesisStore`、独立 Object 或平行生命周期；它复用 `Atomic Information` 的稳定身份、Revision、Evidence、context 与 provenance，并通过 canonical hypothesis 语义区分于普通 observation / judgment / claim。

### 2. Hypothesis 与相邻概念边界

- `Observation / Evidence`：回答“观察到了什么 / 依据在哪里”；Hypothesis 回答“我们暂时认为可能是什么、为什么、未来可如何验证”。
- `Claim`：回答“谁以什么立场说了什么”；Hypothesis 可以来源于某个 Claim，也可以由 Agent / Human 基于多个 Evidence 提出，但提出不意味着已经成立。
- `Judgment`：是在当前 Goal / Evidence / Constraint 下作出的判断；Judgment 可以依赖 Hypothesis，但二者不等同。
- `Action`：回答“做什么”；Hypothesis 可以表达“为什么认为该 Action 会产生某个 Outcome”。
- `Decision`：是 Human 受治理确认的取舍；Decision 可以基于多个 Hypothesis，但不会把这些 Hypothesis 自动变成事实。
- `Pattern / Protocol / Policy / Principle`：是更稳定、可复用的方法或治理结构；被反复验证的 Hypothesis 可以为它们的创建或修订提供依据，但不会通过原地改类型的方式“变身”为这些概念。

### 3. 每次系统性思考必须记录关键 Hypothesis

未来 Protocol 驱动的决策增强中，凡影响 Judgment / Decision 的工作假设都必须被显式记录，并可追溯到：

- 它针对的 scope / Object / Goal；
- supporting Evidence；
- challenging / counter Evidence；
- 预期可观察结果；
- 后续 Feedback / verification result；
- 当前支持状态与修订历史。

不得把重要假设仅隐藏在 Prompt、模型私有 chain-of-thought 或不可追溯的自由文本中。

### 4. Hypothesis 支持状态不得复用 Atomic Information.confidence

`Atomic Information.confidence` 继续表示信息抽取 / 语义理解正确性的置信程度。

Hypothesis 的“被现实支持到什么程度”属于另一个维度。第一版应优先保存 supporting / challenging Evidence、验证次数、适用条件和状态变化，不急于制造伪精确的“真实性概率”。

如果未来真实使用需要数值化 epistemic confidence，必须另行定义其语义和更新规则，不能复用当前 `confidence` 字段。

### 5. 复盘围绕 Hypothesis，而不仅围绕 Decision

Action / Decision 后产生的 Feedback 应能够反查：

```text
Decision / Action
→ relied-on Hypothesis
→ expected observable result
→ actual Feedback
→ supports / challenges / inconclusive
→ Hypothesis revision
```

一次 Decision 失败不意味着所有 Hypothesis 都错误；一次 Decision 成功也不意味着所有 Hypothesis 都被证明。复盘必须分别判断。

### 6. 从 Hypothesis 沉淀为可复用知识

ArcheOS 不新增泛化 `Knowledge` Core 作为第二套 truth。

当 Hypothesis 在多个独立场景中得到反复支持，并且适用条件、反例和不确定性足够清楚时，可以通过治理产生或修订已有 canonical 形式：

- 重复问题的可复用解决结构 → `Pattern`；
- 跨任务可复用的交互 / 判断 / 门禁流程 → `Protocol`；
- 明确范围内可执行参数与约束 → `Policy`；
- 稳定的取舍准则 → `Principle`；
- 仅是稳定事实性认识 → 继续作为受治理的 `Atomic Information` / World Model 依据。

这种“晋升”创建或修订目标知识结构的新版本，并保留其 Hypothesis / Evidence / Feedback provenance；不得删除或重写原 Hypothesis 历史。

## Consequences

- `docs/architecture/CONCEPTS.md` 需要新增 Hypothesis 的 canonical 定义，并在 Information Layer 列表中纳入它；
- M4 / Issue #42 的 Concept Convergence Check 要从“Hypothesis 不是 Core”修订为“Hypothesis 是 Atomic Information 的 canonical specialization”；
- 后续 runtime 需要先通过真实决策实验确定最小字段和状态枚举，不在本 ADR 中提前设计完整 Hypothesis engine；
- Human View 未来需要支持从 Decision / Pattern / Protocol 反查其历史 Hypothesis 与验证证据；
- 不建立 `Hypothesis Object`、`HypothesisStore`、`KnowledgeStore` 或自动升格引擎。
