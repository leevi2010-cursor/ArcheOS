# ArcheOS 系统架构说明

## 总体架构

五层结构：

1. Input Layer

接收原始信息。

2. Processing Layer

完成转写、解析、总结、抽取。

3. Atomic Layer

形成可复用的信息单元。

4. Object Layer

将信息吸收到核心对象。

5. Decision Layer

结合目标产生判断和行动。

## 核心对象（第一版）

|对象|说明|
|-|-|
|Person|人物|
|Company|组织|
|Project|项目|
|Event|事件|
|Goal|目标|
|Decision|决策|
|Note|原子信息|

避免早期引入过多模型。
