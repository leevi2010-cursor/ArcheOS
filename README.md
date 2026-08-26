# ArcheOS — 向阳经营系统重构工程

> **产品名称：向阳经营系统（Sunward Operating System）**  
> `ArcheOS` 是当前重构 / 迁移阶段的工程与仓库代号。

ArcheOS 是一个本地优先、用户拥有、模型无关、可追溯、受治理的长期认知底座。它把持续进入的文件、录音、对话和业务资料转化为可长期维护的 Information / World Model / Context，供 Human、Codex 和其他 External Agent 使用。

ArcheOS **不是 Agent**，也不会把 Agent 推断自动写成长期事实或正式 Decision。推理可以交给外部 Agent；ArcheOS 负责长期认知、Evidence、Context、治理和受控写回边界。

## 当前 Product Stage

当前处于：

> **Stage 1 — 证明“长期认知”真实成立。**

当前重点不是补齐常见软件功能，而是用真实、异构、持续变化的数据证明：信息不丢、来源可追溯、重复/派生/时间变化/冲突不会被错误处理、Object identity 能稳定演化、Context 不会随着数据增长越来越混乱。

产品阶段、Stage Gate 与长期商业化路线以 `docs/product/PRODUCT_ROADMAP.md` 为唯一产品路线权威；当前技术顺序以 `docs/development/ROADMAP.md` 为准。

## 当前已具备的主要能力

当前 main 已形成以下基础边界：

- **Workspace**：本地初始化、doctor 与本机配置；
- **Managed Source**：显式准入、不可变 Source identity、verify / restore、可选 Handoff Marker；
- **Audio Processing**：只从已验证 Managed Source 读取，不再把外部绝对路径作为新 package 的 Source / Evidence 权威；
- **Normalized Representation**：格式无关 contract、strict manifest、stable locator、completeness / warning、verify / no-replace publish；
- **首批多格式 Adapter**：Markdown、text PDF、XLSX、PPTX、image structural preflight；
- **Atomic Information / Claim / Evidence / Residue**：长期 Information foundation 与可追溯历史；
- **Structured World Model**：稳定 Object identity、Name / Role / Lifecycle、Relationship；
- **Information Governance**：安全自动更新、人类判断、冲突与孤立 Object 保护；
- **Context Builder**：Object-scoped、bounded、provenance-aware、truncation-aware Context；
- **Codex 只读接入**：通过本地 MCP 读取 canonical Context / Evidence，不绕过 Governance 写 Core。

`Hypothesis` 已作为 canonical Information 语义写入概念与 ADR，但其决策增强 runtime 属于后续 Product Stage 2，不代表当前已经实现 Hypothesis engine。

当前 Stage 1 尚未完成的关键能力与验证包括 Representation → Atomic Information contract 收口、正式 Semantic Analysis Provider、Conversation Ingestion、Information Consolidation、Object Emergence 与大规模真实数据压力测试；具体顺序不要从 README 推断，请读取 Development Roadmap。

## 本地安装

Git clone 或安装程序**只获取软件，不同步用户本地 ArcheOS 数据**。Workspace 中的真实 Source、Processing、Information 和 World Model 默认保留在本机并由 Git 忽略。

核心 CLI：

```bash
uv tool install git+https://github.com/leevi2010-cursor/ArcheOS.git
# 或在源码目录中：python3 -m pip install .

archeos --version
```

音频和文档运行时按需安装：

```bash
python3 -m pip install '.[audio]'
python3 -m pip install '.[document]'
```

音频运行仍可能需要本地 `ffmpeg`、转写模型和 speaker diarization 模型；核心安装不会为了“可能以后用到”而自动下载大型模型。

## 初始化 Workspace

```bash
archeos init /path/to/archeos-workspace
archeos doctor
archeos config show
```

重复初始化不得覆盖已有 Workspace 数据。

## Managed Source

外部文件必须先显式准入，随后使用 stable `source_id`：

```bash
archeos source admit /path/to/file
archeos source show <source_id>
archeos source verify <source_id>
archeos source list
```

恢复已验证 Source 时使用：

```bash
archeos source restore <source_id> /path/to/target
```

Handoff Marker 是可选外部交接说明，不是 Source、Evidence 或同步机制；仅在用户显式授权时使用相应 CLI。

## 音频处理

音频 Processing 只接受已准入、校验通过的 `source_id`：

```bash
archeos process <source_id> --language zh
```

开发 / 测试可以显式提供 transcript、speaker map 或 analysis fixture，但这些 fixture path 不改变 Source identity，也不得进入长期 provenance。

## Normalized Representation

首批文档 Adapter 已通过统一 Representation boundary 工作。具体可用 Adapter 名称和 CLI 参数以 `archeos representation --help` 与当前代码为准。

重要边界：

- Adapter 只产生可替换的派生表示；
- Representation 不是新的 Source；
- locator 必须回到 immutable Managed Source；
- partial / unsupported content 必须显式 warning，不能静默丢失；
- image structural preflight 不等于 OCR 或视觉语义理解。

已准入并校验通过的微信 JSON Source 可以生成 strict Conversation Representation：

```bash
archeos conversation wechat represent <source_id>
```

该命令只产生私有、Git-ignored 的 Representation 与匿名覆盖指标，不调用 semantic provider，也不写入 Atomic Information 或 World Model。

## 微信信息消化

首次使用必须明确选择一个起点；成功后，日常运行只需执行增量命令：

```bash
# 先只读联系人目录，不读取聊天正文
archeos wechat digest --list-contacts

# 新日常运行必须选择一个联系人
archeos wechat digest --contact "联系人名称" --since 2026-08-01
# 或：archeos wechat digest --contact "联系人名称" --all-history
# 后续增量：
archeos wechat digest --contact "联系人名称"

# 业务验收不写主 Workspace
archeos wechat digest --contact "联系人名称" --since 2026-08-01 \
  --isolated-acceptance-dir /private/path/contact-acceptance
```

联系人名称必须唯一；同名时使用联系人列表给出的会话编号。系统只捕获所选会话及附件，每个联系人使用独立进度。同一联系人不拆给多个并行 Agent；隔离验收完成后只生成私有业务验收包，不自动写入主 World Model。

ArcheOS 会把待处理历史切成连续的有界窗口：每个窗口最长 30 日且最多 1000 条消息。高密度月份会自动拆成多个可续传子窗；每个窗口内的消息与附件全部达到明确终态后，才推进本地 checkpoint。任一窗口失败时保留此前成功 checkpoint，下次从失败窗口原样恢复。隐私受限、暂不支持或需要人工判断的内容会保留并在业务摘要中单独报告。

## Codex 只读接入

初始化 Workspace 后，可显式安装 ArcheOS 管理的本地 MCP 配置：

```bash
archeos integration codex install
archeos integration codex status
archeos integration codex remove
```

它保留其他 Codex 设置，不读取或复制 token，也不会覆盖项目 `AGENTS.md`。重启 / 新开 Codex 会话后，可以通过 ArcheOS 的 read tools 获取 Object、Context 与 Source verify 信息；当前 MCP 不提供绕过 Governance 的 Core 写入口。

## 产品与架构边界

长期结构保持：

```text
ArcheOS Core
长期 Information / Evidence / World Model / Context / Governance
        ↓
External Agent
理解 / 推理 / Judgment / 建议 / 获授权执行
        ↓
Domain Product
围绕明确 Job-to-be-Done 提供用户体验
```

未来可能形成 Founder、Sales、Project、Research、Operations 等 Domain Product，但哪个最先产品化由真实使用和市场 Evidence 决定，不在 Core 中预建领域 Agent 类型。

## 隐私与数据所有权

- 密码、token、私钥和其他凭证不得进入仓库；
- 真实客户录音、聊天正文、Object 数据和 Evidence 默认本地、Git-ignored；
- 任何可能上传真实内容、调用远程模型或首次下载模型的路径必须遵守显式授权与当前 Issue 的 privacy boundary；
- Source / Evidence / World Model 之间保持清晰权威边界；
- 删除、对外同步、权限改变和 consequential Decision 保留明确的人类治理边界。

## 权威文档导航

不要从 README 推断完整产品或架构规则。当前权威关系是：

```text
docs/product/PRODUCT_SPEC.md
  产品长期是什么
        ↓
docs/product/PRODUCT_ROADMAP.md
  依次必须证明什么
        ↓
docs/development/ROADMAP.md
  当前 Stage 为了取得 Evidence 还缺什么
        ↓
GitHub Issue
  一次具体交付
```

横向约束：

- `AGENTS.md`：Architect / Executor 工作规则、Roadmap Alignment、开发前 Concept Convergence；
- `docs/architecture/CONCEPTS.md`：canonical concepts 的唯一权威；
- `docs/product/INFORMATION_GOVERNANCE.md`：信息吸收、自动更新与人类判断规则；
- `docs/architecture/ARCHITECTURE.md`：系统边界与连接方式；
- `docs/decisions/ADR-*.md`：关键架构决策与原因；
- `docs/experiments/`：实验与验证证据。

如果文档发生冲突，不要选择“看起来更新”的一份继续实现；按照 `AGENTS.md` 的权威与冲突处理规则停止相关范围并提交 Architect 决策。
