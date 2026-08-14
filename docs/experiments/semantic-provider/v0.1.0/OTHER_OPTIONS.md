# D. Other Options

## Direct model API

官方 API 的 structured output 能力可作为未来的**独立 Provider Adapter**候选，而不是改写 Representation、Atomic Information 或 Evidence contract 的理由。官方文档说明了 Structured Outputs 的 schema 约束与使用方式：[Structured model outputs](https://developers.openai.com/api/docs/guides/structured-outputs)。

本轮没有用户提供或授权使用 API 凭据，因此没有调用、费用、吞吐或真实资料结果。若 Architect 选择继续，最小试验应只发送同一合成 package，记录版本、schema 方言、超时、结构化验证和成本；真实样本仍需另行一次性授权。

## Local open-source route

本轮不安装或搭建本地模型平台。任何本地模型候选都需要独立门禁：许可证与模型来源、固定 artifacts、Apple Silicon 资源、默认网络行为、离线运行、损坏输入 fail-closed、结构化输出、Evidence locator 和质量。没有这些证据，不应把“本地”视为天然可用或更私密的 production route。

## 结论

两条路线均保持调研状态，不进入 #50 的生产推荐，也不阻塞 #48 先完成 Conversation Representation 与 stable Analysis Units。
