---
artifact_id: archeos-decision-workbench-product-brief
version: 0.1.0
status: approved
owner: Leo
approved_by: Leo
approved_at: 2026-08-19
parent_project: ArcheOS
issue: 114
---

# ArcheOS 决策工作台 Product Brief v0.1.0

## 产品定义

ArcheOS 决策工作台是面向企业经营负责人的图形化 Human View。它让用户从经营资产、关系和推进事项出发，逐层理解当前经营世界，并在需要时追溯到相关 Decision、Evidence 与原始 Source。

第一版是独立的 React mock-data 产品探索，不接后端、不写正式业务数据、不部署，也不建立第二套 World Model truth。

## 产品承诺

企业经营负责人打开页面后，不需要理解 ArcheOS 内部概念或阅读使用说明，就能：

1. 从左侧看到当前重点、待完成与已完成事项；
2. 在向阳总图中理解愿景、价值观、资产依赖、Project / Business Line 与推进事项之间的关系；
3. 点击资产进入适合该资产的专属图形 View；
4. 点击人物、公司、Project、Issue 或 Todo 查看业务详情；
5. 在需要时沿“相关决定 → 依据 → 原文件”逐层追溯；
6. 看见下一动作，而不被设计 rationale、技术 ID 或治理术语干扰。

## 默认用户

- 企业经营负责人；
- 需要理解经营资产、推进状态与关键关系，并保留最终 Decision authority 的人。

第一版不面向开发者、数据管理员或通用图数据库编辑者。

## 核心体验

### 1. 向阳总图

- 上方是 Vision；
- 下方是价值观；
- 中间资产主干按依赖关系从稳定基础向客户方向生长；
- Project / Business Line 共用同一种分枝表达；默认使用有完成边界的 Project；
- Milestone / Issue / Todo 形成可聚焦的推进节点；
- Outcome / Feedback 可以回到资产沉淀或认知修正。

总图是 View / Projection，不表示 Core 只有一棵唯一树。

### 2. 左侧推进栏

- 最多突出三项当前重点；
- 分组显示待完成与已完成的 Issue / Todo；
- 每一项都能在总图中定位；
- 详情可以链接 AI 草案、Decision、Evidence 与 Source。

### 3. 资产专属 View

点击总图资产节点进入可复制 URL 的全屏详情 View；普通点击在当前窗口进入，浏览器原生 `Command / Control + 点击` 可以在新标签页打开。小信息使用侧栏预览。

第一批专属 View：

- 笃善科技 Workspace 的理念世界 v1；
- 财务三表；
- 完整人脉关系图；
- 组织与治理图。

后续可为系统、团队、流程、服务、产品、客户等资产增加不同图形，不要求所有资产共用一种布局。

## 产品体验原则

1. **图表达关系**：位置、连线、形状、颜色与状态承担主要表达；文字只保留名称、金额、关系标签、状态和必要动作。
2. **渐进披露**：总图不堆详情；节点点击后再展开档案、Decision、Evidence 与 Source。
3. **业务语言**：页面不显示设计思想、架构 rationale、schema、View Model、object ID 等内部信息。
4. **生物隐喻**：系统表现为开放、生长、分枝、反馈与沉淀，而不是封闭机器流程。
5. **一份 truth，多种 View**：所有资产世界都从同一 Core projection 派生，不分别保存长期业务真相。
6. **人保留 Decision authority**：AI 建议与人的 Decision 分开表达，第一版 mock UI 不执行外部动作。

## 明确结构边界

推进 ownership 只使用：

```text
Workspace → Roadmap → Milestone → Issue → Todo
Project   → Milestone → Issue → Todo
```

Business Line 可以在 View 中显示与其相关的 Roadmap / Milestone / Issue，但不拥有 Roadmap。Project 不创建内部 Roadmap。

第一版对 Project / Business Line 采用“界面收敛、Core 不合并”的策略：

- 两者共用节点、详情结构和关系交互，不为 Business Line 建立第二套前端模块；
- 新增 mock 条目默认使用 `Project + bounded`；
- 只有用户明确指定，或事项承担持续经营责任且无法给出可信完成条件时，才使用 `Business Line + ongoing`；
- 长期、重复发生或包含多个 Milestone，本身都不足以触发 `ongoing`；
- 不为验证界面而人为制造 ongoing 样本；真实走查中出现例外后，再判断该区分是否产生独立产品价值。

这是一项可逆的 Presentation 实验，不废弃 `business_line` Role，也不修改 canonical Lifecycle。

## 非目标

- 不接 ArcheOS backend、Context Builder、MCP、HTTP 或数据库；
- 不写正式业务数据；
- 不实现通用图编辑器、Wiki 或文件管理器；
- 不继承旧向阳前端的 `roadmap / asset / branch` 物理数据模型；
- 不把“同学、同事、合伙人、股东、法定代表人”等 UI 标签直接升级为 Core Relationship；
- 不在前端展示产品设计 rationale 或内部治理说明；
- 不以该探索作为 Stage 1 Gate 通过证据。

## 成功信号

- 用户无需说明书即可从左侧事项定位到总图节点；
- 用户能够从总图进入资产专属 View，并返回原位置；
- 用户能够理解 Person、Company、推进分枝、Issue 与 Todo；当持续经营例外出现时，能够区分“阶段项目”与“持续事业线”；
- 用户能够从业务详情进入相关 Decision / Evidence / Source 链接；
- 图形减少理解关系所需的文字，而没有制造错误 hierarchy；
- mock 前端可以被 Leo 在桌面端完整走查，并形成明确的继续、调整或停止判断。
