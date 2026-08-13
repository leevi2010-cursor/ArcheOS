# M2-C1 Source Lineage Design

## 技术摘要

Source Lineage 只回答“一个受管输入如何由另一个受管输入派生”，不判断内容正确性或业务权威。本轮把来源、派生、内容等价和未决提示分开，不把 `original`、`duplicate`、`unknown` 与 `derived` 并列成四种 durable relation。

Managed Source 是后续 Processing 与 Evidence 的唯一权威输入。外部原文件只是 intake 来源线索；Archive copy 和 TOS replica 都是 Managed Source 的受管字节位置，不是新的 Source。

## 四个不同维度

| 维度 | 表达方式 | 语义 | 不应推断 |
|---|---|---|---|
| Source registration / intake basis | `registration_basis`、`ingested_from` | 说明 Managed Source 最初如何被用户接入 | `original` 不是 Source 间关系，也不等于内容正确 |
| Source derivation | `child.derived_from = parent.source_id` | 有方向的父子派生关系 | 派生物不是独立来源确认 |
| Content equivalence | 相同 `content_hash` | 两个字节快照内容一致，可支持存储去重 | 不自动合并来源语境或 Source identity |
| Unresolved candidate / warning | 临时 candidate、warning、review required | 证据不足，暂不建立 durable edge | 不持久化 `unknown` 关系 |

只有确证的父子生成事实才记录 `derived_from`。相同 `content_hash` 不创建 duplicate lineage edge；Archive copy 和 TOS replica 也不创建 edge。

## Source identity 与内容等价

相同字节不必然代表相同来源语境：

- 无法证明两个外部接入候选是同一次接入时，各自保留临时来源记录或各自的 Managed Source registration；
- `content_hash` 可以让受控存储复用一份字节对象；
- 一次接入的外部旧文件与其 Managed Source 不计算为两份独立 Evidence；
- 文件名、目录、日期、模板相似度不能创建 durable lineage。

本实验不定义 `SourceOccurrence`、duplicate relation 或正式版本 schema。

## Source derivation

当一个 Source 确实由另一个 Source 经过转写、导出、合并或其他可记录变换产生时，使用方向明确的 `derived_from`：

```text
child Managed Source
  └── derived_from ──> parent Managed Source
```

生成收据至少应能说明 parent `source_id`、parent `content_hash`、transform kind、adapter/version 和生成时间。若只有文件名或目录提示，不建立关系。

## Normalized Representation

Normalized Representation 通常不是新的 Source。每个表示应独立记录：

- representation ID；
- `derived_from_source_id`；
- 生成时的 `source_content_hash`；
- representation kind；
- adapter/version；
- `generated_at`；
- 自身 `status` 和 warnings；
- 回到 Managed Source 的受控 locator。

一个 Managed Source 可以同时拥有多个表示，其中一些 `complete`、一些 `failed` 或 `stale`。表示可删除或重新生成，但不能改变 Managed Source 字节。

如果用户把一个表示单独作为新输入再次导入，它才可能获得新的 Managed Source identity；该次重新导入仍需保留可证明的 `derived_from` 事实，而不能把表示自动当成原 Source。

## 录音示例

本轮只读发现 5 个独立 M4A，内容 hash 互不相同。这个结果只支持“样本之间不是 exact content equivalent”，不支持它们之间或与其他文件之间的派生判断。未读取语音内容，也没有发现可提交的 transcript 或 summary，因此关系保持为未决提示，不创建 `unknown` durable edge。

以下是 synthetic example，仅说明未来存在生成收据时的方向：

```text
Managed audio source
  source_id: src_audio_example
  registration_basis: explicit_user_admission
        │
        ├── Normalized Representation: transcript
        │     derived_from_source_id: src_audio_example
        │     source_content_hash: <managed-audio-hash>
        │
        └── Normalized Representation: summary
              derived_from_source_id: src_audio_example
              source_content_hash: <managed-audio-hash>
```

如果 transcript 或 summary 后来被单独导入，才形成新的 Managed Source；此时应记录新 Source 的接入事实和可验证的 `derived_from`，而不是把表示与原 Source 混为一谈。

## 实测 lineage 证据如何映射

上一轮只读实验提供了以下匿名证据：

- 两个不同目录中的 JPG 字节和 SHA-256 一致：只记录 content equivalence，不能自动建立 duplicate edge 或合并来源；
- ZIP 中 9 个成员与解压目录 9 个 JPG 的 CRC/size 全匹配：内容等价证据较强，派生方向仍需生成或来源收据；
- 一个文件带 `extracted` 提示但 parent 缺失：保留 candidate/warning，不创建 `unknown` edge；
- 两个文件带下载副本后缀但原版本缺失：来源方向未决；
- 两个模板相似文件 hash 不同：不能判为内容等价；
- 5 个独立 M4A 的 hash 均不同：不是相同字节，但对其他 lineage 不作推断；
- Tolaria wikilink 是 authored knowledge link，不是 Source Lineage。

这些分类只用于检验实验设计，不把真实文件名、hash 或内容提交到仓库。

## Synthetic lineage companion record

以下是实验表示候选，不是数据库 schema 或正式 contract：

```json
{
  "child_source_id": "src_child_example",
  "derived_from": {
    "parent_source_id": "src_parent_example",
    "parent_content_hash": "sha256:EXAMPLE_NOT_A_REAL_HASH",
    "transform_kind": "transcription",
    "adapter_version": "example-adapter-1",
    "generated_at": "2026-08-12T00:10:00Z"
  },
  "basis": "adapter_receipt",
  "warnings": []
}
```

## 不应自动化的判断

- 外部文件是否真的代表某个 Source 的 original capture；
- 两个相同 hash 的接入是否具有相同来源语境；
- 哪个版本是最终版；
- 哪个外部旧文件应删除；
- summary 是否忠实；
- 文件时间是否代表业务时间；
- wikilink、同目录或相似名称是否代表 `derived_from`；
- 文件属于哪个 Object。

## 验收条件

- Managed Source 只有在显式准入、复制和校验成功后才有 durable `source_id`；
- 外部旧文件、Archive copy 和 TOS replica 不增加独立 Evidence 数量；
- 相同 `content_hash` 支持内容等价和存储去重，但不自动合并 provenance；
- 只有确证父子事实才建立 `derived_from`；
- 证据不足时保留 candidate/warning，不建立 `unknown` durable edge；
- Normalized Representation 能回到 Managed Source、生成时的 `content_hash` 和 adapter/version；
- Managed Source 字节变化通过新的快照/验证事实表达，不原地覆盖已被引用的字节；
- 公开报告不包含真实位置、hash 或内容。
