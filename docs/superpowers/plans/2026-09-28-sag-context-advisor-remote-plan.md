# SAG 下一阶段修改、实验与消融计划

日期：2026-09-28。执行地点：`chenhao@100.81.188.85:/Users/chenhao/projects/Setup-Agent`。

**2026-10-02 更新：**M0/M1/M4 和 E1 六次运行已完成并归因。后续执行转入[工具修复和十项目消融计划](2026-10-02-sag-ten-project-code-mode-ablation.md)，其中加入 code mode，并替代下文原 E2/E3 的继续方式。下文状态和清单保留为 9 月 28 日的原计划记录。

**状态：计划已整理；本文件中的新修改与新实验尚未执行。** 本次交付只保存计划、核对依据并同步远端。迁移验收已经完成，不能把迁移中的离线回放算成新 Agent 实验。

## 1. 目标和当前判断

SAG 是面向 build/test 的 harness。目标是让较小、较弱的模型在无人反馈下完成规定的软件 setup，保持结果可信，同时降低完整任务的 token 和时间成本。保留 phase 状态机、物理验证以及独立公共验收器。

当前最高优先级是修复跨 phase 的信息交接。现有实现把阶段切换与 Actor 窗口重建绑定，随后依赖有损的阶段摘要和证据投影。Advisor 获得的上下文又比 Actor 更明确，因此已有 Advisor 消融同时测到了“咨询”与“上下文组织”的作用。

先修这个基础问题，再判断 Advisor 的净价值。后续依次处理失败诊断入口、证据新旧状态、评价器兼容性和其他可选优化。每项改变单独留版本与对照，不一次开启所有优化。

### 1.1 已确认的证据

| 观察 | 能支持的结论 | 不能支持的结论 |
|---|---|---|
| CSV off 从 Analyze 到 Build 后，provider 请求只剩 system/user；上阶段的 POM 工具返回不在新窗口 | phase 切换确实移出了原始对话 | 所有额外 token 都由窗口重建造成 |
| `PhaseMachine.digest_lines()` 对阶段结果使用 `summary[:200]`；CSV 的 defaultGoal 和编译配置未进入下一窗口 | 交接丢失了已获得的相关内容 | 把长度无限调大就一定节省 token |
| Advisor 的 digest 额外包含咨询时读取的当前工具链状态；CSV 的建议是环境已齐、直接执行缺少记录的规定命令 | 当前事实呈现不对称，建议提供了明确下一步 | Advisor 拥有完整跨阶段记忆，或证明了更强的故障诊断能力 |
| 五个 off 运行有 61 次 Actor 请求；56 次仍有 Advisor schema，61 次仍有相关指导；实际咨询和尝试调用均为 0 | off 接口不够干净，需单独处理 | 存在 Advisor 拒绝/重试造成的循环 |
| 四短项目 brief/off 都完成 4/4；Actor 请求分别 38/49，总 token 388,938/491,021 | 存在值得追查的额外调用和输入成本 | 单次配对证明 Advisor 因果性节省 102,083 token |
| brief/off 的 Actor 增量为 136,191，其中命令成功返回之后增加 79,058（58.0%） | 收尾和证据复用值得优先研究 | 成功后所有核查都是浪费 |
| 进入 Test 的 CSV off 已有 exit 0、974 passed、11 skipped 和 receipt 路径 | Test 阶段额外读取不能全部解释成遗忘 | 看见聚合计数便可绕过 scope/JVM/完整性验证 |
| RocketMQ demand/adaptive 的 Build/Test complete，35 个质量要求缺少匹配事件 | Maven 插件身份兼容性是测量缺口 | 35 个真实构建失败，或可以直接追认全任务成功 |

四短项目只做过每单元一次；Analyze 阶段的行为在第一次 Advisor 咨询前就已经分叉。已有数据用于提出假设和选择小规模试验，不用来直接更改全局默认。

证据入口：

- [五项目结果与口径](../../../output/advisor-five-ablation-20260926/report/findings.md)
- [RocketMQ 逐轨迹归因](../../../output/advisor-five-ablation-20260926/report/rocketmq-deep-review.md)
- [逐请求及用量](../../../output/advisor-five-ablation-20260926/analysis/requests.json)、[阶段成本](../../../output/advisor-five-ablation-20260926/analysis/phase-costs.json)
- [本计划的代码与原始请求证据清单](../../../output/remote-next-steps-20260928/evidence.json)
- [迁移验收](../../../output/remote-migration-20260927/migration-result.json)：116 万文件、31 个工作树、13 个镜像标签核验；32 次回放一致；38 项关键测试及 5 项迁移镜像检查通过。

## 2. 不变的研究与实现边界

1. Phase 负责进度和证据要求；上下文管理负责让模型保有下一步所需信息。阶段切换不代表原事实失效。
2. 最终成功仍由固定 task/requirements、实际执行和独立物理证据决定。Advisor/Actor 总结、工具调用成功、单纯 exit 0 都不能替代任务验收。
3. 公共验收器只读 Agent-origin 证据，不安装工具、不运行遗漏命令、不补做测试、不替 Agent 等待后台任务。
4. 保留原命令、源码提交、模块范围、JDK/Maven 约束及工作树规则。缺失证据判 unavailable；不能用删要求、跳测试、修改被禁源码来提高成功率。
5. Javadoc 失败不要求必须修复到通过，但若冻结任务要求它，则如实保留该要求的失败和 whole-task incomplete；不能把 build/test 的完成改写成全任务完成。
6. 现有 dirty 修改不清理、不 stash、不隐式合并或提交。`main` HEAD 只是起点，不足以描述当前实际源码；运行必须另封存完整源码文件 hash、配置和工具 schema。
7. 新评分器、诊断变体、新主机结果单独版本化；不覆盖旧实验主表和原始证据。
8. 近期继续使用 `gpt-5.4-mini`；Advisor 为同模型 high，Actor 继承已封存实际参数。API、reasoning、输出上限均逐请求披露。暂不使用 Terra，也不把 API mini 结果当成本地模型性能证明。

## 3. 修改清单与实现顺序

### M0：冻结起点，补足行为观测（首先做）

复用既有 provider 网关、context journal、receipt 和 phase/control events。增加一份可复算的阶段交接审计：进入阶段前后的消息角色、保留/省略的证据引用、原文范围与 hash、上下文预算、当前工具链来源、后续 Actor 工具动作。不增加摘要模型调用。

冻结远端源码状态、输入文件、模型参数、镜像内容身份和评估器版本。原实验镜像源配置摘要为 `sha256:a2d28950852af85d9b64d0e4ee015d989e88e83d9beb71c8ac5a166c41b8e1cc`；远端 Docker 显示 manifest 摘要，按迁移的配置/manifest/文件层绑定表解析，不能把源配置 ID 直接当成远端唯一可用名字。

验收：每个 provider 请求均可归属 Actor/Advisor/其他模型处理；一次响应的多工具调用不重复累计 token；输入载荷可重建。遥测采集不改变模型可见内容。

### M1：修复 Actor 的跨阶段交接（最高优先级）

落点：`src/sag/agent/react_engine.py` 的 `_apply_phase_decision()`、`_phase_intro_step()`，`phase_machine.py` 的 `digest_lines()`，以及 `phase_handoff.py` 的 `project_for()`。复用既有数据结构，避免新造一个通用记忆服务。

第一候选是“结构化事实 + 有界的相关原文保留”：

| 优先级 | 带入下一阶段的内容 | 来源与限制 |
|---|---|---|
| 必需 | 原任务、准确命令/cwd、版本要求、源码及工作树约束、未完成步骤 | 来自冻结输入；Actor 不能改写验收要求 |
| 必需 | pending/terminal、未解决失败、证据冲突、最新适用 receipt、缺失范围 | 来自现有证据状态；限定 run、命令、目录、scope 和输入状态 |
| 必需 | 要求的工具链、已登记状态、最近实际观测与执行使用的工具链 | 分开标注来源。env overlay 的登记不能替代 dispatch_probe |
| 相关原文 | 当前任务依赖的配置/CI/文档片段，例如 defaultGoal、profiles、测试选择和编码 | 保留实际读取内容、路径/ref、行范围/hash；不只保留 pattern/matched |
| 辅助 | Actor 的阶段结论、短计划、尚未验证的假设 | 明确为 claim；不能升级成 verified fact |
| 低优先级 | 已解决的旧错误、重复读取元数据、长日志主体 | 完整归档可检索；显示省略与读取入口 |

具体要求：

- 取消把 `summary[:200]` 当成关键事实的唯一交接方式。按完整条目/字段保留；正文无法全部带入时提供可读取的完整原文入口和缺失声明。
- Actor 与 Advisor 从同一组当前事实构建基础摘要，尤其是工具链状态、原任务进度和最新 receipt。Advisor 可有不同角色指令，但不能独占 Actor 做下一步需要的既有事实。
- Phase 切换仍发布新目标，同时保留与目标有关的近期工具原文。以带来源的引用块传递；如果保留 native tool 消息，必须保留合法的 tool-call/result 配对，不能重发历史动作。
- 成功的 Maven 生命周期可以同时提供 build/test 证据。跨阶段复用遵守既有当前性和完整性校验；若代码、命令范围、JVM 或输入发生影响证据的变化，旧证据不能继续当成当前完成证明。
- “必需”指语义优先级，不表示无限复制日志。沿用已配置的窗口和预算，预留 system、schema、输出空间；先去重和截取低优先级原文。超限时复用现有压缩/分段路径，不能静默丢掉要求或因装不下就跳过咨询。
- 常规交接不调用另一个模型写摘要。若未来引入模型压缩，单独消融并计入全部成本；不能预设一次性摘要会节省 token。
- `phase.note` / `phase.done` 的 Actor 说明可以帮助选择材料，但错误说明不得覆盖实际观察；成功路径不增加强制写总结的额外回合。
- 先保留所有现有 FSM、gate、evidence-close 和 verifier 行为。`reuse_verified_test_phase` 暂不打开。

必要验证：CSV defaultGoal 及 compiler 信息跨阶段保留；Maven 要求与实测不混用；Tomcat 在 Analyze 执行的有效命令可被后续阶段读取；pending 被适用的终态 supersede；旧失败/冲突不丢失；来源变化后证据失效；非法 ref 不提升可信度；窗口不足有完整读取入口；动作/计费/phase 计数不因 carryover 重复。

### M2：把 Advisor off 的接口做干净，但单独测量

落点：`agent.py` 的工具注册、`react_prompt_builder.py`、`config/prompts/react_engine.yaml`、`react_engine.py` 的咨询/触发入口。

产品态 off 应不注册可调用 Advisor schema、不注入主动咨询指令、不产生强制咨询或 disabled 循环。故障关闭时也应留下明确审计信息。保留历史 off 作为实验基线的能力，但标名“旧接口 off”，不与最终产品 off 混称。

**主交接消融先维持旧 off 接口不变。** 否则同时缩短 schema、移除指导、改变交接，就无法解释 token 差异。清理接口在后续独立配对中加入；通过确定性测试不能替代其真实 token 效果测量。

### M3：修正工具输出、receipt 和 Advisor 证据目录

落点：`tools/internal/maven_tool.py` 的错误处理，`agent/invocation_receipts.py`、`react_engine.py` 的 runner 摘要与 Advisor 来源组装、`advisor_review.py`。

- 失败首屏优先展示与本次失败绑定的模块、test identity、异常和 stack。其他测试打印的异常单列为背景输出，不能自动说成最终失败根因。
- 全输出默认保留，给 Actor 可用路径/ref 及搜索/行范围读取方式。`job:`、invocation receipt、output ref 各有清晰类型和合法示例；不恢复未经当前工具契约复核的旧 suggestions。
- 运行中 class/JAR/test 数标为“截至时间 T 的部分观察”；结束后指向实际终态 inventory 和 receipt。不能用“已经看到 class”显示为全部 build 成功。
- Advisor 的来源目录声明时间、pending/terminal 和 supersedes 关系；默认优先最新适用终态。原 pending 仍保留为历史，避免再次把 900 秒快照当最终状态。
- 报告明确主/测试 class、主/辅助 JAR、reported/ran/passed/skipped、build/test 适用模块分母。各字段无法测得时保持 unavailable。

先用 RocketMQ 两类归档失败、成功后终态、旧 pending 和部分构建夹具验证。此项会改变模型输入，完成后单独对照，不混入 M1 的首轮主实验。

### M4：修复 Maven 插件身份的测量兼容性

落点：`src/sag/benchmark/native_evidence.py::maven_events()`、`evaluator.py::native_requirement()` 及关联 Maven observation/requirements 元数据。

把 Maven 3.8.7 中的完整 artifact 名和 3.9.9 中的前缀表达绑定到已声明的插件身份、goal、execution/module。以有效 POM、完整命令和观测记录支持匹配；不靠全局去掉 `-maven-plugin` 或模糊字符串替换。

测试必须包含：两种日志样式、错误插件坐标、同名 goal、跨模块错绑、插件启动但没有完成、skip、日志截断、失败后的 not_run、`--continue`/`-fae` 等不同执行语义。

验收：能说明 RocketMQ 那 35 项到底是哪一条证据满足或仍缺失，负例不能变绿。用新 evaluator version 在全部相关 harness 档案上生成**派生复算表**，旧 primary 评分不覆盖。此离线工作可在模型实验前完成，不增加模型 token；首轮各处理组使用同一冻结评估器。

### M5：重新评估 Advisor 的触发与读取价值

保留已实现的 `brief`、`on-demand`、`adaptive` 作为候选，先不改全局默认。

- Actor 可以主动给出具体问题和短假设；Advisor 将其与实际证据核对。
- 自动触发仅使用已有可观测事件，例如相同失败条件下的重复无进展、证据冲突或显式求助。不得把“时间久”或第一次正常失败直接等同 struggle。
- 同一命令/环境/证据状态的触发去重；不能不断重定向 Actor 或取消它本来要执行的动作。未触发样本只能说明未使用，不能用来声称恢复策略有效。
- 按需读取仅面向本次咨询冻结的归档 allowlist，保留 ref/hash/读取预算。它不能执行 shell、改文件、运行测试或认证成功；缺少新源码/测量时请 Actor 取得。
- 首包提供已知状态与明确问题；后续读取必须补具体缺口。持续重复确认同一已知事实属于应计量的开销。
- 在相同上下文预算、相同模型、相同触发点上比较 brief 和 on-demand；另比较有/无 Actor 说明及错误说明。窗口大小本身不作为填满材料的理由。
- 检查每条关键建议后 Actor 是否执行了相应测量/修复/重跑，以及实际结果；文本更详细不等于修复成功。

### M6：有证据的诊断与退出

对现有提示做有界改动：在把失败归因为项目缺陷、环境无关或决定退出前，区分已知失败条件、已测环境、未排除假设，并选择一个能区分假设的最小读操作/测量。已有证据足够时允许诚实退出；不要求无限重试，不强迫写无法执行的修复。

诊断命令和规定验收命令分开记录。单用例成功不冒充完整 CI 任务成功；修复后需要重新获得规定任务的终态证据。禁止修改源码的任务不因 Advisor 建议而放开限制。

### M7：其余 token/时间优化（交接稳定后）

| 候选 | 单独改变什么 | 不变项/验证重点 |
|---|---|---|
| `compact_setup_prompt` | 移除重复或与 build/test 无关的 prompt 内容 | 保留工具参数、失败语义、分页/引用示例；不能仅以字符变短判收益 |
| `reuse_verified_test_phase` | 已有当前 build/test 证据时减少重复的阶段动作 | 阶段状态和物理验证仍成立；缺失/过期/错误 JVM 不允许自动通过 |
| `export_runtime_handoff` | 把已经观察到的工具路径交付外部调用者 | 这是运行环境交付，不是 M1 的 Actor 记忆；不安装工具、不替公共验收执行命令 |
| 内部 I/O 合并/复用 | 有执行 ID 和计时后处理重复 env/目录/状态读取 | 基于证据版本失效，不能缓存动态终态；先测耗时再改，不按调用次数猜节省 |

各开关当前五项目基线均为 false。先分别比较，再对已验证有效的组合做确认，不把多个开关同时打开后的改进归给某一项。

## 4. 实验顺序和每一步的决策

### E0：本地确定性与离线回放，不调用模型

先执行 M0、M1 的回归与原始请求重建，再完成 M4 的离线复算。保存修改前后 provider 可见文本，明确哪些信息新增、保留或删去。取真实失败、正常成功、旧 pending、错误版本、缺 scope 和伪造 Actor 结论作负例。

现有回归入口：`tests/test_phase_machine.py`、`test_phase_handoff.py`、`test_phase_handoff_test_stats.py`、`test_advisor_engine_flow.py`、`test_advisor_guarantees.py`、`test_native_loop_engine.py`、`test_benchmark_execution_origin.py`、`test_benchmark_requirement_edges.py`、`test_requirement_started_observation.py`。新行为增加对应测试；基线已有失败需在修改前快照复现并披露，新失败阻止进入模型试验。

### E1：首先跑两项目、三组，共 6 个首次尝试

选 Commons CSV 和 Commons Net。两者均为较短的单模块 Maven 项目，分别覆盖 defaultGoal 的复合要求和 install 产物；已观察到配置重读/receipt 重读，可直接检验当前机制。选择依据是失败机制覆盖，不是选择最有利于 SAG 的结果。

| 组 | Actor 交接 | Advisor | 其他行为 | 可回答的问题 |
|---|---|---|---|---|
| A | 冻结旧交接 | off，保留旧接口 | 与旧 off 相同 | 远端上的同期基线 |
| B | M1 修复后的交接 | off，保持与 A 相同接口 | 只改变交接 | 交接修复的整体净效果 |
| C | 冻结旧交接 | brief + phase-entry | 与旧 brief 相同 | 已观察的 Advisor 路径能否复现 |

M1 同时包含关键事实投影和有界原文保留；B-A 估计的是这套交接修改的效果，不能声称单独量化了某个子机制。第一轮不同时引入 M2、M3、M6、M7。

准确复用的任务输入位于 `output/advisor-five-ablation-20260926/runs/<project>-off/inputs/`，配置位于同一运行的 `effective-sag-config.json`。要求文件字节 hash 和语义身份都核对；后者不能代替文件 hash。不要改用名字相近的较早 bench。

| 项目 | Pinned commit | 原规定命令 | 环境 |
|---|---|---|---|
| Commons CSV | `bb0f0fbb0f84de5cf5a0c73a9fa7051fb5135650` | `mvn -Ddoclint=all --show-version --batch-mode --no-transfer-progress` | Java 17，Maven 3.9.16 |
| Commons Net | `ac1ca59667b945ec95b54e503c7b4415fd5e0276` | `mvn -B -f pom.xml -V clean install --no-transfer-progress --batch-mode` | Java 17，Maven 3.9.11 |

CSV 的 defaultGoal 由 pinned POM 定义；不能补写成另一条较弱命令。Net 当前冻结任务已准入 CI 数量比较；CSV 官方引用已核实但冻结任务未准入 CI 计分，继续分开披露。

第一轮 6 次只验证可运行性、采集完整性与明显回归。若有采集错误/新 false success，先停后续批次并修复；不根据一次 token 优势选最终方案。若需要修改候选代码，原尝试保留，新代码作为新版本重新开始完整配对，不与旧版本拼凑重复次数。

### E2：保持代码不变，完成三个轮转区组

E1 无需修改即可进入后续时，再补两轮，累计 `2 项目 × 3 组 × 3 轮 = 18` 次。三轮使每组各处于一次第一/第二/第三执行位置，属于最小顺序平衡设计，不是统计功效充分性的保证。

- CSV：ABC、BCA、CAB；Net：CBA、BAC、ACB。
- 各次均独立冷容器，无共享项目依赖缓存；Docker 镜像层缓存、网络和 provider 缓存如实披露。
- 先逐项目逐轮展示完成状态、token 和时间，再汇总中位数及范围。只有两个项目，不能将请求数当独立实验样本，也不声称普遍统计显著。

如果 B 降低重复读取却增大整体输入成本，进入一个预先登记的组件对照：B 的“事实投影”单独开启，与“事实投影 + 原文保留”比较。先用归档校验信息保留，再决定是否值得新 live run。不要靠盲目增加窗口或固定多带 N 条消息来调优。

### E3：补齐 Advisor 与上下文的交互，再清理 off

需要解释 Advisor 净价值时，加入 D＝修复交接 + brief。在同一冻结版本下补与 A/B/C 匹配的项目和区组，形成 2×2：旧/新交接 × off/brief。已有 A/B/C 只有在源码、评估器、任务、资源和采集方式未变时才能复用；否则开新 campaign。

核心比较：B-A 是无 Advisor 下的交接效果；D-B 是修复交接之后 Advisor 的增量；C-A 是旧交接下的 Advisor 增量。二者差异可以检查 Advisor 是否主要在补偿交接不足，但样本范围仍需披露。

随后才比较修复交接的旧接口 off 与 M2 的干净 off。若两组均没有咨询，分别报告 schema/input 缩减和行为差异，不能把“无调用”说成 Advisor 生效。产品最终 off 必须通过不暴露可调用 Advisor 的契约测试。

### E4：短任务外部确认与故障诊断消融

短任务增加 Tomcat Migration、Commons DbUtils，覆盖已观察的 Analyze 提前执行和纯 test 生命周期。候选方案在这两项上不做项目专属调参。需要同机 harness 参照时，仅跑最终 SAG 候选与 Claude Code，沿用同一 mini；OpenCode 暂不加入这轮。原电脑的 Claude 时间只能作历史背景，不能当远端同期基线。

跨 harness 比较继续披露源码 checkout 由谁完成、工具权限、API 类型、reasoning effort、输出上限和 locale。相同模型名称不等于完全相同的推理配置；如要等化这些条件，另冻结新协议，不在运行后补改。测试任务本身要求的环境与各 harness 自行建立的环境分开记录。

失败诊断先使用已封存的 RocketMQ 两类异常和一份成功终态对照，分别比较：

1. 同一输入下现有与 M6 诊断/退出提示；
2. 同一触发和输入预算下 brief 与 on-demand；
3. 同一事实下不带 Actor 说明、正确说明、错误说明，检查建议是否被错误假设带偏。

这些是小规模归档模型咨询，不运行构建，单独计算请求/token。先冻结场景、模型可见材料和判断规则，再调用；标注预期检查的证据点，不能通过观察回复倒推“正确答案”。建议质量看是否定位证据缺口、选择可区分假设的测量、遵守任务约束，而不是回复长度或自称信心。

现有 adaptive 的零咨询结果不够验证触发策略；用确定性重放覆盖真实无进展、单次失败、状态已变化和重复事件，再进行有针对性的 live 验证。没有证据证明需要定期咨询时，不新增固定周期调用。

只有出现可执行、可证伪的诊断方案后，才考虑一次受控 RocketMQ 复现；先单用例/模块诊断，最终恢复结论仍需要规定完整命令。原有 tracked-source 禁改约束不放松。失败不稳定时先报告复现率，不把一次 passing retry 当根因证明。长任务暂不列入第一批必跑项。

### E5：扩展到 10/20 项与论文实验

前述候选通过后，先用固定清单扩大 SAG 验证，再按论文比较问题决定哪些项目需要同机 Claude/OpenCode 对照。若只研究 SAG 新版本在原任务上的表现，已存在其他 harness 的结果保留即可；若主张同机时间/资源优势，则必须补同机基线，不能与旧电脑耗时直接相减。

扩展项按已冻结的构建要求类别、项目大小、模块数量、Maven/Gradle 和 CI 证据等级分层；所有排除写原因，不能因慢/失败而在运行后从分母删除。先在 5–10 项检查全量数据采集，再扩到 20；大项目逐个运行。约 100 项数据集建设是后续独立工作，不与本轮交接优化混成一次成功率报告。

## 5. 必须收集的指标与解释规则

| 类别 | 每项目、每组、每次至少保存 |
|---|---|
| 任务结果 | whole-task complete/incomplete/unavailable；build/test/docs/quality 分别状态；每项 passed/failed/not_run/unavailable 和来源 |
| Build 实物 | 适用 build 模块分母及通过模块；主/测试 class；主 JAR、辅助 JAR/其他要求产物；文件清单/hash及 scope |
| Test | reported、ran、passed、failed、errors、skipped；raw/unique 粒度、重复/重试规则、case identity 可用性；适用 test 模块分母 |
| CI 对照 | source SHA、job/task/JVM 绑定和准入状态；同一范围下通过要求比例、ran/passed 数量比；无准入或缺分母为 unavailable |
| 模型成本 | provider 逐请求 input+output；Actor/Advisor/压缩分别；cached input 和 reasoning output 各只计一次；重试/失败请求的已知成本与缺失项 |
| 行为 | Actor/provider 请求数与工具调用数分开；phase 数；咨询触发/读证据轮次；实际重复命令；建议→行动→观测→修复结果 |
| 交接效果 | 固定案例的关键事实保留/缺失；丢失后重新读取；同文件/hash/范围且无新问题的重复读取；来源/输入变化后的合理复查另列 |
| 时间与资源 | 原始开始/结束时间、无人反馈 Agent 窗口、准备时间、等待时间、封存耗时；容器峰值 RAM、CPU、宿主/Docker 磁盘、OOM/timeout |
| 自主性 | 执行窗口的人类介入 ledger，明确记录零；实验之间的工程修复单列 |

明确的公式和边界：

- 项目成功率＝满足全部冻结要求的项目尝试数 / 全部预定且实际执行的项目尝试数；未开始项另报，失败、超时和证据 unavailable 不能从分母消失。
- Build/Test 成功率分别按各自适用要求计算；某阶段不适用不能伪造为成功。要求完成比例同时列 passed/required 和 unavailable 数，不能掩盖证据完整性。
- `ran = reported - skipped` 只在该 runner 的计数定义及报告完整性已验证时使用；保留失败和 error。相同总数不证明逐用例相同。
- 总 token＝全部 provider 请求的 input+output。缓存和 reasoning 是子集；不重复相加。不能以工具 turn_record 重复累计一次多工具响应。
- 对比同时给绝对数、差值与注明分母的百分比。先报告完成结果，再解释成本；早停失败的低 token 不是同等效果下高效率。
- 至少把成本按 provision/analyze/build/test/report、以及“规定命令成功返回前/后”两种切法对齐。成功后的核对成本单列，但不预先判为可删除开销。
- 原始总 token 不等于 API 费用，也不等于本地模型时间；本轮不凭缓存比例推断美元优势。

## 6. 远端执行与资源约束

主机 arm64 / 24 GiB；Docker 约 12 GB 内存，实验镜像 linux/amd64。该异构执行环境单独披露。每次 4 CPU、8 GiB、无 swap、7200 秒、150 轮上限，继承既有协议；**并发固定为 1**，不照搬原主机的双并行设置。

每 15 秒采集 RAM/disk，沿用 32 GiB 宿主与 Docker 磁盘保留线；这些是原协议操作边界，不是为了当前结果调出来的科学阈值。启动前还需满足“可用空间覆盖预计归档增长 + 保留线”，增长估计来自归档体积，不凭空给大项目统一配额。资源不足先等或清理本批已封存的停止容器；运行途中出现压力保留记录并安全停止，不能偷偷更改内存或超时后仍当原组。

不得全局 prune。仅在本次运行完整归档、hash 校验、离线复算通过之后删除该运行拥有的已停止容器。模型请求、trace 和最终 receipt 不随容器清理丢失。

开始模型调用前冻结：项目清单/顺序、源码及配置、task/requirements/CI hash、镜像配置与 manifest、评估器/观察器版本、模型/API/effort、环境权限与 locale、冷缓存策略、资源、停止规则、重复次数和 provider 数据范围。复用已批准的发送范围；若准备发送的新材料超出既有具体批准范围，再单独说明并确认。

新 live run 的 Agent 只访问自己的任务和当前工作区，不能把旧尝试、其他 Agent 输出或评测答案挂进去。E4 的归档诊断是明确标记的另一类实验，不能与冷启动任务成功率合并。

正式运行始终传入对应 `--acceptance-task-file` 和 `--ci-target-file`，并保存有效配置。新交接策略的实验开关尚未实现，本文 A/B/C/D 是设计标签，不是可直接使用的 CLI 值；完成 M1 后才生成并校验启动命令。

暂停下一批的条件：源代码/输入 hash 漂移，采集丢字节或计费不守恒，verifier 新 false positive，超预算未被正确记录，磁盘越界或 OOM 风险，模型/API 与冻结配置不一致。正常项目失败仍计入样本，不能为了保持绿色而停止统计。

## 7. 如何决定设计

先看可信度和任务完成，再看整体成本：

1. 任一新 false success、scope 放宽、过期证据复用或缺失原始结果，候选不能成为默认。
2. 首轮只定位机制；三个轮转区组后看方向是否一致、波动是否足以解释差异、关键事实是否保留，不能只选最低 token 的一次。
3. 如果修复交接的 off 已能直接执行、及时收尾，说明这类顺利任务不必依赖 Advisor 补基础事实；不能外推失败诊断也无用。
4. 只有在修复交接后，Advisor 仍改善完成/恢复结果或在相同可信度下减少总成本，才支持它的增量价值。暂时无明显收益时保持可选，避免把固定 phase 咨询当必需步骤。
5. 更大上下文减少读取却增加总 token 时，按已测结果选择事实投影/原文保留边界，不能把“知道更多”直接等同“效率更高”。
6. 最后在未用于调提示的项目上确认。所有阈值、选择理由和版本变化写进 decision record；不使用事后挑选成功样本的结论。

## 8. 论文 RQ 草案

- **RQ1 — Autonomous setup effectiveness.** 在固定 CI 任务、不同项目规模与要求类别上，SAG 能否让较小模型自主完成 build/test 及必要附加要求？对照其他 harness，同时报告局部阶段成功、全任务成功和可信证据覆盖。
- **RQ2 — Contribution of harness mechanisms.** 跨阶段信息保留、工具/receipt 设计、phase 约束和 Advisor 分别如何影响行为、错误恢复及 false-success 风险？当前实验优先隔离上下文与 Advisor；不通过删除公共 ground truth 来做“验证器消融”。
- **RQ3 — Efficiency and CI suitability.** 在结果可信且任务完成情况可比较时，SAG 的总 token、无人反馈运行时间和资源成本如何变化？是否能以较小模型达到可用于 CI 的可靠性/成本取舍？API mini、本地模型和不同主机分别报告。

这些是 extension 的研究问题，不是现有五项目结果已经证明的结论。真正本地模型、Gradle/更大项目与跨组织数据集验证属于后续外部有效性实验。

## 9. 交付与完成清单

- [x] 保存本计划、来源索引和远端接续入口。
- [ ] M0：冻结新 campaign 起点、观测契约和执行清单。
- [ ] M1：交接修复及正/负例回归；不改 phase/verifier 权限。
- [ ] M4：评分兼容性离线修复与全组派生复算，独立版本。
- [ ] E1：CSV/Net × A/B/C，6 次短项目首轮与逐轨迹归因。
- [ ] E2：无改版时补足轮转区组，累计 18 次；或修订版本后重开完整配对。
- [ ] E3：必要时 D 组补齐交互；M2 干净 off 独立比较。
- [ ] M3/M5/M6：工具诊断、Advisor 读取/触发及退出规则，归档消融后再 live 验证。
- [ ] M7：其他 prompt/phase 复用/运行环境交付/I/O 优化逐项验证。
- [ ] E4/E5：额外短项目、必要长任务和 10/20 项扩展；再形成论文层面对比。

最终每轮交付：冻结 protocol、完整尝试清单、逐请求 usage、Actor 实际输入/输出、receipt 与输出文件、worktree/runtime/资源证据、独立复算、逐项目数值表、行为归因和设计决定。研究代码、历史 primary 数据及新派生分析各自留来源。
