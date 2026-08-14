# Semantic Analysis Provider Compatibility Experiment v0.1.0

这是 Issue #50 的脱敏兼容性实验。它只比较 Semantic Analysis Provider 的执行路径；不修改 Core、World Model、Managed Source、Representation 或 Atomic Information schema，也不把任何路线设为生产默认。

## 结论

当前结论是 **no production provider yet**。三条路线都能在一次长、多批次的公开合成输入上返回严格结构化输出，并完整覆盖 13 个单元；但对故意设计的不确定/冲突单元，候选与 Residue 的分类仍有波动。更重要的是，这不能解释或替代此前真实 text-PDF 的失败，因而不能据此恢复 production semantic default。

## 范围与隐私

- 只使用本目录的公开匿名 fixture；
- 输入和模型输出只存在于临时目录，运行结束后删除；
- 不读取 Managed Source、不上传真实资料、不读取凭证、不写 World Model；
- timeout 为 120 秒；超时会终止整个子进程组，运行失败不会转为 Residue；
- 外部 Agent 路线使用本机既有 Codex 登录态，但 ArcheOS 不读取或管理认证材料。

## 路线

| 路线 | 本次状态 | 结论 |
| --- | --- | --- |
| pinned Codex Python SDK / app-server | 合成门禁通过 | 仅证明合成兼容，不能解释真实资料失败 |
| isolated latest Codex Python SDK | 合成门禁通过；可获得的最新包仍为 0.144.4 | 没有版本差异可归因 |
| external Agent handoff（Codex CLI） | 合成门禁通过 | 与产品边界更一致，但可靠性样本不足 |

详见 [Provider Matrix](PROVIDER_MATRIX.md)、[Results](RESULTS.md) 与 [Recommendation](RECOMMENDATION.md)。

## 可重复运行

先在两个隔离环境分别安装 pinned 和 latest SDK；本实验不修改项目依赖：

```bash
uv venv --clear /tmp/archeos-semantic-pinned --python python3
uv pip install --python /tmp/archeos-semantic-pinned/bin/python 'openai-codex==0.144.4'
uv venv --clear /tmp/archeos-semantic-latest --python python3
uv pip install --python /tmp/archeos-semantic-latest/bin/python openai-codex

/tmp/archeos-semantic-pinned/bin/python run_synthetic_benchmark.py \
  --route pinned-sdk --python /tmp/archeos-semantic-pinned/bin/python --timeout 120
/tmp/archeos-semantic-latest/bin/python run_synthetic_benchmark.py \
  --route latest-sdk --python /tmp/archeos-semantic-latest/bin/python --timeout 120
python3 run_synthetic_benchmark.py \
  --route external-agent --codex-bin codex --timeout 120
```

在本目录执行上述命令。没有有效的本机 Codex 登录态时，实验应返回明确 runtime failure，而不是生成候选或 Residue。

## 产物

- [fixture](fixtures/synthetic-analysis-units.json)：13 个公开合成单元，含短文本、表格、长上下文和三个 batch；
- [strict schema](schemas/external-agent-result.schema.json)：外部执行器的最小输出边界；
- [harness](run_synthetic_benchmark.py)：执行、验证 coverage、120 秒终止与临时清理；
- [manifest](manifest.json)：可重复性记录与结果摘要。
