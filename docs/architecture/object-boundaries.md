# 核心对象边界

| 对象 | 负责 | 不负责 | 典型存放 |
|---|---|---|---|
| Project | 组织一个有目标和期限的工作范围 | 定义通用能力 | GitHub Project / Tolaria |
| Issue | 记录一个待解决问题或变更 | 作为长期知识正文 | GitHub Issue / Tolaria issue note |
| Type | 定义笔记对象的结构与最小字段 | 执行业务动作 | Tolaria |
| Knowledge | 解释事实、概念、案例 | 约束 Agent 行为 | Tolaria |
| Tutorial | 让人或 Agent 学会一个主题 | 直接充当运行流程 | Tolaria |
| Protocol | 原则、限制、优先级和决策规则 | 规定详细步骤 | Tolaria + repo spec |
| Workflow | 编排任务步骤、状态和交接 | 定义跨场景价值观 | Repo / Agent |
| Capability | 可被验证的“能做什么” | 绑定某个模型或工具实现 | Repo |
| Skill | Capability 的可调用实现 | 保存长期业务事实 | Repo / Agent runtime |
| Evaluation | 判断输出是否合格 | 生成业务目标本身 | Repo + 数据源 |
| Decision | 对重要分歧形成有背景的选择 | 永久替代 Protocol | Tolaria / ADR |
| Memory | 保存运行中值得复用的状态和经验 | 冒充事实源 | Tolaria / Runtime |
| Agent | 为领域目标组合能力并执行 | 成为所有知识的唯一容器 | Agent repo/runtime |

## 一个简单判断法

- “这是什么？”→ Knowledge / Type
- “为什么必须这样？”→ Protocol / Decision
- “按什么顺序做？”→ Workflow
- “系统能做什么？”→ Capability
- “具体怎么调用完成？”→ Skill
- “怎样算做好？”→ Evaluation
- “这次发生了什么？”→ Issue / Case / Memory
