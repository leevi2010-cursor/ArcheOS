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

## Stage 1 已取得的能力与 Evidence

以下能力已经形成，后续工作默认复用，不重复建设平行 contract：

### A. Source / Representation / Evidence

- Managed Source：稳定 `source_id`、不可变 managed bytes、verify / restore；
- 音频 Processing 已切换到 verified Managed Source；
- Normalized Representation 公共 contract；
- Markdown、text PDF、XLSX、PPTX 与 image structural preflight Adapter；
- Representation → canonical Analysis Unit / Analysis Batch；
- Candidate + Residue 完整 coverage；
- Evidence 可回到 Source / Representation stable locator。

### B. Durable Information

- Atomic Information stable identity 与 append-only Revision；
- Claim attribution；
- production semantic types：`observation / preference / requirement / judgment / decision / commitment / action / question / other`；
- `AtomicInformation.confidence` 只表达抽取 / 理解置信度，不表达现实真实性概率；
- Conversation 与普通 Representation 进入同一 Information lifecycle。

### C. Conversation / External Semantic Execution

- 微信 Conversation Representation 已验证 stable replay 与 message-level locator；
- Conversation / Message 不成为 Core Object；
- production External Agent Semantic Handoff 已建立 strict result contract；
- 真实微信 Semantic Digestion 已能产生可追溯 Durable Atomic Information；
- semantic provider 不获得直接 World Model write 权限。

### D. Information Consolidation

Information Consolidation 已完成真实实验与最小 runtime，当前关系 vocabulary 为：

```text
equivalent
derived
complementary
temporal_update
conflict
uncertain
```

并显式区分 Evidence independence：

```text
same_source_family
independent
unknown
```

Consolidation 不覆盖或删除原 Atomic Information；它负责帮助 Context、Identity Gate 和后续治理正确理解重复、派生、时间变化与冲突。

### E. World Model / Identity Gate

World Model 使用统一 Object 模型：

```text
Object
+ Name(s)
+ Role(s)
+ Lifecycle
+ Relationship(s)
+ related Atomic Information / Evidence
```

Project 等业务名词继续通过 Role 表达，不建立平行 base entity。

Object Emergence 已收敛为 Identity Gate：

```text
明确已有身份
→ automatic bind existing Object

明确新身份 + Evidence 足够 + 值得长期保持 + 低风险
→ automatic create minimal Object

Evidence 暂时不足
→ accumulate Information

identity ambiguity / duplicate risk / consequential
→ Human Judgment

没有长期 identity 价值
→ Atomic Information only
```

`create minimal Object` 只确认 identity 与 Evidence-backed Name；不自动确认 Role / Relationship / Lifecycle / Claim truth。

禁止使用 `confidence > 0.x` 作为 identity truth gate。merge / delete / identity boundary correction 仍需 Human Judgment。

### F. Context / External Agent read boundary

- canonical Context Builder 已建立 Object-scoped、bounded、provenance-aware、truncation-aware read contract；
- External Agent 可以读取 canonical Context / Evidence；
- ArcheOS Core 不建设 Sales Agent / Founder Agent / Project Agent 等领域 Agent。

---

## 当前剩余的 Stage 1 Evidence Gap

基础认知链已经成形。当前最重要的不确定性已经从“能不能实现这些能力”转为：

> **当真实、多来源、长期、混乱的数据规模继续增长时，这条链是否仍然成立？**

因此 Stage 1 后续主线不再优先增加 Core feature，而是进入真实压力验证。

当前要关闭的 Evidence Gap：

```text
真实多来源旧资料
        ↓
Managed Source / Representation
        ↓
Semantic Digestion / Atomic Information
        ↓
Information Consolidation
        ↓
Identity Gate / World Model
        ↓
Context Builder
        ↓
人工业务验收
```

必须重点验证：

1. 完全重复与派生总结不会污染 Context；
2. 时间变化不会被当作 equivalent；
3. 冲突 Claim / Information 可以长期并存；
4. independent Evidence 不会被错误折叠；
5. Object auto-bind / auto-create 不产生错误 identity 或 duplicate Object；
6. 弱 Evidence 能继续积累而不是强迫用户逐条审核；
7. 错误结构可以通过治理纠正且历史仍可追溯；
8. Context 在真实数据增长后仍然足够精华、完整、可展开 Evidence；
9. 本地维护、人工判断和长期使用成本可接受。

任何未解决 P0 / P1 阻止 Stage 1 完成。

---

## 当前主线：#17 真实旧数据压力测试与 clean-cut readiness

Issue #17 是下一阶段的综合 Stage 1 validation authority，但**进入执行前必须以当前 main 做一次 implementation preflight**，不得照搬其历史正文中的旧假设。

#17 的当前 contract 必须遵守：

- 使用当前已合并的 Information Consolidation runtime；
- 使用当前 Identity Gate，而不是“所有新 Object 都人工批准”；
- `auto_bind / auto_create_minimal / accumulate / human_review / no_object` 都必须有真实验收；
- OCR / 扫描 PDF 不是 Stage 1 压力测试的强制前置；当前只有 text PDF 与 image structural preflight 属于已批准基础能力；
- 不为了旧资料兼容建立新的 Core noun / legacy schema；
- 真实资料只保存在本地，GitHub 只保存匿名统计与结论；
- 旧系统是 migration source / design reference，不再是新系统 authority；
- 不设计长期 dual-read / dual-write。

推荐 pilot corpus：20–100 个 Source，至少覆盖多个格式 / source family，并包含：

- exact duplicate；
- derived duplicate；
- temporal update；
- conflict / uncertain；
- existing Object；
- clear new identity；
- ambiguous identity；
- no-object information。

核心验收指标至少覆盖：

```text
source_total
representation_complete / partial / failed
analysis_units_total / eligible / excluded
unaccounted_units
atomic_information_created / existing
residue_items
consolidation_equivalent / derived / complementary / temporal / conflict / uncertain
false_merge / false_split
auto_bind / false_bind
auto_create_minimal / false_create / duplicate_created
accumulate / human_review / no_object
context_information_total / included / grouped
context_pending_judgments
manual_missed_information
manual_false_structuralization
```

#17 完成后，由 Product / Technical Lead 根据 Evidence 做 **Stage 1 Gate Review**：

```text
PASS
→ Product Owner 决定是否进入 Product Stage 2

PARTIAL
→ 只补当前暴露出的最小 Evidence Gap

FAIL
→ 回到具体失败层修正路线，不增加无关能力
```

---

## 并行 / 后置工作

### #89 — 移动硬盘资料 inventory

只读资料盘点可以作为 #17 pilot corpus 准备或多来源补充，但不应抢占 Stage 1 主验证链。不得移动、删除、重命名原文件，也不得对全盘执行无目的 LLM 分析。

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
