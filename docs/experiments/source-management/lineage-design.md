# M2-C1 Source Lineage Design

## 技术摘要

Source Lineage 必须回答“这个文件从哪里来、与哪个 Source 有什么关系”，但不能判断内容正确性或业务权威。本轮建议保留四种关系：`original`、`derived`、`duplicate`、`unknown`，并把内容置信度与方向置信度分开。

Archive copy 不应成为一个新的独立 Source；它是同一 `source_id` 的第二个受管理位置。Normalized Representation 也不是新的独立确认，它引用生成时的 `source_id` 和 source hash。

## 四种关系

| 关系 | 定义 | 可用证据 | 禁止推断 |
|---|---|---|---|
| `original` | 用户确认或来源系统证明的捕获/接收 Source | 用户注册、设备/来源收据、可信导出记录 | original = 内容正确或最新权威 |
| `derived` | 由父 Source 经过转写、总结、提取、渲染、导出等产生 | 生成收据、parent source_id、adapter/version、source hash | derived = 独立来源确认 |
| `duplicate` | 两个 original occurrences 的字节完全一致 | SHA-256 一致；容器成员可用 CRC + size 辅助 | 可以自动删除任一 occurrence |
| `unknown` | 关系或方向证据不足 | 文件名、目录、日期、相似模板等 hint | 强行选择 parent 或最新版 |

## Source occurrence 与 Archive copy

同一字节可能在多个用户目录出现。每个 occurrence 有独立的 original location、observed time 和用户语境，因此不能直接抹掉；但 Archive bytes 可以按 hash 去重。

```text
Original occurrence A ─┐
                       ├─ duplicate content ─ verified archived bytes
Original occurrence B ─┘
```

Archive copy 是系统为某个 Source 保存的字节级副本，不应计为第三个独立 occurrence，也不应提高 Evidence 的来源数量。

## Normalized Representation lineage

每个 Normalized Representation 至少需要：

- representation ID；
- `derived_from_source_id`；
- 生成时的 source hash；
- representation kind；
- adapter/version；
- generated time；
- completeness/status；
- warnings；
- 回到 Source 内部位置的 locator。

当原始 Source 发生变化时，旧 representation 仍可追溯到旧 hash，但必须标记 stale，不能静默改绑到新字节。

## 录音示例

第三个只读 Source 集合中发现 5 个独立 M4A 文件。它们的内容 hash 互不相同，只能说明样本之间不是 exact duplicate；本轮没有读取语音内容，也没有识别可能关联的 transcript 或 summary，因此与其他文件的关系必须保持 `unknown`。

下列仍然只是 synthetic lineage example，用于说明未来存在生成收据时的方向：

```text
audio-source.m4a
  relation: original
  source_id: src_audio_example
        │
        ├── transcript.md
        │     relation: derived
        │     transform: transcription
        │     parent: src_audio_example
        │
        └── meeting-summary.md
              relation: derived
              transform: summarization
              parent: transcript source or representation
              root_source: src_audio_example
```

如果 transcript 和 summary 只是 Normalized Representation，就不应自动注册成新的 Source。如果用户后来单独导入或编辑它们，才可以获得自己的 `source_id`，同时保留 derived lineage。

## 实测 lineage 证据如何映射

上一轮只读实验提供了以下匿名证据：

- 两个不同目录中的 JPG 字节和 SHA-256 一致：`duplicate`，高置信；
- ZIP 中 9 个成员与解压目录 9 个 JPG 的 CRC/size 全匹配：内容重复高置信，派生方向中置信；
- 一个文件名含 `extracted` 但 parent 缺失：`unknown` 或 derived candidate；
- 两个文件带下载副本后缀但原版本缺失：`unknown`；
- 两个模板相似文件 hash 不同：不能判为 duplicate；
- 5 个独立 M4A 的 hash 均不同：彼此不是 exact duplicate，但对其他 lineage 不作推断；
- Tolaria wikilink 是 authored knowledge link，不是 Source Lineage。

这些关系只用于验证 lineage 分类，不把真实文件名、hash 或内容提交到仓库。

## Lineage 记录建议

以下为 Manifest companion record 候选，不是数据库 schema：

```json
{
  "source_id": "src_child_example",
  "related_source_id": "src_parent_example",
  "relation": "derived",
  "basis": "adapter_receipt",
  "transform_kind": "transcription",
  "content_confidence": 1.0,
  "direction_confidence": 1.0,
  "observed_time": "2026-08-11T15:00:00Z",
  "warnings": []
}
```

## 不应自动化的判断

- original 是否真实、有效或业务正确；
- 哪个版本是最终版；
- 哪个重复 occurrence 应删除；
- summary 是否忠实；
- 相同观点是否来自独立来源；
- 文件时间是否代表业务时间；
- wikilink、同目录或相似名称是否代表 derived；
- 文件属于哪个 Object。

## Archive 与 Lineage 的验收条件

- Archive copy 不增加独立 Source count；
- exact duplicate 不重复存储 bytes，但保留 occurrences；
- derived representation 能回到 source_id、source hash 和 locator；
- unknown 关系保持 unknown，不用文件名强制选择方向；
- Source byte 变化使旧 representation 可见地 stale；
- 公开报告不包含真实 location、hash 或内容。
