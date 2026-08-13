# ADR-004 — Managed Source authority and immutable Source identity

- Status: Accepted
- Date: 2026-08-13
- Decision authority: Issue #21 / Architecture Review

## Context

M1 的 Processing 已经可以从本地音频路径生成 Processing package，并把 `source_id` 传递到 Evidence 和 Atomic Information。早期实现把文件名、路径和 hash 组合用于 Source ID，并在 manifest 中保存外部绝对路径。这适合验证 M1 的 provenance 链，但不适合作为向阳经营系统的长期 Source 身份模型：外部文件可能被移动、修改或删除，路径也不是系统内部的权威存储。

PR #20 的 M2-C1 Source Archive Experiment 验证了只读扫描、显式归档、字节校验、Manifest、Lineage 和派生表示之间的边界。Architecture Review 进一步要求把 Managed Source 设为系统权威，并避免把外部旧文件、Archive replica、Normalized Representation 或 Handoff Marker 升级成平行 Source 概念。

本 ADR 固化长期架构边界，不实现 Source runtime，不改造当前 Processing 代码，也不定义数据库 schema。

## Decision

### 1. Source 的正式定义

`Source` 是经过用户明确准入、由系统保存为不可变字节快照，并具有稳定 `source_id` 的正式信息输入。

Source 位于 Input / Information provenance 边界：

- Source 不是 World Model `Object`；
- Source 不因名称、外部路径、文件名、当前格式支持或所在目录而获得身份；
- Source 是后续 Processing、Evidence 和 Normalized Representation 的权威输入；
- Source 字节进入 Evidence 链后，不得由同一 `source_id` 原地覆盖。

### 2. Managed Source 成为系统权威

外部文件的长期角色是临时 intake candidate 或可失效的接入线索：

```text
外部文件
  → 用户明确准入
  → 完整字节复制
  → size / content_hash 校验
  → Managed Source
```

只有复制到系统受控位置并完成校验后，才创建稳定 `source_id`。归档完成后：

- Managed Source 是系统后续 Processing、Evidence 和派生表示的唯一权威输入；
- 外部文件不再被系统自动跟踪、同步或重新处理；
- 外部文件可以被用户保留、移动或删除，不影响已验证的 Managed Source；
- 用户若要使用外部文件的修改，必须显式重新接入并形成新的 Source；
- 外部文件和 Managed Source 物理共存时，不计算为两份独立 Evidence。

第一版本地 Managed Source 根目录为 `01_inbox/` 的受控 Source 区。实际 Source 字节和 Manifest 保持本地、Git-ignored。

### 3. 不可变快照身份

第一版正式决定：

```text
一个 source_id = 一份不可变 Managed Source 字节快照
```

因此：

- 新字节内容必须创建新的 `source_id`；
- 不引入 `source_version_id`；
- 不允许同一 `source_id` 指向被覆盖后的新字节；
- 第一版不自动创建 `supersedes` / `version_of` 关系；
- 如果能证明新 Source 由旧 Source 派生，只使用 `derived_from`；
- 单纯“这是新版”但缺乏明确生成关系时，先保留为两个 Source，不发明版本 ontology。

新快照的 ID 生成算法由后续 implementation Issue 固定。它必须是 opaque、稳定且具有足够低碰撞概率的内部 ID，例如 `src_<uuid>`；不得由外部路径、文件名或 `content_hash` 单独决定。

### 4. `content_hash` 与 `ingested_from`

`content_hash` 是某个受管字节快照的内容身份，用于：

- 复制和恢复后的完整性校验；
- 受控存储中的字节去重；
- 依据原始快照重现实验和派生表示。

`content_hash` 不表示业务真实性，不单独定义 `source_id`，也不自动合并不同接入语境的 provenance。

`ingested_from` 是可选的历史接入来源提示：

- 可以因原文件移动或删除而失效；
- 不参与后续 Evidence 定位；
- 不构成第二个 Source；
- 不代表系统继续监控该位置。

### 5. Normalized Representation 与 Evidence

Normalized Representation 是从某个 Managed Source、其生成时的 `content_hash` 和指定 adapter/version 产生的可替换派生表示。它不是 Source，也不提高 Evidence 来源数量。

Evidence 必须引用 Managed Source 的 `source_id`，或引用明确属于该 Source 的 Normalized Representation；不得依赖外部绝对路径作为长期定位。Representation 删除或重算不能改变 Source 字节。

### 6. Source Lineage

第一版唯一持久化的方向性 Source lineage 为：

```text
child Source derived_from parent Source
```

规则：

- 必须有生成收据、明确 parent/source 记录或人工确认；
- 相同 `content_hash` 表达内容等价，不建立 duplicate lineage edge；
- `original` 不是 Source 间关系；
- 证据不足时保留 candidate / warning，不建立 `unknown` durable edge；
- wikilink、同目录、文件名和时间相似都不能自动建立 `derived_from`；
- Archive replica 和 TOS replica 都不是新的 Source 或 lineage edge。

### 7. Handoff Marker

Handoff Marker 是用户授权后写在外部旧目录中的交接说明。只有 Managed Source 已复制、校验且 Manifest 已持久化后才允许写入。

它不是 Source、Evidence、Lineage 或同步机制。marker 只引导用户通过 `source_id` 回到系统更新。Git 仓库、共享目录或无写权限目录可以不写 marker。

## Consequences

### Positive

- 系统内部有单一、稳定且可验证的 Source 权威；
- 外部路径失效不会破坏 Managed Source、Evidence 或派生表示；
- Evidence 可以在未来恢复同一不可变字节快照；
- Source、Object、Atomic Information 和 Evidence 的层次边界清楚；
- TOS 可以作为存储 adapter 引入，而不形成第二套 Source 身份模型；
- 新字节必须产生新身份，避免历史 Evidence 被静默改写。

### Costs

- 需要显式准入和复制校验流程；
- Source runtime 需要维护受控本地存储和 Manifest；
- 外部文件的后续修改不会自动进入系统，用户必须显式重新接入；
- 迁移旧 M1 path/hash provenance 需要单独的 implementation Issue；
- 新快照的版本展示策略仍需后续架构决策。

## Non-decisions

本 ADR 不定义：

- Source Registry 或 Source runtime 的实现；
- 文件复制、恢复、删除或 Handoff Marker runtime；
- TOS API、bucket、region、retention 或加密配置；
- 数据库 schema 或具体 Manifest storage schema；
- `source_id` 的具体生成算法；
- `source_version_id`、`supersedes`、`version_of` 或其他版本关系；
- PDF、Excel、Markdown、Image 或其他格式 adapter；
- 当前 M1 `process_audio()` 的代码改造。

## Migration note

当前 `archeos.pipeline.process_audio()` 的直接外部路径输入、文件名 stem + SHA-256 前缀 Source ID、以及 processing manifest 中的外部绝对路径，均属于 M1 legacy provenance。它们保持可读以支持历史包，但不再作为未来 Source 身份模型。后续 Source runtime / provenance migration Issue 必须在不破坏旧 Evidence 历史的前提下替换这些依赖。
