# M2-C1 Source Archive User Workflow

## 技术摘要

用户体验应把“扫描”“加入 Archive”“生成 Normalized Representation”“删除原文件”分成四个独立动作。扫描默认零写入；Archive 需要用户明确选择；Normalization 不能改变原文件；删除必须晚于 Archive 验证且由用户单独确认。

第一版建议不让 ArcheOS 直接永久删除原文件。系统只显示删除资格和恢复信息，用户通过 Finder 或原系统处理；未来如果增加删除能力，也应只移动到系统废纸篓。

## 1. 用户扫描目录后看到什么

扫描完成后显示本地结果，不展示业务正文：

```text
Scanned files
  ├── new sources
  ├── already registered
  ├── already archived
  ├── exact duplicate candidates
  ├── derived/unknown lineage candidates
  ├── unsupported formats
  ├── sensitive/restricted sources
  └── estimated archive bytes
```

每个文件在本地界面可显示：

- 文件名和 original location；
- media type、size、modified time；
- hash status；
- archive status；
- processing status；
- duplicate/lineage hints；
- privacy warning；
- 可选动作。

公共报告和 GitHub 版本不显示这些真实值，只输出聚合统计。

## 2. 用户可以选择什么

| 动作 | 效果 | 是否写原目录 |
|---|---|---|
| Register only | 建立本地 Manifest，不复制文件 | 否 |
| Archive locally | 复制并验证到本地 Archive | 否 |
| Archive to TOS | 上传并验证到 TOS Archive | 否 |
| Skip | 不注册、不复制 | 否 |
| Mark restricted | 禁止远程处理，保留本地权限提示 | 否 |
| Review lineage | 查看 duplicate/derived/unknown 候选 | 否 |
| Normalize | 在派生区生成 representation | 否 |

扫描不能默认选择“Archive all”。本轮两个非 vault 文件集合分别约 6.89 GiB 和 1.27 GiB，界面应先显示预计新增 bytes、已去重 bytes 和受限文件数量。

## 3. 文件什么时候进入 Archive

只有用户明确执行 Archive，且通过以下 gate 时：

1. Source 仍可读；
2. hash 已计算；
3. privacy/storage policy 允许目标位置；
4. 用户确认 local 或 TOS；
5. duplicate 检查完成；
6. copy/upload 成功；
7. size/hash 验证成功；
8. Manifest 更新为 `archived`。

失败时状态为 `archive_failed`，原文件仍保留。系统不能因为生成了 preview、OCR 或 transcript 就宣称 Archive 成功。

## 4. 原目录如何留下说明

默认不在原目录留下任何文件，因为扫描承诺只读，而且 Git repository、共享目录和业务目录可能不允许额外 sidecar。

在用户明确授权写入后，可提供可选 sidecar：

```text
<original-name>.archeos-source.json
```

sidecar 只包含：

- `source_id`；
- archived time；
- archive status；
- hash prefix；
- 本地 restore/help 指引。

sidecar 不包含 credential、签名 URL、业务摘要或完整远端路径。对于 Tolaria 等 Git-managed directory，默认关闭 sidecar，避免污染 vault 和 Git 历史。

M2-C1 实验没有创建 sidecar。

## 5. 用户如何删除旧文件

第一版建议：ArcheOS 不直接删除，只提供 `eligible_for_user_removal` 提示。提示成立需要：

- `archive_status=archived`；
- 最近一次 restore/read verification 成功；
- archived hash 与 original hash 一致；
- 没有 archive warning；
- 用户看到 Source 仍可从何处恢复；
- 用户明确选择具体 original occurrence。

用户通过 Finder、Git 或原业务系统删除。ArcheOS 下次扫描时把 original occurrence 标记为 missing，但保留 Manifest 和 archived Source。

如果未来加入系统删除动作：

- 必须单文件或明确选择集；
- 显示完整路径和预计释放空间；
- 默认移动到系统废纸篓；
- 不允许默认永久删除；
- Archive 不可恢复时立即停止；
- 删除后重新读取 Manifest 和 Archive 状态。

## 6. 用户如何恢复

用户选择 Source 后看到：

- archive location 类型：local 或 TOS；
- archived size 和 verification time；
- original location 是否仍存在；
- 可恢复到原位置或用户选择的新位置；
- 恢复文件的 hash verification 结果。

恢复默认写到新文件，不覆盖同名现有文件。覆盖必须单独确认。

## 7. 用户如何理解 Normalized Representation

界面应明确区分：

```text
Source
  Original bytes: protected
  Archive: verified / not archived

Representations
  Text extraction: derived
  Page render: derived
  OCR: derived, confidence shown
  Transcript: derived, confidence shown
```

用户删除某个 representation 不影响 Source Archive；重新生成 representation 也不能改变 source hash。

## 8. 关键提示文案

- “扫描完成：未复制或修改任何文件。”
- “加入 Archive 会复制原始字节；不会删除原文件。”
- “Archive 尚未验证，暂不建议删除原文件。”
- “这是派生表示，不是原始 Source。”
- “发现完全重复内容；将复用 archived bytes，但保留两个原始位置记录。”
- “关系方向无法确认，需要人工选择或保持 unknown。”
- “此文件被标记为 restricted，不会发送到远程处理。”

## 本轮未执行

- 没有真实 Archive UI；
- 没有复制、上传、恢复或删除；
- 没有 sidecar；
- 没有 TOS credential 或 API 调用；
- 音频只做了只读元数据盘点，没有 Archive、恢复或 Normalization workflow smoke test。
