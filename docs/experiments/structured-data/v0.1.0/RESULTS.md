# Issue #108 实验结果

## 结论

**PASS。** 现有 XLSX Normalized Representation 能忠实保留 source cells、公式结构边界与稳定 cell locator；在其上生成可修订的 schema interpretation / normalization Projection，不需要覆盖 Source，也不需要把每个 cell 转成 Atomic Information。

## 匿名指标

| metric | result |
| --- | ---: |
| schema versions | 3 |
| rows total | 9 |
| rows with at least one safe deterministic normalization | 9 |
| rows needing human review | 6 |
| schema drift | 2 |
| non-unique key | 1 |
| unit issues | 3 |
| hidden structure | 6 |
| conflicts | 2 |
| unresolved / human-judgment issues | 12 |
| Atomic Information Candidates | 2 |
| Provider calls | 0 |
| World Model writes | 0 |

## 验证事实

1. 三版 JSON fixture 均实际生成 XLSX，并通过 Managed Source admission、XLSX Adapter、Representation verify；
2. observed structure、inferred structure candidate、domain schema candidate、business mapping candidate 分层输出；
3. 重复 source key 只形成 Identity Gate Evidence，`automatic_object_binding_allowed = false`；
4. `2.8m / 2.8米 / 2800mm / 90cm / 0.9m` 可确定性转换为 mm，并保留原值、规则与 cell Evidence；
5. free-text 中“进口皮 / 左贵妃 / 材质待定”等解释保持 unresolved，不因格式像字段就猜测；
6. v1/v2/v3 header 分别解释，不用 v3 schema 反向覆盖历史版本；
7. 没有明确 replaces 的跨版本价格变化保持 conflict / temporal ambiguity；v3 明确 replaces v2 时仅产生 temporal interpretation candidate；
8. 每个 normalized value 均包含 Source、Representation、cell locator、raw value 与 mapping rule；
9. 修改 mapping 后 Projection 发生变化，原 Representation fingerprint 不变；
10. 9 行表格仅产生 2 条高价值 Atomic Information Candidate，没有逐 cell 原子化；
11. bounded Context preview 分开呈现既有 Object identity/current role、structured state、Atomic Information 和 data-quality warnings；
12. malformed locator 或 mapping collision fail closed，不生成业务 Residue；
13. 两个全新临时 workspace 的完整运行结果 exact equal；
14. Provider 与 World Model 均未参与写入。

## 测试

```text
tests/test_structured_data_experiment.py: 16 passed
```

全仓回归与静态检查以 Draft PR 的 Tests 区和最终执行报告为准。

## 未执行的真实 smoke

本 Issue 没有单独授权任何真实供应商报价进入本实验或 Provider。本轮按 Issue 允许的替代路径使用高保真 synthetic fixture；未声称获得真实报价 smoke Evidence。
