# Recommendation

## 决定

```text
#33 = rewrite
```

Information Consolidation 是必要能力，但当前 #33 不应按现有范围直接实现。

## 为什么不是 `implement`

1. 当前 #33 以整条 Atomic Information pair 的单一 relation 为中心；真实 corpus 证明 relation 经常只对其中一个 Claim projection 成立。
2. 136 条 Information 中结构化 Claim 和 Object binding 均为 0；直接建设自动 classification、active record 和 Context 接入会把缺失的 scope 当作已经解决。
3. confirmed whole-record equivalent 为 0；没有 real-data evidence 支持 equivalent 自动生效。
4. deterministic retrieval 的主要漏召回来自 provenance/scope 缺失，不是缺少远程 Provider 或 embedding。
5. 当前 #33 同时建设 Provider、record/suggestion/decision Stores、review commands、staleness 和 Context integration，不能用一个最小 vertical slice 验证失败边界。

## 为什么不是 `not needed`

- 真实 Context 对照在保留 30/30 Information 与 120/120 Evidence 的情况下，把 30 个顶层条目整理为 6 组；
- temporal、conflict、complementary 与 uncertain 的显式展示明显优于平铺列表；
- 23 个相关/歧义 pair 证明 consolidation boundary 是持续存在的产品问题；
- 5 个负对照证明没有 candidate/relation 分离就会产生 false merge 风险。

## #33 建议重写为最小切片

### 交付 1：bounded deterministic retrieval

- 固定 `top-k=8` 默认上限；
- 组合 exact、same Source、concern、number/date/key token 与 lexical；
- 支持调用方提供 bounded scope；
- 返回 retrieval basis 和 completeness，不产生 relation truth；
- 第一版不引入 embedding 或远程 Provider。

### 交付 2：Claim projection relation contract

- relation target 引用 Atomic Information revision 与 projection/span；
- `equivalent`、`derived`、`complementary`、`temporal_update`、`conflict`、`uncertain` 保持实验词汇；
- `unrelated` 仅为候选拒绝结果；
- 没有结构化 Claim 时允许只读 projection，不修改 Atomic Information；
- 不做 truth resolution，不评价 claimant reputation。

### 交付 3：read-only grouped view

- opt-in 构建；
- 无 judgment 时与当前读取等价；
- 原 Information、revision、Evidence 和 independent source count 全部保留；
- conflict、temporal、complementary、uncertain 永不折叠成单一事实；
- 披露 total、included、grouped、pending/uncertain 与 retrieval completeness。

### 后续 Gate，而非第一版范围

- classification Provider；
- append-only active relation Store；
- suggestion/decision workflow；
- automatic equivalent/derived activation；
- canonical Context Builder 默认接入。

这些能力只有在最小切片生成可复核的 real-data judgments，并由 Architecture Review 固定技术记录边界后再进入后续 Issue。

## 必须保留的 Non-goals

- 不删除、覆盖或改写 Atomic Information / Claim / Evidence；
- 不写 Object、Relationship、Lifecycle 或 World Model；
- 不把 relation label 加入 World Model vocabulary；
- 不建立第二个 Information truth store；
- 不把模型 confidence 当作真实性概率；
- 不自动选择 conflict 中哪一方为真；
- 不建立通用审批平台或 Web UI。

## Architecture Review 请求

Lead 需要在重写 #33 前确认两点：

1. relation target 是否批准为 Claim projection / statement span，而不是 whole Atomic Information pair；
2. 第一版是否批准为 deterministic retrieval + read-only grouped view，并把 Provider、持久化 decision flow 和自动生效延后。

这两个选择不要求新增 canonical concept；若未来要把 relation judgment 作为 durable product record，需另行 Architecture Review。
