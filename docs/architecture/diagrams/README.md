# 向阳经营系统架构图版本索引

本目录保存**版本化的系统架构图与数据流图**。

产品对外名称使用 **向阳经营系统（Sunward Operating System）**；`ArcheOS` 目前是重构阶段的工程 / 仓库代号。

## 当前版本

- 当前架构版本：`v0.2.0`
- 状态：M2 Target Architecture
- 日期：2026-08-11
- 目录：`docs/architecture/diagrams/v0.2.0/`

## 版本规则

- `major`：核心世界模型、主生命周期或系统责任边界发生不兼容变化。
- `minor`：增加新的稳定架构能力、阶段或读取 / 治理边界。
- `patch`：不改变语义的图形修订、标注修正或小范围澄清。
- 已发布版本目录只用于历史读取；发生语义变化时创建新版本，不覆盖旧版本。
- `manifest.json` 记录当前版本和各版本资产。

## 权威关系

图是架构的**可视化表达**，不是独立事实源。若图与文字权威冲突，以以下顺序为准：

1. `AGENTS.md`
2. 当前 GitHub Issue
3. `docs/architecture/CONCEPTS.md`
4. `docs/product/INFORMATION_GOVERNANCE.md`
5. `docs/architecture/ARCHITECTURE.md`
6. 本目录中的版本化图

图中不得自行发明新的 Core 概念；新的概念必须先进入概念治理流程。
