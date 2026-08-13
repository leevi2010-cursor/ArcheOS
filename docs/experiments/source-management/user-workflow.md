# M2-C1 Source Archive User Workflow

## 技术摘要

用户体验应把“扫描”“明确归档”“生成派生表示”“处理外部旧文件”分成独立动作。扫描默认零写入，只显示临时 intake candidate；只有用户明确选择归档、系统完成完整字节复制和 `size`/`content_hash` 校验后，才建立 Managed Source 和稳定 `source_id`。

归档后，Managed Source 成为系统权威。外部原文件是旧入口或接入线索，系统不再监控其变化。用户需要更新时回到向阳经营系统显式重新导入或编辑；外部文件可以由用户自行保留或删除。

## 1. 用户扫描目录后看到什么

扫描完成后显示本地结果，不展示业务正文：

```text
Intake candidates
  ├── not yet admitted
  ├── already managed
  ├── content-equivalent candidates
  ├── derived-from candidates / warnings
  ├── unsupported formats
  ├── sensitive/restricted sources
  └── estimated managed storage bytes
```

每个候选可显示：

- 文件名和临时接入位置；
- media type、size、`observed_at`；
- candidate hash status；
- 是否已有 Managed Source；
- privacy warning；
- content-equivalence 或 lineage warning；
- 可选动作。

扫描结果不是 durable Source，不创建稳定 `source_id`。公共报告和 GitHub 版本不显示真实位置、文件名或业务正文，只输出聚合统计。

## 2. 用户可以选择什么

| 动作 | 效果 | 是否写外部原目录 |
|---|---|---|
| Register candidate only | 保留临时 intake 分析，不创建 Managed Source | 否 |
| Admit to Managed Source | 复制完整字节、校验并创建 `source_id` | 否 |
| Skip | 不准入、不复制 | 否 |
| Mark restricted | 禁止远程处理，保留本地权限提示 | 否 |
| Review warning | 查看 content-equivalence 或 `derived_from` 候选 | 否 |
| Normalize | 从 Managed Source 生成独立 representation | 否 |
| Write handoff marker | 在归档完成后写用户授权的交接说明 | 是，需单独授权 |

扫描不能默认选择“Archive all”。本轮两个非 vault 文件集合分别约 6.89 GiB 和 1.27 GiB，界面应先显示预计 Managed Source 新增 bytes、可复用的受管 bytes 和受限文件数量。

## 3. 文件什么时候成为 Managed Source

只有用户明确执行 Admit，且通过以下 gate 时：

1. 候选字节仍可读；
2. `content_hash` 和 size 已计算；
3. privacy/storage policy 允许目标位置；
4. 用户确认纳入向阳经营系统；
5. 完整原始字节已写入 managed location；
6. Managed Source 与候选的 size 和 `content_hash` 一致；
7. Manifest 已持久化并创建稳定 `source_id`。

在第 7 步之前，只有 intake candidate；不能向用户宣称它已经是系统 Source。失败时保留可操作错误，外部文件不被修改，不能把失败状态伪装成 Managed Source 可用。

## 4. 归档后原目录如何留下说明

归档完成以后，推荐提供可选交接说明。只有以下条件满足后才允许写入：

1. 用户明确选择归档；
2. Managed Source 已成功复制；
3. size 与 `content_hash` 校验通过；
4. Manifest 已持久化；
5. 用户授权向外部目录写入说明文件。

建议支持两种形式：

```text
<原文件名>.archeos.md
向阳经营系统归档说明.md
```

业务语言示例：

```text
此文件已经归档到向阳经营系统。

请不要在此位置继续修改。
如需更新，请回到向阳经营系统，并打开：

Source ID：<source_id>
系统位置：<人类可读的内部位置或入口>

此目录中的旧文件可由用户自行保留或删除。
```

说明文件不得包含 TOS credential、签名 URL、secret、真实业务摘要或不必要的远端存储细节。Git 仓库、共享目录或没有写权限的位置可以不写 marker，并在 Manifest 中记录 `written=false`。只读扫描阶段不写原目录。

## 5. 归档后用户如何更新

```text
Managed Source 已验证
  → 系统停止跟踪外部旧文件
  → 用户回到向阳经营系统
  → 显式上传或编辑新版本
  → 创建新的受管字节快照
  → 保留旧快照及其 Evidence
```

用户如果继续修改外部旧文件：

- 不会自动改变 Managed Source；
- 不会自动触发重新处理；
- 不会覆盖已被 Evidence 引用的字节；
- 需要显式重新导入才会进入新的准入流程。

本实验不决定新快照是否沿用原 `source_id`，但明确禁止原地覆盖已被引用的 Managed Source 字节。

## 6. 用户如何处理外部旧文件

第一版建议 ArcheOS 不直接永久删除，只提供提示：Managed Source 已验证、恢复路径可用，用户可以自行保留或删除外部旧文件。删除是用户独立决定，不会删除 Managed Source。

如果未来增加系统辅助动作：

- 必须单文件或明确选择集；
- 显示用户可理解的旧入口和 Managed Source 恢复入口；
- 默认移动到系统废纸篓，不永久删除；
- Managed Source 不可恢复时立即停止；
- 删除后不把缺失的外部文件误报为 Managed Source 缺失。

## 7. 用户如何理解 Normalized Representation

界面应明确区分：

```text
Managed Source
  Stable source_id: protected
  Managed bytes: verified / unavailable
  External file: old entry / may be missing

Representations
  Text extraction: derived, independent status
  Page render: derived, independent status
  OCR: derived, independent status
  Transcript: derived, independent status
```

每个 representation 引用 Managed Source 的 `source_id` 和生成时的 `content_hash`。删除或重算某个 representation 不影响 Managed Source，也不能把 representation 变成新的 Source。

## 8. 关键提示文案

- “扫描完成：只生成临时接入分析，未复制或修改任何文件。”
- “确认归档后，系统会复制完整字节并校验；校验通过才会创建 Managed Source。”
- “归档完成：后续系统处理以 Managed Source 为准，不再自动跟踪此位置。”
- “如需更新，请回到向阳经营系统重新导入；外部旧文件的修改不会自动同步。”
- “这是派生表示，不是 Managed Source。”
- “发现相同内容；可以复用受管字节，但不会自动合并来源语境。”
- “关系方向无法确认，已保留为待审提示，没有创建长期关系。”
- “此文件被标记为 restricted，不会发送到远程处理。”

## 本轮未执行

- 没有真实 Managed Source UI；
- 没有复制、上传、恢复或删除；
- 没有写 handoff marker；
- 没有 TOS credential 或 API 调用；
- 音频只做了只读元数据盘点，没有 Managed Source copy/restore 或 Normalization workflow smoke test。
