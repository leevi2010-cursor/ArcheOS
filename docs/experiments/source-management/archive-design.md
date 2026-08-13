# M2-C1 Source Archive Design

## 技术摘要

建议采用 **register candidate first, create Managed Source on explicit admission** 的方案。扫描只产生临时 intake candidate、结构观察和风险提示，不建立长期 Source 权威，也不自动复制数据。用户明确归档后，系统复制完整原始字节，校验 `size` 与 `content_hash`，再创建稳定的 `source_id` 并持久化 Managed Source Manifest。

归档完成后，ArcheOS 后续 Processing、Evidence 和 Normalized Representation 只引用 Managed Source。外部原文件变成旧入口或接入线索；它可以失效、移动或被用户删除，系统不再自动跟踪其变化。

## 核心关系

```text
外部零散文件
  = intake candidate / historical hint
  │ 用户明确归档
  ▼
复制完整原始字节 + size/content_hash 校验
  ▼
Managed Source
  = 稳定 source_id + managed_location + content_hash
  │
  ├── Evidence / Processing 的唯一权威输入
  └── Normalized Representation（可替换派生物）
```

Manifest 可以在扫描阶段以临时 candidate 形式存在，但正式 `source_id` 的创建点必须在 Managed Source 已成功写入并验证之后。这里的先后关系不是一条总生命周期；注册/可用性、存储副本、表示状态和交接说明分别记录。

## 身份边界

| 名称 | 本实验定义 | 不是 |
|---|---|---|
| `source_id` | 已完成用户准入、复制和校验的 Managed Source 稳定内部身份 | 外部路径、文件名或 hash 的别名 |
| `content_hash` | 某个受管字节快照的内容身份，用于完整性校验、存储去重和历史复现 | 业务真实性或语义等价证明 |
| `managed_location` | 系统内部权威存储位置；未来可由 local 或 TOS adapter 承载 | 用户原目录路径 |
| `ingested_from` | 可选的最初接入来源提示；可以失效、移动或被删除 | 后续 Evidence 定位 |
| handoff marker | 外部目录中的可选交接说明 | Source、Evidence 或同步机制 |

外部文件和 Managed Source 在物理上可以同时存在，但角色不同：外部文件是用户侧旧副本/接入入口，Managed Source 是系统权威。它们不能被计算为两份独立 Evidence。

## 原文件与 Managed Source

归档前，外部文件是待接入材料。归档时必须复制完整原始字节，不做转码、压缩、OCR 或内容修改；复制完成后比较大小和 `content_hash`。只有验证通过，Managed Source 才可用，`source_id` 才可持久化。

归档后：

- 系统不再自动监控外部文件；
- 外部文件发生修改不会改变 Managed Source；
- 不会因为外部文件变化自动重新处理或覆盖 Evidence；
- 用户需要回到向阳经营系统中更新；
- 外部文件若要再次进入系统，必须执行显式重新导入；
- 已被 Evidence 引用的 Managed Source 字节不得原地覆盖。

后续更新应生成新的受管字节快照并保留旧快照及其 Evidence。本实验不决定新快照是否沿用原 `source_id`，版本身份策略留给后续正式 ADR。

## 什么时候复制

### 不立即复制

- 用户只执行目录扫描；
- 用户尚未选择要纳入系统的范围；
- 权限、隐私或存储策略尚未确认；
- 文件位于已有可恢复、受治理的外部版本系统，但用户尚未要求独立 Managed Source；
- 当前只需要 intake 分析，不需要建立系统权威。

### 应复制

- 用户明确执行“加入向阳经营系统”；
- 外部位置易失、可移动或可能被其他应用修改；
- 后续 Processing 需要稳定、不可变的字节基准；
- 需要跨设备恢复或由 TOS 承载受管存储；
- 保留政策要求系统持有完整原始快照。

Tolaria Markdown 已处于 Git/version 语境。M2-C1 不应默认复制当前扫描到的 2,168 个 Markdown 文件；可以先作为 intake candidate 或历史来源提示，是否创建 Managed Source 必须由用户明确选择。

## 建议的归档协议

1. **Observe**：只读获取 media type、size、`observed_at` 和临时 intake 位置。
2. **Hash**：流式计算候选字节的 SHA-256，不创建临时明文副本。
3. **Admit**：用户明确选择把该候选纳入系统。
4. **Write Managed Source**：复制完整字节到受控 managed location。
5. **Verify**：比较候选与受管字节的 size 和 `content_hash`。
6. **Create identity**：验证成功后创建稳定 `source_id`，持久化 Managed Source Manifest。
7. **Record replica status**：分别记录 local/TOS storage replica 的验证状态，不把它们当作新的 Source。
8. **Normalize separately**：派生表示引用 `source_id`、生成时的 `content_hash`、adapter/version 和自身状态。
9. **Write handoff marker**：仅在用户另行授权且前述 Manifest 已持久化后，才向外部目录写交接说明。

任何复制或校验失败都不能创建可用的 Managed Source，也不能把外部文件宣传为系统权威。失败原因应可操作且不包含凭证或真实内容。

## 如何避免重复

`content_hash` 只表达字节一致：

- 受控存储可以按 `content_hash` 复用一份字节对象；
- 两个无法证明同一来源语境的外部接入候选，不因 hash 相同而自动合并为同一 `source_id`；
- 同一次接入中的外部旧文件和 Managed Source 不计为两份独立 Evidence；
- 文件名、路径、日期、模板相似度不能替代 hash 校验或来源判断；
- 不自动删除外部目录中的文件。

本实验不建立长期 `observed_locations[]` 历史模型。`ingested_from` 只保留最初接入来源的可选、可失效提示；后续 Evidence 定位使用 Managed Source。

## Managed Source 存储布局候选

以下是存储布局候选，不是数据库 schema：

```text
managed-root/
├── sources/
│   └── <source-id>/original-bytes
├── manifests/
│   └── <source-id>.json
└── normalized/
    └── <source-id>/<representation-id>/...
```

`managed-root/sources/<source-id>/original-bytes` 是系统权威位置；Normalized Representation 独立存放，不能覆盖 Managed Source。内部布局可以由 storage adapter 实现，不能改变 Source 身份语义。

## 如何支持 TOS

TOS 应作为 Managed Source 的 storage adapter：

- TOS replica 是 Managed Source 的受管存储副本，不是新的 Source；
- object key 可以基于 `source_id` 与 `content_hash`，但不得泄露不必要的文件名；
- 上传前计算 `content_hash`，上传后验证对象大小和可用校验信息；
- 只有验证成功才把该 replica 标为 `verified`；
- credential、access token、secret、签名 URL 不写入 Manifest；
- bucket、region、retention、加密和访问日志属于部署配置；
- 同一 Managed Source 的 TOS replica 不提高 Evidence 来源数量，也不替代 local authority 的语义定义。

本轮没有执行 TOS API、上传或恢复测试，不能声称 TOS adapter 已通过验收。

## 正交状态建议

不要使用一条 `discovered → registered → archived → normalized` 总生命周期。至少分开记录：

| 维度 | 示例 | 说明 |
|---|---|---|
| Managed Source availability | `available` / `unavailable` | 系统权威字节当前是否可读；不由外部路径决定 |
| Storage replica status | `pending` / `verified` / `failed` | local 或 TOS 各自记录，不互相覆盖 |
| Normalized Representation status | `complete` / `failed` / `stale` | 每个表示独立记录，可同时存在多个结果 |
| Handoff marker status | `not_requested` / `written` / `not_written` | 外部交接说明的独立状态 |

外部旧文件丢失不等于 Managed Source 丢失；某个 OCR 失败不等于 Managed Source 归档失败；TOS replica 尚未创建不等于 local Managed Source 不可用。

## 安全边界

- 扫描只产生临时 intake candidate，不建立长期 Source 权威；
- Managed Source 只有在复制与 size/content_hash 校验后才成立；
- 后续 Processing 与 Evidence 只引用 Managed Source；
- 外部原文件可以保留、失效或被用户删除，不自动回写系统；
- handoff marker 不是 Source，也不是同步机制；
- Normalized Representation 不替代 Managed Source；
- TOS 不创建新的 Source 模型；
- 本实验不创建 Object、Atomic Information、World Model、数据库 schema 或 runtime。
