# ArcheOS — 向阳经营系统重构工程

> **产品名称：向阳经营系统（Sunward Operating System）**  
> `ArcheOS` 是重构 / 迁移阶段的工程与仓库代号。

ArcheOS 是一个本地优先、用户拥有、模型无关、可追溯、受治理的长期认知底座。它把持续进入的文件、录音、对话和业务资料转化为可长期维护的 Information / World Model / Context，供 Human 和 External Agent 使用。

ArcheOS **不是 Agent**。理解、推理、建议与执行可以交给 Codex、GPT、Claude 或未来其他 External Agent；ArcheOS Core 负责长期认知资产、Evidence、Context、Governance、Audit 与受控写回边界。

长期产品结构：

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

README **不维护当前完成度、当前 Product Stage、当前开发主线或 Issue 状态**。这些内容变化频繁，应分别读取：

- `docs/product/PRODUCT_ROADMAP.md`：当前 Product Stage / Stage Gate；
- `docs/development/ROADMAP.md`：当前 Evidence Gap 与技术顺序；
- GitHub Issues / PRs：具体实施状态与最新 Evidence。

---

## 核心概念边界

ArcheOS 长期使用少量 canonical concepts：

```text
Source
→ Normalized Representation / Processing
→ Atomic Information + Claim + Evidence + Residue
→ Information Consolidation / Governance
→ Object + Name + Role + Lifecycle + Relationship
→ Context Builder / Projection / View
```

重要边界：

- Source 是不可变、已准入的 managed raw-byte snapshot；
- Representation 是 Source 的可替换派生表示，不是新的 Source；
- Atomic Information 保存最小、可独立理解、可追溯的信息；
- Claim 表达某主体 / 来源对 statement 的立场，不等于 Fact；
- Hypothesis 属于 Information Layer，复用 Atomic Information identity / Revision / Evidence，不建立独立 Store；
- Object 表达需要长期保持稳定身份的现实 / 经营对象；
- Person / Company / Project / Business Line 等通过 Object Role 表达，不建立平行 base entity；
- Conversation / Message 是 Representation / Processing 形态，不是 Core Object；
- Context、Projection、View 是读取 / 展示能力，不成为第二份 truth。

完整 canonical 定义以 `docs/architecture/CONCEPTS.md` 为准。

---

## 本地安装

Git clone 或安装程序**只获取软件，不同步用户 Workspace 数据**。真实 Source、Processing、Information 与 World Model 默认保存在本地并由 Git 忽略。

```bash
uv tool install git+https://github.com/leevi2010-cursor/ArcheOS.git
# 或在源码目录中：
python3 -m pip install .

archeos --version
```

按需安装可选运行时：

```bash
python3 -m pip install '.[audio]'
python3 -m pip install '.[document]'
```

具体 CLI、Adapter 和参数以当前代码中的 `archeos --help` 与子命令 `--help` 为准；README 不复制易漂移的完整命令清单。

---

## Workspace

初始化本地 Workspace：

```bash
archeos init /path/to/archeos-workspace
archeos doctor
archeos config show
```

Program / Code 与 User Workspace 分离：

```text
GitHub / package install
= 程序与治理文档

User Workspace
= 私有 Source / Processing / Information / World Model
```

GitHub clone 不等于复制用户长期记忆。

---

## 数据所有权与隐私边界

- 密码、token、私钥和其他 credential 不得进入仓库；
- 真实客户录音、聊天正文、Object 数据和 Evidence 默认本地、Git-ignored；
- 用户已授权给当前 External Agent / Provider 的业务数据，可以在调用期间出现在 stdin、内存、受控临时文件、Provider request 与必要本机运行上下文；
- 禁止未授权 Provider fallback；
- 禁止真实业务正文进入 public GitHub；
- 禁止无治理长期保留真实 debug 日志或临时资料；
- External Agent / Provider 的结果必须经过 strict validation 后才能进入长期 Information；
- consequential change、identity ambiguity、merge / delete 等继续遵守当前 Governance 与 Human Judgment 边界。

产品规则以 `docs/product/INFORMATION_GOVERNANCE.md` 为准。

---

## 权威文档

不要从 README 推断完整产品路线、当前完成情况或 implementation contract。

权威链：

```text
docs/product/PRODUCT_SPEC.md
  → 产品长期是什么
        ↓
docs/product/PRODUCT_ROADMAP.md
  → Product Stage / Stage Gate
        ↓
docs/development/ROADMAP.md
  → 当前 Evidence Gap 与开发顺序
        ↓
GitHub Issue
  → 一次具体交付
        ↓
PR / Experiment / Real-world Validation
  → Evidence / Roadmap Feedback
```

横向约束：

- `AGENTS.md`：项目协作规则、Roadmap Alignment、Concept Convergence；
- `docs/architecture/CONCEPTS.md`：canonical concepts 的唯一权威；
- `docs/product/INFORMATION_GOVERNANCE.md`：信息吸收、自动更新与 Human Judgment；
- `docs/architecture/ARCHITECTURE.md`：系统边界与数据流；
- `docs/decisions/ADR-*.md`：关键架构决策；
- `docs/experiments/`：实验与真实验证 Evidence。

如果这些 authority 发生冲突，应停止受影响范围并提交 Product / Technical Lead 决策，不从 README、历史 Prompt 或旧 Issue 猜测当前规则。
