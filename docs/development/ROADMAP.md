# ArcheOS 开发路线图

## 文档职责

本文件只定义 ArcheOS 的阶段演化顺序，不承担单个开发任务的实现规格。

- `AGENTS.md`：定义 Agent 的工作规则与权威关系。
- `docs/architecture/CONCEPTS.md`：定义 Core 概念。
- `docs/product/INFORMATION_GOVERNANCE.md`：定义信息吸收、自动更新与人工判断的产品规则。
- 本 `ROADMAP.md`：定义长期阶段顺序。
- GitHub Issue：定义当前一次开发必须交付什么；复杂 Issue 可以内嵌 Architect 批准的 Implementation Plan 与 Test Cases。
- Durable Spec / ADR：仅在稳定契约或架构决策需要跨多个 Issue 复用时建立。

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

#### #22 — M2-C1b 本地 Managed Source 准入、校验与恢复

实现本地受控 Source 区、用户明确准入、完整字节复制、大小与 `content_hash` 校验、Manifest 持久化和恢复验证。它是后续 Processing 切换的前置条件。

#### #24 — M2-C1d 音频 Processing 切换到 Managed Source

把音频 Processing 从 legacy 外部 path/hash provenance 切换到已验证的 Managed Source；不改变既有 Source、Evidence 和 Atomic Information 的语义边界。

#### #28 — M2-C2a 开源调查、复用治理与多格式基准

在正式多格式 Adapter 前完成官方开源能力调查、许可证与隐私边界评估，以及受控本地基准。它可以与 #23、#24 并行，但不实现 runtime。

#### #29 — M2-C2b Normalized Representation 公共契约

在 #28 的调查结论基础上固定多格式派生表示的最小公共 contract，不实现具体格式 Adapter。

#### #30 — M2-C2c 首批多格式 Adapter（内部并行）

在 #29 后按已批准 contract 实现首批格式 Adapter；内部可以按格式并行，但不得形成平行 Representation 语义。

#### #31 — M2-C2d Representation → Atomic Information

在首批 Representation 稳定后，把可追溯的派生表示接入 Atomic Information 入口，保留 Source 与 locator 证据链。

#### #48 — M3-B1a 微信 Conversation Representation 与统一信息消化接入

在 #31 稳定后，以现有真实微信 Source 验证 Conversation Representation、message-level Evidence、bounded context 与统一信息消化。微信只做 provider-specific mapping，不建立微信专用长期信息模型。

#### #32 — M2-C3a Information Consolidation 真实实验（内部并行）

以真实、受控样本验证跨表示与跨 Source 的整理边界，不直接扩大为正式 runtime。微信 Conversation 在可用后应成为真实实验的重要输入之一。

#### #33 — M2-C3b Information Consolidation 运行时

在 #32 的实验结论通过后，实现受治理的信息整理运行时。

#### #34 — M2-C4 Object Emergence

在 Information Consolidation 有充分证据后，研究从 Information 到长期 Object 的受治理形成边界。

#### 并行任务：#23 Handoff Marker

Issue #23 可以在 #22 完成后并行处理用户交接说明。它不阻塞 #24 的主数据链，也不改变 Managed Source、Evidence 或 Processing 的权威边界。

共同的 runtime 目标：

- `01_inbox/` 作为第一版本地 Managed Source 根目录；
- 外部路径只保留为可失效 `ingested_from`，不再作为后续 Processing / Evidence 权威；
- 一个 `source_id` 对应一份不可变字节快照，新字节显式重新接入并创建新的 `source_id`；
- `content_hash` 支持完整性和存储去重，但不合并 Source provenance；
- TOS 只作为 storage adapter / replica；
- 不引入 Source version graph、`supersedes` 或 `version_of`。

上述 runtime 和后续阶段不属于 Issue #21 的实现范围；Issue #21 不实现复制、恢复、删除、TOS 或多格式 adapter。

### 横向能力：Human View / Frontend（已规划，后置，不启动）

Human View 是人类理解和治理 ArcheOS 的 Presentation 层，不是新的业务 truth，也不是当前信息消化主线的前置条件。

第一版需求方向记录为：

- **Object Profile**：一个长期 Object 的名称、Role、Lifecycle、关键关系、当前认知与未决事项；
- **Relationship Graph / 向阳生长图**：可视化 Object 之间的长期关系，支持聚焦相关节点，但不把展示树结构反写为新的 canonical hierarchy；
- **Timeline**：展示与 Object 相关的重要 Atomic Information、Decision、Change Journal 与时间变化；
- **Information / Evidence Drill-down**：从结论展开到 Claim、Evidence、Representation locator 和 Source；
- **Pending / Conflict / Unresolved**：明确展示待判断、冲突、不确定和 Context truncation，不把候选伪装成事实；
- **Human Judgment**：在真实使用证明 CLI / Agent prompt 审核效率不足时，为已有 Governance 提供轻量人类操作界面；不得另建第二套 Proposal / Review truth；
- **Context Preview**：让用户看到 Agent 实际会读取到的 bounded Context，帮助发现缺失、噪声和错误结构化；
- **Source / Intake Status**：查看 Source、Representation、processing/completeness/warnings，但不把前端变成通用文件管理器。

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

**当前不创建 Frontend 实现 Issue，不启动开发。** 只有当真实使用反复证明“人类理解 / 审核成为主要瓶颈”后，再由 Architect 根据当时的 canonical read contracts 创建最小 Human View Issue。

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
- 缺失的真实业务语义交由 Architect 决策，不在 migration script 中偷偷造 schema；
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

ArcheOS 不开发自己的 Agent。M3 的目标是让 Codex 与未来其他 Agent 能够读取同一份受治理 Context，并把有长期价值的对话重新进入同一 Information lifecycle。

### M3-A — 可安装 CLI + Codex 只读接入（#35，已完成）

提供标准 `archeos` CLI、Workspace init/doctor、本地只读 MCP 与 Codex 一键接入。Agent 读取 canonical Context / Evidence，不获得绕过 Governance 的直接写权限。

### M3-B — Conversation Ingestion

Conversation 是输入 / Representation 形态，不是新的业务 Core。

当前顺序：

```text
#48 微信 Conversation production v1
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
→ Atomic Information + Residue + Evidence
→ Consolidation / World Model / Context
```

Provider 不建立自己的 Atomic Information 生命周期。

### M3-C — Workspace Portability（#44，已规划，当前后置）

跨机器 Workspace、snapshot、single-writer authority 和远端 replica 继续保留为规划项，但在单机真实数据消化与日常使用尚未稳定前不作为当前优先级。

---

## M4 — 主动认知与决策增强（后置探索）

ArcheOS 的职责是增强外部 Agent / Human 的长期认知和决策依据，不实现自有 Decision Agent。

目标方向：

```text
Context / Evidence / Preference / Constraint / Goal
→ 外部 Agent 主动发现问题、探索、学习和提出 Decision Proposal
→ Human Judgment / governed Decision
→ Action / Commitment
→ Feedback 重新进入 Information lifecycle
→ 更新后的 Context
```

#42 负责未来真实业务决策契约实验，当前保持 blocked。正式 Goal、consequential Decision 仍保持 human-in-the-loop；不提前建设 MotivationEngine、ValueSystem、CausalGraph 或 autonomous Agent runtime。

---

## 当前推荐顺序

已完成阶段不再重复执行；当前主线从 #31 继续：

```text
#31 Representation → Atomic Information
 ↓
#48 微信 Conversation → 统一信息消化
 ↓
#32 Information Consolidation 真实实验
 ↓
#33 Information Consolidation 运行时
 ↓
#34 Object Emergence
 ↓
#17 真实旧数据压力测试 / clean-cut readiness
```

并行 / 后置：

```text
#47 Conversation 跨 Provider 研究       后续继续，不阻塞 #48
#43 Codex Conversation production       等 #47 收敛
#44 Workspace portability               后置
Human View / Frontend                    已写入 Roadmap，不启动
#42 主动认知 / 决策增强实验              blocked
```

旧 `sunward-operating-system` 从现在起不再承担产品主线；新输入、新认知、新 Agent Context 与新功能均进入 ArcheOS。#17 只负责完成最后的旧数据 / UI 资产盘点和仓库正式 Archive 条件确认，而不是维持旧系统继续运行。

---

## 演化总原则

ArcheOS / 新向阳经营系统始终沿一条主线演化：

**Input → Processing → Atomic Information → Structured Object → Decision → Feedback**

并遵守：

1. Core 概念以 `CONCEPTS.md` 为准；
2. 产品行为规则以 `INFORMATION_GOVERNANCE.md` 为准；
3. 优先复用已有概念，不建立平行模型；
4. Object ID 稳定，Name / Role / View 可以演化；
5. Atomic Information 与 World Model 分层，历史与 Evidence 不丢失；
6. 存储 adapter 可替换，不让业务规则依赖 SQLite / JSONL；
7. Change Proposal 只服务需要人类判断的变更，不成为所有写入的强制中间层；
8. Change Journal 保留自动和人工变更的审计链，但不成为第二份事实源；
9. Context Builder 是统一上下文组装能力，默认 bounded / provenance-aware / truncation-aware；
10. 真实数据验收是阶段门槛，不以 synthetic tests 代替真实语义验证；
11. 迁移采用 clean-cut / 单向导入，除非出现真实不可丢数据或外部消费者才重新讨论 compatibility；
12. Human View 只消费 canonical read contracts，不建立第二份 truth；
13. ArcheOS 不开发自己的 Agent，而是增强外部 Agent / Human 的认知与决策能力；
14. 长期产品名回归“向阳经营系统”，但工程改名不得早于真实使用稳定和旧系统 clean-cut；
15. 不因为未来可能需要某个能力，就提前建设完整框架。
