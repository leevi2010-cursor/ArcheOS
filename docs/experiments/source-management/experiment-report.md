# M2-C1 Source Archive Experiment Report

## 技术摘要

本轮实验支持一个收敛结论：ArcheOS 可以设计安全、可追溯的 Source 接入层，但系统权威必须是 **Managed Source**，而不是用户原目录。扫描只产生临时 intake candidate；只有用户明确归档、复制完整原始字节并通过 `size` 与 `content_hash` 校验后，才创建稳定 `source_id`。

归档完成后，Managed Source 成为后续 Processing、Evidence 和 Normalized Representation 的唯一权威输入。外部原文件可以继续存在，也可以被用户删除，但只是旧入口或可失效的 `ingested_from` 提示；系统不自动监控、同步或重新处理它。

该结论仍是 design-level evidence。实验没有真正创建 Managed Source、执行 copy/restore、上传 TOS 或生成 Normalized Representation，因此不能宣称 Source Archive runtime 已通过验收。

## 实验问题与回答

### Source 什么时候成立？

扫描时只产生临时 candidate，不产生 durable `source_id`。用户明确准入、完整字节写入受控位置、`size` 与 `content_hash` 验证成功后，才创建 Managed Source identity。

### 用户原文件与 Managed Source 如何共存？

物理上可以同时存在：用户原目录中的旧文件，以及系统内部的 Managed Source 完整字节快照。归档后两者角色不同：前者是旧入口/来源线索，后者是系统权威。它们不能被计算为两份独立 Evidence。

### 外部文件发生变化怎么办？

归档后系统不再自动跟踪外部文件。用户在原目录继续修改，不会改变 Managed Source、触发重新处理或覆盖既有 Evidence。需要更新时，用户回到向阳经营系统显式重新导入或编辑；新内容必须生成新的受管字节快照，保留旧快照及其 Evidence。本实验不决定新快照是否沿用原 `source_id`。

### 如何保证可追溯？

Managed Source Manifest 以 `source_id`、`managed_location`、受管快照 `content_hash`、size、归档时间和可用性为主。`ingested_from` 只记录最初接入来源，允许失效，不参与后续 Evidence 定位。每次复制或恢复都以 verification record 记录当时的观察 hash、受管 hash、size 和验证时间。

### 如何处理相同字节？

相同 `content_hash` 说明字节一致，可支持受控存储去重；它不自动证明相同来源语境，也不自动合并两个 Source registration。单次接入的外部旧文件和其 Managed Source 不增加 Evidence 数量。

### 如何表示派生关系？

只有确证的父子生成事实才使用方向明确的 `derived_from`。相同内容由 `content_hash` 表达；证据不足时保留 candidate/warning，不创建 `unknown` durable edge。Normalized Representation 引用 Managed Source 和生成时的 `content_hash`，不自动制造新的 Source。

### 如何支持 TOS？

TOS 作为 Managed Source 的 storage adapter。TOS replica 只记录在 `storage_replicas` 中，不形成新的 Source；上传后验证成功才标记 replica verified。credential、签名 URL 和部署细节不进入 Manifest。本实验不实现远程存储。

### 用户如何处理原文件？

系统只在 Managed Source 已验证、Manifest 已持久化且恢复路径清楚时提示用户具备删除资格。第一版不直接永久删除；用户可以自行保留或删除外部旧文件。交接说明只能在用户另行授权后写入。

## 实测证据

| 证据 | 结果 | 对 Managed Source 设计的影响 |
|---|---|---|
| 混合目录规模 | 223 files / about 6.89 GiB | 扫描不能等于全量归档，必须显式准入 |
| Markdown corpus | 2,168 files，抽样 2 | 先保留接入线索；是否建立 Managed Source 由用户选择 |
| PDF | text/layout 与 drawing 两类 | Managed Source 保留原 PDF，表示可以多种并存 |
| Excel | 2 workbooks / 33 sheets / 799 media / 1,239 formulas | CSV 不能成为完整 Managed Source 快照 |
| Image | scene 与 document photo | 先完成隐私判断，再决定是否准入和派生处理 |
| 相同字节样本 | 跨目录 byte-identical pair | 可做 content dedup，但不自动合并来源语境 |
| Archive/extracted | 9 archive members 与 9 extracted files 全匹配 | 只提供内容等价/派生候选，不自动建立 durable edge |
| Independent audio | 5 M4A / about 83.1 MiB / 77.7 minutes / 5 unique hashes | 可做接入候选盘点；未创建 Managed Source 或归档副本 |

## 正交状态模型

本实验不使用一条总生命周期。建议分开记录：

```text
Managed Source availability
  available / unavailable

Storage replica status
  local: verified
  TOS: pending or absent

Normalized Representation status
  each representation: complete / failed / stale

Handoff marker status
  not_requested / written / not_written
```

这些状态可以同时存在：外部旧文件缺失不等于 Managed Source 不可用；一个 OCR 失败不等于 Managed Source 归档失败；TOS replica 尚未创建不等于 local Managed Source 无效。

这只是实验设计表达，不是数据库 schema、正式 Source contract 或迁移框架。

## 设计通过的边界

- 扫描只产生临时 intake candidate；
- Managed Source 只有显式准入、复制和 size/content_hash 校验成功后才成立；
- `source_id` 是 Managed Source 稳定身份，不依赖外部路径；
- `managed_location` 是系统后续读取的权威位置；
- `ingested_from` 可以失效，不参与 Evidence 定位；
- 外部旧文件与 Managed Source 不增加独立 Evidence 数量；
- 相同 `content_hash` 支持存储去重，但不自动合并 provenance；
- 只有确证父子事实才建立 `derived_from`；
- storage replica、representation、handoff marker 状态正交表达；
- 已被 Evidence 引用的 Managed Source 字节不可原地覆盖；
- 真实内容不进入 GitHub。

## 尚未通过的边界

- 没有真实 Managed Source local copy/restore smoke；
- 没有 TOS upload/download/verification smoke；
- 没有音频 Managed Source copy/restore 或 Normalization smoke；
- 没有验证重新导入时新快照的 `source_id` 策略；
- 没有验证超大文件中断续传；
- 没有验证 encrypted PDF、损坏文件和权限变化；
- 没有确定 Managed Source 本地权限、加密和备份政策；
- 没有实现或决定 handoff marker runtime。

## 不属于 M2-C1 的内容

- Object、Atomic Information、Claim、World Model；
- Information Digestion；
- 数据库 schema 或正式 Source contract；
- Tolaria 或旧系统迁移；
- 全量目录整理；
- 真实文件上传；
- 自动删除和生命周期迁移框架；
- 新快照沿用或更换 `source_id` 的正式版本策略。

## 下一步建议

1. Architect 审查 Managed Source identity、`managed_location`、`content_hash` 与重新导入边界。
2. 创建独立 implementation Issue，只用 synthetic fixtures 验证 candidate → explicit admission → managed copy → verify → restore。
3. 增加 synthetic audio + transcript + summary fixture，验证 representation 引用 Managed Source，而不把表示自动注册成 Source。
4. local Managed Source 验收后，再单独验证 TOS storage adapter，不把 credential 写入测试或 Manifest。
5. 另行定义新受管快照的 version identity 策略；在此之前禁止覆盖已被 Evidence 引用的字节。

## 最终结论

ArcheOS Source Archive 的最小安全形态不是“长期跟踪原目录”，而是：

> 先以只读扫描发现 intake candidate；经用户明确准入后，把完整字节复制并验证为 Managed Source；以后只以 Managed Source 作为系统权威，用 `ingested_from` 保留可失效的来源线索，用 `content_hash` 保证快照完整性和去重，用 `derived_from` 表达确证派生，并让外部旧文件退出后续权威链路。

该方向符合本轮真实目录的规模、隐私和重复特征，但仍需要 Architect 审核和后续 synthetic runtime prototype 才能进入实现。
