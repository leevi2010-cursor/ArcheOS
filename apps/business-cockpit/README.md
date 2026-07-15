# 元枢 ArcheOS 双工作台交互原型

`AR-UX-PROTOTYPE@0.2.2-prototype` 是一个零依赖、全部使用假数据的可点击产品原型。它用于验证店铺经营者与平台管理者的导航、权限提示、关键旅程和异常交接，不是正式产品实现。

## 运行

```bash
cd apps/business-cockpit
python3 -m http.server 8080
```

打开 `http://localhost:8080/`。首次进入店铺经营者的经营驾驶舱；顶部“我的”可查看身份、Workspace、权限和环境详情，并切换工作台。

## 边界

- 不连接后端、Amazon、Amazon Ads、飞书、真实 Runtime 或真实容器。
- 不读取环境变量、浏览器存储或真实凭证；三类凭证只显示遮罩假引用。
- 所有按钮只修改内存中的 mock 状态，刷新后复位。
- Capability 是业务职责，Capability Package 是其不可变版本化制品；不展示旧的派生能力概念。
- Knowledge 使用 System Core、Distributable Domain、Enterprise Private、Runtime Evidence / Learning Candidate 四分类；Tolaria 仅显示为可替换 Markdown 来源标签。

## 覆盖

- J1：身份切换、两套导航、不同默认首页和持续上下文。
- J2：Agent Profile 候选、验证报告、三种场景和发布确认。
- 经营者主路径：全部店铺老板驾驶舱、经营目标与护栏、Agent 关系、分层经营结果、统一收件箱和经营动态。
- J3：经营目标与护栏问答、默认规则、冲突保守处理。
- J4：三类假凭证、帮助和六种验证状态。
- J5：收件箱中的写入 Change 主/替代方案、批准/拒绝和 digest 失效。
- J6：第二隔离容器、SecretRef、自检、唯一写入者和 lease 冲突。
- J7：收件箱与 Agent 安全控制中的异常、跨身份连续定位、紧急停止和恢复检查。
- J8：Capability Package 结果契约及四类 Knowledge 提升边界。

详细自检与走查记录见 `evidence/`。
