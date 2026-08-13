# Inbox 目录规则

## 目的

`01_inbox/` 保存原始、尚未处理的信息输入。

在第一版 Managed Source 架构中，`01_inbox/` 是本地受控的 Source 字节根目录。只有用户明确准入、完整复制并通过大小和 `content_hash` 校验的字节，才可以进入正式的 Managed Source 区域。它不是通用扫描结果目录：外部接入候选在准入前不得复制到这里，也不得据此创建长期 `source_id`。

## 规则

- 接受音频、图片、PDF、PPT、文档、视频和外部采集等原始输入。
- 每个原始 Source 都必须保持不可变：处理过程中不得编辑、重命名、覆盖、移动、就地摘要或删除。
- 不在此目录加入结论、业务分类或结构化对象更新。
- 为下游追溯保留必要的接入来源信息，但不得把外部路径或文件名作为长期 Source 身份。
- 只有用户明确准入、完整复制并通过大小和 `content_hash` 校验后，才能创建正式 Managed Source。
- 一个 `source_id` 对应一份不可变的 Managed Source 字节快照。字节一旦被 Evidence 引用，不得原地覆盖；新的字节快照必须显式重新接入并创建新的 `source_id`。
- 准入后，下游 Processing 和 Evidence 使用 Managed Source 字节。外部旧文件的变化不会被自动跟踪或同步。
- `ingested_from` 只作为可选、可能失效的接入来源提示；它不是 Evidence 定位，也不是第二个 Source。
- 用户实际输入必须保留在本地并由 Git 忽略，除非产品负责人明确批准提交脱敏 fixture。
- 即使目录内容被忽略，也必须保留受 Git 跟踪的本 `AGENTS.md`。

Processing 输出应写入 `02_processing/`，并遵守 `02_processing/AGENTS.md`。
