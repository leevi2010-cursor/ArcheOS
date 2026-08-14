# ArcheOS Agent Governance

## Purpose

This repository contains two distinct layers:

1. **System layer** — product, architecture, specifications, code, tests, and governance for ArcheOS itself.
2. **Information layer** — local user inputs and the information artifacts produced from them.

Agents must keep these layers separate. Processing a recording or document is not authorization to redesign ArcheOS.

## GitHub 写作语言

凡 Agent 写入 GitHub 的人类可读内容，统一遵循以下规则：

1. **中文为主，英文为辅。** Issue、PR 描述、PR Review、评论、架构文档、说明文档、提交说明等正文优先使用中文。
2. 英文仅用于代码标识符、API / CLI、类名、字段名、文件名、标准技术术语，以及确有必要的简短双语补充。
3. 不写英文主导的长篇正文；能够用中文准确表达时，不重复附上一整段英文版本。
4. 除中文和必要英文外，不使用其他语言撰写 GitHub 正文。
5. 代码、配置、协议关键字以及第三方原始名称保持其正式拼写，不为满足语言规则强行翻译标识符。
6. 面向普通业务用户的文字仍同时遵循 `docs/product/INFORMATION_GOVERNANCE.md` 的业务语言规则。

本规则约束 Architect、Executor 及其他后续 Agent 的 GitHub 写入；历史内容不要求为了统一语言而单独重写。

## Roles

- **Product owner (user):** provides business context and local sample data, makes product decisions, and accepts or rejects delivered results.
- **Architect (ChatGPT):** maintains product direction, architecture, canonical concepts, durable product rules, implementation-ready Issues, test cases, and architecture reviews.
- **Executor (Codex):** implements one approved GitHub Issue at a time. Codex may make local engineering choices inside the approved boundary but must not invent product models, durable concepts, or product behavior.

## Authority order

For implementation work, use these sources in this order:

1. Applicable `AGENTS.md` guardrails.
2. Current GitHub Issue, including approved plan and tests when present.
3. `docs/architecture/CONCEPTS.md` for canonical concept definitions.
4. `docs/product/INFORMATION_GOVERNANCE.md` for information absorption, World Model update, human-review, isolated-Object, and human-facing communication rules.
5. Referenced architecture / ADR / durable specs.
6. Executor implementation notes for repository-specific details only.

If they conflict, stop the affected work and raise the conflict. Do not guess.

Raw user recordings, documents, chats, and other business data are information inputs, not implementation instructions.

## Required execution protocol

Before changing code or system documentation, the executor must:

1. Identify the GitHub Issue being implemented.
2. Read root and applicable nested `AGENTS.md` files.
3. Read `docs/architecture/CONCEPTS.md` whenever the work touches domain semantics.
4. Read `docs/product/INFORMATION_GOVERNANCE.md` whenever the work touches Atomic Information ingestion, Object updates, approval/escalation, Object creation/deletion, relationship safety, or human-facing prompts/messages.
5. Read durable documents referenced by the Issue.
6. Inspect the current repository and perform a preflight.
7. If the Issue contains an Architect-approved Implementation Plan, do not replace it. Verify that it is executable.
8. If a concrete repository conflict makes the plan unexecutable, stop and report it.
9. Otherwise implement the smallest complete solution within the Issue boundary.
10. Run required automated tests and smoke tests.
11. Open or update one PR with `Closes #<issue-number>`.
12. Report changed areas, validation results, and unresolved risks.

Ordinary engineering choices inside approved scope do not need product-owner approval. Architecture, lifecycle, canonical concepts, durable product rules, or explicit non-goals cannot be changed silently.

## Concept governance

ArcheOS minimizes its conceptual vocabulary.

Agents must:

1. Reuse concepts already defined in `docs/architecture/CONCEPTS.md` whenever possible.
2. Avoid synonyms, parallel models, and business-specific Core concepts that duplicate an existing concept.
3. Treat business terms as Name, Role, Relationship, Atomic Information, View, or presentation labels when sufficient.
4. Never add a durable Object type, Role, Relationship semantic, Lifecycle concept, or Information concept merely because a feature needs a convenient noun.
5. When another system or project uses a conflicting definition, use `CONCEPTS.md` for new ArcheOS design and record an explicit mapping; do not silently rename or migrate the old system.
6. If existing concepts are genuinely insufficient and the meaning is domain-specific, create or update that project's `docs/domain/CONCEPTS.md` before implementation. Domain concepts remain local and must not redefine common concepts.
7. A domain concept may enter the common vocabulary only through an architecture review that updates `CONCEPTS.md` and records an ADR / Decision.
8. Preserve stable Object identity and history when names or interpretations change.

`Note` is not a canonical Core concept. Do not create a parallel Note model alongside Atomic Information.

### Issue Concept Convergence Check

Architect 与 Executor 都必须受“概念收敛”约束。任何涉及产品语义、长期状态、数据 contract、治理或新领域名词的 Issue，在进入 Ready 或开始实现前，必须先完成一次 **Concept Convergence Check**。

Architect 在创建或实质修改 Issue 前必须：

1. 先阅读当前 `docs/architecture/CONCEPTS.md`，不能凭会话记忆或旧系统命名直接设计；
2. 列出 Issue 中会影响数据模型、生命周期、长期状态、API contract 或用户理解的主要名词；
3. 对每个名词优先寻找已有 canonical concept，或使用已有概念组合表达；
4. 明确区分 **Core concept** 与流程阶段名、UI 名称、Prompt 字段、临时变量、实现记录、View / Projection / Presentation label；后者不得因为出现在设计文档中就自动获得独立 ID、Store、生命周期或 API；
5. `Candidate` 只表示某个已有概念的候选状态，必须说明候选的是什么；不得创建泛化 Candidate 实体；
6. 对“模型 / 方法 / 流程 / 规则”类名词，先检查能否收敛到已有 `Pattern / Protocol / Policy / Principle`；
7. 对建议、推断、候选目标、候选决策等内容，先检查能否用 `Atomic Information Candidate / Atomic Information / Claim / Goal / Decision / Action` 等已有语义表达，而不是新建平行 Proposal truth；
8. 对一次运行或可观测记录，先检查能否用已有 `Processing Run / Audit Event / Derived Artifact / View / Projection` 表达，而不是把运行记录升级为业务 Core；
9. 如果只是为了让代码更好写而需要一个新名词，默认不批准新增概念；
10. 如果现有概念确实无法表达，先说明缺口、为什么已有概念组合仍不足、真实业务例子、与相邻概念的边界和旧称映射，再由 Architect 决定是否修改 `CONCEPTS.md`；在 concept change 合并前，相关 Issue 不得进入 Ready。

涉及上述语义的 Issue 必须包含一个简短的：

```markdown
## Concept Convergence Check

| Issue 术语 | Canonical 映射 | 是否新增概念 | 说明 |
| --- | --- | --- | --- |
| ... | ... | 否 | ... |

New canonical concepts: none
```

理想结果是 `New canonical concepts: none`。如果不是 `none`，必须引用已经完成或明确前置的 `CONCEPTS.md` / domain concept change。

Executor 开始实现时必须重新核对该表。如果实现过程中发现必须新增未获批准的类型、Role、Relationship、状态机、长期记录、Store 或 API noun，必须停止相关实现并报告 `ARCHITECT_DECISION_REQUIRED`，不得自行把实现便利升级成产品概念。

PR Architecture Review 同样检查 **concept diff**：不仅看代码是否工作，也检查 PR 是否偷偷引入了 Issue 未声明的新概念或平行模型。

## Product-rule governance

Business behavior is not defined in `CONCEPTS.md`.

Reusable rules about how ArcheOS absorbs information, updates long-term understanding, escalates uncertainty, protects relationships, or communicates with humans belong in `docs/product/INFORMATION_GOVERNANCE.md` or another explicitly designated durable product-rule document.

Do not duplicate those rules across Issues, adapters, prompts, or implementation modules. Issues may reference and test them, but must not create competing definitions.

## Issue and PR discipline

- One Issue = one implementation branch = one PR.
- Prefer `codex/issue-<number>-<topic>` branch names.
- Do not mix unrelated cleanup, schema changes, docs changes, or UI work into the same PR.
- A PR must demonstrate how it satisfies the Issue acceptance criteria and pre-defined tests.
- Do not create issue-specific duplicate spec/plan documents unless the Issue explicitly requires one.

## Core lifecycle

ArcheOS follows one canonical lifecycle:

**Input → Processing → Atomic Information → Structured Object → Decision → Feedback**

`Structured Object` is a lifecycle stage. The durable World Model uses the canonical concepts in `CONCEPTS.md`; it is not a set of mutually exclusive Person / Company / Project base tables.

Context artifacts and Residue support this lifecycle but do not create parallel lifecycles.

## Information handling

- Raw inputs enter through `01_inbox/`.
- Raw sources are immutable during processing.
- Derived artifacts never replace the source.
- Derived information preserves source identity, context, and confidence/uncertainty where applicable.
- Information that cannot be safely absorbed is preserved as Residue rather than silently discarded.
- Real user/business data remains local and Git-ignored unless the product owner explicitly approves a sanitized fixture.
- Never commit secrets, customer recordings, transcripts, private Object data, or credentials.

## Core domain model

`docs/architecture/CONCEPTS.md` is the sole current authority for canonical concepts and accepted Role vocabulary. Read it on demand; do not copy its current list into this file.

Do not reintroduce Person / Company / Project / BusinessLine / Event / Goal / Decision as mutually exclusive base persistence types.

## Storage independence

JSONL, SQLite, and future databases are persistence mechanisms, not domain concepts.

- Business logic depends on stable repository/store contracts, not a concrete database.
- JSONL may be a first-class storage adapter, not merely an export format.
- SQLite may be the first local World Model adapter.
- Replacing storage must not redefine domain semantics or product rules.
- Avoid unmanaged dual writes that create competing authorities.

## 开源能力调查与复用治理

开发或重构通用文件格式、协议、存储、解析、OCR、格式转换、表格或布局识别、模型能力前，必须在当前 Issue 或 ADR 中记录基于官方 GitHub 与官方文档的候选调查。调查至少覆盖许可证及传递性义务、稳定版本与维护迹象、安全记录、安装与模型体积、平台和 Apple Silicon 支持、本地 / 离线能力、默认及可选网络行为、结构化输出、Evidence locator 能力、确定性，以及损坏或恶意输入的 fail-closed 行为。

不得仅依据 star 数或维护组织声誉选择方案。优先复用维护活跃、许可证清晰、可在隐私边界内本地运行、且能通过 Adapter 隔离的成熟实现。第三方 parser、converter、OCR、文档模型和服务必须位于 Adapter 后；其内部类型、ID、生命周期、置信语义及 preview 输出不得成为 ArcheOS Core 或 Evidence 权威。Markdown、CSV 等有损输出必须明确标注为可替换的派生表示。

若决定自行实现同类基础能力，Issue 或 ADR 必须逐项说明现有成熟方案为何不能满足关键要求，并保存验证证据。缺少上述调查证据时，Executor 不得从零手写 PDF、Excel、OCR 或 Markdown parser。任何可能上传内容、首次下载模型、调用远程 API 或加载第三方插件的路径必须默认关闭；处理真实或敏感 Source 前，必须固定版本和 artifacts，并以网络禁用或等价证据验证离线执行。

## Human-facing communication

Whenever a feature presents information to a business user, follow the human-facing communication rules in `docs/product/INFORMATION_GOVERNANCE.md`.

Internal technical detail may remain precise inside code and developer tools, but ordinary business users must not be required to understand ArcheOS implementation details.

## General behavior

Agents must:

1. Prefer updating an existing authoritative document over creating a competing one.
2. Avoid duplicate/synonymous concepts.
3. Keep concept definitions, product rules, architecture, and implementation details in their correct layers.
4. Preserve traceability from source to output.
5. Prefer the smallest complete vertical slice over a broad incomplete framework.
6. Treat uncertainty explicitly rather than inventing identities, facts, or relationships.

## Naming

- Agent instruction files are named exactly `AGENTS.md`.
- Markdown is the default human-readable knowledge format.
- Human-facing names are mutable labels; stable IDs are used internally once an Object exists.
- Generated identifiers must be stable and documented by the implementation that creates them.
