# M2-C1 Source Archive Inventory

## 技术摘要

本轮只读实验复用了已完成脱敏验证的代表性样本，并把用户指定的微信文件目录作为第三个只读 Source 集合补查独立录音。Markdown、PDF、Excel、图片和录音都可以作为接入候选；只有用户明确归档、完整复制并通过 `size` 与 `content_hash` 校验后，才成为 Managed Source。

扫描结果只是临时 intake candidate、结构观察和风险提示，不创建 durable `source_id`，也不让外部路径成为后续 Processing 或 Evidence 的权威。

第三个 Source 集合中发现 5 个独立 M4A 文件，合计约 83.1 MiB、77.7 分钟；均为 AAC、48 kHz，包含单声道与双声道，5 个内容 hash 互不相同。本轮只读取文件系统和媒体元数据，没有播放、转写或分析语音内容。

本报告只记录匿名样本编号、聚合大小和结构特征，不包含真实文件名、原始路径、人员、公司、项目、证件、合同、图片、录音、业务正文或真实 hash。

## 实验边界

- 原始目录：只读元数据和少量结构检查。
- 未执行：复制、移动、删除、重命名、Managed Source 写入、TOS 上传、Normalization、OCR、转写、业务判断。
- 未生成：Object、Atomic Information、World Model 或数据库 schema。
- 输出：仅 M2-C1 实验设计和脱敏报告。

## 测试集合

| 来源集合 | 聚合规模 | 本轮用途 |
|---|---:|---|
| 混合文件目录 | 223 files / about 6.89 GiB | PDF、Excel、图片、重复与项目资料样本 |
| Tolaria Markdown vault | 2,168 Markdown files | Markdown 接入候选与版本化知识文件样本 |
| 微信文件目录 | 98 files / about 1.27 GiB | 独立录音接入候选与受限本地来源样本 |

本轮没有全量读取内容。混合目录只对少量匿名样本做结构检查；Tolaria 只抽取两份 Markdown 的结构指标。

## 代表性 Source 接入候选

| 样本 | 类型 | 大小 | 结构特征 | 作为接入候选 |
|---|---|---:|---|---|
| MD-01 | Markdown | about 4.7 KiB | frontmatter、H1/H2、列表、表格、wikilink | 是；归档后保留 Git/version context 与 block locator |
| MD-02 | Markdown | about 3.7 KiB | frontmatter、H1/H2、表格、代码块、wikilink | 是；不可扁平化为无结构文本 |
| PDF-01 | text/layout PDF | about 250 KiB | 3 pages，可提取文本，含视觉分组 | 是；Managed Source 保留原 PDF，表示可多种并存 |
| PDF-02 | drawing PDF | about 679 KiB | 1 page，图形密集，空间关系重要 | 是；纯文本表示不完整 |
| XLSX-01 | Excel workbook | about 102 MiB | 17 sheets、469 media、617 formulas、167 merged ranges | 是；Managed Source 必须保存完整 workbook 字节 |
| XLSX-02 | Excel workbook | about 139 MiB | 16 sheets、330 media、622 formulas、146 merged ranges | 是；CSV 不能替代完整字节快照 |
| IMG-01 | scene JPEG | about 241 KiB | 1,279 × 1,706，场景信息为主 | 是；视觉表示是派生物 |
| IMG-02 | document JPEG | about 495 KiB | 828 × 1,920，文字和视觉标记并存 | 是；默认 restricted/local-only |
| AUDIO-WX | 5 M4A files | about 83.1 MiB total | 77.7 minutes，AAC / 48 kHz，mono/stereo，5 个 unique hashes | 是；原音频归档后才成为 Managed Source |
| VIDEO-01 | MP4 composite media | about 3.68 MiB | 52.8 seconds，HEVC + AAC | 是复合候选；不能替代独立 audio 验收 |

## Source 适用性判断

### 可作为接入候选

- 用户明确拥有或有权处理的原始文件；
- 能以只读方式读取并计算候选字节的 `content_hash`；
- 能记录 `observed_at` 和临时接入线索；
- 能明确 media type 与 size；
- 敏感内容可以被本地权限保护；
- 即使格式暂不支持，也可以保留为 candidate 并记录处理限制。

### 尚不是 Managed Source

- 仅扫描到、尚未显式准入和复制校验的外部文件；
- `.DS_Store` 等本地系统元数据；
- 无法证明来源的临时锁文件或缓存；
- 仅用于预览的临时渲染文件；
- Archive 或 TOS replica，它们是 Managed Source 的受管字节位置；
- Normalized Representation，除非以后被用户显式重新导入并重新归档。

## 关键观察

1. **Source 身份在归档校验之后成立。** 扫描阶段不创建长期 `source_id`。
2. **Managed Source 是系统权威。** 归档后外部文件只是旧入口或 `ingested_from` 线索，系统不自动跟踪其变化。
3. **`content_hash` 不等于来源合并。** 相同字节可支持存储去重，但不自动合并无法证明相同的接入语境。
4. **外部旧文件与 Managed Source 不是两份独立 Evidence。** 物理上可以并存，系统后续只引用 Managed Source。
5. **Normalized Representation 与 Source 分离。** 文本、结构化 JSON、截图、OCR 和转写都是可替换派生表示。
6. **录音可完成 Source 级盘点，但本轮未归档。** 5 个独立 M4A 的元数据和 hash 足以验证接入分析路径，不代表 audio Managed Source runtime 已通过验收。

## 本轮只读性结果

- 没有写入三个原始目录；
- 没有创建 Managed Source Archive；
- 没有上传 TOS；
- 没有复制任何真实 Source 到仓库；
- 没有删除或移动原始文件；
- Git 只包含匿名实验文档和 synthetic Manifest example。
