# 微信真实 Information Consolidation 语料准备结果

## 结论

```text
recommendation = technical_blocker
```

本轮在第 5 次、也是该 window 唯一一次 External Agent 调用中遇到
`result_contract_failure`。运行时按既有合同 fail closed：没有发布该 window
的信息包，没有写入 Durable Atomic Information，也没有继续调用后续 4 个已准入
window。全程没有 retry、fallback 或 provider switch。

前 4 个成功 window 已产生足够数量的 relation candidate hints，但只覆盖 2 个完成
正式 Information 链路的主题族。其余 3 个已选主题族尚未进入 Durable Information，
因此不能把局部计数解释为 Corpus Readiness PASS，#32 应继续保持 Blocked，等待
Architecture Review 对本次严格结果合同失败作出后续决定。

## Discovery 与正式准入边界

- 只读枚举了 1,607 个本机可访问会话；对其中 11 个候选会话、213 条消息做了
  bounded 深度检查。
- 最终选择 5 个真实业务主题族、9 个 conversation windows，共 94 条消息；其中
  78 个 canonical Analysis Units 可进入语义分析。
- discovery 没有自动创建 Source。只有上述 9 个选中 window 转为现有严格 WeChat
  export contract，并分别完成 immutable Managed Source admission。
- 9/9 Managed Source verify、9/9 Conversation Representation verify 通过。
- 9 个选中 window 的 source sequence span 合计 95 个位置；最小转换排除 1 条
  当前 contract 无法表示的 non-canonical 记录，最终 94 条消息进入 9 个
  exports / Representations。没有改写、复制或伪造业务陈述。
- canonical WeChat Conversation Representation 足以承载所选材料的重要 sender、
  time、order、content 与 unavailable metadata；未发现需要第二套 contract 的语义
  blocker。

## 匿名指标

| 指标 | 结果 |
| --- | ---: |
| conversations_enumerated | 1,607 |
| conversations_deep_examined | 11 |
| candidate_topics_found | 5 |
| selected_conversation_families | 5 |
| selected_windows | 9 |
| selected_messages | 94 |
| selected_analysis_eligible_units | 78 |
| provider_calls | 5 |
| provider_completed | 4 |
| strict_packages_published | 4 |
| durable_information_created | 24 |
| equivalent_or_derived_candidate_groups | 2 |
| complementary_candidate_groups | 2 |
| temporal_update_candidate_groups | 2 |
| conflict_or_uncertain_candidate_groups | 2 |
| unrelated_negative_controls | 3 |
| managed_source_readback | 9/9 |
| conversation_representation_readback | 9/9 |
| strict_package_readback | 4/4 |
| durable_information_readback | 24/24 |
| evidence_readback | 33/33 |
| successful_window_exact_replay | 4/4 |
| replay_provider_calls | 0 |
| replay_duplicate_information | 0 |
| Object writes | 0 |
| Relationship writes | 0 |
| Lifecycle writes | 0 |
| World Model writes | 0 |

上述 relation 数量只是本地 case index 的候选提示，不是 #32 的最终 relation truth。
候选提示全部引用已读回的 Durable Atomic Information；没有为了达到阈值制造业务事实。

## External Agent 调用记录

统一使用 `codex-cli` route、provider version `0.147.0`、120 秒默认 deadline、
batch size 100。每个 window 最多一次调用。

| 调用 | eligible anchors | covered | elapsed_ms | 结果 | Durable 新增 |
| ---: | ---: | ---: | ---: | --- | ---: |
| 1 | 7 | 7 | 34,301 | succeeded | 6 |
| 2 | 5 | 5 | 28,828 | succeeded | 4 |
| 3 | 15 | 15 | 43,977 | succeeded | 5 |
| 4 | 17 | 17 | 61,383 | succeeded | 9 |
| 5 | 14 | 0 | 42,093 | result_contract_failure | 0 |

第 5 次调用的 durable audit Readback 通过，`process_cleanup_status=verified`，
`package_published=false`，`information_ingested=false`。调用 6–9 未执行，不计入
Provider calls。

## Readback、幂等与安全边界

- 4 个成功 package 均通过 strict package、Source、Representation、Evidence 与 Store
  Readback；33/33 Evidence records 回到 canonical WeChat Representation locator。
- 4 个成功 window 完成 exact replay：Provider 调用为 0，新增为 0，已有记录为 24，
  Store bytes 保持不变。
- 第 5 个 window 的 invalid result 未形成 package 或 Durable Information；其后没有
  额外调用。
- Provider 没有 World Model write capability；本地 `04_core` 未产生 Object、
  Relationship、Lifecycle 或其他 World Model 文件。
- 本轮失败 raw diagnostic bundle 在提取 allowlist 结论后已精确清理；其他任务的既有
  diagnostic bundles 未被删除。
- discovery 临时副本与未选 snapshot 已清理；正式 Managed Source、Representation、
  Durable Information、audit 与 Git-ignored local case index 保留供 Review/后续读回。
- 微信数据库、消息、联系人均未修改；未调用 `new-messages`，未启用监听、同步或
  auto-reply。
- GitHub 不包含真实正文、sender/identity、群名、业务主题名、Source/Information ID、
  locator、路径、hash、prompt、raw result、stdout/stderr 或 credential。

## Repository Validation

- WeChat / Semantic Handoff / failure diagnostics focused tests：52 项通过。
- 完整测试（含 document extra）：443 项通过。
- `compileall`、diff check 与公开内容隐私扫描：通过。
- 全仓 Ruff 静态检查已实际运行：当前 head 报告 107 项既有问题。与
  `origin/main...HEAD` 对比，本 PR 修改的 Python 文件数为 0，因此本 PR 新增 Ruff
  问题为 0；未跨 Issue 修改主线既有静态规则问题。

## Product Alignment / Roadmap Feedback

- Product alignment：**PARTIAL**。只读 discovery、bounded admission、严格失败关闭、
  Evidence/Readback、幂等与 World Model 隔离均得到真实证据。
- Expected evidence obtained：**partial**。候选类型计数已出现，但 5 个主题族中只有
  2 个完成正式 Information 链路，不能支持 #32 进入 Ready。
- Roadmap feedback：**review**。应由 Architecture Review 判断本次
  `result_contract_failure` 的后续处理与是否需要新的单次真实调用授权；本 PR 不调整
  Provider、Prompt、schema、deadline 或 runtime contract。
