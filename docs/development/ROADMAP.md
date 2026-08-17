# ArcheOS 开发路线图

## 文档职责

本文件定义 ArcheOS **为了通过当前 Product Stage，需要按什么顺序建立能力、完成实验与取得真实验证证据**。

它不是功能清单、历史 changelog，也不替代单个 GitHub Issue 的 implementation contract。

权威关系：

```text
docs/product/PRODUCT_SPEC.md
  → 产品长期是什么
        ↓
docs/product/PRODUCT_ROADMAP.md
  → Product Stage / Stage Gate
        ↓
docs/development/ROADMAP.md
  → 当前 Stage 的 Evidence Gap 与技术顺序
        ↓
GitHub Issue
  → 一次具体交付
        ↓
PR / Experiment / Real-world Validation
  → Evidence + Roadmap Feedback
```

横向约束：

- `AGENTS.md`：项目协作、Roadmap Alignment、Concept Convergence；
- `docs/architecture/CONCEPTS.md`：canonical concepts；
- `docs/product/INFORMATION_GOVERNANCE.md`：信息吸收、World Model 更新与 Human Judgment 规则；
- `docs/architecture/ARCHITECTURE.md` 与 Accepted ADR：系统边界与长期技术决策。

如果实现或真实实验产生的 Evidence 挑战本路线，应先形成 Roadmap Feedback，再修订路线；不得让代码静默成为新的产品权威。

---

## 当前 Product Stage

当前以上游 `docs/product/PRODUCT_ROADMAP.md` 为准：

> **Stage 1 — 证明“长期认知”真实成立。**

Stage 1 要证明的不是“功能够不够多”，而是：真实、异构、持续变化的信息不断进入后，系统仍然能够保持：

- 重要信息不丢失；
- Evidence / provenance 可追溯；
- 重复和派生信息不会无限制造噪声；
- 时间变化不会被错误去重；
- 冲突和不确定不会被静默覆盖；
- Object identity 不持续分裂；
- 错误可以纠正而不破坏历史；
- Context 随数据增长变得更有用，而不是更混乱；
- 人工治理成本可接受。

当前不进入 Stage 2 的 autonomous decision / active cognition 主线。

---

## Stage 1 技术认知链状态

基于已完成的 Issue #17 真实旧数据 Pilot，当前技术认知链状态为：

> **ESTABLISHED / REAL-WORLD VALIDATED**

已取得的端到端 Evidence 包括：

- 真实多来源资料通过 privacy gate、Managed Source 与 Normalized Representation 进入统一处理边界；
- External Semantic Handoff 在 25 个真实 batches 上保持 strict schema、protocol/input binding、anchor coverage、Evidence、audit 与 package/readback PASS；
- 275 条 Durable Atomic Information 成功写入，633 个 eligible Units 全部被 Candidate 或 Residue 守恒；
- read-only Consolidation 保持 fail-closed，未覆盖原 Information、未产生未经证明的 relation write；
- bounded Identity Gate 覆盖 `create_minimal / bind_existing / accumulate / human_review / no_object`，幂等、stale revision 与 collision 检查通过；
- Product Owner 批准的 `business_line` Role update 通过既有 Governance 写入，pending identity 保持 `deferred`；
- Context Builder 完成最终 readback 与 Product Owner 业务验收；
- 真实 legacy pilot 完成，未留下未解决 P0 / P1、privacy breach、错误 identity、duplicate Object 或 false consolidation。

完整匿名证据见：

- `docs/migration/v0.2.0/PILOT_RESULTS.md`；
- `docs/migration/v0.2.0/manifest.json`。

这证明技术链能够在本次真实边界内运行，不等于 Product Stage 1 已自动通过。

---

## Stage 1 Gate Evidence

当前只保留两个 material Evidence Gap：

### 1. Consolidation truth coverage

Issue #17 对 100 个有界真实候选对完成 read-only Consolidation，结果全部为 `uncertain`。这不是 runtime failure：runtime 正确地在证据不足时 fail closed，也没有制造错误 relation。

但该结果尚未证明系统对真实 `equivalent / derived / temporal_update / conflict` 案例具有足够分类质量，`false_merge / false_split` 也因缺少人工 truth 仍为 `not_measurable_yet`。因此这里是 Evidence coverage gap，而不是新增 Consolidation runtime 的默认理由。

### 2. Context breadth

Product Owner 已接受一个真实 Object 的最终 Context，2 条相关 Information 均被纳入，Evidence、Role 与 pending judgment 可读回。

该结果证明了这个案例的 Source → Information → World Model → Context 链路，但尚未证明 multi-Object、更大规模、长期增长条件下 Context 的业务效用与治理成本。这是产品证据广度不足，不是 Context Builder contract failure。

---

## 当前主线：Stage 1 Gate Review

Issue #17 已完成；当前不再把新的 feature 或 validation Issue 预设为主线。Product / Technical Lead 下一步应基于上述证据作出 Stage 1 Gate Review：

```text
PASS
→ Product Owner 可以明确决定是否进入 Product Stage 2

CONTINUE / PARTIAL
→ 只选择关闭已识别缺口所需的最小实验

FAIL
→ 指明失败的具体层，并只修正该层
```

若 Gate Review 选择 `CONTINUE / PARTIAL`，带人工 truth 的 Consolidation 验证与 multi-Object Context 验证只是候选工作；Lead 应决定执行其中一个、两个或都不执行，而不是把两项自动列为必做序列。

在 Gate Review 与 Product Owner 的 Stage 2 明确决定之前：

- 不启动 Issue #42；
- 不建设 Decision Engine、自主 Agent 或 Protocol runtime；
- 不启动大型 Human View / Frontend；
- 不建设 Workspace multi-master；
- 不另建 migration framework；
- 不因未来能力清单自动创建新工作。

---

## 并行 / 后置工作

### #89 — 移动硬盘资料 inventory

该只读资料盘点保持 Deferred / Planned，不属于当前 Gate Review 的默认后续工作。不得移动、删除、重命名原文件，也不得对全盘执行无目的 LLM 分析。

### Conversation 扩展：Codex / ChatGPT

微信已经证明 generic Conversation → Information lifecycle 可以成立。Codex / ChatGPT 接入仍有产品价值，但当前不是 Stage 1 的最短主路径。

以后启动时必须基于当前 generic Conversation contract 重新 preflight；不得复活各 Provider 独立的长期 Information lifecycle。

### Workspace Portability

跨机器 snapshot、single-writer authority、verified replica 保留为后置能力。单机长期认知链尚未完成 Stage 1 Gate 前，不建设 multi-master sync。

### Human View / Frontend

Human View 是 Projection / Presentation，不是第二套 truth。

只有当真实使用证明“理解、审核、Evidence drill-down 或 Context preview”成为主要瓶颈时，再建立最小 UI Issue；当前不因技术完整性提前启动大型前端。

---

## Product Stage 2 — 决策增强（Blocked）

只有 Stage 1 Gate 通过，并由 Product Owner 明确进入 Stage 2 后，才启动：

```text
Hypothesis
→ Judgment
→ Decision
→ Action
→ Feedback
```

ArcheOS 不建设自有 Decision Agent。External Agent 负责推理；ArcheOS 提供长期 Context、Evidence、Hypothesis、Protocol / Policy / Pattern、治理、审计与受控写回。

Stage 2 当前 authority 以 `docs/product/PRODUCT_ROADMAP.md` 与 blocked Issue #42 为准；开始前必须重新做 Concept Convergence 和 implementation preflight。

---

## 永久技术边界

以下规则不因当前 Issue 顺序改变：

- ArcheOS Core ≠ Agent；
- Source ≠ Representation；
- Conversation / Message ≠ Core Object；
- Claim ≠ Fact；
- Hypothesis 复用 Atomic Information lifecycle，不建立 HypothesisStore；
- Project 是 Object Role，不是独立 base entity；
- Context / View / Projection ≠ 第二份 truth；
- Provider 不能绕过 strict validation 直接写 Durable Information / World Model；
- TOS / R2 / S3 等只可作为 storage adapter / replica，不改变 identity；
- 不因为代码方便创建新的 Core noun；
- canonical concept 不足时，先修改 `CONCEPTS.md` + ADR，再允许 implementation Issue Ready。
