# ArcheOS Business Governance Cockpit

这是 ArcheOS 的第一版经营驾驶舱前端原型。

## 目标

让经营负责人从业务视角看清：

- 当前租户的经营方向与业务宪章；
- 业务指标和趋势；
- Agent 的贡献与采用率；
- 高风险业务预警；
- Skill / Capability View 的健康状态；
- 需要人类审批的进化建议。

## 运行

这是一个零依赖静态原型：

```bash
cd apps/business-cockpit
python3 -m http.server 8080
```

浏览器访问 `http://localhost:8080`。

也可以直接打开 `index.html`。

## 当前实现

- Amazon US Store 与黄鸿儒展厅两个 mock 租户；
- 租户切换；
- 业务 KPI、策略、Agent 贡献、预警、进化建议和能力状态；
- 经营策略草案编辑；
- 进化提案进入审批队列的演示交互；
- 响应式桌面与窄屏布局。

## 边界

该目录只属于 **Human Governance / Business Control Plane**。

它不负责：

- Agent Runtime 的任务调度；
- Skill 的真实安装和版本发布；
- 业务数据采集；
- 生产认证、权限和多租户隔离；
- 自动修改 Skill 或经营策略。

## 后续接口建议

前端未来通过 ArcheOS Control Plane API 获取数据，不直接连接 Hermes 或业务数据库：

```text
Business Cockpit
      ↓
ArcheOS Control Plane API
      ├── Tenant Constitution
      ├── Business Metrics
      ├── Agent Contribution
      ├── Evolution Proposals
      └── Capability View
             ↓
Runtime Adapters / Business Data Connectors
```

建议后续迁移到 React + TypeScript 时保持 `apps/business-cockpit/` 作为应用边界。

关联 Issue：#2
