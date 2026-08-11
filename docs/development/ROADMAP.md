# ArcheOS 开发路线图

## 文档职责

本文件只定义 ArcheOS 的阶段演化顺序，不承担单个开发任务的实现规格。

- `AGENTS.md`：定义 Agent 的工作规则与权威关系。
- `docs/architecture/CONCEPTS.md`：定义 Core 概念。
- `docs/product/INFORMATION_GOVERNANCE.md`：定义信息吸收、自动更新与人工判断的产品规则。
- 本 `ROADMAP.md`：定义长期阶段顺序。
- GitHub Issue：定义当前一次开发必须交付什么；复杂 Issue 可以内嵌 Architect 批准的 Implementation Plan 与 Test Cases。
- Durable Spec / ADR：仅在稳定契约或架构决策需要跨多个 Issue 复用时建立。

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

目标：让 Processing 产出的信息进入长期 Note，并在受治理的边界下持续更新 Structured World Model。

长期方向：

```text
Atomic Information Candidate
        ↓
Durable Note
        ↓
Information Digestion / Governance
        ↓
Object + Name + Role + Lifecycle + Relationship
        ↓
Structured World Model
        ↓
Object Context
```

### M2-A — World Model Foundation

目标：建立稳定 Object identity、Name/Role/Lifecycle 历史、Relationship Graph 与 Object Resolver。

实现 authority：Issue #6 / PR #7。

关键原则：

- Object ID 稳定；
- Name / Role 可以演化；
- Lifecycle 与 Role 分离；
- Core 保存 Graph；
- Person / Company / Project / BusinessLine 等通过 Role 表达，不建立平行 base entity。

### M2-B1 — Durable Note + Automatic Ingestion

目标：把 contract-valid Atomic Information Candidate 自动吸收为长期 Note，不要求逐条人工审核。

需要实现：

- stable Note identity；
- append-only Note revisions；
- Evidence / context / confidence / source provenance 保留；
- processing candidate → Note 的幂等导入；
- NoteStore 抽象；
- JSONL 作为第一版正式存储 adapter；
- 私有 Note 数据不进入 public Git；
- 默认 processing workflow 可以进入 Note ingestion，不再以 per-note human review 作为强制 gate。

本阶段不解释 Note 对 World Model 的影响。

### M2-B2 — Note → World Model Digestion & Governance

目标：让 Note 结合当前 Structured World Model，判断它是补充、更新还是冲突，并按 `INFORMATION_GOVERNANCE.md` 执行。

需要实现：

- Note → existing Object 的保守识别与绑定；
- 补充信息自动吸收；
- 目标明确、Evidence 足够、无冲突且低风险的已有 Object 更新可以自动执行；
- 新建 Object、删除 Object、冲突、Object/Relationship 不确定等情况请求人类判断；
- 人类判断可以通过 prompt / conversation adapter 完成，不要求正式前端；
- 所有面向人的审核内容使用通俗业务语言；
- Object 创建与删除遵守孤立对象保护；
- 自动与人工变更都保留 Note/Evidence/历史与执行结果；
- 业务治理位于 Store/Repository 之上，不写死在 SQLite/JSONL adapter。

### M2-B3 — Object Context

目标：为后续 Domain Agent 提供一个统一、可追溯、有限边界的长期上下文读取入口。

输入一个 Object 后，能够组装：

```text
Object
+ current Name / Role / Lifecycle
+ Relationships + neighbor Objects
+ related Notes
+ Evidence
+ relevant history / recent changes
        ↓
   Object Context
```

要求：

- 只依赖 NoteStore / WorldModelRepository 等稳定 contract；
- 不直接绑定 JSONL 或 SQLite；
- 输出有明确数量边界和 completeness / truncation 信息；
- 不引入新的业务 ontology；
- 不需要 LLM、vector database 或正式 UI。

### M2-C — Human View（后置）

Object Profile、向阳生长树、Relationship Graph、Timeline 等人类展示能力暂不阻塞 M2-B1～B3。

Core 保持：

```text
Structured World Model
  → Projection / View Model
  → Presentation
```

前端和 HTML renderer 在长期数据闭环稳定后再进入实现。

---

## M3 — Domain Agent

目标：在 Note、Structured World Model 与 Object Context 之上增加销售、品牌、项目等领域解释能力，而不污染 Core。

候选：Sales Agent、Brand Agent、Project Agent。

Domain Agent 优先读取 Object Context，而不是各自重新扫描全部原始资料。

---

## M4 — 决策与反馈闭环

目标：让结构化信息参与 Goal → Decision → Action → Feedback，并保留依据与结果反馈。

---

## M5 — 多格式输入

目标：把已经验证的 Processing 能力扩展到 PDF、图片、PPT、视频等输入格式，不复制新的对象体系或生命周期。

---

## 演化总原则

ArcheOS 始终沿一条主线演化：

**Input → Processing → Atomic Information → Structured Object → Decision → Feedback**

并遵守：

1. Core 概念以 `CONCEPTS.md` 为准；
2. 产品行为规则以 `INFORMATION_GOVERNANCE.md` 为准；
3. 优先复用已有概念，不建立平行模型；
4. Object ID 稳定，Name / Role / View 可以演化；
5. Note 与 World Model 分层，历史与 Evidence 不丢失；
6. 存储 adapter 可替换，不让业务规则依赖 SQLite / JSONL；
7. 先用真实数据验证当前阶段，再进入下一阶段；
8. 不因为未来可能需要某个能力，就提前建设完整框架。
