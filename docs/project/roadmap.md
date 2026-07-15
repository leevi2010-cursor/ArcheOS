---
artifact_id: AR-RM-v0.1
version: 0.3.0
status: in_review
owner_role: delivery-manager
depends_on: AR-ARCH@0.5.0
product_target: AR-PRD@0.6.0
---

# ArcheOS v0.1 交付路线

## 1. 交付结论

本 Roadmap 以 `AR-PRD@0.6.0` 的完整 v0.1 验收为终点，而不是以搭建基础框架为完成。路线只覆盖单 Workspace、单 Amazon Ads Agent、两个隔离容器的业务价值闭环；所有 PRD AC-1 至 AC-15（含 AC-1A）均必须取得可追溯证据后，v0.1 才可申请验收。

当前状态为 `in_review`，等待 Leo 审批。Roadmap 获批且 `./scripts/governance-check create-milestone` 通过前，不得创建 Milestone、Issue 或开始实现。

## 2. 边界声明

| 项目 | 结论 |
|---|---|
| Layer | L2 — ArcheOS Control & Evolution 的交付治理；交付物贯穿 L2、L3 和 L4，但本 Roadmap 不改变分层语义 |
| Object | Roadmap；后续拟创建的 Milestone 只是开发协作对象，不是产品核心对象 |
| Responsibility | 将已批准产品验收和架构不变量组织为有依赖、可观察、可验收的交付阶段 |
| Non-responsibility | 不改写 Product Brief、PRD、Architecture、ADR 或 Schema；不决定未批准架构；不创建 Issue；不编码 |
| Source of truth | 产品结果与 AC：PRD v0.6.0；对象与接口：Architecture v0.5.0、object-boundaries、ADR-0004；版本与审批：manifest；Amazon 实现来源：`amazon-ops-kit` 精确 commit/digest 与许可证清单 |
| Validation | 每阶段通过契约/权限/故障/恢复/端到端验收；最终按 AC-1 至 AC-15、7 天阈值和审计证据验收 |
| Reuse | 稳定契约与治理骨架跨领域可复用；Amazon 映射、Adapter、Knowledge 与运营策略保持领域专用 |

## 3. 固定交付原则

1. 主责任链固定为 `Business Intent / KPI → Agent Deployment → Capability Package → Skill → Tool / External System → Business Result`。Agent Deployment 对 Workspace KPI 负责；Capability 对结果契约负责；Skill 对方法负责。
2. `Capability` 是稳定业务身份，`Capability Package` 是同一对象的不可变版本化制品；不建立重复实体，不使用旧 `Capability View` 语义。
3. Knowledge、Policy 和 Governance 横向支撑；反馈链固定为 `Business Result → Observation → Measure / Evaluation → Change → 新版本`。Observation 不可变，Evaluation 是过程。
4. Amazon Ops Kit 是只读实现参考和内部执行内核来源，不是第二产品或第二事实源；不机械把其模块或 Capability Matrix 行提升为 Capability Package。
5. 凭证只以 `SecretRef` 存储并由运行环境的 Secret Provider 解析。禁止复制 `.env`，禁止把秘密值写入 Roadmap、Milestone、Issue、日志、测试夹具、Knowledge、Observation、Change diff、证据包或仓库。
6. Amazon 写入必须同时经过服务端授权、审批边界、Writer Lease/fencing token 和确定性 Write Ledger。关键控制不可验证时 fail closed；恢复时不得自动补执行故障期间积压写入。
7. 每个 Milestone 必须先在 Roadmap approved 后独立创建和审批；后序阶段不能用前序“基本可用”替代明确验收。

## 4. Milestone 顺序与依赖

```text
AR-v0.1-M1 治理与契约骨架
        ↓
AR-v0.1-M2 双工作台与 Agent Factory
        ↓
AR-v0.1-M3 Amazon 广告安全运营闭环
        ↓
AR-v0.1-M4 受控进化、韧性与双容器
        ↓
AR-v0.1-M5 连续运行与完整 v0.1 验收
```

### AR-v0.1-M1 — 治理与契约骨架

**上游：** Roadmap approved；Architecture v0.5.0；ADR-0004；Capability Schema；Amazon Ops Kit 只读来源可访问。

**可观察交付物：**

- Workspace、Business Intent/KPI Assignment、Agent Deployment、Capability/Package、Skill、Tool、Policy、四类 Knowledge、Runtime Instance、Observation、Measure、Change 的最小契约、状态与引用规则；
- Capability Package 最小结果契约及兼容/不兼容变更、验证、发布、暂停、废弃和回滚规则；
- 领域资产生命周期与依赖影响规则；Knowledge 正式资产和运行证据/学习候选的两套独立状态机；
- Authorization、SecretRef、证据引用、Writer Lease/fencing token、Write Ledger 和幂等键的安全契约；
- Amazon Ops Kit Capability Matrix/来源清单，逐项标注 `直接复用 / 需 Adapter / 需重写 / 仅历史参考`，记录来源 commit/digest、许可证及第三方依赖、现有测试、目标契约、兼容边界和判定理由；
- 通过业务结果契约判定 Amazon Ads Capability Package 候选；数据完整性、诊断、关键词学习、受控写入等默认分类为 Skill/Tool/Policy/Measure/质量门禁，除非能证明独立业务结果。

**验收：**

- 契约可机器验证，状态转换、版本引用、摘要变化和权限拒绝有测试证据；
- 一个 Amazon Ads Capability Package 满足 Schema 与 FR-17 最小结果契约，并能演示验证、发布、暂停、回滚及兼容 Skill 替换；
- 四类 Knowledge 均可被区分，企业私有内容隔离，未评审候选不能作为正式 Knowledge 加载；
- Source Matrix 中每项来源均可追溯且未复制秘密或把内部模块机械升级为 Capability；
- 覆盖 AC-7、AC-13 的契约和资产部分，并为 AC-14、AC-15 建立可验收骨架。

**非范围：** Business Cockpit 完整页面、真实 Amazon 写入、跨 Workspace 资产发现、自动批量 Knowledge 提升。

**下一责任角色：** 交付负责人提交验收；项目经理核验后派发下一 Milestone，不自动转交实现。

### AR-v0.1-M2 — 双工作台与 Agent Factory

**上游：** M1 approved；已发布的契约测试制品与 Amazon Ads Package 候选。

**可观察交付物：**

- 在同一 Business Cockpit 中交付持续可见的“平台管理者 / 店铺经营者”身份、Workspace 和权限范围；服务端授权独立执行；
- 平台管理者资产工作台：Capability Package、Knowledge、Skill、Policy、默认规则、版本、证据、依赖影响和部署状态；
- Agent Factory 生成 Agent Profile 候选和验证报告，发布确认完整展示 KPI 责任、精确资产版本、权限、审批、SecretRef 需求、Schedule、Runtime、限制与回滚目标；摘要变化使旧确认失效；
- 店铺经营者完成已发布 Agent 选择、经营导向/风险/限制问答、默认规则跳过、最终配置确认以及三类凭证的逐项帮助与无秘密验证；
- 利润优先、销量/订单优先、清库存优先至少产生三种可见的不同策略或参数组合。

**验收：**

- Leo 以两种身份分别完成端到端走查；导航、默认首页和可操作范围不同，越权请求在服务端被拒绝；
- 资产不可发布、digest 变化、权限或验证失败会阻塞 Agent 发布并给出原因；
- 从空白 Workspace 生成可发布 Agent Profile 候选；平台确认前不得实例化 Agent；
- 凭证帮助不显示、记录或回传秘密，仓库、日志和测试证据经秘密扫描无泄漏；
- 完成 AC-1、AC-1A、AC-6、AC-8，并完成 AC-7 的用户可见生命周期部分。

**非范围：** 真实连续广告运营、外部消息通知、多用户组织、公开 Agent 市场。

**下一责任角色：** 交付负责人提交验收；项目经理核验后派发下一 Milestone。

### AR-v0.1-M3 — Amazon 广告安全运营闭环

**上游：** M2 approved；已确认 Agent Profile；Ops Kit 来源清单和 Adapter 兼容测试通过；真实凭证仅由运行环境提供。

**可观察交付物：**

- 元枢统一体验内的计划数据获取、完整性/新鲜度检查、诊断、候选决策、审批/授权写入、readback、观察和复盘；经营者无需进入 Ops Kit 界面或命令；
- KPI → Capability Package → Skill → Tool/External System → Observation/Measure 的完整追溯；明确区分已核实、推断和因数据不足无法判断；
- Writer Lease、fencing token、确定性 Write Ledger、幂等去重、审批和回滚；预算红线、权限不足、数据过期、重复风险全部 fail closed；
- 应用内持久异常通知和待办，覆盖店铺级写入异常与平台级资产/部署异常的确认、升级、处置、恢复验证和责任人；
- Ops Kit 版本、digest、健康、兼容性和错误证据在元枢平台管理视图可见。

**验收：**

- 至少一个真实广告动作完成候选、授权、执行、readback、观察和复盘，关键动作证据完整率 100%；
- 缺失数据、过期数据、预算红线、越权、重复请求和 Ops Kit 不兼容故障注入均不产生未授权写入；
- Writer Lease 过期、旧 fencing token、重复幂等键和并发写入测试证明只有有效写入者提交一次；
- 店铺级与平台级异常各完成一次全闭环，重启后待办仍存在，写入由经营者明确恢复；
- 完成 AC-2、AC-5、AC-9、AC-12、AC-14 的运营部分。

**非范围：** Agent 自我修改、第二容器接管、7 天最终观察期、Ops Kit 独立产品界面。

**下一责任角色：** 交付负责人提交验收；项目经理核验后派发下一 Milestone。

### AR-v0.1-M4 — 受控进化、证据韧性与双容器

**上游：** M3 approved；安全写入基线与不可变证据链通过验收。

**可观察交付物：**

- Change 闭环：问题、假设、目标对象、diff、风险、验证、审批、受限发布、观察、保留/回滚；写入方法/参数/范围变化强制事前审批，读取/分析变化可受限自动验证但完整留痕；
- Knowledge 四分类的分发与影响检查，以及一次“企业私有 → 所有者授权 → 脱敏 → 抽象 → 验证 → 人工批准 → 可分发领域 Knowledge 新版本”闭环；Tolaria 不可用不影响已发布制品运行；
- 部署外证据持久化、90 天运营证据规则、关键审计持续保留、无秘密机器可读导出包和空白索引恢复；恢复不覆盖不可变历史；
- 控制面/Authorization/Writer Coordination/Secret Provider/证据存储故障时停止新写入，只读降级、紧急停止、积压清理、重新验证和人工恢复；
- 同一 Agent 在第二隔离容器通过显式 SecretRef 和配置部署；不依赖第一容器状态，写入协调防止同范围同周期重复执行。

**验收：**

- 一个真实反馈产生的 Change 完成全闭环；演示写入变化等待审批和替代方案，读取/分析变化受限验证，以及失败后恢复前一版本；
- 容器重建后证据可查；证据包无秘密且可恢复关联；删除/清理留审计记录；
- 故障注入覆盖控制面、协调、Secret Provider 和证据持久化：新写入全部阻止、可信读取标记只读降级、积压不自动补执行；
- 第二容器在第一容器不可用时独立完成至少一个运营周期；两个容器的秘密、状态、日志隔离且不重复写入；
- 完成 AC-3、AC-4、AC-10、AC-11、AC-15，并补齐 AC-9 的恢复约束。

**非范围：** 跨实体电脑迁移、Runtime fleet、自动 Knowledge 批量提升、企业合规平台。

**下一责任角色：** 交付负责人提交验收；项目经理核验后派发最终验收 Milestone。

### AR-v0.1-M5 — 连续运行与完整 v0.1 验收

**上游：** M1–M4 全部 approved；验收环境、真实 Amazon 账号权限和证据存储就绪。

**可观察交付物：**

- 单 Workspace Amazon Ads Agent 的 7 天连续自动运营记录；
- PRD AC-1 至 AC-15（含 AC-1A）完整追溯矩阵、验收报告、故障注入报告、开放风险和回滚演练；
- 发布候选的精确 Capability Package、Skill、Tool Adapter、Policy、Knowledge、Agent Profile、Runtime 和 Ops Kit 来源版本/digest 清单；
- 第二容器独立周期、Change 闭环、失败恢复、证据导出/恢复和人工接管的最终演示。

**验收：**

- 连续观察期 7 天，计划任务成功率 ≥95%，关键运营动作证据完整率 100%，未授权高风险写操作 0 次；
- 第二容器独立运营 ≥1 个完整周期；自我改进闭环 ≥1 次；改进失败后的恢复演示 1 次；
- 所有 AC 均有证据引用、责任人和明确的 pass/fail；任何未通过项都阻止 v0.1 完成声明；
- 发布前再次执行治理审计、权限与秘密扫描、恢复和回滚检查。

**非范围：** M1 平台化与 M2 跨领域/企业交付能力。

**下一责任角色：** 交付负责人回传项目经理；项目经理核验后决定是否升级 Leo 进行 v0.1 最终验收，不自行进入 PR 或 Release 角色。

## 5. PRD AC 覆盖矩阵

| PRD 验收 | 主验收 Milestone | 前置证据 |
|---|---|---|
| AC-1 Agent 孵化 | M2 | M1 资产契约 |
| AC-1A 引导与凭证帮助 | M2 | M1 SecretRef/Policy |
| AC-2 广告运营闭环 | M3 | M1–M2 |
| AC-3 可控进化 | M4 | M3 真实反馈与写入基线 |
| AC-4 独立容器部署 | M4 | M3 Writer Coordination |
| AC-5 经营与安全 | M3 | M1 Policy/Measure |
| AC-6 双工作台 | M2 | M1 Authorization |
| AC-7 领域资产生命周期 | M1 + M2 | 契约后完成用户可见验收 |
| AC-8 Agent Factory 发布确认 | M2 | M1 资产与摘要规则 |
| AC-9 异常闭环 | M3 + M4 | M3 运营闭环，M4 故障恢复 |
| AC-10 数据与证据 | M4 | M1 Observation/Change 契约 |
| AC-11 人工接管 | M4 | M3 安全写入基线 |
| AC-12 Ops Kit 产品边界 | M3 | M1 Source Matrix，M2 统一体验 |
| AC-13 Capability Package | M1 + M2 | M1 契约生命周期，M2 平台展示/Factory |
| AC-14 KPI 责任追溯 | M3 | M1 KPI/Measure 契约 |
| AC-15 Knowledge 分类与分发 | M4 | M1 四分类契约，M2 资产入口 |

M5 对以上全部 AC 进行最终回归和阈值验收；矩阵中的阶段通过不等于 v0.1 已完成。

## 6. 跨阶段风险与控制

| 风险 | 控制与验收 |
|---|---|
| Ops Kit 边界误判或来源漂移 | M1 建精确 commit/digest、许可证、测试和兼容清单；digest 变化触发重验；不复制源码或客户事实 |
| Capability 粒度虚假膨胀 | 以独立业务结果契约评审；内部步骤归入 Skill/Tool/Policy/Measure/质量门禁 |
| 凭证泄漏 | 只存 SecretRef；运行时解析；仓库、日志、夹具和导出包秘密扫描；故障时 fail closed |
| 双容器重复写入 | Writer Lease + fencing token + Write Ledger + 确定性幂等键；并发、过期租约和网络故障注入 |
| 控制面故障沿用旧授权 | 停止新写入；只读降级；紧急停止；恢复清单和经营者明确恢复 |
| 证据丢失或恢复篡改 | 部署外持久化、不可变 Observation、清单/digest、空白索引恢复和保留策略测试 |
| 7 天业务结果受归因延迟影响 | 明确 Measure 窗口、置信度和不可判断状态；不得以局部指标冒充 Agent KPI 达标 |
| Knowledge 跨 Workspace 泄漏 | 四分类、所有者授权、隔离测试、脱敏和人工批准；候选默认不可加载 |

## 7. v0.1、平台化 M1 与跨领域 M2 边界

| 阶段 | 本路线承诺 | 明确不承诺 |
|---|---|---|
| v0.1 Amazon MVP | 单 Workspace、单 Amazon Ads Agent、双工作台、Agent Factory、领域资产生命周期、四类 Knowledge、完整运营/异常/Change/证据闭环、双容器安全写入、PRD 全量 AC | 多租户组织、公开市场、跨实体机客户交付、库存/定价/客服/商品写入 |
| 平台化 M1 | 后续候选：多 Workspace、资产发现与依赖体验、质量中心、Knowledge 批量治理、Runtime fleet、模型/成本治理 | 不属于 v0.1 完成条件；不得提前固化服务拆分 |
| 跨领域/企业交付 M2 | 后续候选：多 Domain Agent、组织角色、租户隔离、合规、授权升级与商业化 | 不属于 v0.1；Amazon 特例继续留在 Domain Adapter |

本表中的“平台化 M1 / 跨领域 M2”是产品演进阶段，不是本 Roadmap 的 `AR-v0.1-M1` 至 `AR-v0.1-M5` 交付 Milestone 编号。

## 8. 审批与下一步

- Roadmap v0.3.0 当前申请 `in_review`，需要 Leo 审批后才能改为 `approved`。
- 在批准前，Milestone、Issue、Implementation、PR 和 Release 保持 `blocked`。
- Leo 需要确认：五阶段顺序与 v0.1 完整终点；Amazon Ops Kit 只读来源治理；双容器写入安全验收；M1/M2 后续边界。
- 批准后由项目经理派发交付负责人运行 `./scripts/governance-check create-milestone`；只有 PASS 才能创建第一个 Milestone。
