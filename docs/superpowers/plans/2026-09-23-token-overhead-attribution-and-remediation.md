# SAG Token 开销归因与修复计划

日期：2026-09-23

2026-09-24 后续补充：两个小项目的 prompt 对照和 12 次 DBCP 只读诊断均已关闭；失败摘要候选暂不接入。下一轮加入 OpenCode 同版本的工具结果与 receipt 读取设计对照，按来源绑定、receipt 展示、调用/交付分别消融。参见 [工具结果与证据读取计划](2026-09-24-tool-evidence-navigation-ablation.md)。以下状态段保留原批次时间点，不代表仍在运行。

状态：F2 两项目全链路复验已封存，自动 Test close 漏记 finalizer 事实的问题已修复。15 项 SAG-only 正在收集：CLI/CSV 的 v2 有效记录保留；JCS/Net 原生任务通过，但独立验收漏采保持原字节/时间戳的重建产物。补入 inode/device 后，197 项回归、2 项原生对照及真实 Maven 三次安装对照通过，v3 于 2026-09-24 09:26:00 UTC 开始复验这两项并继续其余 11 项。Actor、Advisor、任务和实际模型配置均未改变；原 CLI、JCS、Net 的采集故障记录与消耗保留。参见 [记录映射](../../../output/sag-fifteen-post-repair-20260924/primary-cohort.json)、[v3 修订](../../../output/sag-fifteen-post-repair-20260924/measurement-amendment-v3.json)与[真实 Maven 对照](../../../output/sag-fifteen-post-repair-20260924/qualification-v3.json)。不采用 B 或全局关闭 Advisor，生产默认尚未改动。严格 CI 档案核验仍有独立缺口，不能将本地完成写成 CI 等价。完整进程续跑仍未验证，运行时提示文件沿用共同实验协议。

目标：在保持完整 build/test 验证、CI 任务要求和无人类反馈运行的前提下，消除失控请求，降低弱模型执行 setup 的上下文与协调成本。

## 1. 决策摘要

目前的证据不支持“SAG 普遍更省 token”。高开销包含一个明确的控制缺陷和几项日常成本：

| 优先级 | 问题 | 已有证据 | 处理方式 |
|---|---|---|---|
| P0 | DBCP 报告生成后无工具空转 | 124 次请求，2,170,306 token，占该运行 85.8% | 修复终止契约、无进展处理与压缩路径；先离线复现 |
| P1 | 报告阶段能力与封存规则不一致 | 模型仍看到 file_io/bash，但读写被封存规则拒绝；随后缺报告又阻止关闭 | 用封存快照完成报告交付，使工具说明、准入与阶段状态一致 |
| P1 | Advisor 混淆日志生产者与读取者 | DBCP 失败日志被 search 成功读取后，段落头变成 search/success | 分开生产结果与读取结果；修复引用去重和排序 |
| P1 | Advisor 按容量填入大量低相关日志 | DbUtils/Gson 的 test 咨询各约 54.4k input；大部分日志为依赖下载 | 先改输入选择，保持触发策略不变进行消融 |
| P2 | Advisor 机械触发 | 四个 SAG 运行均咨询三次；正常运行中占总 token 的 25%–34% | 单独比较入口咨询、问题触发和关闭 advisor |
| P2 | Actor 固定提示与协议负担偏重 | 尚未读取项目输出，首次请求已有约 7.8k input | 精简重复说明和 schema 文案，单独验证约束与能力不丢失 |
| P2 | 已完成的 build/test 仍需多轮阶段协调 | 正常三个项目在 test/report 又花 6–7 次 actor 请求 | 复用已有受信证据；将确定性收尾交给 harness，单独消融 |

先修正确性，再比较效率。不得通过放松验收、少跑测试、删除失败记录或事后扣除异常 token 宣称优化成功。

## 2. 证据范围与复现入口

### 2.1 本文使用固定快照，不追写在途结果

归因快照为 **2026-09-23 22:45:06 UTC**，包含 15 个计划槽位中当时已封存的 **11 个**、共 **296 次模型请求、3,941,681 token**。不是五个项目全部结束后的最终统计，也不表示阅读本文时实验仍处于同一进度。

冻结运行基于 `main` 的基准提交 `6cb3147f0065d6e6ef52d3c77fe3fbd4adf8fd9e` 及当时的未提交源文件。**提交号本身不能唯一标识运行实现**；以 pilot `plan.json` 的源文件字节/权限清单、定义文件哈希和封存 controller 为准。调查已核对 1,140 个源文件、79 个定义文件；逐请求正文与 usage 回执哈希通过复核。

该源快照的 `snapshot_sha256` 为 `76761adb057ab4b9c8d261d1ef86aae577644cd285f50400edcc9aa641c49bd2`。

| 资料 | 用途 |
|---|---|
| [原始归因报告](../../../output/requirements20-mini-20260923/token-investigation-20260923/findings.zh.md) | 模型所见、实际动作和代码路径 |
| [逐请求分析](../../../output/requirements20-mini-20260923/token-investigation-20260923/request-analysis.json) | 角色、阶段、usage、请求引用及 SHA-256 |
| [请求长表](../../../output/requirements20-mini-20260923/token-investigation-20260923/requests.csv) | 复算和配对分析 |
| [归因细节](../../../output/requirements20-mini-20260923/token-investigation-20260923/diagnosis-details.json) | 空转区间、日志构成、各阶段账目 |
| [核验记录](../../../output/requirements20-mini-20260923/token-investigation-20260923/validation.json) | 哈希、守恒与 controller 差异审查 |
| [分析文件清单](../../../output/requirements20-mini-20260923/token-investigation-20260923/manifest.json) | 防止归因材料被静默改写 |
| [冻结实验计划](../../../output/requirements20-mini-20260923/pilot/plan.json) | 任务、模型、资源、顺序与失败保留规则 |

工作目录中的通用 campaign controller 曾为后续 SAG-only 运行增加参数，其字节与 pilot 封存版不同；已审查运行循环及冻结检查函数未变，pilot 封存 controller 与计划哈希匹配。复现不能用工作副本替代封存版。

`diagnose.py` 针对上述固定快照核对归因；`analyze.py` 会重新读取当时已完成槽位。后续若扩充分析，应输出到新的版本目录，不覆盖本文引用的快照。

### 2.2 当前数字

单位为服务端记录的 input + output；状态来自共同外部完整任务验收。

| 项目 | SAG | Claude Code | OpenCode | 状态与解释 |
|---|---:|---:|---:|---|
| Commons DbUtils | 255,140 | 99,297 | 107,647 | 三组 complete；SAG 为基线的 2.57× / 2.37× |
| Commons CSV | 204,559 | 38,241 | 209,183 | SAG/OpenCode complete；Claude 因 Maven 不符合要求而 incomplete |
| Commons DBCP | 2,528,253 | 139,320 | 75,909 | SAG 的 build/test 有结果，但完整任务因 Javadoc incomplete；两基线 complete |
| Gson | 233,733 | 50,399 | 快照未包含 | 两组共同验收 unavailable，存在复用编译产物的来源绑定缺口 |

CSV 是反例：SAG 比同样完成任务的 OpenCode 少 4,624 token，约 2.2%。不能写成“SAG 在每个项目都更费”。Gson 的证据缺口要独立修复，不能为本次效率实验追认 success。

| 项目 | Actor 请求数 / token | Advisor 请求数 / token |
|---|---:|---:|
| DbUtils | 17 / 173,849 | 3 / 81,291 |
| CSV | 15 / 153,087 | 3 / 51,472 |
| DBCP | 150 / 2,459,426 | 3 / 68,827 |
| Gson | 15 / 154,335 | 3 / 79,398 |

### 2.3 比较限制

- 每项目/组目前仅一次；这些是机制证据和描述性成本，不是统计显著性证明。
- 使用 gpt-5.4-mini，并非已验证的小型本地模型结论。SAG advisor 为 mini-high；本计划不引入 Terra。
- SAG actor 未显式传 reasoning 参数，baseline actor 使用 Responses reasoning none；Claude Code 使用已披露的 Anthropic→Responses 兼容适配器。当前数据不能解释为所有模型参数完全相同的纯 harness 因果对照。
- cached input 已包含于 input，reasoning 已包含于 output，均不得重复相加。总 token、API 金额和本地推理时间是不同量。
- system/schema/日志的文本分项是 tokenizer 估计，不能冒充 provider 的精确分项计费。原始请求总数和 usage 才是总成本依据。

## 3. 归因：从模型所见到代码机制

### F1. 报告交付与阶段终止协议脱节，且无工具分支失去约束

DBCP 的实际轨迹：

| Actor 请求 | 模型所见或动作 | 框架结果 |
|---|---|---|
| 22 | `phase(done)` 结束 report | `report_missing` |
| 23–24 | 用 file_io / bash 创建报告 | evidence 已封存，拒绝执行 |
| 25 | `phase(blocked)` | 仍为 `report_missing` |
| 26 | `report(generate, status=partial)` | 报告生成，文字显示 `SETUP COMPLETED: PARTIAL` |
| 27 | 自然语言给出 partial 总结 | 框架提示“没有调用工具，请继续或调用 phase” |
| 28–150 | 继续给出无工具回复 | 重复追加同一类提示，直到总预算耗尽 |

第 27–150 次均无 tool call；其中 114 次重复“phase 已关闭、报告已生成、结果 partial”。这是模型的理解，实际 report phase 尚未关闭。

`react_engine.py` 的 native loop 在无工具分支直接 `continue`，绕过后面的 `_compact_window_if_needed()`；旧 no-progress 路径依赖 setup 已移除的 `manage_context`。现有对 rejected phase claim 的保护不能覆盖纯文本回复，且部分路径明确排除 sealed state。`phase_min_floors` 是后续阶段保留量，不是 report 阶段上限。

**证实的成本：2,170,306 token，占该运行 85.8%。** 扣去这段后的 357,947 只是账目分区，不能作为修复后成绩。尚未证实真实修复会保留完全相同的前 26 步轨迹。

代码入口：`src/sag/agent/react_engine.py` 的 `_run_native_loop`、`_handle_phase_signals`、`_compact_window_if_needed`、`_close_phase_for_agent_no_progress`、`_phase_budget_numbers`；`src/sag/tools/report_tool.py` 的完成提示与 metadata。

### F2. 封存阶段仍广告不可执行的工具，错误又没有给出可行收尾路径

DbUtils report 阶段读取 receipt/handoff 被拒绝，关闭阶段被 `report_missing` 拒绝，再手写报告也被拒绝。`_evidence_execution_closed()` 按工具名禁止 sealed 后的证据动作，但 schema 与提示仍展示这些能力。

这既增加请求，也可能把物理任务已完成的运行困在报告交付中。**原始 build/test 结果、完整 CI 任务状态与 SAG 控制流终止状态必须分开保留**；报告失败不能抹去已验证的测试，也不能被描述成流程完全正常。

### F3. Advisor 输入以容量为导向，成功日志挤占决策上下文

DbUtils/Gson 的 test 入口咨询分别输入 54,402 / 54,403 token。最大日志块中，依赖下载行分别占 94.2% / 83.6% 字符，估计约 45,654 / 40,757 token。建议主要是复用已有成功 receipt，关闭 test 阶段。

`_advisor_output_sections()` 展开完整历史 raw output；`pack_advisor_context()` 对能容纳的 optional 内容优先原样保留，超限后才压缩。当前 65,536 window 是能力上限，却间接变成装入目标。另有 executor framework 和完整 actor schema 被送入不能直接调用工具的 advisor。

本轮为 extractive compression，没有隐藏的 semantic summarizer 调用。12 次 advisor 请求的 cached input 都是 0。**已证实成本和低相关内容被发送；未证实删除这些内容后 advice 和最终轨迹一定不变。**

### F4. 机械咨询是否值得，尚需独立消融

四个运行各三次咨询：provision 一次为 actor 请求，build/test 各一次为 harness 入口咨询。正常三个运行中，advisor 占总 token 的 25%–34%。

CSV 的 actor 比 OpenCode 少 56,096 token，但 advisor 又花 51,472，最终差额仅 4,624。这个账目提示了机会，**不等于 no-advisor 实验**。不能仅凭 advice 看起来简单，就认定该咨询没有防止重复执行或错误行动。

### F5. 固定输入与阶段协调使正常路径也偏重

首次 actor 请求已有 7,769–7,841 input token。system 约 3.5k、完整工具 schema 约 4.6k 均为文本估计。阶段窗口会重置，不能笼统归因为“全部历史从不清理”；固定协议仍在每轮重复。

DbUtils、CSV、Gson 的 agent 侧各一次 Maven 命令已经执行 tests，随后 test/report 又分别花 77,563 / 69,302 / 67,325 actor token，共 6–7 次请求。核验与交接必要，但部分读取、重述与报告生成可由确定性代码完成。

正常三个运行的 input 占总 token **98.2%**，因此优先缩短输出或降低 advisor reasoning 未击中本轮主因。

### F6. 日志读取成功覆盖了生产者失败语义

DBCP 的失败 build output_ref 被 search 成功读取后，advisor 段落头部变为 `tool=search, operation_outcome=success`。反向遍历后按 ref 去重保留了读取者外壳，并可能降低失败优先级。

这不是“构建失败变成成功”的有效证据。当前 advisor 仍识别出 Javadoc 错误，所以**不能把它计为本次已证明的失败原因或 token 贡献**；但它是修复上下文选择前必须解决的语义缺陷。

## 4. 修复必须保持的约束

1. **判定权不变。** Phase termination 管控制流；最终 verdict 由现有 finalizer 依据封存证据计算。模型文本、report 的旧 completion 字段不能直接建立成功。
2. **任务不减配。** 完整冻结命令、JDK/Maven 要求、工作树规则、模块/产物/测试/文档/质量检查要求不变。DBCP 的 Javadoc 失败继续如实记录；不为省 token 删除该要求或修成项目专用例外。
3. **证据与模型视图分开。** 原始日志、报告、receipt、源码绑定完整封存。摘要和节选只改变模型视图，不删除证据，不参加成功判定；missing 仍是 unknown/unavailable。
4. **大窗口仍可使用。** 不因必需内容装不下就跳过已触发咨询；先 rank，再区分保留、节选、摘要，必要时分片。不同 advisor 的真实窗口分别处理。
5. **工程保持简单。** 复用现有 loop、phase gate、snapshot、AdvisorSection、packer、request ledger 和离线分析；不新建通用 workflow、memory manager、检索服务或第二套 evaluator，也不重新引入 context management tool。
6. **实验隔离。** 不修改在途 campaign 的代码与配置。不切换其当前 checkout；实现使用隔离目录或等待该 campaign 封存。旧失败和全部 token 保留，新版本新 run ID。
7. **现有能力保留。** 修复针对所有 setup 项目的通用控制/上下文路径；项目名只出现在回归夹具与实验选择中。

## 5. 实施任务

下面保留原实施顺序，并逐项标记当前状态。每项先补能重现行为的测试，再做最小修改，再核对真实调用路径；只通过纯函数测试不能宣称 live loop 已修好。具体运行证据见 §9。

### T0. 固定最小回归夹具与诊断基线

依赖：无。

- [x] 从当前归档提取 DBCP 报告生成→无工具回复、DbUtils sealed 读取拒绝、成功 build/test 复用三类最小片段。
- [x] 新建 `tests/fixtures/token_overhead/`，记录原请求 ID、源文件 SHA-256 和提取规则；不要复制整套巨量日志或凭证。
- [x] 大量下载行使用有来源说明的测试生成数据扩充，保留真实错误/版本/命令边界，避免庞大 golden 文件。
- [x] 使用 scripted `NativeTurn` 驱动真实 engine/phase/gate/render 路径；原缺陷应由行为断言复现，不调用线上模型。
- [x] 至少一条收尾集成场景贯穿真实 phase gate 与 finalizer；现有 loop 测试中“总接受”的 `_PhaseTool` 可用于控制流单测，不能独自证明验收边界正确。

测试落点：现有 `tests/test_native_loop_engine.py`、`tests/test_advisor_context.py`、`tests/test_report_tool_phase_context.py`；必要时新增单一场景文件 `tests/test_token_overhead_regressions.py`。

完成条件：能证明请求会空转、封存能力矛盾和低相关日志扩张；原始 usage 与分析分账可重算。夹具只复现问题，不替代新版本的真实实验。

### T1. 统一无工具回复的收尾路径，修复报告终止契约

依赖：T0。优先级：P0。

代码：`react_engine.py`、`report_tool.py`；复用 phase claim/gate/transition policy 和既有无进展控制。

- [x] 所有已完成的模型轮次都经过适用的压缩、轨迹记录与无进展检查；无工具分支不能直接逃过这些步骤。确保 turn/journal 恰好记录一次。
- [x] ReportTool 只声明“报告已生成”和封存 verdict，不直接关闭 phase 或改写 evidence。保留现有“report tool 不自行终止活动 phase”的边界。
- [x] Engine 在 report 工件已绑定当前封存 snapshot 时，通过现有 gate/transition policy 完成控制收尾；记录 controller 发起的合法 transition，不信任自然语言或旧 `completion_signal`。
- [x] 无可验证终止条件时，给出一次与当前状态一致、实际可执行的协议纠正；同一状态已经纠正过而再次无工具返回，则走既有诚实终止路径，不无限重发相同提示。
- [x] “同一状态”依据 phase attempt、受信证据/封存快照、真实 job 状态及 repair context；不能用消息长度、token 数或每轮递增的审计序号作为进展。
- [x] 未完成 job 仍由 barrier 负责等待，不按聊天空转中止；未执行的强制 test floor 不得被新保护跳过。sealed 状态的控制失败不能反向写坏已经封存的 build/test verdict。

这里的“一次纠正”是一次协议修复机会：同一原因、同一证据、同一纠正已送达，再发送相同内容没有新信息。不是随意设定若干轮重试；不需要给 report 再引入一个调参阈值。

验收场景：

- [x] 重放 DBCP 尾段不再消耗剩余全局 150 次预算；partial 保持 partial。
- [x] 有报告但 snapshot 不匹配、报告未持久化、证据冲突时不能自动成功关闭。
- [x] 正常 done、真实修复后有新证据、正在运行的 job、缺少 test attempt 分别沿原正确路径运行。
- [ ] 重启/重放不会重复生成终止事件，token 和模型请求账目仍守恒。当前已验证同一 engine 重复收尾幂等、真实控制日志的只读恢复投影、既有 control replay 测试及 provider 账目；尚未新增完整进程重启后恢复已关闭 phase 的端到端场景，不能等同声称该场景已完成。

重点测试：`test_native_loop_engine.py`、`test_react_engine_progress_guard.py`、`test_react_engine_abort_wiring.py`、`test_report_tool_phase_context.py`、`test_control_layer_replay.py`、`test_native_messages.py`。

### T2. 修复封存报告接口，并保留日志生产者身份

依赖：T0；报告控制部分依赖 T1。优先级：P1。分成两个可独立审查的小提交。

**T2a — 报告能力一致性**

- [x] Report 直接消费当前 sealed snapshot，不要求模型重新读取 live workspace 或手写报告以建立已有事实。
- [x] sealed 状态的 schema、提示与准入一致：只公开确实可执行的报告/控制能力，拒绝信息提供现有有效动作。
- [x] 不为解决“读失败”开放任意 sealed 后 bash 或写入。确有诊断读取需求时，只投影已封存字节并保持其原始引用，不写回 verdict-bearing evidence；该能力另由需要它的回归场景证明必要。
- [x] DbUtils 场景不再出现“要求报告→拒绝创建报告→仍缺报告”的死路。

**T2b — 生产者与读取者分离**

- [x] `_advisor_output_sections()` 从现有执行 lineage/receipt 找到输出生产者；读取者工具成功只表示读取成功。
- [x] 去重以输出对象与生产调用的稳定身份为依据；不因后续 search 覆盖失败状态，也不拼接不同 producer 的结果。
- [x] 生产者缺失或引用冲突时显式 unknown/conflict，不从读取成功推断运行成功；保留指向原始错误的检索路径。

代码：`react_engine.py`、`react_llm.py` 的实际 schema 投影、`report_tool.py`、`react_engine.yaml`。先复用已有元数据；只有确认生产者字段真的缺失才在原数据模型中最小补齐。

测试：`test_report_tool_phase_context.py`、`test_report_tool_metrics_artifact.py`、`test_tool_result_evidence_contract.py`、`test_advisor_engine_flow.py`。必须覆盖“build failed→search succeeded→advisor 仍看到原 build failed”、相同 ref 冲突与缺 producer。

完成 T1/T2 后形成 **正确性基线 C**。坏循环不保留为生产功能开关；旧版仅作为封存历史和离线反例。

### T3. Advisor 从“装满窗口”改为“按当前问题选择信息”

依赖：T2b。优先级：P1；需要效率消融。

代码：`react_engine.py` 的 `_advisor_messages` / `_advisor_output_sections`、`advisor_context.py`；复用 `advisor_compaction.py`，不新增检索架构。

输入规则：

| 信息 | 默认表示 | 超限时 |
|---|---|---|
| 冻结任务、字面命令、runtime/工作树约束、当前明确问题 | 必须保留的核心字段 | 不丢失约束；必要时按来源分片 |
| 当前 phase、受信判定、失败签名、producer/ref、前后状态变化 | 结构化 facts | 去掉重复表示，不能把未知压成成功 |
| 与当前问题有关的错误、源码、配置、失败前后日志 | 完整相关片段与原件引用 | 可溯源节选/摘要，标明遗漏 |
| 已成功步骤、已解决失败、重复历史 | 状态摘要与引用 | 不反复展开整段原文 |
| 下载进度、重复成功输出、无关工具说明 | 原件留档，默认不进入本次视图 | 若当前问题正是下载/依赖失败，恢复相关上下文 |
| Actor 工具能力 | 精简的可用操作契约 | 仅需要参数细节时补充对应 schema，不默认发送全部 schema |

- [x] 选择相关信息发生在窗口压缩之前；不能因为还有空间就回填无关成功日志。
- [x] 保持现有较大 context window 和按模型计算的上限；先结构化去重/节选，再在需要时语义压缩，必要时分片，不能以装不下为由跳过已触发的咨询。
- [x] 不引入固定“必须压到 X token”的武断目标；预算首先来自模型上限，默认内容来自当前问题，节省以真实请求比较验证。
- [x] 保持现有 section audit，记录选择原因、原件引用、full/excerpt/summary 状态及估计 token。任何 summarizer/分片请求也必须进入角色成本账本。
- [x] 不让压缩器修改 receipt、case identity、物理判定或 CI scope。引用的压缩材料不是新的证据。

测试：`test_advisor_context.py`、`test_advisor_compaction.py`、`test_advisor_engine_flow.py`、`test_advisor_jvm_test_observations.py`。

关键验收：成功 build 后追加与当前决策无关的下载行，默认 advisory payload 不随原始日志线性增长；依赖下载失败时关键错误仍保留；窗口变小也不静默丢失必需约束；全文仍可按来源追溯。不能只检查“token 更少”。

### T4. 独立比较 Advisor 触发策略

依赖：T3 的输入方案已验证。优先级：P2。

- [x] 对比现有入口咨询、存在未决问题时咨询、`advisor_mode=off` 三种策略，均使用已修复控制面。
- [x] 问题触发依据可审计状态：有新失败或证据冲突、方案/环境存在未决约束、相同修复未推进；actor 的主动咨询入口保留。
- [x] 完全相同的问题和证据不机械重复咨询；新的失败、版本/命令/证据变化能够重新触发。避免仅凭“上次已经问过”压制新问题。
- [x] 不把 advisor 请求变成执行许可；咨询失败不能阻断本来可执行的合法工具调用。
- [x] 记录触发原因、输入事实摘要指纹、advice、后续动作及结果。只能把有证据的关联写成“与建议一致”，不能自动认定 advice 导致成功。

T4 的实现与情景单元测试已完成；仅健康 DbUtils 做了真实对照，未覆盖旧 when-stuck redirect 的困难路径，因此仍不选择该策略为默认。

代码：`react_engine.py` 的 entry consult/consult 入口、`advisor.py` 的工具说明；尽量复用现有配置与 call audit。

测试：`test_advisor_guarantees.py`、`test_advisor_engine_flow.py`、`test_advisor_tool.py`。旧 always-on 测试属于待比较策略，不得仅删除断言来宣布修好。

### T5. 精简 Actor 固定输入

依赖：正确性基线 C。优先级：P2；与 T3/T4 分开比较。

- [x] 从真实 provider 请求清点 system 与 schema 的重复信息，优先合并多处 lifecycle、成功定义、工具示例和重复任务描述。
- [x] 保留任务约束、当前 phase、关键版本/命令、证据边界、真实可用能力和检索方式；一般 build/test 中不按 phase 名称剥夺修环境或读源码能力。
- [x] 不用更激进的全局日志截断替代精简。Actor 当前已有 observation clamp；必须保留完整输出落盘及 `search` / `rg` / `sed` 的可达路径。
- [x] 首轮消融只改重复文案，不同时改 schema 必填条件、工具行为和检索策略；若需减少 schema 结构，再单列候选。

代码：`react_prompt_builder.py`、`react_engine.yaml`、`react_llm.py`；工具定义只修改确证重复的描述。

测试：`test_system_prompt_native.py`、`test_react_prompt_builder.py`、`test_react_engine_prompts.py`、`test_prompt_vocabulary.py`、`test_react_llm.py`。用情境断言检查关键能力和约束，不新增脆弱的整段字符串 golden。

### T6. 减少已满足要求的阶段协调

依赖：T1/T2；独立于 T5 验证。优先级：P2。

- [x] 对 build 中已产生的 test receipt，用现有验收器检查任务/命令/版本/范围/工作树/执行身份是否一致，再复用其结论。
- [x] 分清“已有一次测试尝试，不能强迫重复”与“测试要求已通过”。失败、未知、缺身份或范围冲突的记录不能自动满足成功门槛。
- [x] 保留 test phase 的显式审计和 gate；只有全部相应要求被受信事实满足时，harness 才可完成确定性交接，避免模型重复读取/重述相同事实。
- [ ] 将报告文件、运行时路径等确定性导出复用已有 snapshot/recorder；不让模型在多阶段反复手写同一份元数据。
- [x] 不能用原生 SAG 结论替换共同外部验收。Gson 的编译产物连续性缺口仍单独记录，不能借本任务提升结果。

代码：`react_engine.py`、`phase_gates.py`、既有 receipt/test-attempt 与 report 路径。先复用现有 cross-phase 逻辑，不新建“已经成功”缓存或第二套判定器。

测试：`test_cross_phase_test_attempt.py`、`test_phase_handoff_test_stats.py`、`test_job_lifecycle_phase_gate.py`、`test_report_tool_metrics_artifact.py`。必须覆盖换 JDK、换命令/profile、范围不一致、零测试、缺少报告、partial 与相同合格 receipt；零测试需区分已有声明及证明的空选择与缺少执行证据，沿用原验收语义。

### T7. 消融、选型与收敛

依赖：T1/T2 先建立 C；其余候选按下面的协议逐项进入。

- [x] 将每个候选的 source/config/prompt/任务/模型 wire 参数冻结到独立清单；请求与结果均保存原文和哈希。
- [x] 输出逐项目、逐阶段、逐角色的结果与成本；保留失败、超时、partial、unavailable 和未完成发送的请求记录。
- [ ] 每项独立判断是否保留；不默认将所有节省候选打包上线。只在收益和正确性均明确后组合，并检查交互。
- [ ] 最终默认设计以新的完整实验为准；归档临时实验分支与配置，不在生产保留永久的坏路径兼容层。

## 6. 消融协议

### 6.1 版本与对照关系

| 组 | 内容 | 比较回答的问题 |
|---|---|---|
| H | 原始冻结运行，保留全部失败与成本 | 法证基线；不为复制 124 轮失控而重新在线运行 |
| C | T1/T2 正确性修复，其余策略不变 | 正确性修复后的可执行基线；H→C 只能作前后描述，不能视为随机配对 |
| A | C + T3 上下文选择 | 对 C：更相关的 advisory 输入是否降低成本、保持能力 |
| B | A + T4 问题触发 | 对 A：改变触发是否值得 |
| N | A 的 advisor-off 条件 | 对 A：advisor 在同一控制/输入框架下的净效果 |
| D | A + T5 Actor 文案精简 | 对 A：固定提示负担能否降低 |
| E | A + T6 确定性交接 | 对 A：阶段协调能否减少 |
| F | 只组合通过验收的候选 | 对各自父组及 C：组合收益是否仍成立，是否出现交互回退 |

每个对照比较只变更表中定义的干预。**冻结触发策略不等于强行固定实际咨询次数**：模型轨迹改变可能自然改变后续调用，必须记录这种变化。使用固定归档咨询输入的离线配对隔离“打包本身”的效果；真实运行衡量整条轨迹的总效果。E 若自然减少了后续咨询，需披露该中介变化，不能把各组节省百分比相加。

### 6.2 项目选择与运行顺序

先用已观察的小项目定位机制：

| 项目 | 选择依据 |
|---|---|
| DbUtils | 三基线均完成，日志下载多，存在报告交接问题；最直接的正常成本比较 |
| DBCP | 已确证无工具循环、封存冲突、Javadoc 失败；验证诚实失败与有界终止 |
| CSV | 完整 defaultGoal，质量/文档要求重但日志相对紧凑；防止只对下载日志优化 |
| Gson | 多模块与大日志，存在 receipt/产物复用边界；检验范围保持和不误报 |

1. T0–T2 先离线重放及针对性测试，不花线上模型预算复现已知失控。
2. C/A 先在 DbUtils、DBCP 做机制冒烟；每项目/组一次只用于接线与数据完整性检查，不用于效应估计。通过后补 CSV、Gson，再逐一引入 B/N/D/E。
3. 同一小对照内源版本与定义不变；新容器/新依赖缓存，串行运行，预先轮换组顺序，不因当前分数改变顺序或换项目。
4. 完整五项目原始批次全部封存后，统一收录其结果；GJF 的准入和结果必须按自身冻结证据审查，不因本计划追认成功。未准备好或证据不足的槽位明确保留，不找更容易项目替换。
5. 只有候选通过小规模核验后才扩展既有 20 项目清单；名单与标签在读取新结果前冻结。大项目单个运行，沿用 4 CPU、8 GiB、无 swap、32 GiB 磁盘保留和 7,200 秒总期限；这些是当前 pilot 已声明边界，不为某个版本临时放宽。

开发集已经用于发现缺陷，不能把其优化后结果伪装为未见项目泛化。论文确认集与样本量需另行预注册，标签分层覆盖构建系统、大小、模块和要求类型。

### 6.3 重复次数与统计边界

初次单次运行是工程冒烟，不宣称显著性。正式效应比较前，依据独立预实验的项目内波动、预先声明的最小关心差异与资源预算确定重复数并冻结；不能观察到有利结果后停止，也不能只保留最好一次。

若预算只支持单次或少量重复，按逐项目配对明细报告，明确是探索性证据。不给出无依据的“降低至少 30%”验收线，也不从四个项目计算虚假的普遍优越性。正式汇总以项目为比较单位，运行重复用于估计项目内波动，不把同一项目多次请求当作独立项目样本。

## 7. 指标、停止条件与采用规则

### 7.1 保持原来的任务指标，再增加诊断分账

| 维度 | 记录方式与解释 |
|---|---|
| 完整任务 | 共同 evaluator 的 complete / incomplete / unavailable 及各原因；不以模型或 CLI 退出词代替 |
| Build / Test | 原要求协议中的分组结果、已证明分子/要求分母、测试 counts/identity；缺范围继续不可评分，不另造宽松标准 |
| 自主运行 | 原有来源可追溯的 human_interventions、流程终止、超时和基础设施失败；不能从沉默推断 0 |
| Token | 全部 provider 请求的 input/output/total/cached/uncached；actor/advisor/summarizer 分账，失败请求已知与未知成本分开 |
| 无人类反馈时间 | 沿用当前 recorder：新容器准备开始至共同验收关闭，含 acceptance；源码快照/导出开销不在该边界。agent_seconds 另列，不能混用 |
| 流程诊断 | 无工具回复、重复纠正、被拒工具、LLM 调用数、阶段 token、终止原因 |
| Advisor 诊断 | 触发原因、section 来源/选择/压缩、输入大小、建议与后续动作关联；不自动标注“有用” |
| 资源 | 沿用当前采样的内存/磁盘、OOM/空间停止记录，区分模型失败与基础设施中断 |

定义：

`T_run = Σ_request (input_tokens + output_tokens)`，含所有失败和收尾请求。若存在 usage 不明的请求，total 标明不完整，known_tokens 单列，不填零。

`T_per_complete = 同一冻结组全部尝试的总 token / complete 尝试数`，只在 usage 完整且分母非零时提供，同时展示总尝试数、complete 数和状态分布；不能以成功样本自身的均值代替它。分母为零则不可估计，保留真实成本。

同时提供“共同 complete 的配对成本”，明确其样本数和选择偏差；它只是次级结果，不能删掉主表中的失败/unknown。不要用所有项目 token 简单相加后的一个倍数掩盖 DBCP 这种极端值。

### 7.2 硬性停止条件

- 新版本出现虚假成功、原件/receipt 绑定丢失、sealed verdict 被改写、必须执行的 test floor 被跳过：停止该候选的推广，保留失败槽位。
- 请求账本缺失、封存哈希不符或独立重算失败：停止后续昂贵槽位，先修记录链；不把该槽位归为普通模型失败。
- 内存/磁盘达到既有保护条件：按协议停止并归档，不能靠删除未封存证据继续。
- 正常模型/项目失败仍是数据，按预先协议继续，不按当前成绩重排或替换项目。

### 7.3 采用规则

正确性修复通过相关反例、真实 loop、重放与跨阶段验收后进入 C，不要求保留有缺陷的旧行为作为生产选项。

效率候选只有在完整证据与约束保持、未观察到新的错误成功或任务能力回退，并在预先冻结的配对中有一致的成本收益时才作为默认候选。小样本未见回退不是统计非劣性证明；存在 token/成功率/时间权衡时必须同时披露，不用“token 更少”单独拍板。关闭 advisor 后若更省但更容易失败，不能被写成无条件优势。

## 8. 回归运行与交付清单

按任务运行上文相关测试，避免每个小改动都重复全仓测试；最终候选再跑涉及 loop、phase、sealed verdict、receipt、advisor、request ledger 的集成回归。命令使用项目环境，例如：

```bash
UV_CACHE_DIR=/private/tmp/setup-agent-uv-cache PYTHONPATH=.:src uv run pytest -q \
  tests/test_native_loop_engine.py \
  tests/test_react_engine_progress_guard.py \
  tests/test_report_tool_phase_context.py \
  tests/test_advisor_context.py \
  tests/test_advisor_engine_flow.py \
  tests/test_cross_phase_test_attempt.py \
  tests/test_model_request_ledger.py
```

这个命令是现有相关测试的起点，不代表 T0 的新增反例已经写好或通过。

- [ ] 各任务的最小修复、反例测试与真实调用路径证据。
- [ ] C 与各候选的源文件、配置、prompt、wire 参数、任务和 CI 输入冻结清单。
- [ ] 每个槽位的原始请求/回复/usage、完整日志、receipt、工件、干预记录与资源记录。
- [ ] 逐项目完整任务/build/test 结果，以及阶段/角色 token 和无人类反馈时间对比。
- [ ] 每项候选的保留/撤回/未决结论；已证实机制与仍待验证的假设分开。
- [ ] 最终默认配置及组合回归；原始失败归档不覆盖、不删减。
- [ ] 论文可用论述边界：当前可主张具体缺陷及其开销；只有新的控制对照与确认集支持后，才能主张 SAG 对弱模型的效率或成功率优势。

DbUtils、DBCP 的 C/A 在线机制冒烟及逐请求核对已完成，结果未达到当前候选的默认采用条件。下一项是独立验证工具操作契约保留，并补齐执行入口与 receipt 的覆盖差异；在输入选择验证完成前，不混入触发策略、Actor 提示或阶段交接的改变，也不重跑全部 20 项目。


## 9. 实施与验证记录（2026-09-23，美东时间）

| 任务 | 当前状态 | 可审查证据 |
|---|---|---|
| T0 | 完成：三类归档片段、源请求哈希与提取脚本 | `tests/fixtures/token_overhead/`、`tests/test_token_overhead_regressions.py` |
| T1 | 收尾修复已接入 native loop；进程重启场景待补 | 四次 C/A 正常收尾、无无工具回复空转；两次 A 均由模型只调用 report 后，控制器经真实 gate 核对快照和字节完成 flow close |
| T2a | 完成：报告按当前 snapshot 和实际字节绑定；sealed schema 与准入一致 | `report_delivery.py`、`phase_gates.py`、`react_llm.py`；旧 completion metadata 不被采信 |
| T2b | 完成：生产者身份和输出对象联合确定日志语义 | failed build → successful search 仍标 failed；缺生产者/冲突标 unknown/conflict |
| T3 | 当前 A 不设为默认；默认仍 `all`，A2 恢复工具用途说明仅离线 | A/DBCP 总 token +14.4%；A/DbUtils −9.1% 但原生要求评分 unavailable；复合 Bash/管道缺 receipt，日志也未走预期摘要路径 |
| T4–T6 | 待实施与独立消融 | 不计入当前 C/A 版本 |
| T7 | 第一轮 C/A 冒烟及审计完成，未完成全部候选选型 | 当前 A 不默认启用；未进行 A2 在线请求或 T4–T6 消融 |

### 9.1 已冻结版本

- C：`854530fce3e89a149b431f7885f96b5b4b56e9555e73c7e4efec732e71c5bc53`，仅 T1/T2 正确性修复。
- A：`aad5edc1e22b59b8fac8f2e45c32a1fab394e561e8eab2032b91792959d2ee29`，C + T3，65,536 context window、咨询触发和 Actor 提示不变。
- 两版生产代码差异仅 `advisor_context.py`、`react_engine.py`、`settings.py`。
- 具体源字节在 `output/token-repair-20260923/{C,A}-source/`；线上运行从隔离 checkout 读取，后续工作副本编辑不会改变在途运行。
- [定向回归输出](../../../output/token-repair-20260923/offline-candidate-validation.txt)：576 passed。不是全仓测试结果。

### 9.2 当前线上证据

C/DbUtils 已由共同外部验收判为 **complete**：8/8 CI 要求、build 2/2、test 1/1、quality 5/5；测试总数为 523/523，与 CI counts 一致。逐用例身份等价性未因此建立。

该次运行用了 **263,596 token**（actor 179,351、advisor 84,245），17 次 actor 请求、3 次 advisor 请求；含独立验收的运行时间为 **437.13 秒**。无语义 summarizer 请求，报告交付后正常关闭。该次最后一轮实际同时调用 report 和 phase，不能作为“模型只生成报告时自动补齐关闭”的线上证据。

历史 DbUtils 为 255,140 token，本次增加约 3.3%。这证明不能把正确性修复直接等同于正常路径的 token 改善。DBCP 的 C 运行已完成：完整任务 incomplete、22/23 要求；build 8/8、test 1/1、quality 13/13、Javadoc 0/1。1,596 passed / 9 skipped，与 CI counts 一致。实测 253,742 token（actor 180,341 / 16 请求；advisor 73,401 / 3 请求），含独立验收 840.23 秒。两次 C 均无无工具回复。A 对照也已完成，结果见 §9.5；不使用“扣除旧坏循环”的算术值代替实测新成绩。


### 9.3 补充审查发现：报告尾段的重启投影（已补修）

直接将 C/DbUtils 的完整 `control_events.jsonl` 交给 `recover_active_repair_context_from_path()`，会得到 `control stream continues after evidence_close`。原因是该恢复器只允许 seal 后出现 publication，而真实运行还会记录合法 report/phase 交付事件。原始 H/DbUtils 也具有同类 seal 后事件；这是之前已有、此前定向测试未覆盖的恢复接口缺口，并非 C/A 的输入选择差异。

共同外部验收的归档哈希与独立 score 重算已通过；它们不等于此内部控制事件的重启投影。已在工作副本补修：合法报告控制事件可以继续通过原配对/门控检查，新 build、嵌套 build、build facts、跨阶段跳转、重复封存、丢失配对和关闭后继续执行仍拒绝。新增回归后定向集合 621 passed；两份 C 真实控制流在修复后完成只读恢复，归档字节保持不变。完整进程续跑仍未据此声称完成。C/A 的正常启动运行继续保留冻结字节，不将此后补丁混入其中一个对照。


### 9.4 当前交付与批准边界

[C/A 消融结果报告](../../../output/token-repair-20260923/comparison/findings.zh.md)提供实际逐项目、原生/共同验收、要求组、角色、成本与资源结果。四次归档及独立评分重算通过，本轮四个实验容器已按协议删除。A 在用户明确批准后，于 2026-09-24 00:43:30 UTC 启动、01:12:37 UTC 完成封存；既定两项目顺序与冻结代码不变。

最后补修的 `replay.py` 与回归文件哈希记录在 `output/token-repair-20260923/restart-validation.json`。它们不属于先前 C/A 源快照。没有新增 T4/T5/T6 候选，也未变更默认上下文选择。

### 9.5 C/A 结果与下一步边界

| 项目 | C → A token | C → A 含验收耗时 | 共同验收 | 原生要求验收 |
|---|---|---|---|---|
| DbUtils | 263,596 → 239,633，−9.1% | 437.13 → 429.62 秒 | 两组均 complete，8/8 | C complete；A unavailable，精确 CI 命令 receipt 缺失 |
| DBCP | 253,742 → 290,241，+14.4% | 840.23 → 1,294.52 秒 | 两组均 incomplete，22/23，Javadoc 未过 | 两组同共同验收 |

当前 A 不满足 §7.3 的默认采用条件。不能将 A/DbUtils 的外部验收成功反向填成原生 SAG 成功。两次 A 均无无工具 Actor 回复；模型只调用 report 后的控制器自动收尾已在线触发。

已确认的执行链路：A 精简工具表删除函数用途说明；DBCP advisor 建议合并检查与构建，actor 通过复合 Bash 执行；DbUtils actor 在固定命令后追加 `| tee`。这些 shell 形式没有进入 literal JVM runner 的 receipt 路径，又不在当前 build 日志摘要覆盖范围内。两项均因此触发强制 test，再被固定任务 gate 指出 CI 命令缺 receipt。DBCP 随后补跑完整命令，DbUtils 以 partial 结束。六次 A 咨询都完整保留固定任务和 benchmark brief，不能把违规变体归因为任务约束被截断。

工作副本已准备最小 A2：恢复精简工具表中的函数 description，保持其他行为不变。127 项定向测试通过；patch 与前后哈希在 `output/token-repair-20260923/A2-offline-candidate.*`。**A2 尚未进行在线模型请求，也未设为默认。** 描述增加的文本估计不是真实行为收益；部分视图需重新打包，不能用简单 token 加法宣称上线预算。

后续实验先独立验证这一操作契约修正。shell 执行/receipt 覆盖及强制 test 与固定 CI 命令重复执行的问题需另行修复和对照；依据受信的执行身份、命令、环境与输出绑定，不通过放松 gate 或看到命令名称就追认成功。T4/T5/T6 暂不混入当前输入策略对照。

### 9.6 A2 冻结与启动状态（2026-09-24，美东时间）

用户要求继续下一步后，已从 A 的冻结源字节建立独立 A2 checkout，生产代码仅恢复 advisor 精简能力表中的函数 description。源码 SHA 为 `66635ff3f0a25d67d7be554641b184fb3f716336306db9f2648b109117f7f105`，其余生产文件均与 A 相同。40 项 advisor context / engine flow / compaction 测试在这份隔离源码上通过。

两项目仍为 DBCP、DbUtils，同序串行；任务定义、模型配置、controller、镜像和资源策略逐项核对一致。冻结计划及前置核验见 `output/token-repair-20260924-a2/experiment-plan.json`、`preflight.json` 和 `A2-smoke/plan.json`。

启动曾被自动审批拒绝，因为此前的数据外发批准只覆盖 A。用户随后明确批准 A2，原阻塞解除，A2 于 2026-09-24 05:12:01 UTC 开始按冻结顺序运行。实验中不改代码或任务；具体启动状态记录于 `output/token-repair-20260924-a2/launch-status.json`。


## 10. 2026-09-24 继续执行与 SAG-only 批次

用户已批准后续必要实验，并明确“修复完成后再跑 15 个项目，只跑 SAG 收集数据；已存在的 OpenCode / Claude 不重跑”。审批范围沿用 mini 模型、既定数据出口、固定任务、资源限额和完整留档，不包含弱化验收。

- A2 已封存：DbUtils 189,109 token，原生/外部 complete；DBCP 354,974 token，原生/外部 incomplete（Javadoc）。详见 [A2 归因](../../../output/token-repair-20260924-a2/comparison/findings.zh.md)。
- 修复 Java `1.8.0_504` 与 `[8,)` 的比较；环境注册与工具发现复用同一 Java 版本语义，保留 patch 上下界与其他工具的原语义。增加已绑定 JVM receipt 的 bash 日志投影覆盖。
- C3/A3 的 source 都是 `ac088832ca1afd031afd43acab8369cd36e54eac62a6bf77d53802bded98bc7e`，但 A3 的配置误写在协议根部，runner 读取嵌套的 `model_config`，实际请求均为 all。[审计及已发生费用](../../../output/token-repair-20260924-context3/comparison/findings.zh.md) 保留，不称为消融效果。A4 仅修正嵌套配置，复用 C3 控制和同一源码；已用冻结 Config 子进程证明生效，首个真实请求也为 relevant。[A4 计划](../../../output/token-repair-20260924-context4/experiment-plan.json)。
- T4/T5/T6 分别为问题触发、重复提示精简、既有合格 test receipt 的阶段交接。测试交接必须通过同一个 PhaseTool 要求门、完整 task、scope/receipt 绑定、零失败/错误及强制尝试门；未知和失败仍由 Actor 处理。全部当前默认关闭。
- 策略候选在 DbUtils 单项目上分别比较 P/B/N/D/E；这是成功路径的机制冒烟。采用组合前还要做较重要求项目验证。v1 因去重遗漏环境变化而停用；v2 因同样错放配置且旧 runner 强制打开 Advisor 而停用，两版发送数均为 0。v3 已逐组核对嵌套声明、真实 Config 子进程和单变量差异；[v3 计划](../../../output/token-repair-20260924-strategies-v3/experiment-plan.json)。
- portable runner 拒绝错放与未知模型设置，保留显式 `advisor_mode=off`，在首次模型请求前验证实际子进程配置，运行后再次对照原生 run-pin；相关 98 项测试通过。配置未生效的运行不是模型失败，也不得用其结果选择策略。
- 当前定向回归为 490 passed / 1 skipped，后续 20 项触发/交接检查通过；集合有重叠，不相加。Curator 通用 legacy wrapper 补充独立 84 项回归通过；这些不是模型实验成绩。
- [15 项前瞻名单](../../../output/sag-fifteen-post-repair-20260924/prospective-cohort.json) 从已准备原集合按定义完整性和已知记录缺口选择，包括 CSV 重跑。DbUtils/DBCP 属于独立机制对照；Gson/Cayenne/Jackrabbit 的旧不可用或待审限制保留。本批不声称与旧五项合成 20 个不同项目。
- 15 项定义已复制到独立 prepared 目录；Curator 增加字节绑定的通用 wrapper profile，任务命令与 required rows 不变。完整项目运行仍必须实际观察 launcher 输入、二进制、运行时、产物和报告，不凭 profile 直接成功。

A4 已结束：DbUtils 177,385 token（相对 C3 -17.4%），DBCP 273,575（+24.4%）；原生/外部结果、要求组和测试计数保持一致，六次真实请求来源审计通过。输入选择本身未证明整体省 token。详见 [C3/A4](../../../output/token-repair-20260924-context4/comparison/findings.zh.md)。

五组策略均完成 DbUtils 8/8 要求和 523 测试。P/B/N/D/E 总 token 分别为 139,842 / 138,206 / 158,092 / 104,039 / 86,918。D 的真实 tool schemas 不变，首轮输入减少 532 token；E 的实际 controller Test gate 已观测，并通过冻结源码的新进程只读重放。选择 D+E 进一步组合验证，不从 B 的 -1.2% 或 N 的 +13.1% 推导应切换触发策略。[策略数据与限制](../../../output/token-repair-20260924-strategies-v3/comparison/findings.zh.md)。

组合 F 于 2026-09-24 07:11:54 UTC 启动，顺序为 DbUtils、DBCP。source 保持 `ad1302c0242bf20eb78922ddd9908268b0b13c27466cf3dcbee33eba30e4c643`，保留 mini-high phase-entry Advisor；只启用提示精简和合格测试交接。必须验证 DBCP 不会因已有成功测试而升级其 Javadoc 未完成的全任务结论。[组合决策](../../../output/token-repair-20260924-combined/strategy-review.json)。

F 的 DbUtils 外部验收与原生 requirements 均 complete（8/8、523 测试），107,180 token；但复核原生 sealed verdict 发现 partial、tests unavailable。E 也有相同问题：自动 close 发出了正确 gate event，却未调用既有 `_record_gate_facts`，finalizer 因而只看到无权用作 headline 的 tool observations。原先将 requirements complete 简写为“原生 complete”不够准确，已在策略报告更正；E/F 不满足工程准入，原始记录不追认。

F2 在 `output/token-repair-20260924-combined-v2` 封存：仅增加该事实写入，所有任务、模型与参数保持不变，source `740b5821f8f6eea947663ddac103f2aa0fc919d4916198e23b7c58d6645ba56d`。新增 sealed snapshot 的 receipt scope、测试计数、judgment 断言先复现失败，再通过 171 项相关检查（1 项跳过）。正式扩展前必须同时核对 SAG sealed verdict、原生要求表和外部验收，不能只核对后两者。

F2 已通过：DbUtils sealed success、原生与外部要求 complete（8/8、523 测试），102,407 token、392.49 秒；DBCP sealed partial、原生与外部 incomplete（22/23，Javadoc），1,596 通过/9 跳过，286,675 token、983.51 秒。后者没有自动 Test close，且比 F 多 83,904 token，97.3% 的增长来自 actor，多了五次请求；本例未执行 F2 唯一变动的代码分支，不能把波动直接归因于该事实写入。仅据正确性准入与正常路径机制收益扩展收集，不宣称总体节省。[F2 结果](../../../output/token-repair-20260924-combined-v2/comparison/findings.zh.md)。

十五项已于 2026-09-24 08:03:28 UTC 串行启动：[冻结计划](../../../output/sag-fifteen-post-repair-20260924/campaign/plan.json)。source 与 F2 相同，mini actor / mini-high phase-entry advisor，relevant context + 提示精简 + 合格测试交接，仅 SAG。旧 CSV pilot 的 Java 21 与正式 CI Java 17 不同，不计算配对 token 优势；不新增 OpenCode/Claude 请求。

仍未完成：15 项 SAG 运行及归因、最终默认配置决策。完整进程恢复不在本次 fresh-run 数据中得到验证；报告已经确定性导出，运行时路径提示仍遵循各 harness 共同的既有实验协议，暂不改变基线比较接口。B 的健康路径未覆盖旧 when-stuck redirect，该候选保持默认不启用。已有失败、配置未生效的运行及全部请求成本保留。

### 10.1 十五项批次中的测量修订

扩展时发现三项外部记录问题：空 CI URL 导致本地成绩导出异常、异常前尚未保存已发生用量、同一 JAR 的多个声明角色被路径键覆盖。修复后通过 195 项相关检查，形成 v2。随后 Maven 重建文件保留字节和 mtime，portable collector 丢弃了新的 POM/site descriptor；增加与原生 observer 一致的 device/inode 及复制一致性检查，197 项相关检查和 2 项原生边界检查通过。真实小型 Maven 的无模型对照复现了旧三字段指纹漏采，形成 v3。

CLI/CSV 的有效 v2 记录保留，v3 预先指定 JCS/Net 复验及尚未执行的 11 项；Actor、Advisor、任务与请求配置不变。三次测量历史尝试的 343,011 token 单列保留，不从成本中消失。v3 的 Net 已完整通过 18/18；JCS 改善为 83/84，仍缺根模块安装后的 site descriptor 证据，不能追认为 84/84。后续按固定顺序继续，仅 SAG，无新 baseline。当前结果、逐行源码哈希与原始缺口见 [批次说明](../../../output/sag-fifteen-post-repair-20260924/README.md)。

另外，旧的 CI 准入标记未通过当前独立 provenance 核验，离线审计发现嵌套原件导出和 transport 支持问题。本批本地固定任务记录可用于修复回归；正式 CI 等价性、逐用例相同性和跨 agent 成本优势仍不可据此宣称。[CI 档案审计](../../../output/sag-fifteen-post-repair-20260924/ci-archive-audit/findings.zh.md) 保留了可恢复与仍不足的证据。

### 10.2 Wrapper 与多步骤验收接口（首次 v4 请求前完成）

RAT 的模型执行记录为 sealed physical success（1,146 通过、3 跳过），但原生固定任务表因相对 Wrapper 与同一路径的绝对写法不匹配而 unavailable，外部验收因多余 `maven_bin` hint 报错、未执行。控制器按规则暂停，原始 89,803 token 与两个 unavailable 保留为指定主记录，不重跑模型。

v4 修复三项测量接口：按冻结 launcher 筛选并记录可选路径提示；recorder CLI 返回实际命令退出码，使多步骤任务成功后继续、失败后停止；原生适配器仅在 cwd 解析后的绝对路径与 receipt 的 toolchain executable 一致时接受相对 Wrapper 写法。模型、提示、任务、验收要求和版本核验不变。前两项的相关集合 167 passed，路径与授权证据集合 92 passed / 1 skipped；集合有重叠，不相加。真实 CLI 子进程覆盖了正确和错误 JDK、按序两个成功步骤以及首步失败停止。RAT 原始 receipt 的路径子检查也离线复现修复前拒绝、修复后匹配，但未反向重新评分原任务。

首次 v4 模型请求于 2026-09-24 10:27:14 UTC 开始，source `bf17b6fa90db21798a77d0ce953f8c614ae5c1577c09b764f79848292e082add`，仅运行尚未开始的十项。Tentacles 已作为实际路径验证通过：原生/外部 11/11、2 个测试通过；同样出现的多余 Maven hint 被记录并忽略。其他已关闭结果与前三次测量历史均保留，[固定映射](../../../output/sag-fifteen-post-repair-20260924/primary-cohort.json)逐行披露源码版本。

JCS 安装产物仍留有严格的证据缺口。无模型真实 Maven 对照发现，成功 `clean install` 可保持已安装 site descriptor 的字节、mtime、inode、device、ctime 全部不变；这反驳“安装成功必定产生新指纹”的必要条件。后续应独立设计同一 invocation 的 native install 完成、声明坐标/路径、源产物与安装产物字节绑定的连续性证明，并保存未变更文件的观测。本批缺失原件不可补造，也不因此追认成功。详见[原始对照](../../../output/sag-fifteen-post-repair-20260924/recorder-repair/installed-site-probe/diagnosis.json)。
