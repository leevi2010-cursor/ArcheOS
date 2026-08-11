# M2-C1 Source Archive Design

## 技术摘要

建议采用 **register first, copy on explicit admission** 的 Source Archive 方案：扫描只建立本地 Manifest 草稿和风险提示，不自动复制 6.89 GiB 数据，也不自动复制 Tolaria vault。只有用户明确选择 Archive、原位置不具备长期保留能力，或后续处理需要稳定原始字节时，系统才复制并验证。

Archive 的职责是保存不可变的 Source 字节，不是整理目录、迁移系统、生成业务信息或替代原始文件。Normalized Representation 必须存放在独立派生区域，并通过 `source_id` 和源 hash 回到 Archive Source。

## 核心关系

```text
User Original File
  │ read-only observation
  ▼
Source Manifest (registered)
  │ explicit archive admission
  ▼
ArcheOS Source Archive (byte-identical copy)
  │ verified source_id + source hash
  ▼
Normalized Representation (derived, replaceable)
```

Manifest 在复制之前就需要存在，用于记录扫描和用户选择；Archive 成功后更新位置和状态。上图表达依赖关系，不表示扫描时自动复制。

## 原始文件是否保留

原始文件默认保留，并保持在用户原来的目录、名称和权限范围中。ArcheOS 不修改内容、mtime、文件名、目录结构或 Git 状态。

Archive 成功不等于允许删除原始文件。用户删除原文件必须是独立、显式的动作，并至少满足：

- Archive 已完成且 hash 验证一致；
- Archive 可以读取或恢复；
- Manifest 已持久保存；
- 相关 Normalized Representation 能回到 archived Source；
- 用户确认删除的是 original occurrence，而不是唯一未归档 Source；
- 删除动作可恢复，首选系统废纸篓而不是永久删除。

M2-C1 只设计该 gate，本实验没有执行删除。

## Archive 是否需要复制

### 不立即复制

- 用户只执行目录扫描；
- Source 已处于用户认可的长期、版本化、可恢复存储中；
- 文件体积很大，用户尚未选择保留范围；
- 隐私或权限尚未确认；
- 文件是完全重复内容，且已有可验证 archived blob；
- adapter 暂不支持，但 Source 原位置仍稳定可读。

### 应复制

- 用户明确执行“加入 Archive”；
- 原文件位于临时目录、移动设备、下载缓存或易失位置；
- 后续 Normalization 需要一个稳定、不可变的源字节基准；
- 原始目录可能改名、移动或由其他应用修改；
- 需要跨设备恢复或使用 TOS 作为长期 Source 存储；
- 合规或经营要求需要独立保留原始 Evidence。

### 已有受治理存储

Tolaria Markdown 已处于 Git/version 语境。M2-C1 不应默认再复制当前扫描到的 2,168 个 Markdown 文件。可先注册 repository、commit 和 relative path，Archive 状态保持 `registered_external`；只有保留政策明确要求独立副本时再归档。

## 建议的 Archive 操作协议

1. **Observe**：只读获取 media type、size、mtime 和 original location。
2. **Hash**：流式计算 SHA-256，不创建临时明文副本。
3. **Register**：创建或复用 `source_id`，状态为 `registered`。
4. **Deduplicate**：检查是否已有相同 hash 的已验证 archived bytes。
5. **Admit**：用户明确选择 local archive 或 TOS archive。
6. **Copy/Upload**：复制原始字节，不做转码、重压缩、OCR 或内容修改。
7. **Verify**：比较源与 Archive 的 size 和 SHA-256；远端至少验证上传结果，并在可行时读取复核。
8. **Commit Manifest**：只有验证通过后，设置 `archive_status=archived`。
9. **Normalize separately**：派生表示写入独立位置，引用 `source_id` 和源 hash。

任何一步失败都不能把状态伪装成 `archived`。失败记录应保留可操作原因，但不得包含凭证或真实内容。

## 如何避免重复

Archive 去重以内容 hash 为准，不以文件名为准：

- 相同 SHA-256 的 Source occurrence 复用同一份 archived bytes；
- 每个 original location 仍保留自己的 Manifest occurrence 和扫描时间；
- Archive copy 本身不创建新的业务 Source；
- 不同 hash 即使文件名相同，也不能自动合并；
- 相同模板、相同日期或同目录只产生 lineage hint，不作为去重证据；
- 不自动删除用户目录中的重复文件。

为避免把路径隐私泄露到公共报告，真实 `original_location` 和 `archive_location` 只应存在于本地 Manifest；导出报告必须脱敏。

## Archive 布局建议

以下只是存储布局候选，不是数据库 schema：

```text
archive-root/
├── sources/
│   └── sha256/<prefix>/<full-hash>/original-bytes
├── manifests/
│   └── <source-id>.json
└── normalized/
    └── <source-id>/<representation-id>/...
```

Source bytes、Manifest 和 Normalized Representation 分区存放，防止派生物覆盖原始 Source。文件扩展名可以作为可读元数据保留，但不参与内容 identity。

## 如何支持 TOS

TOS 应作为 Archive storage adapter，而不是新的 Source 模型：

- `archive_location` 使用不含凭证的 `tos://bucket/key` 形式；
- object key 建议基于内容 hash，避免文件名泄露和重复上传；
- 上传前本地计算 SHA-256；上传后验证对象大小和可用校验信息；
- 只有验证成功才更新 `archive_status`；
- TOS credential、access token、secret、签名 URL 不写入 Manifest；
- bucket policy、加密、region、retention 和访问日志由部署配置管理，不写入业务 Source；
- 同一内容对象已存在时，验证后复用，不覆盖；
- Manifest 保存必要的 object/version locator，但公共导出必须脱敏。

本轮没有执行 TOS API、上传或恢复测试，因此不能声称 TOS 路径已经通过验收。

## Archive 状态建议

| 状态 | 含义 |
|---|---|
| `registered` | 已观察并记录，未复制 |
| `registered_external` | 原文件处于用户认可的外部受治理存储，暂不复制 |
| `archive_pending` | 用户已选择 Archive，尚未完成 |
| `archived` | Archive bytes 已复制并验证 |
| `archive_failed` | 复制或验证失败，原文件仍是唯一有效来源 |
| `source_missing` | 原位置不可访问，不能据此假定 Archive 完整 |

状态数量保持最小。更复杂生命周期必须由后续 Architecture Review 决定。

## 安全边界

- 扫描不等于 Archive；
- Archive 不等于迁移；
- Archive 不改变用户原目录的权威；
- Normalization 不替代 Archive；
- TOS 不接管 ArcheOS domain semantics；
- hash 一致只证明字节重复，不证明业务等价；
- Source Archive 不创建 Object、Atomic Information 或 World Model。
