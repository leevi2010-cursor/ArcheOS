# Issue #108 脏结构化数据有界实验

## 目的

本实验验证现有 `Source + Normalized Representation + Derived Artifact / Projection + Evidence + Atomic Information + Identity Gate + Context` 是否足以承载结构不稳定的供应商多版报价，而不把 Source schema 当作正确业务 truth，也不把整张表强制原子化。

实验代码位于 Processing / experiment 层，不是 production Adapter、通用 ETL、schema registry 或新 Core Store。

## 输入

`fixtures/` 包含三版公开合成报价定义。运行时将它们生成真实 XLSX 文件，并通过现有链路处理：

```text
synthetic XLSX
→ Managed Source
→ XlsxRepresentationAdapter
→ verified faithful Normalized Representation
→ experimental structure/data-quality assessment
→ replaceable Projection candidate
→ bounded Context preview
```

三版 fixture 覆盖：

- 重复产品编号；
- 表头与 schema drift；
- free-text 隐含尺寸；
- mm / cm / m / 米混用；
- 缺失与非数值价格；
- 价格冲突；
- 明确与不明确的版本替代关系；
- 同名不同编码的 identity ambiguity。

## 运行

```bash
uv run --extra document python \
  docs/experiments/structured-data/v0.1.0/run_experiment.py \
  --output /tmp/archeos-structured-data-result.json
```

Harness 只在全部检查成功后原子发布指定的结果文件。Representation 或 mapping 解释失败会返回非零状态，不生成 Residue，不写 World Model。

## 四层边界

输出明确区分：

1. `observed_source_structure`：现有 XLSX Representation 的 faithful cells 与 locator；
2. `inferred_structure_candidate`：header row、record row 等可修订结构推断；
3. `domain_schema_candidate`：本供应商报价场景需要的字段候选；
4. `business_canonical_mapping_candidate`：source header 到业务字段的版本级映射候选。

后 3 层均可重新生成，不覆盖第 1 层，也不自动成为 World Model truth。

## 安全边界

- 真实业务资料：0；
- Provider calls：0；
- World Model writes：0；
- Object create / bind：0；
- 运行失败进入 Residue：0；
- GitHub 中仅含公开 synthetic fixture 与匿名聚合结果。
