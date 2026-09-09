**D3R1 修复计划：执行判定、证据归属与 CI 比较**

日期：2026-09-08；更新：2026-09-09。状态：R0–R10 已实施，R11 分层实验与逐项归因已完成，见 [R11 最终报告](/Users/chenhao/Documents/github/Setup-Agent/docs/superpowers/reports/d3r2-r11-20260909.md)。本轮仍未证明 Agent 整体任务完成能力达标，新发现的范围误判、计划/执行不一致、输出与持久化缺陷已逐项披露。

原始 23 项在冻结产品 `f8309f35`、驱动 `a598cfa9` 上各尝试一次；另保留两轮小项目对照和独立官方模型控制，共 32 次模型尝试。完整 Python suite 为 7380 passed、22 skipped；独立 HTTP 确定性正例完成 9/9 模块、2775/2775 报告结果及删证据消融。原始 23 项仍无可评分 CI 结论，缺范围不评分。

R5 结果见 [报告归属修复报告](/Users/chenhao/Documents/github/Setup-Agent/docs/superpowers/reports/d3r1-remediation-r5-20260909.md)。R4 结果见 [Maven 模块解析修复报告](/Users/chenhao/Documents/github/Setup-Agent/docs/superpowers/reports/d3r1-remediation-r4-20260908.md)。R3 结果见 [文档读取修复报告](/Users/chenhao/Documents/github/Setup-Agent/docs/superpowers/reports/d3r1-remediation-r3-20260908.md)。首批结果见 [R0–R2 修复报告](/Users/chenhao/Documents/github/Setup-Agent/docs/superpowers/reports/d3r1-remediation-r0-r2-20260908.md)。依据是已封存的 [23 项目实验与归因报告](/Users/chenhao/Documents/github/Setup-Agent/docs/superpowers/reports/d3r1-20260907.md)、[原设计](/Users/chenhao/Documents/github/Setup-Agent/docs/superpowers/specs/2026-09-07-ci-defined-build-scope-design.md) 与 [上一轮实施计划](/Users/chenhao/Documents/github/Setup-Agent/docs/superpowers/plans/2026-09-07-ci-defined-build-scope.md)。源码核查基线为 `79ae52aa2da7692eeaf63ab0b7b0a39c33ee20d4`。

目标是让 SAG 的进度能够被可靠衡量：先正确判断实际执行，再提高固定任务的完成程度，补齐可比较的官方 CI 证据，最后比较时间与成本。修复采用现有控制器、执行计划、receipt、assessment、发布与读取边界；不再增加一套工作流框架。

**已经确认的问题与修复边界**

| 基线事实 | 对修复的要求 |
| --- | --- |
| 原始结果 5 success、15 partial、3 failed；Spark 与 Kafka 两个整体虚假 success | 消除已知误报，并保留能够正确判成功的正向对照 |
| Spark、Kafka、ZooKeeper、Storm、SeaTunnel 共五个内部测试成功反例 | 进程结束、exit 0、有绿色 XML，均不能独立证明测试运行完成 |
| 53 份 receipt 的授权链和计数重建通过；完整最终报告池为 15/23 | 复用已有效的证据链；修复语义与归属，避免再造权威系统 |
| 主体/案例粒度各为 8 available、5 下界、10 unavailable；执行粒度为 12、5、6 | 明确粒度与缺失原因；不靠放大行数上限或原始 XML 数替代完整身份 |
| 14 项 CI 比较 invalid，9 项无 matched cell | 分别报告比较输入缺陷与 Agent 执行问题；不能解读为 0/23 达标 |
| `evaluate_attainment`、`view_from_certificate` 当前没有生产调用者，调用存在于测试与脚本 | 内核测试通过还不足以宣告 matrix 已接通，必须验证生产发布和读取链 |

三个尚未发现虚假成功的原始 success 是 Tomcat Jakartaee Migration、Commons CSV、Commons DBCP。它们没有冻结 matched CI cell，不能称为 CI 达标。也不能把其余项目统一降级后计算“修正成功率”。Camel 的 237 条单测实际完成；未运行所选 IT 属于生命周期/范围缺口，不加入上述五个执行状态反例。

全程遵守以下约束：

- 本地结论描述 SAG 实际做了什么；磁盘扫描保持诊断用途。CI 范围、生命周期、项目自身 red 单独披露。
- **缺范围不评分。** 删除范围、报告或授权证据，不得提高相应评分或成功结论；不得回退为构建成功即 1/1。CI 证据缺失不反过来否定已有充分证明的本地执行。
- 未执行、执行中断、执行完整但测试 red、证据不足，需要不同解释。没有报告不等于完整地执行了零个测试。
- receipt execution 维持现有包含 skipped 的计数定义，单列 skipped；它不等于真正运行过测试体的数量，修复不能暗中更换这一口径。
- 保留 receipt 大小、身份行和读取预算。超限保留下界与原因，不无限扩容、不扫描整个仓库补齐分母。
- 不写项目名特例，不新增通用依赖图、插件注册框架、约束求解器或第二个调度器。仅在已有边界无法表达实际事实时增加最小字段，并显式处理版本。
- D3R1 的报告、日志、目标及 [摘要封存](/Users/chenhao/Documents/github/Setup-Agent/logs/d3r1-20260907/artifact-seal.json) 保持不变。后续结果另存新目录；旧 receipt 的离线重判不是新一次成功运行。

**实施顺序：四个阶段、十二项任务**

| 阶段 | 任务 | 优先级 | 依赖 | 阶段交付 |
| --- | --- | --- | --- | --- |
| A：校准判断 | R0 校准集；R1 异步评估；R2 执行完成门禁 | P0 | R0 → R1 → R2 | 五个反例不再虚假成功，正向对照不被误伤 |
| B：修复具体边界 | R3 文档读取；R4 模块解析；R5 证据归属；R6 工具链；R7 计划派发；R8 报告核对 | P1，说明修正为 P2 | R3/R4/R6 依赖 R0；R5 依赖 R2/R4；R7 依赖 R1/R2/R3/R6；R8 依赖 R4/R5 | 小夹具与小项目能忠实执行并解释结果 |
| C：接通比较 | R9 官方目标；R10 生产比较与展示 | P1，旧展示修正为 P2 | R9 依赖 R4/R5/R8；R10 依赖 R2/R5/R7/R8/R9 | 至少一条有充分目标证据的真实生产比较闭环 |
| D：验证进展 | R11 分层实验与归因 | 验收 | R0–R10；小项目本地检查可随 B 提前进行 | 新旧对照、23 项逐项状态及限制 |

A 阶段可作为首批独立交付。C 阶段的官方数据可用性不阻塞 A/B 的修复验收；数据不足时，C 阶段保留未完成状态。任务表表示依赖关系，不要求并行开多个 agent。

每项实施先固定失败用例与预期，再修改指定边界，运行列出的定向测试；通过后才进入依赖它的步骤。测试文件均为现有入口，新增场景优先加入其中。下述测试清单使用统一命令，替换末尾文件列表即可：

```sh
UV_CACHE_DIR=/private/tmp/sag-ci-scope-uv-cache LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONPATH=. \
  uv run --offline --no-sync python -m pytest -q tests/test_receipt_assessor.py
```

**R0 — 固定校准集和验收口径**

代码/测试入口：[test_physical_validator.py](/Users/chenhao/Documents/github/Setup-Agent/tests/test_physical_validator.py)、[test_build_test_verdict.py](/Users/chenhao/Documents/github/Setup-Agent/tests/test_build_test_verdict.py)、[test_receipt_assessor.py](/Users/chenhao/Documents/github/Setup-Agent/tests/test_receipt_assessor.py)、[test_attainment.py](/Users/chenhao/Documents/github/Setup-Agent/tests/test_attainment.py)。预计新增 `tests/fixtures/d3r1_remediation/`，仅存脱敏且最小化的结构和日志片段。

- [x] 从封存的五个反例提取 receipt/assessment/输出/报告关系；保存原文件路径、摘要、提取规则及人工预期，不复制整个 1.9 GB 实验目录进测试。
- [x] 加入下表正反对照。预期由实际任务状态和证据确定，不从当前 `verdict` 字段反推。
- [x] 将确定性解析/语义重放与生产授权链测试分开。前者只做离线诊断；后者用测试控制器正常签发本次记录，不能给历史 JSON 手工添加 `authority_ok=true`。
- [x] 固定评估器版本与预期，随后修复不能为了通过测试而修改这些标签；若原标签确有错误，记录证据、版本和影响。

| 校准场景 | 必须得到的判断 |
| --- | --- |
| Spark：已有绿报告，但缺 yq 的相关任务失败 | 不得宣告全部测试执行完成 |
| Kafka：有大量报告，但 daemon 消失 | 保留已执行量，整体执行不完整；不臆断 OOM |
| ZooKeeper：缺 autoreconf/编译工具异常 | 不能因退出码已终止而成为 test success |
| Storm：相关模块编译失败，仅一条 skipped | 不能据该 skipped 行宣告请求任务完成 |
| SeaTunnel：缺 Docker 导致 setup 失败，ignoreFailures 后 exit 0 | 必须保留环境初始化故障，不能判执行成功 |
| 真正完成的运行，仅有项目测试 assertion red | 本地执行仍可 completed，red 如实披露；CI 结论另判 |
| 先失败，再以可核对的同等范围修复并成功 | 当前完成状态可以恢复；历史失败和修复链保留 |
| 后一次只跑更小的范围 | 不能抹掉尚未完成的原任务；单独说明实际完成的子范围 |
| 全部合法 skipped 且命令正常结束 | 可披露 runner 结束及含 skipped 的 receipt execution 总数；不得称为测试体已运行或通过，也不能满足要求非空实测的任务 |
| 授权缺失、报告缺失或读取失败 | unknown/unavailable 或已有部分事实，不能补成成功 |
| 已有本地成功，但 CI 无目标/缺模块范围 | 保留本地事实，不给缺失轴分数，不构造 met |

验收：旧代码能暴露五个已知错误及所关联的最小边界缺陷；正向对照已经能独立说明为什么应被接受。执行上述四个测试文件。

**R1 — 给异步 receipt 评估补齐当前上下文与绑定输出**

修改：[job_obligations.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/job_obligations.py) 的 `_finalize_settlement`、[evidence_assessments.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/evidence_assessments.py) 的 `ensure_receipt_assessed` / `assess_dispatch`，以及 [build_tool.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/tools/build/build_tool.py) 的既有评估调用。

- [x] 以 ZooKeeper 精确调用方式重现 `current target_sha pin is unavailable`；加入同步和异步路径对同一合法输入应产生一致评估的用例。
- [x] 从控制器现有 authority 读取当前 run/SHA/fingerprints；从已绑定作业记录读取完整输出并核对摘要。复用 `assess_dispatch` 产生主评估及 prerequisite 等附属评估。
- [x] 去掉“发现文件名包含 receipt slug 就算全部评估完成”的判断，按确切记录身份和发布状态核对所需评估。receipt 已落盘与评估已经完整是两件事；缺少输入明确暴露，允许按原有幂等规则补齐缺项。
- [x] 不把旧 contract 当作当前 pin；不覆写不可变评估，不用展示摘要或任意输出引用冒充完整绑定日志。

验收：同步/异步产生相同的主结论与前置故障；重复 settlement 不重复计数；错误 SHA、日志摘要不符、主评估存在但附属项缺失均有专门用例。运行 `tests/test_receipt_assessor.py tests/test_prerequisite_assessments.py tests/test_receipt_v2_and_assessments.py tests/test_job_obligations.py tests/test_detached_receipt_persistence.py tests/test_evidence_publications.py`。

**R2 — 修正“测试执行完成”的判定**

修改：[physical_validator.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/physical_validator.py) 的 `_test_execution_receipt_summary` 与现有 assessment 读取；必要的映射落在 [verdict_finalizer.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/verdict_finalizer.py)。

- [x] 以 R0 场景固定终止、初始化故障、项目 red、证据不足的判定表。先检查已有 typed assessment 能表达哪些故障，只为已证实缺失的类别增加最小诊断。
- [x] 用当前授权 receipt、已核对报告和相关 assessment 共同判断执行是否完成。`exit != 0` 不能一律等于环境失败；`exit == 0` 也不能覆盖 setup 故障、未完成任务或缺失证据。
- [x] 替换仅按 `(tool, action, cwd)` 选最新记录的粗粒度覆盖：复用现有 command/run/scope 与恢复链，只有能够证明兼容范围的后续执行才可收口前次义务。不能用窄范围成功消除宽范围未完成，也不能让已修复的历史故障永远阻塞当前结果。
- [x] 保留 `completed / partial / failed / unknown` 的现有表达。能证明测试未能启动时说明 failed；已启动但未完成时说明 partial；无法证明时说明 unknown。判断是否发生过执行时读取可核对的 totals，不能只看受上限约束的身份行数组是否非空。

验收：五个内部反例均不再标 success，Spark/Kafka 不再整体 success；项目自身 red 的完整执行、合法恢复、Commons CLI 正例不被误伤。不存在“全改为失败即通过”的验收方式。运行 `tests/test_physical_validator.py tests/test_build_test_verdict.py tests/test_verdict_physical_oracle.py tests/test_repair_run_scope.py tests/test_run_wide_receipt_scope.py tests/test_verdict_finalizer.py tests/test_verdict_live_authority.py`。

**R3 — 将文档数据读取与展示截断分开**

修改：[document_map.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/document_map.py) 的 `_read_head` / `read_entry_text`、[project_analyzer.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/tools/internal/project_analyzer.py) 的读取接入；复用 [runtime/container_io.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/runtime/container_io.py) 和 [evidence_records.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/evidence_records.py) 的无损传输能力。

- [x] 重现 Polaris Makefile 的中部内容被展示层删掉、截断片段却被索引的问题；夹具包含中部 recipe、尾部换行、多字节字符。
- [x] 数据路径在现有字节预算内做有边界的无损读取、验证 frame/返回状态，并对实际读取字节计算摘要。索引和后续内容读取共用这条路径；展示仍可截断。
- [x] 文件超过预算时明确说明只索引了前缀及其范围，不能标为全文；错误 frame、读取失败、源内容改变均不得伪装为成功读取。不要简单地先读完整大文件再截断。

验收：22,437 字节样例的预算内内容与原始字节一致；显示是否截断不改变源摘要。该消融只证明读取边界修复，不单独解释 Polaris 的 runner 选择。运行 `tests/test_document_map.py tests/test_document_map_live_authority.py tests/test_container_io.py`。

**R4 — 共用一处 Maven Reactor Summary 解析**

修改：[maven_tool.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/tools/internal/maven_tool.py) 与 [ci_logs.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/metrics/ci_logs.py)。预计提取一个纯函数到 `src/sag/metrics/maven_reactor.py`，只表达行、状态和摘要边界；若实施时找到等价公共函数则直接复用。

- [x] 用保留的日志片段覆盖带版本号/点号的名称、零或一个填充点、ANSI/时间戳、连续多个构建摘要，以及没有 reactor summary 的单模块构建。
- [x] 两端调用同一语法。解析层保留 SUCCESS/FAILURE/SKIPPED、摘要块及重复显示名；各消费端按自己的证据规则使用，不能将失败/跳过模块当成成功构建。
- [x] 同名显示标签未必是同一模块。能够由当前 receipt/声明证明的坐标才归一化；无法证明时披露歧义，不能 set 去重后宣称得到完整范围，也不能猜 alias。

验收：RocketMQ 19 条带点名称不再全丢；Camel Examples 的 83 条 SUCCESS 行全部识别；Camel 的 687 条状态行完整保留，其中两个重复标签明确披露。单模块 `.` 不从整个混合 job 的任意 BUILD SUCCESS 推导。运行 `tests/test_ci_logs.py tests/test_maven_gradle_tool_contracts.py tests/test_module_keys.py tests/test_d3_harvester.py`。

**R5 — 分开已执行报告归属与强制补跑资格**

修改：[forced_build_graph.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/forced_build_graph.py)、[attempt_policy.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/attempt_policy.py)、[receipt_structure.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/receipt_structure.py)、[physical_validator.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/physical_validator.py) 的候选解析接缝。

- [x] 先用两份 POM 重现“父聚合 child，child 继承父”被误判为环；分别测试真实继承环、聚合环、路径越界和深度/节点上限。
- [x] 在现有遍历中区分聚合与继承的访问状态，修正合法结构误判；不顺带实现完整 Maven/Gradle 图解释器。
- [x] 已执行证据能通过本次 SHA、cwd、receipt、报告路径/摘要和已有模块事实证明归属时，应允许核对；是否允许发起新 forced attempt 仍使用更严格的图边界。
- [x] HTTP 否定属性 profile、FreeMarker apply-from 超出声明解析能力时，只能使用独立证明的观察坐标。无法证明的身份继续 unavailable，不能靠取消校验或磁盘猜测变为完整。

验收：普通父子结构不再误报环；新任务边界没有被放宽；同一已执行报告是否可归属不取决于能否另起一次强制构建。运行 `tests/test_forced_build_graph.py tests/test_receipt_proven_structure.py tests/test_physical_validator_modules.py tests/test_receipt_bound_test_counts.py tests/test_forced_attempt_native.py`。

**R6 — 保留 Java 约束语义，并从最终 runner 生成说明**

修改：[physical_survey.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/physical_survey.py)、[project_setup_tool.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/tools/internal/project_setup_tool.py)、[java_versions.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/tools/internal/java_versions.py)、[build_preflight.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/tools/internal/build_preflight.py)；工具链说明落在 [maven_tool.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/tools/internal/maven_tool.py)。

- [x] 用最小 POM 覆盖 Enforcer 裸 `11`、`[11]`、`[11,)`、有上界范围及 compiler release 17。裸版本是 Enforcer 下界，单值方括号是精确约束；规则依据见 [Maven 官方范围说明](https://maven.apache.org/enforcer/enforcer-rules/versionRanges.html) 与 [Java 版本归一化](https://maven.apache.org/enforcer/enforcer-rules/requireJavaVersion.html)。
- [x] 保存原约束与来源，停止将其压成一个“必须相等的 major”。复用既有版本/运行时工具，选择满足已证明约束的运行时；已满足条件时不要降级或重新安装。复杂或未解析约束保留 unknown。
- [x] 同时考虑当前编译器的 release 要求；若项目明确使用独立 toolchain，分别核对运行 Maven 的 JVM 与编译器，不能简单将所有字段取最大值。相互冲突的精确要求应明确失败原因。
- [x] 最终 resolver 结束后，从实际 `runner_choice` 生成 Maven 说明并与 receipt 对齐。SeaTunnel 的旧 wrapper 不满足 `[3.9,)` 时继续允许选正确的已注册 Maven。

验收：Camel Quarkus 的已知约束组合不再把可用 Java 17 降到 11；精确/上界约束不被放松；无法安装或激活仍如实披露。不承诺因此解决该项目所有后续故障。运行 `tests/test_build_preflight.py tests/test_build_tool_preflight_integration.py tests/test_survey_fingerprint_pin.py tests/test_framework_survey.py tests/test_maven_version_provision.py tests/test_enforcer_attribution.py`。

**R7 — 让执行计划保留正确执行器、前置步骤和实际范围**

修改：[project_execution_plan.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/project_execution_plan.py)、[build_tool.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/tools/build/build_tool.py)、必要的现有 Maven/Gradle/Python 后端；环境修复复用 [env_overlay.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/runtime/env_overlay.py) 和现有 contract/receipt。

- [x] 以小型混合项目固定反例：文字计划是 `make client-unit-test`，结构化步骤却向 Gradle 传同名参数。核对计划中的 tool/params/cwd 与最终 argv，发生矛盾时不能无声换执行器。
- [x] 盘点现有公开入口再决定最小扩展。当前 `build(action='native')` 是 Python 环境能力入口，不能直接改成任意 Make/shell 执行器。Make 包装的 pytest 必须通过已验证且留下标准报告的路径；若必须补一个受限命令入口，同样经过现有授权、作业管理和 receipt 链。
- [x] 保留来源明确的初始化和上游产物步骤：wrapper 初始化、shade 的 package/install、测试工作目录、JDK/profile、test 与 verify 的差别。使用已有顺序步骤，不建通用 DAG，不给所有 Maven 项目自动追加 install。
- [x] 需要计量的修复重试回到结构化入口。用一次失败后变更受控环境的重试验证 argv/cwd/env、输出摘要、report delta 与恢复关系，覆盖 Geode 类缺口；任意 bash 输出不被反向猜造成 receipt。
- [x] 继续使用当前宿主绑定的 Analyze 计划。后续策略调整需走现有受验证流程；窄范围 fallback 可以如实完成，但不得覆盖原计划、改写固定验收条件或冒称完成未执行的 IT。

验收：小夹具证明 Make/Gradle 分派正确、wrapper 先初始化、shade 产物阶段正确、`-Dit.test` 随 `test` 不被说成已执行 Failsafe。第一次失败和第二次成功均有独立可核对记录。运行 `tests/test_project_execution_plan.py tests/test_maven_gradle_tool_contracts.py tests/test_python_tool.py tests/test_build_tool_preflight_integration.py tests/test_repair_run_scope.py tests/test_invocation_receipts.py tests/test_native_affordance.py`。

**R8 — 校准报告核对器，保留身份、案例与执行的区别**

入口：[receipt_test_rows.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/receipt_test_rows.py)、[receipt_suite_totals.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/receipt_suite_totals.py)、[report_metrics.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/tools/report_metrics.py)。离线审计参考封存的 [evaluate.py](/Users/chenhao/Documents/github/Setup-Agent/logs/d3r1-20260907/evaluate.py)，需要改动时另建有版本的脚本，不改原文件。

- [x] 先定位“不支持格式”发生在产品 reader 还是本轮离线审计。当前 Struts/SeaTunnel 的格式缺口至少存在于离线核对器；不能直接写成产品丢报告。
- [x] 用小样本核对 JUnit、原生 TestNG 和 Failsafe summary。summary 作为汇总/一致性证据；同一调用的汇总和 testcase 不能相加成两次执行。只有已证明产品存在同类缺口时才修改生产 reader。
- [x] 重复运行增加 receipt execution，不自动增加 latest subject/case。完整 totals 与受限身份行分开；2048 行上限触发后继续保留下界，不把所有 XML 元素当成唯一测试身份。
- [x] Curator 的 suite 声明 1 条而实际 3 条保持 conflict；新增格式支持不能把自相矛盾的数据洗成完整报告。

验收：原可核对的 53 份 receipt 计数没有无解释漂移；新增可用量明确来自哪种归属/格式修复；重复格式、截断、坏 XML、读取失败均不提高完整性。运行 `tests/test_receipt_scoped_rollup.py tests/test_receipt_bound_test_counts.py tests/test_nested_reportdir_dedupe.py tests/test_gradle_receipt_rows_e2e.py tests/test_gradle_receipt_metrics_e2e.py tests/test_pytest_report_aggregation.py tests/test_report_metrics.py`。

**R9 — 版本化修正官方 CI 目标，建立真正的正向比较样本**

修改：[d3_harvest_target.py](/Users/chenhao/Documents/github/Setup-Agent/scripts/d3_harvest_target.py)、[target_record.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/metrics/target_record.py)、[ci_vetting.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/metrics/ci_vetting.py)、[build_scope.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/metrics/build_scope.py)、[parity.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/metrics/parity.py)。优先使用已封存的 [官方补证](/Users/chenhao/Documents/github/Setup-Agent/logs/d3r1-20260907/official-ci-supplement/README.md)。

- [x] 为 23 项建立目标可用性清单，逐项列出 repo/SHA、job/cell、JDK/平台、完整命令序列、cwd/profile、模块 basis、测试池及每项来源摘要；无法证明的字段明确缺失。
- [x] 修正已证实的采集错误：Camel Examples 长名称漏行、Cayenne 嵌套构建被误归根；保留不同 job/JDK/shard，不把另一条绿色记录覆盖原 cell。矩阵表达式只能在固定 cell 输入能够完全解析时展开，无法解析保持 unknown。
- [x] 每次补证或纠错产出新目标版本和差异说明，记录是“增加证据”“修正采集”还是“改变任务”。保持旧 D3R1 目标摘要不变。只有结构真的变化才提升 schema，数据补证使用新的数据清单版本。
- [x] 小型受控目标先验证完整正向评分及已知新 red 的负向路径；同时争取至少一个小项目具备同 SHA 的真实官方模块范围和完整测试池。优先 commons-cli/gson，不能将控制夹具、日志打印的测试总数或另一 cell 的报告冒充其官方测试池。

验收：有来源的字段能重建；跳过全部测试的绿色 job 不生成非空已测宇宙；缺模块或测试范围不给整体分数。若真实官方正向样本仍缺证据，记录为 C 阶段未通过，不能用 14 个 invalid→invalid 消融代替。运行 `tests/test_d3_harvester.py tests/test_target_record.py tests/test_ci_vetting.py tests/test_build_scope.py tests/test_parity.py tests/test_attainment.py tests/test_kafka_build_scope_acceptance.py`，其中 Kafka 只读归档，不实际构建。

**R10 — 将比较结果接入真实生产发布和统一展示**

修改边界：[java_success_certificates.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/java_success_certificates.py)、[attainment.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/metrics/attainment.py)、[verdict_finalizer.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/agent/verdict_finalizer.py) 及现有 publication/readers；展示入口为 [main.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/main.py)、[verdict_rates.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/verdict_rates.py)、[report_tool.py](/Users/chenhao/Documents/github/Setup-Agent/src/sag/tools/report_tool.py)、[Web models](/Users/chenhao/Documents/github/Setup-Agent/src/sag/web/models.py) 和 [OverviewTab.tsx](/Users/chenhao/Documents/github/Setup-Agent/webui/src/pages/detail/OverviewTab.tsx)。

- [x] 第一小步打通生产者：在现有收口边界从当前授权证据构造真实 `JavaCertificateInput`，调用既有证书与 attainment 内核。逐字段核对来源，不能直接把实验脚本中的离线 `CertificateView` 搬来并设为有权威。
- [x] 第二小步打通发布：通过既有发布协议保存比较结果及 run/SHA/证书/目标版本绑定，读取端只读同一结果。缺目标、缺范围、缺证书证据均有明确状态；不得在最终收口时临时联网挑 CI target。
- [x] 本地执行结论和 CI attainment 继续分开。生命周期 parity 不进入 alpha；同池身份明确的新 red 才用于对应的 CI 缺陷判断。缺 CI 比较不能改变已充分证明的本地执行状态。
- [x] 第三小步统一展示：CLI 实际入口改用已有事实渲染函数，移除磁盘模块 `fully/most/half` 率标签；Markdown、简版、Web API/UI 展示同一封存结果、下界和缺失原因。Maven 说明使用 R6 的实际选择。
- [x] 若现有发布模型缺少必需字段，做最小显式版本变更，不默认为旧记录补成功证书。比较层不另起评判器，也不替代整个 finalizer。

验收必须穿过生产调用链：真实 BuildTool/控制器记录 → 证书 → 比较 → 发布 → CLI/完整报告/简版/Web 读取。覆盖可评分正例、缺轴、错误 repo/SHA、旧 run、授权删除、重复读取；首次有效正例移除范围后必须从可评分变为不可评分，不能只测试 invalid 的自循环。至少一条真实官方样本通过 R9；否则只能交付“接线已实现、真实正向验证尚缺”的状态。

运行 `tests/test_java_success_certificates.py tests/test_attainment.py tests/test_certificate_v2_surface.py tests/test_evidence_publications.py tests/test_cli_report_verdict_mirror.py tests/test_report_contract.py tests/test_reporting_condensed_summary.py tests/test_web_verdict.py tests/test_web_metrics_read_model.py`。对 CLI 需经过实际命令入口做集成测试，不能只测 `render_snapshot_metric_lines`。若修改前端，运行现有 Vitest 与 TypeScript/Vite build；浏览器至少核对成功、部分、缺证据三种页面状态。

**R11 — 分层验证，再运行完整 23 项**

复用 [小项目探针](/Users/chenhao/Documents/github/Setup-Agent/scripts/ci_scope_small_project_probe.py) 的控制器/receipt 检查，参考 [上轮小项目结果](/Users/chenhao/Documents/github/Setup-Agent/docs/superpowers/reports/ci-scope-small-projects-20260907.md)。原探针使用固定容器名和旧输出目录，新一轮须先参数化实验 ID/容器/输出路径并拒绝已存在目录，不能原样运行撞到旧实验。

| 层次 | 做什么 | 通过条件 |
| --- | --- | --- |
| L0：离线校准 | R0 五个反例、正向对照、真实日志解析；旧证据只读重放 | 已知误报消除、正例保留；全部变化能归于具体规则 |
| L1：最小真实夹具 | 父子 POM、shade 依赖、Make 包装 pytest、Java 范围、失败后环境修复 | 计划、实际命令、receipt、报告与结论逐项一致 |
| L2：两个小项目 | commons-cli、gson 各做确定性生产工具探针，再做模型驱动 CLI 对照 | 验证真实执行闭环，完整保留失败和 fallback；不要求两项都绿才记录 |
| L3：全量回归 | A/B/C 门槛满足后，冻结新版本完整跑 23 项，每项一次 | 所有 23 项都在报告中；判定、证据质量、比较资格分别核对 |

小项目沿用已知 revision：commons-cli `e17111798da51037659b3594d9c0b3b525040081`，gson `b3f4ca20087f9066de4c340522ff84e0558e1ad1`。旧 Gson 探针曾出现 JPMS VM 启动失败，因此它也是有价值的反例，不能预先写成必然成功。

- [x] **先冻结实验协议。** 记录基线/候选源码 SHA、目标版本、镜像 digest、架构/JDK、缓存规则、模型与参数、资源、任务验收条件、评估器版本。确定性探针和环境消融固定命令；Agent 对照固定任务和验收线，允许 Agent 自己选择命令，但不能自行降低完成标准。
- [x] **小项目采用新鲜对照。** 旧版使用 `79ae52a`，新版使用修复后的冻结提交。建议每次 20 分钟、75 次迭代作为新的小项目预算，两组完全相同；各项目先各跑一个成对样本。若要进一步比较时间/成本，预先声明三组成对重复，交替先后顺序，独立容器/缓存，所有尝试保留并报告波动。这个小样本用于发现趋势，不能单凭它宣称普遍、稳定的效率优势。
- [x] **消融一次只动一个因素。** 固定源字节只切换展示截断；固定执行只切换新旧完成判断；固定同一合法证书分别删除 CI 模块范围、测试范围或授权。至少一个正向输入必须因删除证据失去评分资格。消融只在独立测试/新实验副本上投影，不改已发布的历史结果。
- [x] **消除评估器变化造成的假进步。** 对可重放的历史证据，用新旧评估器分别打标签，报告“纠正判断”的数量；对真正新跑的数据报告“任务完成”的变化。CI 补证前后使用不同版本列出，不能把目标更换带来的差异计为 Agent 改进。
- [x] **全量运行保持可比。** 新一轮沿用原 23 个项目/SHA，以及 D3R1 的镜像、模型、资源、150 迭代/7200 秒等冻结条件；有必要变动的因素单列。旧新证据能够用同一评估器/目标版本核对时再做对照；不能核对的明确不可比。保留所有 timeout、failed、partial、invalid 与缺目标，不只挑成功重跑。
- [x] **检查所改变的产品边界。** 完整 Python suite 在候选冻结前跑一次，记录新增失败与既有基线；前端被改才跑其全套测试和 build。R1/R10 涉及的终端重复通知、发布中断及重启读取要有定向集成测试；其余 Web 调度/浏览器并发、父进程死亡窗口未覆盖时明确保留，不扩成整个平台重写。

R11 的勾选对应已完成的实验与审查动作：固定五例的旧/新 completion 方法对照为 5→0，旧 23 项报告池使用相同解析器复读，53 份 receipt 总量无漂移；这不生成新的历史授权结论。新 23 项原始标签为 1 success、18 partial、3 failed、1 超时且未发布，6 项未建立执行计划。Struts 新出现下游跳过仍 Test success 的反例，整体能力和通用判定验收仍未达成。

全部 32 次模型尝试及成本均已保留；932 个冻结源码文件、运行 pins 和 14,414 个旧封存文件校验通过。最终 HTML 已验证桌面/手机布局与来源交互，Markdown/JSON 与同一已审查 artifact 对应。原始任务缺少逐项目独立预登记的详细验收清单，实际命令、profile、CI 输入及外部依赖仍有差异，因此报告不计算事后定义的严格任务完成率或效率增益。

**如何用这轮修复衡量进展**

| 维度 | 报什么 | 验收/解释 |
| --- | --- | --- |
| 判断正确性 | 固定校准集上的误报与误拒数量；逐条预期与实际 | 五个已知内部误报归零，两个整体误报消除，正向对照通过；不外推为所有场景零误报 |
| 任务完成 | 固定验收任务中完成/部分/未完成/无法判定的数量，保留全部 23 项 | 分母由实验协议确定；不能用 Agent 后来选择的更小计划重定义分母 |
| 证据完整性 | subject/case/execution 各自 available、下界、unavailable；完整报告池/23 | 计数变化能追到具体来源；publication complete 不等于报告池或身份完整 |
| CI 可比较性 | 有效目标与完整比较输入数/23，另列缺目标、invalid、缺轴的原因 | 这是测量条件的进度。达标率只能在可评分集合内给出，并同时披露该集合大小/23 |
| CI 达标与流程 | 可评分项目的 met/not_met/partial 等原始结果；生命周期 parity 独列 | 缺范围不评分；test、verify、release smoke 不混成同一任务 |
| 资源效率 | 所有尝试的耗时、调用、token；同等完成质量样本的成对差异 | 失败成本也计入总成本；不同完成质量、预算或目标不直接比较 token 高低 |

最终交付包含：修复变更清单及定向检查结果；机器可读的校准结果；带完整 pins 的小项目/全量实验清单；每个项目“执行问题、Agent 策略问题、证据/解析问题、CI 数据问题、待验证环境原因”的归因。原因可多选，不相加伪造总数。

不在本轮预设根因的事项：Kafka daemon 的底层退出原因、Jackrabbit 无测试报告的唯一原因、Camel Examples 历史 SNAPSHOT 字节、Struts/ZooKeeper 的用户与架构影响。修复确定边界后仍失败，再选最小测试做单变量实验；不提前承诺修完某个模块即全部 23 项通过。

**首批实施范围建议固定为 R0–R2。** 其完成标志是成功判定可信，而不是 success 数字增加。R3–R8 随后改善真实执行与可核对性；R9–R10 满足后才能宣告新的 CI matrix 已有生产闭环。所有任务通过其验收后再勾选；提交按这些边界拆分，commit message 不添加 Co-Authorship。
