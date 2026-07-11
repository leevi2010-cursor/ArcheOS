# ArcheOS Business Governance Cockpit

这是 ArcheOS 面向经营负责人的完整前端原型，属于 **Human Governance / Business Control Plane**。

## 页面结构

- `#overview`：经营驾驶舱——目标、KPI、风险、Agent 贡献和进化建议总览；
- `#performance`：业务表现——趋势、经营归因假设、异常指标和复盘问题；
- `#agents`：Agent 贡献——职责、权限、采纳率、人工介入和资源加载情况；
- `#capabilities`：能力视图——由 Skill、Tool、Policy 和 Evaluation 派生的覆盖矩阵；
- `#evolution`：进化中心——从业务信号到实验、发布和回滚的闭环；
- `#governance`：治理规则——平台规则、租户宪章、Agent Policy 与 Skill Defaults；
- `#runtime`：执行面管理——可插拔 Runtime、Adapter、部署版本和业务级状态；
- `#memory`：知识与记忆——Tolaria 对象、记忆生命周期和知识质量；
- `#audit`：审计日志——决策、规则、发布、运行、审批和回滚记录。

## 运行

这是零依赖静态原型：

```bash
cd apps/business-cockpit
python3 -m http.server 8080
```

浏览器访问 `http://localhost:8080`。

## 已实现交互

- Amazon US Store 与黄鸿儒展厅两个 mock 租户切换；
- Hash 路由和九个主要页面；
- 经营策略草案编辑；
- 新建经营议题；
- 进化提案进入审批队列；
- 桌面和窄屏响应式布局。

## 架构边界

前端未来只连接 ArcheOS Control Plane API，不直接连接 Hermes、Codex、Tolaria 或业务数据库：

```text
Business Cockpit
      ↓
ArcheOS Control Plane API
      ├── Tenant Constitution
      ├── Business Metrics
      ├── Agent Contribution
      ├── Skill / Capability View
      ├── Evolution & Governance
      ├── Runtime Inventory
      ├── Knowledge References
      └── Audit Events
             ↓
Runtime Adapters / Tolaria Adapter / Business Data Connectors
```

本原型不负责生产认证、多租户数据隔离、真实 Skill 发布、Runtime 调度或自动修改经营策略。

## 后续工程化建议

保留 `apps/business-cockpit/` 应用边界，下一阶段迁移至 React + TypeScript，并将页面拆为独立 route、feature 和 API client。迁移前先确定 Control Plane API contract，避免前端直接耦合具体 Runtime。

关联 Issue：#2
