# ArcheOS 开发路线图

## 文档职责

本文件只定义 ArcheOS 的阶段演化顺序，不承担单个开发任务的实现规格。

- `AGENTS.md`：定义 Agent 的工作规则与权威关系。
- `docs/architecture/CONCEPTS.md`：定义 Core 概念。
- `docs/product/INFORMATION_GOVERNANCE.md`：定义信息吸收、自动更新与人工判断的产品规则。
- 本 `ROADMAP.md`：定义长期阶段顺序。
- GitHub Issue：定义当前一次开发必须交付什么；复杂 Issue 可以内嵌 Architect 批准的 Implementation Plan 与 Test Cases。
- Durable Spec / ADR：仅在稳定契约或架构决策需要跨多个 Issue 复用时建立。

产品名称长期使用 **向阳经营系统（Sunward Operating System）**；`ArcheOS` 仅作为当前重构迁移阶段的工程 / 仓库代号。

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

这是进入迁移和 Domain Agent 前的**阶段门槛**，不是新功能建设阶段。

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

B4 不允许为了“让测试通过”而顺手新增 ontology、Web、Domain Agent 或大型基础设施。

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

#### M2-C2 — 多格式 Normalized Representation（占位）

在 Managed Source runtime 稳定后，扩展 PDF、图片、PPT、视频等格式的派生表示。具体 Issue 等前置能力完成后再设计。

#### M2-C3 — Information Consolidation（占位）

在多格式表示可靠后，研究跨表示、跨 Source 的信息整理边界。具体 Issue 等前置能力完成后再设计。

#### M2-C4 — Object Emergence（占位）

在 Information Consolidation 有足够证据后，研究从 Information 到长期 Object 的受治理形成边界。具体 Issue 等前置能力完成后再设计。

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

### 横向能力：Human View（后置）

Object Profile、向阳生长树、Relationship Graph、Timeline 等人类展示能力继续后置，不阻塞 B1～B4、Migration Readiness 或首个 Domain Agent。

长期读取边界保持：

```text
Canonical State
  → Context Builder / Projection
  → View Model
  → Presentation
```

未来 Human View 与 Domain Agent 应尽量消费同一 canonical state/read contracts，而不是建立第二份 read truth。

### M2-D — Migration Readiness & Clean-cut Plan

实现 authority：Issue #17。

只在 B4 无 P0/P1 blocker 后开始。

目标：盘点 Tolaria 与旧 `sunward-operating-system`，把旧资料/能力分成：

```text
KEEP
IMPORT
REBUILD
RETIRE
```

核心迁移原则：

- 旧系统只作为 migration source，不作为新架构权威；
- 不长期 dual-read / dual-write；
- 旧开发态 structured state 不因为存在就必须兼容；
- Raw Source / Evidence / provenance 不可因 structured reset 被误删；
- IMPORT/REBUILD 必须映射到现有 canonical concepts；
- 缺失的真实业务语义交由 Architect 决策，不在 migration script 中偷偷造 schema；
- 先形成版本化 inventory / mapping / cutover plan，再创建少量单向 Import Issues。

旧系统真正值得借鉴的模式以“收敛后复用”为原则：

- Context Builder bounded / provenance / truncation；
- 真实数据作为阶段门槛；
- clean-cut migration；
- consequential change 的审计性。

旧系统以下复杂度不自动继承：Proposal Queue、Web Review Center、Agent Contract、MCP/HTTP、D1 coexistence、长期 compatibility facade。

---

## M3 — Domain Agent

M2-D readiness 完成后开始。

目标：在 Atomic Information、Structured World Model 与 Context Builder 之上增加领域解释能力，而不污染 Core。

第一条优先：**Sales Agent**。

后续候选：Brand Agent、Project Agent。

Domain Agent：

- 优先读取 canonical Context Builder；
- 不重新扫描全部 raw sources 作为常规路径；
- 不各自建立 Context Builder；
- 可以产生报告、判断、建议和需要治理的变更请求；
- 不因为领域术语新增平行 Core ontology。

至少一个真实 Domain Agent 工作流通过后，才具备正式 cutover 旧向阳写入路径的关键条件之一。

---

## M4 — 决策与反馈闭环

目标：让结构化信息参与 Goal → Decision → Action → Feedback，并保留依据与结果反馈。

---

## 当前推荐顺序

```text
#9  M2-B1 Durable Atomic Information
 ↓
#10 M2-B2 Digestion + Lightweight Governance
 ↓
#11 M2-B3 Context Builder — Object scope v1
 ↓
#16 M2-B4 Real End-to-End Validation
 ↓
#21 M2-C1a Managed Source 架构权威
 ↓
#22 M2-C1b 本地 Managed Source 准入 / 校验 / 恢复
 ↓
#24 M2-C1d 音频 Processing 切换到 Managed Source
 ↓
M2-C2 多格式 Normalized Representation
 ↓
M2-C3 Information Consolidation
 ↓
M2-C4 Object Emergence
 ↓
#17 M2-D Migration Readiness / Clean-cut Plan
 ↓
M3 Sales Agent
 ↓
单向 Imports / Cutover（按 Readiness 结果拆分）
```

Issue #23 Handoff Marker 在 #22 后可并行，不阻塞 #24 主数据链。旧数据压力测试与迁移准备不得先于上述 #22、#24 和 M2-C2～C4 主线阶段；此前 #17 保持阻塞。

Human View 可在核心链路稳定后并行进入，但不作为上述主线的前置依赖。

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
12. 不因为未来可能需要某个能力，就提前建设完整框架。
