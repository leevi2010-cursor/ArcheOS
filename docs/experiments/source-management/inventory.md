# M2-C1 Source Archive Inventory

## 技术摘要

本轮只读实验复用了 Input Normalization Experiment 中已经完成脱敏验证的代表性样本，并把用户指定的微信文件目录作为第三个只读 Source 集合补查独立录音。结论是：Markdown、PDF、Excel、图片和录音都适合作为 ArcheOS Source，但前提是原始字节保持不变、Source Manifest 与敏感内容保持本地、Normalized Representation 始终被标记为派生物。

第三个 Source 集合中发现 5 个独立 M4A 文件，合计约 83.1 MiB、77.7 分钟；均为 AAC、48 kHz，包含单声道与双声道，5 个内容 hash 互不相同。本轮只读取文件系统和媒体元数据，没有播放、转写或分析语音内容。

本报告只记录匿名样本编号、聚合大小和结构特征，不包含真实文件名、原始路径、人员、公司、项目、证件、合同、图片、录音、业务正文或真实 hash。

## 实验边界

- 原始目录：只读元数据和少量结构检查。
- 未执行：复制、移动、删除、重命名、Archive 写入、TOS 上传、Normalization、OCR、转写、业务判断。
- 未生成：Object、Atomic Information、World Model 或数据库 schema。
- 输出：仅 M2-C1 实验设计和脱敏报告。

## 测试集合

| 来源集合 | 聚合规模 | 本轮用途 |
|---|---:|---|
| 混合文件目录 | 223 files / about 6.89 GiB | PDF、Excel、图片、重复与项目资料样本 |
| Tolaria Markdown vault | 2,168 Markdown files | Markdown Source 与版本化知识文件样本 |
| 微信文件目录 | 98 files / about 1.27 GiB | 独立录音 Source 与受限本地来源样本 |

本轮没有全量读取内容。混合目录只对少量匿名样本做结构检查；Tolaria 只抽取两份 Markdown 的结构指标。

## 代表性 Source 样本

| 样本 | 类型 | 大小 | 结构特征 | 是否适合作为 Source |
|---|---|---:|---|---|
| MD-01 | Markdown | about 4.7 KiB | frontmatter、H1/H2、列表、表格、wikilink | 是；保留 Git/version context 与 block locator |
| MD-02 | Markdown | about 3.7 KiB | frontmatter、H1/H2、表格、代码块、wikilink | 是；不可扁平化为无结构文本 |
| PDF-01 | text/layout PDF | about 250 KiB | 3 pages，可提取文本，含视觉分组 | 是；原 PDF 与页面渲染都应保留 |
| PDF-02 | drawing PDF | about 679 KiB | 1 page，图形密集，空间关系重要 | 是；纯文本 representation 不完整 |
| XLSX-01 | Excel workbook | about 102 MiB | 17 sheets、469 media、617 formulas、167 merged ranges | 是；Archive 必须保存完整 workbook 字节 |
| XLSX-02 | Excel workbook | about 139 MiB | 16 sheets、330 media、622 formulas、146 merged ranges | 是；不能用 CSV 代替 Source |
| IMG-01 | scene JPEG | about 241 KiB | 1,279 × 1,706，场景信息为主 | 是；视觉 representation 是派生物 |
| IMG-02 | document JPEG | about 495 KiB | 828 × 1,920，文字和视觉标记并存 | 是；默认 restricted/local-only |
| AUDIO-WX | 5 M4A files | about 83.1 MiB total | 77.7 minutes，AAC / 48 kHz，mono/stereo，5 个 unique hashes | 是；原音频为 Source，转写只能是派生表示 |
| VIDEO-01 | MP4 composite media | about 3.68 MiB | 52.8 seconds，HEVC + AAC | 是复合 Source；不能替代 audio experiment |

## Source 适用性判断

### 适合作为 Source

- 用户明确拥有或有权处理的原始文件；
- 能以只读方式计算内容 hash；
- 能记录稳定的原始位置或来源系统定位；
- 能明确媒体类型、大小和观察时间；
- 敏感内容可以被本地权限保护；
- 即使格式暂不支持，也能被注册为 `processing_status=unsupported`，而不是被丢弃。

### 不应直接作为业务 Source

- `.DS_Store` 等本地系统元数据；
- 无法证明来源的临时锁文件或缓存；
- 仅用于预览的临时渲染文件，除非用户明确把它作为独立输入；
- Archive 自己生成的字节级副本，它只是同一 Source 的保管位置，不是新的独立来源；
- Normalized Representation，除非以后被用户作为新输入再次注册并保留完整 lineage。

## 关键观察

1. **Source 与格式支持无关。** 一个文件可以是有效 Source，即使当前没有 adapter。
2. **Source 与 Archive copy 不是两个事实来源。** Archive 只保存同一原始字节，不能增加来源数量。
3. **Source 与 Normalized Representation 不可互换。** 文本、结构化 JSON、截图、OCR 和转写都是派生表示。
4. **重复文件仍有 occurrence 价值。** 相同字节出现在不同目录时，内容可以去重，但位置与来源记录不能被抹掉。
5. **Tolaria 已有 Git/version 语境。** 扫描不应自动复制整个 vault；是否需要额外 Archive 必须由保留策略决定。
6. **录音可以安全注册，但尚未归档。** 元数据足以确认它们是 5 个不同的独立音频 Source 候选；由于没有复制、恢复或转写，本轮不能宣称 audio Archive 或 audio lineage runtime 已通过验收。

## 本轮只读性结果

- 没有写入两个原始目录；
- 没有创建 Archive；
- 没有上传 TOS；
- 没有复制任何真实 Source 到仓库；
- 没有删除或移动原始文件；
- Git 只包含匿名实验文档和 synthetic Manifest example。
