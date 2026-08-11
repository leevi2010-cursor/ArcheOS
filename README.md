# ArcheOS（元枢）

> **当前结论：** ArcheOS 将被重新定位为 Leo 的统一信息治理与 Agent 记忆底座。未来，电脑中几乎所有具有长期价值的信息，都应按统一治理规范沉淀到本目录或由本目录登记其权威来源，供人、Codex、其他 Agent 和自动化系统持续使用。
>
> 当前阶段只确认定位与边界，尚未确定最终目录结构、对象模型和写入协议。旧 ArcheOS 产品代码与配套工件从当前版本清除，但仍可通过 Git 历史恢复。

## 定位

ArcheOS 不是一款单一业务应用，而是一套以目录为载体、以治理规范为核心的长期信息基础设施。

它负责把分散在电脑文件、沟通记录、业务系统和 Agent 工作过程中的信息，转化为可追溯、可理解、可复用、可授权和可演化的信息资产。不同工具可以读取或使用这些资产，但不各自维护一套彼此冲突的长期记忆。

这个目录未来可以同时承载：

- Codex 与其他 Agent 的长期记忆和上下文入口；
- 各领域的概念、方法、经验和研究知识；
- 项目、客户、产品、运营等业务数据与业务记录；
- 决策、规则、权限、状态、证据和变更历史；
- 面向不同人或 Agent 生成的索引、摘要和工作视图。

## 核心目标

1. **统一沉淀**：让长期有价值的信息进入同一个可治理的信息空间，而不是散落在聊天、下载目录和各类工具中。
2. **保留事实来源**：任何重要结论都能追溯到原始来源、责任人、时间和证据。
3. **形成共享记忆**：Codex 和其他 Agent 从同一套受控资产构建上下文，减少重复解释与记忆漂移。
4. **沉淀领域资产**：把一次性信息逐步转化为可复用的领域知识、业务对象和方法。
5. **支持持续演化**：信息可以被修订、合并、归档和派生，同时保留版本与审计线索。
6. **保持可迁移**：优先采用开放、可读的格式，避免系统能力被某个 Agent、模型或软件锁定。

## 信息如何进入系统

```text
电脑文件 / 沟通记录 / 业务系统 / Agent 产出
                    ↓
             收集与来源登记
                    ↓
       分类、去重、校验、授权与版本治理
                    ↓
   原始记录 / 事实 / 知识 / 业务对象 / 决策与规则
                    ↓
       索引、上下文包、Agent 记忆与业务视图
                    ↓
          人、Codex、其他 Agent 与自动化
```

Agent 记忆是治理后信息的一种使用方式，而不是独立的事实源。摘要、索引和上下文包可以重新生成；原始来源、确认过的事实和正式决策必须保留清晰的权威归属。

## 治理原则

- **来源可追溯**：重要信息必须记录来源；推断、候选信息与已确认事实不得混写。
- **权威唯一且有边界**：每类事实都要说明由哪个文件、对象或外部系统负责，目录不无条件复制外部系统的权威。
- **状态明确**：草稿、待确认、有效、失效、已归档等状态必须可区分，Agent 产出不得自动视为事实或决策。
- **变更可审计**：重要写入应保留作者、时间、原因、版本和必要的读回验证。
- **最小必要权限**：读取、写入、分发和删除遵循明确授权，敏感信息按范围隔离。
- **原始与派生分离**：原始材料、治理后的对象以及面向使用场景生成的视图分别管理。
- **生命周期受控**：信息应有更新、合并、失效、归档和恢复规则，避免无限堆积。
- **人拥有最终裁决权**：Agent 可以整理、建议和执行获授权的操作，但不能替代 Leo 对权限、事实、决策和删除的最终确认。

## 安全边界

“几乎所有信息”不等于把所有内容以明文提交到 Git。

- 密码、访问令牌、私钥和其他凭证不得写入仓库；这里只保存安全引用或使用说明。
- 受隐私、合同或法规约束的信息，应按权限分区，必要时仅登记元数据和受控存储位置。
- 大型文件、频繁变化的数据或必须由业务系统维护的记录，可以保留在合适的外部存储中，并由本目录登记来源、标识、治理状态和访问方式。
- 删除、对外同步和权限变更属于高风险动作，必须经过明确授权并保留验证证据。

## 本系统不是什么

- 不是一个随意堆放文件的总目录；
- 不是某个模型私有、不可迁移的黑盒记忆；
- 不是所有外部业务系统的机械镜像；
- 不是未经确认就把 Agent 推断升级为事实的自动知识库；
- 不是以收集数量为目标、缺少清理和失效机制的永久档案箱。

## 当前阶段

目前处于**定位确认阶段**：

- 已明确统一信息治理、共享 Agent 记忆、领域知识和业务数据的总体方向；
- 已清理旧 ArcheOS 产品路线的现行代码与配套工件；
- 与现有 Tolaria／开放信息系统（OIOS）的关系仍待确认；本 README 不自动迁移、合并或取代其现行权威；
- 尚未批准新的信息架构、目录规范、对象模型、权限模型或自动采集方案；
- 在治理规范确定前，不进行大规模信息迁移。

旧版本仍保留在 Git 历史中：

- `c9aa46e`：退役前完整工件快照；
- `31af498`：旧 ArcheOS 的归档状态。

## 定位确认后的下一步

1. 定义信息分层、核心对象及各自的权威边界；
2. 设计目录结构、命名、元数据、状态和版本规范；
3. 定义收集、校验、写入、读回、修订、归档与删除协议；
4. 定义 Codex 与其他 Agent 的读取、记忆生成和受控回写方式；
5. 选择一个真实领域做小规模迁移试点，再决定是否扩大到电脑中的其他信息。

## M1 通用音频处理

仓库提供一个最小 Python CLI，将 `.m4a`、`.mp3` 或 `.wav` 音频转换为 Processing 包，并把符合契约的 Atomic Information Candidate 自动吸收为本地 durable Atomic Information。运行环境需要 `ffmpeg`、`mlx_whisper`、官方 Codex Python SDK 和本地 pyannote 模型；不会修改原始音频，也不会自动修改 `04_core/` World Model 或决策层。

处理链分为三个可替换边界：

```text
audio → TranscriptionProvider → SpeakerProvider → AnalysisProvider → review package
```

- `TranscriptionProvider` 默认使用本机 `mlx_whisper`；
- `SpeakerProvider` 默认使用本地 `pyannote/speaker-diarization-community-1` 自动生成中性 `Speaker_N` 标签，也可读取已有 diarization map；
- `AnalysisProvider` 的首个实现通过官方 `openai-codex==0.144.4` Python SDK 使用 Codex app-server runtime，不读取或复制登录令牌。

Codex SDK/runtime 负责登录状态、app-server 生命周期、模型执行、运行时管理和 structured output；ArcheOS 只提交 AnalysisProvider 输入输出契约并消费已完成结果。ArcheOS 不管理 token、不选择模型、不实现重试，也不修补非法模型输出。

建议使用 Python 3.13 的独立虚拟环境安装固定的 M1 runtime 依赖，并单独安装转写工具：

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m pip install mlx-whisper
```

`openai-codex` 会安装匹配的 Codex runtime，并复用本机现有 Codex 登录状态。ArcheOS 不发起登录，也不接触 OAuth/token 文件。

自动 speaker diarization 首次使用前，需要：

1. 在 Hugging Face 接受 [`pyannote/speaker-diarization-community-1`](https://huggingface.co/pyannote/speaker-diarization-community-1) 的访问条件；
2. 通过本机 Hugging Face CLI 执行 `hf auth login` 配置访问权限；
3. 首次运行时下载模型到标准 Hugging Face 本地缓存。

模型下载后的推理在本机执行。ArcheOS 不保存、打印或提交 Hugging Face access token，并在未显式设置时默认使用 `PYANNOTE_METRICS_ENABLED=0`。

```bash
python3 -m archeos process 01_inbox/discussion.m4a --language zh
```

首次运行可能需要由 `mlx_whisper` 和 pyannote 下载模型。也可以使用已有 Whisper JSON、speaker map 或 schema-compliant analysis JSON 进行可重复处理和诊断；提供 `--speaker-map` 时不会运行自动 diarization：

```bash
python3 -m archeos process 01_inbox/discussion.m4a \
  --transcript /path/to/discussion.transcript.json \
  --speaker-map /path/to/discussion.speakers.json \
  --analysis-file /path/to/discussion.analysis.json
```

speaker map 使用转写片段编号，只接受中性标签，不执行 Person 身份匹配：

```json
{
  "segments": [
    {"segment": 1, "speaker": "Speaker_1"},
    {"segment": 2, "speaker": "Speaker_2"}
  ]
}
```

自动 diarization 只在某个 speaker 对 transcript segment 具有明确、占多数的正时间重叠时赋值。相同 overlap、无明显主导或无有效 overlap 时保留未知 speaker；缺少可用时间戳且没有 speaker map 时会给出可操作错误。M1 不执行声纹、voice embedding、Person 匹配或真实身份推断。

每个来源会生成确定性的 `source_id`，输出位于 `02_processing/<source_id>/`：

```text
manifest.json
transcript.md
meeting_summary.md
atomic_information_candidates.jsonl
residue.md
```

`manifest.json` 同时报告语义条目数和去重后的证据片段数：
`atomic_information_candidates` / `atomic_information_candidate_segments`、`residue_items` / `residue_segments`。
若同一片段同时支持 Atomic Information Candidate 和 Residue，`digestion_coverage.overlap_segments`
会记录该重叠，因此始终可以按“Atomic Information Candidate 片段数 + Residue 片段数 - 重叠片段数”核对已覆盖片段总数。

同一来源已有处理包时，CLI 会停止而不是覆盖。分析可以从一个转写片段提取多条 Atomic Information Candidate，也可以用多个片段共同支持一条 Candidate；歧义、冲突、上下文不足或证据不足的信息进入 residue。新 Processing 包使用 schema `1.1` 和 `candidate` 状态，明确区分自动 Atomic Information ingestion 与受治理的 World Model write。

## M2-B1 Durable Atomic Information

正常 `process` 命令会在五个 Processing artifacts 成功落盘后，把 contract-valid candidates 作为 revision 1 自动写入本地 `03_information/atomic_information.jsonl`。`atomic_information_id` 由来源和 candidate ID 确定性生成；精确重试不会产生重复记录，来源内容发生变化时会 fail closed。原始 `concerns` 作为文本保留，M2-B1 不将其解释为 Object ID，也不执行任何 World Model 写入。

可以使用手动命令安全重试或导入已有 M1 schema `1.0` 包：

```bash
python3 -m archeos information ingest 02_processing/<source_id>
```

开发和测试可以通过 `process --information-store <path>` 或 `information --store <path> ingest ...` 覆盖默认 Atomic Information store。实际 Atomic Information 数据位于 Git 忽略的 `03_information/**`；测试只使用合成数据和临时目录。读取旧 M1 schema `1.0` 包时仅兼容其历史 artifact 文件名；不会保留任何旧领域类型、ID、CLI 或存储路径别名。

## M2-A 本地 World Model

M2-A 使用标准库 SQLite 保存稳定 `Object` identity，并把 Name、Role、Lifecycle
与 Relationship 分开建模。默认数据库是本地且被 Git 忽略的
`04_core/archeos.sqlite3`；内部关系始终引用 opaque `object_id`，人工读取则通过
resolver 同时看到当前名称和 active Roles。

```bash
python3 -m archeos object create --name "Synthetic Operations" \
  --role business_line
python3 -m archeos object show <object_id>
python3 -m archeos object rename <object_id> --name "Renamed Operations"
python3 -m archeos object add-role <object_id> brand
```

这些命令只提供本地开发和人工验证边界，不会从 Atomic Information Candidate
自动创建或修改 Object。Role 仅接受 `CONCEPTS.md` 已批准的 vocabulary；
relationship 与 lifecycle 的基础操作由 repository contract 提供，并通过临时数据库测试。

## M2-B2 受治理消化

M2-B2 以显式命令把 durable Atomic Information / optional Claim 解释为对现有 World Model 的受治理变更。Claim enrichment 作为同一 `atomic_information_id` 的新 revision 保存，可保留已解析或尚未解析的 claimant、`assert / deny / uncertain` stance 与归因置信度；旧 B1 记录缺少 Claim 时继续读取为 `claim=None`。确定性名称匹配只使用当前或历史 Name，并只做空格和大小写归一化；唯一精确匹配可以绑定稳定 Object ID，歧义不会猜测，无匹配不会自动新建 Object。

安全、明确且证据充分的既有 Object 更新可以自动执行。Relationship 只接受 `part_of / member_of / responsible_for / depends_on / related_to`，并保留方向。新建、删除、Claim 冲突、歧义、关系不确定及 Role/Relationship 重解释会生成轻量 Change Proposal，等待人类批准、拒绝或稍后决定。所有实际变更进入 append-only Change Journal；Atomic Information、Claim、Evidence 与 World Model 历史均保留。Atomic Information extraction confidence 和 Claim attribution confidence 不会被复制为 World Model truth confidence。

批准前会同时检查 World Model before-state 与 Atomic Information revision。SQLite transaction 内的 apply receipt 会记录已经真正提交的结构变更；若后续 Atomic Information binding、Change Journal 或 Proposal JSONL 写入失败，重试只补齐缺失记录，不会重复创建、删除或结束结构。B2 不会自动跟在 B1 ingestion 后运行。

```bash
python3 -m archeos digest information <atomic_information_id>
python3 -m archeos digest pending
python3 -m archeos digest decide <proposal_id> approve
```

默认解释器通过官方 Codex SDK 使用 read-only、deny-all、ephemeral structured-output runtime。开发、测试和可重复诊断可用 `digest information ... --interpretation-file <path>` 提供结构化解释；该路径不需要网络。默认本地记录位于 Git 忽略的 `03_information/change_proposals.jsonl` 与 `03_information/change_journal.jsonl`，World Model 仍使用 `04_core/archeos.sqlite3`。

运行自动化测试：

```bash
python3 -m unittest discover -s tests -v
```
