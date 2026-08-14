# 实验结果

## 执行结果

| 指标 | sample-A | 汇总 |
| --- | --- | --- |
| 正式调用次数 | 1 | 1 |
| provider_completed | false | false |
| structured_output_valid | false | false |
| eligible units | 105 | 105 |
| candidate_count | not_assessed_without_valid_output | not_assessed_without_valid_output |
| residue_count | not_assessed_without_valid_output | not_assessed_without_valid_output |
| covered eligible units | 0 | 0 |
| unaccounted eligible units | 105 | 105 |
| latency | not_retained_due_to_fail_closed_observability_failure | 同左 |
| runtime_failure | privacy_boundary_violation_and_unverifiable_result | 同左 |
| temporary artifacts cleanup | true | true |
| privacy boundary passed | false | false |

## 质量与 Evidence 检查

本次没有可审计的有效结构化输出，因此以下人工检查均标记为 `not_assessed_without_valid_output`，不得解读为零：事实遗漏、错误信息、信息类型准确性、Evidence 精确性、Evidence 误绑定、过度主张、结构完整性，以及手工抽查结果。

正式调用期间，真实输入可通过本地进程参数被观察到；同时未保留可供严格验证的结构化结果回执。两项任一项均足以使实验 fail-closed。未重试，未回退，未替换样本，也未检查或恢复可能含敏感内容的本地运行日志。

没有发生 Durable Atomic Information、Object、Relationship 或 World Model 写入。临时目录在运行结束后已清理。
