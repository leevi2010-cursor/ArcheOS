# 离线与 restricted 资料安全门禁

## 本轮实际证据

- Tesseract 的合成输入试跑明确传入本地 `--tessdata-dir`；同时以空目录试跑模型缺失路径，命令失败，未出现 traineddata 自动下载。
- 当前主机没有可由非特权进程建立的网络 namespace / 防火墙规则。本轮不能把“无 DNS / HTTP”宣称为已经独立审计；PaddleOCR 也未获得可运行的预取模型。因此断网执行门禁为 `blocked`。
- 本轮未读取任何真实或 restricted Source。合成脚本只在新建临时目录写入输入、输出与错误流，退出时按该新建目录清理；不把识别结果写入 Git 或公共日志。

## restricted 默认路线（未来实现前置条件）

1. 在非敏感环境下载并人工审计 runtime、模型和 traineddata 的版本、许可证、大小与摘要；将固定 artifacts 放入受控、Git-ignored 的只读模型目录。
2. 在能强制禁网并可记录 DNS / HTTP 尝试的隔离环境中，对合成输入先验证成功路径、模型缺失 fail-closed 路径和无自动下载。
3. 仅当上述证据完整时，允许受控 Adapter 接收已验证的 Managed Source 本地 materialization；拒绝 URL、插件、云 API 与 cloud fallback。
4. 每次 restricted 运行使用最小权限的专用临时目录；禁止共享模型 cache、持久化 OCR 文本、预览上传、人脸识别、身份推断或 Person 匹配；finally 清理临时输出。
5. 输出只能是可替换的派生表示，必须带 engine、runtime/version、model artifact、配置、network policy、page/image index、坐标系、bbox/polygon、OCR score 与 warning/completeness。OCR score 只表示识别器自身评分，绝不表示事实真实性。

大图、多页 PDF、压缩炸弹、损坏文件与不支持格式必须在 OCR 前预检，并设页数、像素、解压比、运行时间、内存和临时目录配额；超限或模型缺失均 fail closed 到人工路径。
