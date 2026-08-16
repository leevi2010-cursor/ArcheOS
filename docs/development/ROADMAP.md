# ArcheOS 开发路线图

## 文档职责

本文件定义 ArcheOS **为了通过当前 Product Stage，需要按什么顺序建立能力、完成实验与取得真实验证证据**。它不承担单个开发任务的实现规格，也不独立决定长期产品方向。

权威关系：

- `AGENTS.md`：定义 Agent 的工作规则、Roadmap Alignment 与权威关系；
- `docs/product/PRODUCT_SPEC.md`：定义 ArcheOS 长期是什么、为谁创造什么价值、产品边界是什么；
- `docs/product/PRODUCT_ROADMAP.md`：定义 Product Stage、Stage Gate，以及为了成为目标产品依次必须证明什么；
- `docs/architecture/CONCEPTS.md`：定义 Core 概念；
- `docs/product/INFORMATION_GOVERNANCE.md`：定义信息吸收、自动更新与人工判断的产品规则；
- 本 `ROADMAP.md`：在已批准 Product Stage 内，定义为了关闭 Evidence Gap 所需的技术演化与验证顺序；
- GitHub Issue：定义当前一次开发必须交付什么；复杂 Issue 可以内嵌 ChatGPT Product / Technical Lead 批准的 Implementation Plan 与 Test Cases；
- Durable Spec / ADR：仅在稳定契约或架构决策需要跨多个 Issue 复用时建立。

因此，本文件回答的是“为了证明当前产品阶段成立，系统接下来缺什么能力”，而不是“一个完整软件理论上还应该有哪些功能”。技术完整度不能反过来成为产品路线权威。

如果 Experiment、Issue、PR 或真实使用产生了与 Product Roadmap 假设冲突的新 Evidence，应通过 `AGENTS.md` 定义的 Roadmap Feedback 机制向上反馈；必要时先修正 Product Roadmap，再重排本文件，而不是让实现静默漂移。

## 当前 Product Stage 对齐

当前上游 Product Stage 以 `docs/product/PRODUCT_ROADMAP.md` 为准，目前是：

> **Stage 1 — 证明“长期认知”真实成立。**

当前 Stage Gate 的核心不是完成某一组功能，而是证明真实、异构、持续变化的信息长期进入系统后，ArcheOS 仍能保持信息守恒、provenance、冲突 / 时间变化 / 不确定性、Object identity 与 Context 质量，并且不会随着数据增长越来越混乱。

因此，当前 Development Roadmap 的主要技术工作之所以有优先级，是因为它们分别关闭以下 Evidence Gap：

```text
Source / provenance 是否稳定
        ↓
多格式与 Conversation 是否进入同一 Information lifecycle
        ↓
语义执行是否稳定、可审计、隐私可控
        ↓
重复 / 派生 / 补充 / 时间变化 / 冲突是否能受治理地整理
        ↓
长期 Object 是否能从真实 Information 中安全形成与演化
        ↓
Context 是否随着真实数据增长变得更有用而不是更嘈杂
        ↓
真实旧数据与长期使用压力下，整条认知链是否仍然成立
```

任何新的大型能力、UI、平台基础设施或集成在进入当前主线前，都必须说明它关闭哪一个当前 Stage Gate 的 Evidence Gap；否则默认后置。维护、安全、隐私、完整性和回归修复按 `AGENTS.md` 的 Maintenance / Integrity 例外处理。

产品名称长期使用 **向阳经营系统（Sunward Operating System）**；`ArcheOS` 仅作为当前重构迁移阶段的工程 / 仓库代号。待新系统完成真实数据验证、旧 `sunward-operating-system` 完成 clean-cut 退役且命名切换不会造成双重权威时，再将 ArcheOS 正式恢复为“向阳经营系统”产品名称；在此之前不为了改名打断当前主线。

旧 `sunward-operating-system` 从当前阶段起视为 **legacy / migration source / UI design reference**：不再新增产品能力、不再写入新的 canonical 经营数据、不再作为 Agent 长期上下文权威。仓库是否正式 Archive 留到 #17 完成最终 KEEP / IMPORT / REBUILD / RETIRE 盘点后执行。

---

## M0 — 治理与基础结构（已完成）

目标：建立统一的系统治理、信息目录与 Issue → Approved Plan / Tests → 实现 → PR → 审核协作方式。

---

## M1 — 通用音频信息消化（已完成）

目标：把未知业务领域的本地音频转化为可追溯的 Processing 包。

核心流程：

**录音 → 转写 → Speaker Attribution → 上下文保留 → Atomic Information Candidates + Residue**

已实现：GitHub Issue #4 / PR #5。

---

## M2 — 长期 Information + Structured World Model（当前）

目标：让 Processing 产出的信息进入长期 Atomic Information，并在受治理的边界下持续更新 Structured World Model，最终形成可被 Agent / Human View 统一读取的 Context。

主线只保留：

```text
Atomic Information Candidate
        ↓
Durable Atomic Information
        ↓
Information Digestion / Governance
        ↓
Structured World Model
        ↓
Context Builder
        ↓
Real-world Validation
```

不因为旧向阳系统曾存在 Agent Contract、Proposal Queue、Review Center、MCP/HTTP 等能力，就把它们重新带回当前主线。

### M2-A — World Model Foundation（已完成）

目标：建立稳定 Object identity、Name/Role/Lifecycle 历史、Relationship Graph 与 Object Resolver。

实现：Issue #6 / PR #7。

关键原则：

- Object ID 稳定；
- Name / Role 可以演化；
- Lifecycle 与 Role 分离；
- Core 保存 Graph；
- Person / Company / Project / BusinessLine 等通过 Role 表达，不建立平行 base entity。

### M2-B1 — Durable Atomic Information + Automatic Ingestion

实现 authority：Issue #9。

目标：把 contract-valid Atomic Information Candidate 自动吸收为长期 Atomic Information，不要求逐条人工审核。

需要实现：

- stable Atomic Information identity；
- append-only revisions；
- Evidence / context / confidence / source provenance 保留；
- 幂等导入与 origin collision fail-closed；
- `AtomicInformationStore` 抽象；
- JSONL 作为第一版正式 storage adapter；
- 私有数据不进入 public Git；
- normal processing workflow 可自动进入 durable ingestion。

本阶段不解释 Atomic Information 对 World Model 的影响。

### M2-B2 — Atomic Information → World Model Digestion & Lightweight Governance

实现 authority：Issue #10。

目标：判断长期 Atomic Information 对 World Model 的影响，并以最小治理闭环执行。

```text
Atomic Information
        ↓
补充 / 更新 / 冲突 / 不确定
        ↓
Governance
   ├─ 安全且明确 → 自动执行
   └─ 需要人判断 → Lightweight Change Proposal
        ↓
World Model Change
        ↓
Change Journal
```

原则：

- safe automatic change **不创建 Proposal**；
- Change Proposal 只为真正需要人判断的变化存在；
- 新建 Object、删除 Object、冲突、身份/关系不确定需要人类判断；
- 自动和人工批准的变化都进入 append-only Change Journal；
- 人类判断可以通过 prompt / CLI 完成，不要求 Web Review Center；
- 所有人类可见审核内容使用通俗业务语言；
- Object 创建与删除遵守孤立对象保护；
- 不建设 Proposal Queue、通用 Agent Contract、MCP/HTTP、Web Review Center。

### M2-B3 — Canonical Context Builder — Object scope v1

实现 authority：Issue #11。

目标：建立一个统一、可追溯、有限边界的上下文读取能力。

第一版：

```text
Context Builder
scope = Object
```

Context Bundle 组装：

```text
Object
+ current Name / Role / Lifecycle
+ one-hop Relationships
+ related current Atomic Information
+ bounded Evidence
+ recent Change Journal
+ pending human judgments
+ completeness / truncation metadata
```

原则：

- 只有一个 canonical Context Builder；
- 不建立 Object Context / Agent Context / Business Context Builder 等平行概念；
- bounded retrieval；
- provenance-aware；
- truncation/completeness 必须显式；
- pending judgment 不能伪装成 fact；
- read-only、deterministic、storage-independent；
- 第一版无 LLM、vector ranking、recursive graph、Web UI。

以后 Goal / question / business situation 等上下文需求继续扩展同一个 Context Builder。

### M2-B4 — Real End-to-End Validation

实现 authority：Issue #16。

这是进入后续真实资料扩展和迁移验证前的阶段门槛，不是新功能建设阶段。

使用至少一段真实家具经营录音验证：

```text
Recording
→ Processing
→ Atomic Information
→ B2 Digestion / Governance
→ World Model
→ Context Builder
→ 人工与原 transcript 对照
```

重点检查：

- 信息守恒；
- Evidence / provenance；
- Object resolution；
- 自动更新 / human judgment 边界；
- conflict / unresolved；
- false structuralization；
- Context completeness；
- pending judgment 是否与 fact 分离。

P0 信息丢失、P1 世界模型/治理错误阻止进入下一阶段。

B4 不允许为了“让测试通过”而顺手新增 ontology、Web、自有 Agent 或大型基础设施。

### M2-C1a — Managed Source 架构权威

架构 authority：Issue #21 / ADR-004。

Issue #21 只固化 Managed Source 的权威边界，不实现 runtime。后续执行顺序由以下主线决定：

```text
外部文件
  → 只读 intake candidate
  → 用户明确准入
  → 完整字节复制 + size/content_hash 校验
  → Managed Source（稳定 source_id、不可变快照）
  → Normalized Representation
  → Evidence / Atomic Information
```

后续主线阶段：

#### #22 — M2-C1b 本地 Managed Source 准入、校验与恢复（已完成）

实现本地受控 Source 区、用户明确准入、完整字节复制、大小与 `content_hash` 校验、Manifest 持久化和恢复验证。

#### #24 — M2-C1d 音频 Processing 切换到 Managed Source（已完成）

音频 Processing 已从 legacy 外部 path/hash provenance 切换到已验证的 Managed Source；新 Processing package 不再依赖外部绝对路径作为 Source / Evidence 权威。

#### #28 — M2-C2a 开源调查、复用治理与多格式基准（已完成）

完成官方开源能力调查、许可证与隐私边界评估，以及受控本地基准，为多格式 Adapter 提供复用证据。

#### #29 — M2-C2b Normalized Representation 公共契约（已完成）

固定格式无关的 Normalized Representation contract 与本地运行骨架。

#### #30 — M2-C2c 首批多格式 Adapter（已完成）

已按统一 contract 实现 Markdown、text PDF、XLSX、PPTX 与 image structural preflight；OCR / 复杂视觉语义继续后置。

#### #31 — M2-C2d Representation → Atomic Information contract（当前）

把具有可分析业务内容的 Normalized Representation 接入唯一 Atomic Information 生命周期，建立 stable Analysis Units、Candidate / Residue coverage、Evidence、strict package 与 durable ingestion contract。

#31 **不再承担正式 semantic execution provider 的选择**；其 production semantic provider 已拆到 #50。#31 可以在不选择 live Provider 的前提下完成 contract / plumbing，并保持 fail-closed。

#### #50 — M2-C2e Semantic Analysis Provider 验证（当前，可与 #48 并行）

独立验证正式 semantic execution path：比较 pinned/latest Codex、External Agent handoff 以及必要的其他成熟 Provider，回答哪条路线能够稳定、可审计、隐私可控地产生严格 structured result。

#50 不修改 Atomic Information / World Model 语义；其结论是后续真实多格式和微信 semantic digestion 的执行门禁。

#### #48 — M3-B1a 微信 Conversation Representation v1（#31 后，可与 #50 并行）

以现有真实微信 Source 验证 Conversation Representation、stable message locator、message-level Evidence、bounded context-only units、metadata preservation、analysis eligibility 与 deterministic batching。

#48 只把微信稳定地转换成可交给 #31 `RepresentationAnalysisProvider` contract 的 Analysis Units，**不负责选择 production semantic provider，也不在本 Issue 宣称真实微信已经完成长期 Atomic Information 消化**。

#### 微信真实 Semantic Digestion（#48 + #50 后，后续最小 Issue）

当 #48 的 Conversation Representation 通过 Architecture Review，且 #50 给出正式 provider route 后，再创建最小 Issue 完成：

```text
Conversation Analysis Units
→ production semantic provider
→ Atomic Information Candidate + Residue
→ Durable Atomic Information
```

真实 50 条微信的语义质量、遗漏/错误信息、Evidence 与 unresolved referent 在此阶段验收，而不是提前塞回 #48。

#### #32 — M2-C3a Information Consolidation 真实实验

在统一 Atomic Information 能稳定承接真实多格式 / Conversation 数据后，以真实、受控样本验证跨 Source 的 equivalent、derived、complementary、temporal_update、conflict、uncertain 等整理边界，不直接扩大为正式 runtime。

#### #33 — M2-C3b Information Consolidation 运行时

在 #32 的实验结论通过后，实现受治理的信息整理运行时。

#### #34 — M2-C4 Object Emergence

在 Information Consolidation 有充分证据后，研究从 Information 到长期 Object 的受治理形成边界。

#### 并行任务：#23 Handoff Marker（已完成，非主线 Gate）

Handoff Marker 只提供可选外部交接说明，不改变 Managed Source、Evidence 或 Processing 的权威边界。

共同的 runtime 原则：

- `01_inbox/` 作为第一版本地 Managed Source 根目录；
- 外部路径只保留为可失效 `ingested_from`，不再作为后续 Processing / Evidence 权威；
- 一个 `source_id` 对应一份不可变字节快照，新字节显式重新接入并创建新的 `source_id`；
- `content_hash` 支持完整性和存储去重，但不合并 Source provenance；
- TOS 只作为 storage adapter / replica；
- 不引入 Source version graph、`supersedes` 或 `version_of`。

### 横向能力：Human View / Frontend（已规划，后置，不启动）

Human View 是人类理解和治理 ArcheOS 的 Presentation 层，不是新的业务 truth，也不是当前 Stage 1 信息消化主线的前置条件。

第一版需求方向记录为：

- **Object Profile**：一个长期 Object 的名称、Role、Lifecycle、关键关系、当前认知与未决事项；
- **Relationship Graph / 向阳生长图**：可视化 Object 之间的长期关系，支持聚焦相关节点，但不把展示树结构反写为新的 canonical hierarchy；
- **Timeline**：展示与 Object 相关的重要 Atomic Information、Decision、Change Journal 与时间变化；
- **Information / Evidence Drill-down**：从结论展开到 Claim、Hypothesis、Evidence、Representation locator 和 Source；
- **Pending / Conflict / Unresolved**：明确展示待判断、冲突、不确定和 Context truncation，不把候选伪装成事实；
- **Human Judgment**：在真实使用证明 CLI / Agent prompt 审核效率不足时，为已有 Governance 提供轻量人类操作界面；不得另建第二套 Proposal / Review truth；
- **Context Preview**：让用户看到 Agent 实际会读取到的 bounded Context，帮助发现缺失、噪声和错误结构化；
- **Source / Intake Status**：查看 Source、Representation、processing/completeness/warnings，但不把前端变成通用文件管理器；
- **Protocol 执行 / Decision 追溯 View**：按阶段查看一次 Protocol 执行使用了哪些 Context、产生了哪些 `Derived Artifact`、哪些 Evidence / unresolved / Hypothesis 支撑最终 Judgment；该页面是 `Projection / View`，不创建 `Thinking Run / Decision Trace` Core；
- **Protocol Library**：查看当前有哪些 canonical `Protocol`、各自解决什么类型的问题、当前 active version、历史版本、状态与变更记录；
- **Pattern Library（前端可显示“模型库”）**：查看系统目前有哪些 canonical `Pattern`、适用于什么问题、输入输出、适用条件、限制、版本与状态，以及哪些 Protocol 阶段使用它；前端“模型”只是业务展示名称，不创建第二套 Model truth；
- **Decision → Protocol / Pattern Drill-down**：Decision 页面显示本次使用的 Protocol version、Pattern version、Policy snapshot、关键 Hypothesis、Evidence 与外部基础模型运行 provenance，并可跳转到对应详情；
- **Protocol / Pattern Governance**：未来允许用户观察、比较和追溯版本；编辑与激活分离，采用 draft → validate/compare → active → deprecated/rollback，不原地覆盖历史 Decision 使用过的版本。

旧 `sunward-operating-system` 前端可以作为设计素材，优先复用：

- React / React Flow 图交互经验；
- 经营树、关系聚焦、卡片与详情面板的视觉模式；
- Evidence 来源跳转；
- `confirmed / candidate / needs_review` 一类状态表达；
- unresolved questions、Timeline、经营态势等 Human View 经验。

不得直接继承旧前端的 canonical 数据模型和 API，包括 `roadmap / asset / branch` 物理实体语义、旧 `cognition_kind`、旧 cognitive relation vocabulary 或旧 Review Center 数据权威。

长期读取边界保持：

```text
Canonical State
  → Context Builder / Projection
  → View Model
  → Presentation
```

**当前不创建 Frontend 实现 Issue，不启动开发。** 只有当真实使用反复证明“人类理解 / 审核成为主要瓶颈”，或 Product Roadmap 进入需要 Human View 的阶段后，再由 ChatGPT Product / Technical Lead 根据当时的 canonical read contracts 创建最小 Human View Issue。

### M2-D — Migration Readiness & Clean-cut Plan

实现 authority：Issue #17。

只在前置真实数据链路无 P0/P1 blocker 后开始。

目标：盘点 Tolaria 与旧 `sunward-operating-system`，把旧资料/能力分成：

```text
KEEP
IMPORT
REBUILD
RETIRE
```

核心迁移原则：

- 旧 `sunward-operating-system` 已停止作为产品主线和数据权威，仅作为 migration source / design reference；
- 不长期 dual-read / dual-write；
- 旧开发态 structured state 不因为存在就必须兼容；
- Raw Source / Evidence / provenance 不可因 structured reset 被误删；
- IMPORT/REBUILD 必须映射到现有 canonical concepts；
- 迁移映射只用于**已经存在的旧代码、旧数据和旧语义**；不得用 mapping 为尚未开发的新设计保留平行概念；
- 缺失的真实业务语义交由 ChatGPT Product / Technical Lead 决策，不在 migration script 中偷偷造 schema；
- 前端资产优先判断为 `REBUILD / reference`，只复用可证明有价值的交互，不继承旧数据模型；
- #17 完成旧数据与前端资产盘点后，可正式 Archive 旧 `sunward-operating-system` 仓库；
- 先形成版本化 inventory / mapping / cutover plan，再创建少量单向 Import Issues。

旧系统真正值得借鉴的模式以“收敛后复用”为原则：

- Context Builder bounded / provenance / truncation；
- 真实数据作为阶段门槛；
- clean-cut migration；
- consequential change 的审计性；
- Human View 的图、卡片、Evidence drill-down 与经营态势表达。

旧系统以下复杂度不自动继承：Proposal Queue、Web Review Center、Agent Contract、旧 MCP/HTTP contract、D1 coexistence、长期 compatibility facade。

---

## M3 — Agent Integration + Conversation Ingestion

ArcheOS 不开发自己的 Agent。M3 的目标是让 Codex 与未来其他 External Agent 能够读取同一份受治理 Context，并把有长期价值的对话重新进入同一 Information lifecycle。

### M3-A — 可安装 CLI + Codex 只读接入（#35，已完成）

提供标准 `archeos` CLI、Workspace init/doctor、本地只读 MCP 与 Codex 一键接入。Agent 读取 canonical Context / Evidence，不获得绕过 Governance 的直接写权限。

### M3-B — Conversation Ingestion

Conversation 是输入 / Representation 形态，不是新的业务 Core。

当前顺序：

```text
#48 微信 Conversation Representation v1
↓
#50 gate 后的微信真实 Semantic Digestion
↓
#47 继续完成 Codex / ChatGPT 跨 Provider 对照研究
↓
#43 Codex Conversation production（按 #47 结论收敛）
↓
ChatGPT Export Provider（未来独立 Issue，按 #47 结论决定）
```

统一目标：

```text
WeChat / Codex / ChatGPT / future provider
→ provider-specific capture/import
→ Conversation Representation
→ Representation Analysis Units
→ production semantic provider
→ Atomic Information + Hypothesis + Residue + Evidence
→ Consolidation / World Model / Context
```

Provider 不建立自己的 Atomic Information 生命周期。

### M3-C — Workspace Portability（#44，已规划，当前后置）

跨机器 Workspace、snapshot、single-writer authority 和远端 replica 继续保留为规划项，但在单机真实数据消化与日常使用尚未稳定前不作为当前优先级。

---

## M4 — 主动认知与决策增强（Product Stage 2，后置探索）

ArcheOS 的职责是增强 External Agent / Human 的长期认知和决策依据，不实现自有 Decision Agent。真正的推理交给 External Agent；ArcheOS 负责用已有 `Protocol / Policy / Pattern / Context / Hypothesis / Judgment / Decision / Action / Feedback` 等 canonical concepts，让推理过程系统化、可控、可观察、可版本治理、可追溯。

M4 对应 Product Roadmap Stage 2 的主要技术探索。**Stage 1 未通过前不启动。**任何 M4 实现 Issue 在进入 Ready 前都必须重新读取当时的 `CONCEPTS.md` 并完成开发前 Concept Convergence。

### M4-A — Protocol 驱动的决策增强契约实验（#42，当前 blocked）

#42 启动时优先验证 canonical `Protocol`，而不是建设 `DecisionEngine` 或新的“思考系统”Core。

第一版建议流程：

```text
Signal / Event / Human request
→ Protocol
→ Context Builder
→ 候选 Action + 预期结果 + Hypothesis
→ Judgment：基于 Goal / Preference / Requirement / Policy / Evidence / Pattern
→ Challenge（Protocol 阶段标签）
→ Atomic Information Candidate（Agent recommendation / judgment）
→ Human Decision
→ Action / Commitment
→ Feedback
→ Hypothesis revision / updated Context
```

概念边界：

- `Protocol`：控制跨任务可复用的思考步骤、门禁和流转；
- `Policy`：控制最少候选 Action 数、风险偏好、时间范围、是否强制 Challenge 等可调参数；
- `Pattern`：承载反复问题对应的可复用解决结构；前端可以把 Pattern Library 显示为“模型库”；
- `Hypothesis`：记录会影响 Judgment / Decision、并可被后续 Evidence / Feedback 支持或反对的可检验命题；
- `Context Builder`：提供 Goal / World Model / Information / Evidence / Preference / Requirement / previous Decision / Feedback 等 bounded Context；不建立 Decision Context Builder；
- External Agent：执行真正推理；
- `Derived Artifact`：保存 Protocol 阶段的结构化中间结果；
- `Audit Event`：记录关键运行和版本 provenance；
- `Judgment`：表达 Agent 对候选 Action 的比较和推荐；
- Human `Decision`：正式决策，继续受治理且 human-in-the-loop。

候选 Action 回答“可以做什么”；Hypothesis 回答“为什么预期该 Action 会产生某个结果”。两者不需要新的 `Option` Core；Hypothesis 已由 `CONCEPTS.md` / ADR-005 定义为 Atomic Information 的 canonical 语义形态。

类似“先提出 3 个可行方案”不得写死为经营业务代码；它属于可版本化 `Policy` 与 Prompt 约束。代码只控制阶段顺序、权限、输入输出 contract、预算、失败处理与 human gate。

不要求、不展示、不长期保存外部模型的私有 chain-of-thought；系统只保存可检查的 `Derived Artifact`、Hypothesis、Judgment、Evidence、候选 Action 与 Feedback。

### M4-B — Protocol Governance（规划，未启动）

长期至少需要：

- `protocol_id + version`；
- 历史使用过的 version 不原地覆盖；
- draft / active / deprecated；
- 用户可观察当前 active version、历史版本、阶段定义、Prompt / Policy / Pattern 绑定；
- version diff、change rationale、created/approved provenance；
- 支持基于真实 Decision Case 做新旧版本对照；
- 新版本先 draft / validate，再 active；
- 必要时可以 rollback；
- 每次执行通过 Audit Event / Derived Artifact 固定记录实际使用的 Protocol version、Policy snapshot 与 Pattern version。

用户未来可以调整 Protocol，但“可编辑”与“立即生效”必须分离。

### M4-C — Pattern Library（前端可显示“模型库”，规划，未启动）

优先复用 canonical `Pattern` 表达用户所说的“问题解决模型 / 决策模型”，不新增 ReasoningModel / DecisionModel Core。

Pattern Library 至少需要回答：

1. 当前有哪些 Pattern；
2. 每个 Pattern 解决哪类重复问题 / 不解决什么；
3. 哪个 Protocol / stage 使用哪个 Pattern；
4. Pattern 需要哪些输入、产生哪些结构化输出；
5. assumptions / constraints / applicability；
6. 当前版本、状态、owner、来源与变更历史；
7. 哪些真实 Decision / Feedback / Hypothesis 支持或挑战该 Pattern；
8. 某次 Decision 实际使用的是哪个 Pattern version。

已经参与历史 Decision 的 Pattern version 不原地覆盖；修订产生新版本，并允许 compare / deprecate / rollback。

GPT / Codex / Claude 等外部基础大模型的 provider/model/version 属于 execution provenance，与 canonical Pattern 分开记录。

### M4-D — Decision 可追溯 View + Human Oversight（规划，前端后置）

不建立 `Thinking Run` 或 `Decision Trace` Core。未来通过 `Projection / View` 从 canonical state 与运行审计读取：

```text
Decision
→ Protocol version
→ Policy snapshot
→ Pattern version(s)
→ Hypothesis
→ 阶段 Derived Artifacts
→ Agent Judgment / Atomic Information Candidate
→ Context / Evidence
→ external model provenance
→ Feedback
```

前端应让用户清楚看到：为什么启动、使用了哪版 Protocol / Pattern、产生了哪些候选 Action、依赖哪些 Hypothesis 和 Evidence、经过什么 Judgment / Challenge、Human Decision 与 Agent 推荐有何差异、后续 Feedback 是否支持或反对原 Hypothesis。

这一能力与 Human View / Frontend 同步规划，但**当前不启动前端开发**。

### M4-E — 主动触发机制（更后置探索）

当被动 Protocol 被真实业务验证后，再研究何时应该主动启动思考。优先复用已有概念组合：

```text
Goal
+ Health / State
+ Requirement / Red Line
+ Signal / Event / opportunity
+ Policy
→ 启动 Protocol
```

暂不建设 `ThinkingTrigger`、`MotivationEngine`、`ValueSystem`、`CausalGraph` 或 autonomous Agent runtime。生存、增长、Vision、Red Line 等动力机制必须先通过真实业务实验验证，再决定是否需要新的 canonical concept。

---

## 当前推荐顺序

以下顺序是**当前 Product Stage 下关闭 Evidence Gap 的最佳已知技术顺序**，不是不可修改的长期产品承诺。若真实实验产生 Roadmap Feedback，应先判断影响范围，再按权威链修正 Product / Development Roadmap。

当前主线：

```text
                     #31
       Representation → Information contract
                     ↓
           ┌─────────┴─────────┐
           ↓                   ↓
          #48                 #50
 WeChat Conversation      Semantic Provider
  Representation            validation
           │                   │
           └─────────┬─────────┘
                     ↓
         微信真实 Semantic Digestion
              （后续最小 Issue）
                     ↓
                    #32
     Information Consolidation 真实实验
                     ↓
                    #33
       Information Consolidation runtime
                     ↓
                    #34
              Object Emergence
                     ↓
                    #17
       真实旧数据压力测试 / clean-cut
                     ↓
             Stage 1 Gate Review
```

并行 / 后置：

```text
#47 Conversation 跨 Provider 研究       微信 v1 后继续，不阻塞 #48
#43 Codex Conversation production       等 #47 收敛
#44 Workspace portability               后置
Human View / Frontend                    已规划，不启动
#42 Protocol 驱动的决策增强实验          Product Stage 2；当前 blocked
Protocol / Pattern Governance            Stage 2 后置规划，不启动
```

旧 `sunward-operating-system` 从现在起不再承担产品主线；新输入、新认知、新 Agent Context 与新功能均进入 ArcheOS。#17 只负责完成最后的旧数据 / UI 资产盘点和仓库正式 Archive 条件确认，而不是维持旧系统继续运行。

---

## 演化总原则

ArcheOS / 新向阳经营系统始终沿一条主线演化：

**Input → Processing → Atomic Information → Structured Object → Decision → Feedback**

并遵守：

1. `PRODUCT_SPEC.md` 定义长期产品方向与边界；`PRODUCT_ROADMAP.md` 定义 Product Stage 与 Stage Gate；本文件只能在该上游方向内安排技术演化；
2. 新的重大能力、集成、UI 或平台基础设施进入 Ready 前必须通过 `AGENTS.md` 的 Roadmap Alignment Check，说明它关闭哪个当前 Evidence Gap；
3. Experiment / Issue / PR / Real-world Validation 可以产生 Roadmap Feedback；失败实验只要获得了预期 Evidence，也可以是有效产品进展；
4. Core 概念以 `CONCEPTS.md` 为准；未开发概念必须在实现前完成收敛，不用 mapping table 延后概念决策；
5. 产品行为规则以 `INFORMATION_GOVERNANCE.md` 为准；
6. 优先复用已有概念，不建立平行模型；
7. Object ID 稳定，Name / Role / View 可以演化；
8. Atomic Information 与 World Model 分层，历史与 Evidence 不丢失；
9. 存储 adapter 可替换，不让业务规则依赖 SQLite / JSONL；
10. Change Proposal 只服务需要人类判断的变更，不成为所有写入的强制中间层；
11. Change Journal 保留自动和人工变更的审计链，但不成为第二份事实源；
12. Context Builder 是统一上下文组装能力，默认 bounded / provenance-aware / truncation-aware；
13. 真实数据验收是阶段门槛，不以 synthetic tests 代替真实语义验证；
14. 迁移采用 clean-cut / 单向导入；mapping 只解释已经存在的 legacy code/data/API，不为未开发设计保留平行术语；
15. Human View 只消费 canonical read contracts，不建立第二份 truth；
16. ArcheOS 不开发自己的 Agent，而是增强 External Agent / Human 的认知与决策能力；
17. 长期产品名回归“向阳经营系统”，但工程改名不得早于真实使用稳定和旧系统 clean-cut；
18. M4 实现必须在 Product Stage 2 到来时再次执行 Concept Convergence Check；流程阶段名、UI 名称、Prompt 字段和运行记录不得自动升级为 Core concept；
19. 使用 canonical `Protocol` 控制流程 / 门禁，`Policy` 控制可调参数，`Pattern` 承载可复用解决结构，`Hypothesis` 保存可检验命题，Prompt 只作为实现 artifact；
20. 历史 Decision 必须固定引用当时使用的 Protocol / Policy / Pattern 版本或 snapshot，并能追溯关键 Hypothesis / Evidence；
21. 决策可观测性通过 `Derived Artifact / Audit Event / Projection / View / Evidence` 实现，不新建 ThinkingRun / DecisionTrace truth，也不存储模型私有 chain-of-thought；
22. 前端“模型库”是 canonical Pattern Library 的业务显示名称，不创建第二套 Model truth；
23. 用户未来可以观察、比较、调整 Protocol / Pattern，但编辑与激活必须分离，变更先验证再生效，并保留回滚能力；
24. 不因为未来可能需要某个能力，就提前建设完整框架。
