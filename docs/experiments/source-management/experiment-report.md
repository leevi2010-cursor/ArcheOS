# M2-C1 Source Archive Experiment Report

## 技术摘要

本轮设计实验支持一个初步结论：ArcheOS 可以建立安全、可追溯的 Source 管理层，但必须采用“只读注册、显式归档、字节验证、派生分离”的边界。扫描不应自动复制；Archive copy 不应被算作独立 Source；Normalized Representation 不应替代 Source；删除原文件必须是 Archive 验证之后的独立人工动作。

该结论目前仍是 design-level evidence。实验虽然验证了独立录音可以在不读取语音内容的情况下完成元数据盘点和 hash 去重检查，但没有真正复制 Source、上传 TOS、恢复文件或生成 Normalized Representation，因此不能宣称 Source Archive runtime 已通过验收。

## 实验问题与回答

### 原始文件是否需要保留？

需要。Archive 默认与原文件并存。只有用户确认、Archive 可恢复且 hash 验证一致时，系统才可以提示原 occurrence 具备删除资格。

### Archive 是否必须复制所有扫描文件？

不需要。建议 register-first：扫描只生成 Manifest 和风险提示。用户明确 Archive、原位置易失或需要跨设备恢复时才复制。Tolaria 等已有版本化存储的 Source 可以先保持 `registered_external`。

### 如何保证可追溯？

每个 Source 使用 opaque `source_id`，Manifest 同时记录 original location、archive location、source hash、media type、created/observed time、archive status 和 processing status。所有 Normalized Representation 引用 source_id 和生成时的 source hash。

### 如何处理重复？

相同 hash 的 bytes 只归档一次，但不同 original occurrences 继续保留各自位置和观察记录。Archive copy 不增加来源数量，也不能被当作多来源确认。

### 如何支持 TOS？

把 TOS 作为 Archive storage adapter。object key 基于内容 hash，不保存凭证；上传后验证 size/checksum，只有验证成功才更新 Manifest。具体 bucket、region、retention 和加密配置不属于 Source domain model。

### 用户如何删除旧文件？

第一版不建议 ArcheOS 直接删除。系统显示删除资格和恢复位置，用户使用 Finder、Git 或原系统删除。未来如实现，只允许显式移动到废纸篓，并在删除前后读回验证。

## 实测证据

| 证据 | 结果 | 对设计的影响 |
|---|---|---|
| 混合目录规模 | 223 files / about 6.89 GiB | 禁止 scan = archive all |
| Markdown corpus | 2,168 files，抽样 2 | Git/version context 应进入 Manifest 观察信息 |
| PDF | text/layout 与 drawing 两类 | Archive 必须保留原 PDF；representation 可多种并存 |
| Excel | 2 workbooks / 33 sheets / 799 media / 1,239 formulas | CSV 不能成为 Archive Source |
| Image | scene 与 document photo | privacy route 必须早于远程处理 |
| Exact duplicate | 跨目录 byte-identical pair | bytes 去重，occurrence 不删除 |
| Archive/extracted | 9 archive members 与 9 extracted files 全匹配 | container lineage 与 duplicate 必须显式 |
| Independent audio | 5 M4A / about 83.1 MiB / 77.7 minutes / 5 unique hashes | 音频可注册；不读取内容也可做 Source 级识别与 exact dedup |

## 推荐的 Source 生命周期

```text
discovered
  → registered
  → archive_pending (explicit user action)
  → archived (copy + verification)
  → normalized (derived representations)

failure branches:
  → archive_failed
  → source_missing
  → processing_unsupported / processing_failed
```

这不是数据库 schema，也不是 Migration framework。它只是实验中用于检验用户操作和 Manifest 状态是否自洽的最小状态描述。

## 设计通过的边界

- 原始目录只读；
- Archive 与 original location 分离；
- Archive copy 保持 byte-identical；
- Manifest 先于 Archive 存在，并在验证后更新；
- exact duplicate 复用 archived bytes；
- occurrences 与 content dedup 分离；
- Normalized Representation 与 Source 分离；
- TOS credential 不进入 Manifest；
- 删除与扫描/归档分离；
- 真实内容不进入 GitHub。

## 尚未通过的边界

- 没有真实 local archive copy/restore smoke；
- 没有 TOS upload/download/verification smoke；
- 没有音频 Archive copy/restore 或 Normalization smoke；
- 没有验证 source_id 在 rename/move 后的稳定策略；
- 没有验证超大文件中断续传；
- 没有验证 encrypted PDF、损坏文件和权限变化；
- 没有确定 local Manifest 的权限、加密和备份政策；
- 没有决定 Tolaria Git history 是否已经满足某些 Source Archive 保留要求。

## 不属于 M2-C1 的内容

- Object、Atomic Information、Claim、World Model；
- Information Digestion；
- 数据库 schema；
- Tolaria 或旧系统迁移；
- 全量目录整理；
- 真实文件上传；
- 自动删除和生命周期迁移框架。

## 下一步建议

1. 由 Architect 审查 `Source Manifest`、Archive copy 语义和 `source_id` 稳定策略。
2. 创建独立 implementation Issue，只用 synthetic fixtures 验证 register → local copy → verify → restore。
3. 增加一个 synthetic audio + transcript + summary lineage fixture；真实录音只在另行授权的本地 smoke 中验证 register、copy、verify 和 restore，且不得提交。
4. local archive 通过后，再单独验证 TOS adapter，不把 credential 写入测试或 Manifest。
5. 删除能力继续保持非目标；先验证恢复，再讨论 move-to-Trash UX。

## 最终结论

ArcheOS Source Archive 的可行最小形态不是“把所有文件搬进新目录”，而是：

> 以 Manifest 注册 Source，以显式动作归档原始字节，以 hash 验证 Archive，以 lineage 连接派生表示，并把删除权保留给用户。

该方向与本轮真实目录的规模、隐私和重复特征相符，但仍需要 Architect 授权和 synthetic runtime prototype 才能进入实现。
