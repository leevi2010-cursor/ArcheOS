# M2-C2a 多格式文档能力基准实验 v0.1.0

## 结论等级

本目录记录 Issue #28 的研究、只读本地基准与开源复用治理结论。它不是运行时设计，也不表示任何候选已经成为 ArcheOS 依赖或正式 Adapter。

- **已调查**：已核对官方仓库或官方文档；
- **已做本地结构基准**：只读匿名样本，结果仅为聚合指标；
- **未验证**：尚未在本地真实样本或断网环境中运行，不可表述为已支持；
- **推荐**：下一契约或 Adapter Issue 可优先验证，不是自动准入授权。

## 索引

- [候选清单](CANDIDATES.md)：官方来源、能力与建议角色；
- [基准计划](BENCHMARK_PLAN.md)：样本、命令模板、指标和隐私约束；
- [结果](RESULTS.md)：匿名本地只读实测与未验证项；
- [许可证与安全](LICENSE_SECURITY.md)：网络、模型、供应链与隐私边界；
- [综合推荐](RECOMMENDATION.md)：按格式的优先级和后续 contract 建议；
- [实验 manifest](manifest.json)：版本、平台与样本聚合计数。

## 范围边界

本实验未实现 `Normalized Representation` runtime、任何生产 Adapter、OCR pipeline、Atomic Information、Object、TOS 或 UI。本轮真实样本仅在本机由受控工具只读处理；除确认结构结果所必需的最小人工检查外，未进行业务内容分析。本目录不记录或提交真实路径、文件名、正文、图片、hash、人员、公司、项目或凭证。
