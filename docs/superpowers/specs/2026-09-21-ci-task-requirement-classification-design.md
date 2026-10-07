# CI 任务验收、要求分类与失败归因设计

- 日期：2026-09-21
- 修订：2026-09-22，根据用户逐项 review 核对 pinned POM、归档 CI 日志和两个 recorder 的现状后更新。
- 状态：已进入实现与验证；新增共同评分器、双端证据适配和分组统计已落地，元数据审阅及运行准备度单独记录。参见 [实现记录](../reports/2026-09-22-requirements-v2-implementation.md)，不得将单元测试通过等同于新 campaign 完成。
- 范围：SAG 与其他 coding agent 共用的 Java setup benchmark；先应用于既有 20 项目开发集。
- 原则：**每个任务以冻结的 CI 要求验收；按要求类别分析能力边界。分类不降低验收要求。**
- 关系：延续 [CI-defined build scope](2026-09-07-ci-defined-build-scope-design.md) 的范围与缺失证据约束，细化 [SAG-MS-1](2026-08-27-sag-ms-1-measurement-standard.md) 的任务描述和实验分析；不改写历史成绩或直接替换现有 verdict。

## 1. 要解决的问题

项目的 CI 任务差异很大：有的完成编译和测试即可，有的还要求 JAR、安装到本地仓库、Javadoc、集成测试或原生程序。所有项目都叫“build 成功”，无法说明完成了什么；只看总体成功率，也无法定位新增哪项要求后更容易失败。

本设计分别回答：

1. **任务完成了吗？** 是否满足这个版本、这个 CI job/cell 的全部已冻结验收要求。
2. **完成到哪里？** 编译、打包、各类测试、文档和其他检查各自有什么证据。
3. **哪类任务容易失败？** 在相同要求组内比较 agent，并观察前置条件满足后的失败位置。
4. **新增要求是否导致下降？** 用同项目配对实验验证，区分相关性与因果归因。

例如：编译、单元测试和打包均通过，Javadoc 失败。正确结果是“完整任务未完成；已完成的三项保留通过；文档要求失败”。不能把已通过的测试改成失败，也不能把整个任务报为成功。

## 2. 现状与边界

以下为 2026-09-22 本轮实现之前对本地代码和归档的复核；实施后的状态见实现记录：

| 现有部分 | 已有能力 | 本设计要补的内容 |
|---|---|---|
| [acceptance_task.py](../../../src/sag/agent/acceptance_task.py) | 固定 repo/SHA、有序命令、目录、启动 JVM 与 Maven 要求；按授权回执核对步骤 | 一条命令内部包含哪些要求，以及各要求的独立结果 |
| [verdict_finalizer.py](../../../src/sag/agent/verdict_finalizer.py) | 保存 `task_completion`，限制未完成任务的整体成功声明 | 以后接入经验证的要求结果；先不切换权威来源 |
| [ci_comparison.py](../../../src/sag/agent/ci_comparison.py)、[attainment.py](../../../src/sag/metrics/attainment.py) | CI 目标、范围、测试及生命周期比较 | 要求标签、分组分析与可解释的失败位置 |
| [portable metrics v1](../../../output/sag-benchmark20-20260917/METRICS.md) | 独立于 SAG 的命令记录、产物清单、JUnit 与计数比较 | 阶段结果拆分；目前整条 `verify` 失败可能同时使 build/test task 字段为假 |
| 工作树检查 | SAG 的 `acceptance_task.py` 已调用 `git diff --quiet HEAD --`，但未持久保存变化路径／未跟踪文件快照；portable 保存 tracked-clean 布尔值和未跟踪文件列表 | 两端保存统一的原始 Git 探测及出处；不能把即时 clean 检查描述为完全没有检查，也不能把它当成已归档的完整工作树证据 |
| 人工干预计数 | SAG 当前结果没有 `human_interventions`；portable 从外部 telemetry 读取 | campaign runner 显式记录无交互运行方式及干预计数；历史缺字段不补零 |
| 运行时符合性 | 两端已有启动 JDK 主版本、固定 Maven 版本检查 | 将这些检查作为显式任务前置证据，连同本次实际启动器探测一并归档 |

20 项目归档中，11 项具有可用的 CI 构建范围和测试计数；7 项存在参考证据缺口；Commons CLI、Gson 是未匹配 CI 的 smoke task。CSV、DBCP 在那 7 项中有可用的构建范围，但缺少可用测试计数。该分布来自 [冻结 manifest](../../../output/sag-benchmark20-20260917/manifest.json)，不代表重新采集或重新运行的结果。

这些项目仍可参与共同固定任务的比较，但不能统称为“20 项完整 CI 等价验证”。标签完善不能补造缺失的 CI 测试池或模块分母。20 项目已参与开发和调试；论文中应披露其开发集身份。

## 3. 什么是同一个验收任务

任务身份至少绑定：仓库、完整 commit、选定 CI job/cell、工作流与构建配置、原始命令及顺序、目录、profiles/模块选择、运行时要求和预先声明的改动。

正式实验冻结这些内容及其证据摘要。相同项目名不足以说明是同一任务；不同 JDK cell、测试排除项或打包目标可能改变题目。

### 3.1 验收标准与执行方式

CI 行为决定验收要求。Agent 可以自由调查、安装工具和尝试修复；正式验收不能由 agent 在看到失败后删减。

第一版继续执行冻结的原始验收命令，作为复现行为的可操作标准。不要求 agent 的探索命令与 CI 一样，也不根据命令文字相似度自动认可替代命令。未来如允许替代执行方式，必须预先证明其模块、产物、测试与检查范围等价，并作为新协议版本。

原任务中的 `deploy → install` 等替换必须保留为已声明变体，单独说明发布步骤未被评估。它可以是有效 benchmark 任务，但不能声称完整复现原始部署行为。

Commons CLI 的历史 `mvn clean verify` 是该 commit 自身 `defaultGoal` 的命令目标前缀缩减，可作为“已声明变体”的现成例子。它保留生命周期中已经绑定的检查，省略的是后续显式追加的目标；不能描述为完全不做质量检查。现有匹配 CI 证据仍不足，因此披露为“defaultGoal 的已声明缩减／smoke，CI 匹配未证实”，不借分类更新把它升级为匹配目标。原任务及成绩不变。

### 3.2 三类结论保持独立

| 结论 | 能支持的声明 |
|---|---|
| 固定任务完成 | 指定命令及其已定义要求完成 |
| 与选定 CI 任务对齐 | 任务的版本、环境约束、范围及声明的变体有可核验来源 |
| CI 范围／测试内容达标 | 有足够 CI 与本次执行证据，可比较模块、计数或案例身份 |

官方 CI 计数缺失时，第一项仍可被证实；第三项对应分数保持 unavailable。相同测试总数只能支持计数比较，不能证明测试内容相同。三项不合并成一个加权分数。

## 4. 要求分类：允许重叠，按真实依赖分析

分类单位是任务中的具体要求；项目标签由这些要求汇总。一个任务可以同时包含编译、打包、测试和文档要求。

| `kind` | 含义 | 需要明确的子类型或范围 |
|---|---|---|
| `compile` | 生成规定范围内的编译结果 | 生产代码／测试代码、模块、语言与工具链 |
| `package` | 生成分发产物 | JAR、WAR、ZIP 等；产物所属模块与用途 |
| `install` | 安装任务产物到本地依赖仓库 | 仓库位置、坐标、分类器；不等同远程发布 |
| `test` | 运行规定测试 | 单元、集成、原生检查；测试池与排除规则 |
| `documentation` | 生成或验证文档 | Javadoc 等，以及必需入口文件／检查结果 |
| `quality_check` | 其他 CI 必需检查 | 许可证、格式、静态分析、覆盖率门槛等具体检查 |
| `native_compile` | 生成原生可执行产物 | 目标平台、工具链、二进制与后续执行关系 |

只记录 CI 实际要求的项目。生成覆盖率报告与必须达到覆盖率门槛是两项不同要求，不能看到 JaCoCo 名称就推断存在门槛。未支持的自定义要求保留原命令和 `unclassified` 标记，不被删除或默认通过。

### 4.1 构建终点与独立分支

可用“编译 → 打包 → 本地安装”描述常见构建终点，但该链只适用于确实存在这些依赖的任务。测试、文档、质量检查和原生编译按实际工作流建立关系，不被强行排成全局 Level 1–6。

Maven 生命周期会执行选定阶段之前的阶段，但实际插件工作取决于 packaging、绑定和 profile；`verify` 字样本身不能证明集成测试存在。聚合 POM 也不因此必须产出 JAR。[Maven 官方说明](https://maven.apache.org/guides/introduction/introduction-to-the-lifecycle.html)

Gradle Java 插件的 `build` 关联组装和检查；具体项目仍可扩展任务及依赖。最终以冻结版本的配置和原生执行证据为准。[Gradle Java 插件说明](https://docs.gradle.org/current/userguide/java_plugin.html)

SAG 的 Analyze/Build/Test/Report phase 是 agent 控制阶段，不能用来证明某个编译、测试或文档要求已经完成。同一次工具调用可以满足多项要求；不同要求也可以在同一 agent phase 内完成。

### 4.2 分组依据必须先于实验结果

任务标签由官方工作流、该 SHA 的构建文件、解析后的配置及归档 CI 原生输出共同支持。记录来源路径／URL、内容哈希和依据位置。

- 命令给出候选类别；构建配置确认绑定、默认 goal、产物及排除项。
- CI 原生输出用于核对具体执行内容；被跳过的后续步骤不能因日志中没有出现就从要求中消失。
- 来源冲突或不能确认时标记待核验，不以 agent 的成功／失败来决定标签。
- 有证据的标签可以使用；不能确认全部标签时，不能把该任务放进“仅编译、无其他要求”等排他组。

要求范围至少能区分模块、产物、测试集合和检查项。范围无法完整列举时保留 unknown／已知下界，不用磁盘扫描数量替代官方范围。

### 4.3 用有效 POM 生成产物期望，人工审例外

冻结 manifest 中九个单步骤 install 任务的 `expected_artifacts` 都为空；空数组表示旧包未提供显式清单，不能解释为不要求产物。Whisker、JCS、HttpClient、HttpCore、RAT、Curator、Jackrabbit、RocketMQ 的归档 scope 列表合计 92 个条目，含聚合模块及参考证据尚不完整的条目；它不是 92 个应生成的 JAR，也不是都已认证的 CI 构建模块。

要求清单默认从该 SHA、任务指定 profiles／属性／JDK 下的有效 POM 和已解析 reactor 推导，记录解析命令、工具版本、父 POM／插件来源及原始输出摘要：

| 有效 packaging | 默认声明的主产物期望 |
|---|---|
| `jar`，包括省略后按 Maven 默认得到的 jar | 一个无分类器的主 JAR |
| `bundle` | 扩展映射已确认时，一个主 bundle JAR |
| `war` | 一个主 WAR |
| `pom` | 不要求主二进制产物；install 仍需要对应 POM／坐标证据 |
| 其他或无法解析的扩展 | 标记例外，不能套用 jar 规则 |

同时读取 finalName、输出目录、版本／坐标、classifier 及任务的实际生命周期终点。只有任务要求到达打包或安装时才产生对应产物义务，不能给仅到 test 的 DbUtils 增加 JAR 要求。安装位置以本次有效 localRepository 为准。

这类期望标记为 `declared`，不冒充“CI 已观察到该产物”。人工只审影响默认规则的例外，如 skipIfEmpty、shade 替换主产物、仅附加 classifier、assembly／自定义插件、profile 改写、有效 POM 无法解析。来源未解决的例外保留 unknown；不得根据 agent 的失败事后降低期望。

有效 POM 解析属于实验前的元数据准备，禁止在候选 agent 工作区中预编译或安装项目来帮其完成任务。元数据准备的下载／缓存不暗中传给某个 arm；公平的缓存政策另行冻结。

## 5. 对既有 20 项目的初步映射

下表按已冻结的命令和已复核的 defaultGoal 分配审阅入口。CSV/DBCP 的默认目标已知；各项目完整产物及全部绑定检查的核验仍按 §4.2–4.3 完成。

| 命令形态 | 项目数 | 项目 |
|---|---:|---|
| 显式到 `test` | 2 | Commons DbUtils；Tomcat Jakarta EE Migration |
| 显式到 `package` | 1 | RocketMQ |
| 含 `install` 的单步骤任务 | 9 | Commons Net；Sling Commons MIME；Sling Commons OSGi；Creadur Tentacles；Creadur Whisker；Commons JCS；HttpComponents Client；HttpComponents Core；Creadur RAT |
| 显式到 `verify` 的单步骤任务 | 4 | Commons CLI；Gson；Cayenne；Jackrabbit |
| 安装后对指定模块验证 | 1 | Curator |
| Gradle 构建、原生编译、原生运行 | 1 | FreeMarker |
| 执行 POM 声明的完整 defaultGoal：verify＋质量检查＋Javadoc | 2 | Commons CSV；Commons DBCP |

必须保留的具体差异：

- DbUtils 的 `test` 任务不能被额外要求必须生成 JAR。
- Sling 两项任务显式包含 `javadoc:javadoc`；文档要求与编译、测试分开记录。
- Curator 的首步 `install -DskipTests` 是合法的构建前置步骤，第二步才执行规定范围的测试；不能要求第一步有测试，也不能省略第二步。
- FreeMarker 的 JVM 测试完成不代表原生编译及运行完成；三个命令分别保留结果。
- CSV/DBCP 是要求类别密集的默认任务，包含编译、测试、打包、许可证、API 兼容性、静态分析／重复代码检查和文档；不能擅自缩减成 `test` 或 `verify`。要求密集不等于已证明运行时间最长或成功率最低。
- Jackrabbit 显式启用了 `integrationTesting` profile；实际集成测试范围仍需配置和报告核验。

### 5.1 已确认的 defaultGoal 与来源

CSV 的 pinned commit `bb0f0fbb0f84de5cf5a0c73a9fa7051fb5135650`：

```text
clean verify apache-rat:check japicmp:cmp spotbugs:check pmd:check pmd:cpd-check javadoc:javadoc checkstyle:check
```

DBCP 的 pinned commit `d16b6a36bba2eb70270a709642df8e7792cf2a6e`：

```text
clean verify apache-rat:check japicmp:cmp checkstyle:check spotbugs:check pmd:check pmd:cpd-check javadoc:javadoc
```

目标集合相同，但 DBCP 的 Checkstyle 在 SpotBugs 前，CSV 的 Checkstyle 在 Javadoc 后。失败位置分析保留各自实际顺序。两份本地 pinned POM 分别与另一份官方采集归档逐字节一致，所列目标也可在相应 CI 控制台核对：

| 项目 | POM 归档与 SHA-256 | 执行归档 |
|---|---|---|
| CSV | [pinned POM](../../../output/java-benchmark-20260916/raw/configs/apache/commons-csv/bb0f0fbb0f84de5cf5a0c73a9fa7051fb5135650/pom.xml)，`835232d07307110b0dd87052f9a51a4f638c7a4ad1a856b0ae013dfca80e186c` | [官方 job 日志](../../../output/sag-benchmark20-20260917/ci/commons-csv/8457228fb3d3-job-101760303455.log) |
| DBCP | [pinned POM](../../../output/java-benchmark-20260916/raw/configs/apache/commons-dbcp/d16b6a36bba2eb70270a709642df8e7792cf2a6e/pom.xml)，`b94d3f73e4a42361d63cdd87f525e36bfb383b3cbcd78ec01c1764ed075f2cca` | [官方 job 日志](../../../output/sag-benchmark20-20260917/ci/commons-dbcp/6e3ebf1823cc-job-101761476043.log) |

CLI 的 [pinned POM](../../../logs/ci-scope-small-projects-20260907/ci-targets/commons-cli/sources/pom.xml)（SHA-256 `b870866131e5a83dfef4c6d93641eb6800150a1aad4586287df51c1e0396dd35`）声明的是以下序列；其中没有 CSV/DBCP 的显式 `pmd:cpd-check`，也不应套用其他项目的完整目标列表：

```text
clean verify apache-rat:check japicmp:cmp checkstyle:check spotbugs:check pmd:check javadoc:javadoc
```

因此，文档要求组至少包含 Sling 两项、CSV、DBCP 共四项；质量检查标签也须覆盖 CSV/DBCP，连同 Sling 已记录的 Enforcer 等检查至少四项。其他项目可能也通过父 POM 绑定检查，此处是已确认下界，不是全组最终总数。FreeMarker 的原生任务当前只有一项，见 §9.3 的抽样限制。

`japicmp:cmp` 需要解析配置指定的发布基线工件，缓存不足时涉及 Central／声明镜像下载；不能笼统假定总是“最近一次发布”。CSV 的 `commons.bc.version` 为 `1.14.1`，DBCP 为 `2.14.0`，实际解析坐标及来源仍须归档。下载失败应定位到依赖获取／网络证据，不伪装成 API 不兼容。

RAT 可检查工作树内不在排除规则中的陌生文件。Agent 或 harness 写入仓库的笔记、脚本、日志可能触发失败，未跟踪文件政策和来源归因必须进入任务证据模型；不能事后删除文件或添加 RAT 排除项来挽救某个 arm。

## 6. 最小数据设计

复用现有任务定义、CI 目标及证据，不增加新的调度器。第一阶段增加一个伴随任务的 `requirements.json`，以及每次运行的 `requirement-results.json`。下面是设计中的字段，不是现有接口声明。

**新协议以 `requirements.json` 取代 portable manifest 的 `stages`，作为验收要求及分类的唯一来源。** `stages: ["build", "test"]` 无法表达 CSV/DBCP 的文档与质量要求，只作为 v1 历史字段保留；新分析器不得从它反推、补全或覆盖 requirements。两者不一致时显示迁移差异，不取较宽松的一方。原命令数组和旧 ZIP 不修改。

| 对象 | 必需信息 |
|---|---|
| 要求清单 | schema 版本、任务摘要、CI 来源／变体说明、标注完整性、要求列表 |
| 任务级前置条件 | `worktree_integrity`、`runtime_conformance` 的固定政策；两端同样适用，不作为难度加分项 |
| 单项要求 | `id`、`step_id`、`kind`、必要的子类型、范围、验收证据规则、来源引用 |
| 依赖 | 仅保存已知前置要求 ID；未知依赖明确标记，不猜测 |
| 单项结果 | 要求 ID、运行／调用身份、状态、原因、支持状态的原始证据引用 |
| 工作树记录 | 起始、验收前后、evidence close 的 HEAD、tracked diff 路径、未跟踪路径；每条探测的 argv、退出码、原始输出摘要／引用及采集时点 |
| 运行时记录 | 每个验收调用的实际启动器、JDK／Maven 等探测原文与结果、规范化版本、要求来源及符合性 |
| 人工干预记录 | run ID、runner 版本、非交互配置／通道政策、观测区间、干预事件和显式计数；由 runner 产生 |
| 运行汇总 | 原任务摘要、要求清单摘要、评估器／政策版本、前置条件、命令结果、要求结果、未解决冲突 |

原 `AcceptanceTask` 有序步骤保持不变，依赖字段仅供分析阻塞位置，不参与重新排序或执行命令。第一版用有限枚举和明确规则；不提供任意表达式、规则语言或插件框架。

完整评估身份为 `(原任务摘要, requirements 摘要, 政策／评估器版本)`，并随 run pin 冻结。验收范围、预期产物或容许的测试结果改变时，必须重新冻结任务协议版本。只有展示措辞变化可以不改变任务语义，但也需要记录分类版本。同一完整评估身份不能对应两份不同的必需要求。

结果不得通过增加“可选”、删除困难要求或改变分组来改善。未要求的类别在任务定义中是“不适用”，不生成一条免费的 passed 记录。

## 7. 独立证据与四种状态

所有证据必须绑定同一任务、commit、运行及对应调用。完整日志、原生报告、产物及环境记录由可信 recorder 保存；agent 总结和阶段声明只作为调查线索。

| 状态 | 判定依据 |
|---|---|
| `passed` | 规定范围内该项验收条件有完整、相容的通过证据 |
| `failed` | 有可归属到该项要求的明确失败或不符合预定验收条件的证据 |
| `not_run` | 有证据表明该项没有执行，记录前置失败、主动遗漏或预算终止等原因 |
| `unavailable` | 无法判断、证据丢失、来源冲突、解析不支持或范围未解决 |

单独“没找到证据”只足以判为 unavailable。not_run 可由明确的跳过记录证明，也可由下面限定的 runner 语义证明；不要求 Maven 一定打印 `SKIPPED`。

**Maven 同模块 fail-fast 规则：** 在模块、goal occurrence 及后续绑定已经解析清楚的前提下，下列四项同时成立，可以判后续要求为 `not_run`：

1. 日志完整且与该 invocation 的字节摘要绑定，正常终态已确认，记录能够观察本规则所需的插件执行信息。
2. 该模块内上游插件／要求已明确失败，并能定位到对应 goal 与执行序列；只有一个泛化 `BUILD FAILURE` 不够。
3. 目标要求确实绑定在该执行序列的后续阶段／目标位置，没有该次目标执行的记录或其他相反证据；不能仅按插件名称推断，因为目标可能被重复执行或 fork。
4. 有效命令及配置采用失败即停，未启用 `-fae/--fail-at-end`、`-fn/--fail-never`、`--continue` 或其他继续执行／吞错机制。必须包括 wrapper、`.mvn/maven.config`、`MAVEN_ARGS` 等有效输入，而非只看 agent 请求字符串。

记录 `reason=upstream_failed`、`inference_rule=maven_same_module_fail_fast`、阻塞要求 ID、解析后的绑定及日志引用。这是有来源的语义推断，与直接跳过事件使用同一 not_run 状态，但披露不同的依据。例如编译插件失败后，同模块后续 Surefire 未启动，不需要额外的 skipped 行。

第一版只将这条规则用于已验证的同模块串行执行边界。多模块／并行 reactor 要结合模块级终态与依赖处理，不能把一个模块的失败推及全部其他模块；FreeMarker 的 `--continue` 和 Gradle 独立分支分别读取任务事实。日志截断、quiet 模式缺少必要可观察信息、fork／执行计划无法解析或存在相反证据时，不使用此推断，保留 unavailable。

### 7.1 各类别如何证明

| 类别 | 需要的主要证据 | 不足以单独证明的现象 |
|---|---|---|
| 工作树完整性（任务前置） | HEAD、tracked diff 和未跟踪文件原始快照、采集时点、退出码、文件来源／改动政策 | 只看 commit；只有一个即时 clean 布尔值；缺记录时默认干净 |
| 运行时符合性（任务前置） | 绑定每次调用的真实启动 JDK 主版本、固定 Maven 版本及任务要求的附加工具链／能力 | 命令退出 0；环境变量写着 Java 17；机器上安装过符合要求的 JDK |
| 编译 | 本次调用中目标范围的原生编译结果；适用的生成／有效复用产物证据 | 磁盘上有一些 `.class`；一句整体 `BUILD SUCCESS` |
| 打包 | 目标模块的打包完成事实，以及预期角色的产物身份、路径、哈希和可读取性 | 任意位置找到一个 JAR；只有 sources/test JAR，却缺主产物 |
| 本地安装 | 安装操作及该运行指定仓库中的对应坐标、POM／产物证据 | Maven 缓存里存在同名旧版本 |
| 测试 | 来源明确、范围相容的本次测试报告及正常终态；区分 pass/failure/error/skip | 只编译测试；全 skip；复用旧 XML；总数相同 |
| 文档／检查 | 对应原生任务结果和该要求规定的输出／检查报告 | 整条命令退出非零就认定所有子项都失败 |
| 原生编译与运行 | 生成二进制的来源及哈希；绑定这个二进制的运行终态 | 只有 native-image 已安装；只有 JVM 测试成功 |

产物哈希用于证明本次证据身份，不要求 agent 的 JAR 与 CI 的 JAR 字节相同。时间戳等非确定性内容可能不同。多模块项目按预期产物角色验收；聚合 POM、无源码模块和源码 JAR 都不能套用“一模块一主 JAR”的规则。

缓存命中和增量复用只在预先统一的缓存政策、来源和范围能够验证时认可。无法确认来源的残留文件不能通过。清理策略不能只对一个 agent 额外执行；也不能把清理 Maven/Gradle 依赖缓存当成默认的产物清理。

### 7.2 命令结果与子要求结果

整条命令非零只证明这条必需命令没有按要求完成。它不自动把所有子要求设成 failed。只有编译成功证据确实存在时，才能在后续 Javadoc 失败后保留 compile passed；日志不足时该项仍是 unavailable。

反过来，某个测试集通过也不能覆盖后续必需步骤缺失。打包步骤在测试失败后未运行，应记录 not_run，并引用阻塞事实。

截止时间耗尽的完整任务未完成，但这不自动说明编译器或测试代码有错误。记录终止原因、当时进行的要求及其可证实状态；丢失终态时不补造要求级 failed。来源不匹配的失败证据也不能用来归因当前任务。

旧的成功调用不能覆盖同一验收尝试中的较新失败；多次尝试不拼接最好的片段。重试选择和最终验收的规则在实验前固定，全部重试计入成本。

### 7.3 CI 中已有红测或允许失败的检查

本设计不把 CI 的绿色 badge 当成“所有子项均通过”。必须披露 allow-failure、continue-on-error、已知红测及 flaky 情况。

已有 CI 相对测试策略应当随目标冻结；只有身份和范围可对齐时，才可证明“本次红测属于已知容许集合”。总红测数量相同不足以免除一条新的失败。无可靠对应证据时，该项相对 CI 的结论保持 unavailable，并保留原始红测。

SAG 当前 CI 比较与 portable v1 的零红测试条件存在语义差异。这次分类不静默修改任一实现；新一轮跨 agent 实验必须使用同一版本的测试结果政策。第一批验证使用已确认的绿色参考任务；包含允许失败的任务另行核验后加入。

### 7.4 工作树完整性：两个适配器使用同一政策

至少在干净任务起点、正式验收前后及 evidence close 采集：

```sh
git rev-parse HEAD
git diff --name-only HEAD --
git ls-files --others --exclude-standard
```

实际机器记录宜使用相应 `-z` 路径输出，正确处理换行等特殊文件名，同时保留可读展示。每条命令独立保存退出码和原始输出哈希／引用；探测失败不能当成空列表。tracked diff 覆盖 staged 与 unstaged 相对于 HEAD 的改动；`--exclude-standard` 不包含 ignored 文件，不声称完整列举磁盘所有额外文件。

SAG 需要在 evidence close 将这两份路径列表及 HEAD 作为结构化记录写入 session 证据目录，并保存对应的起始／验收边界快照。该目录由 recorder 管理、位于被测仓库外；不要为收集证据反而在仓库中写文件触发 RAT。Portable 也保存探测原文、退出码和 tracked 变化路径，不只保存 clean 布尔值。两端都由 host 收集最终快照；若进程被终止而无法采集，明确保留缺失。

共同政策：

- 固定源码任务要求 tracked 文件保持不变；如允许修改，必须是实验前声明的独立任务变体，保存补丁及哈希，不能事后白名单化。
- 未跟踪文件**不一律淘汰**。保留路径、必要的内容摘要和来源：初始环境、原生构建生成、agent 工具写入、harness 写入或未知。不得伪造报告、插入改变构建／测试范围的输入来满足固定任务。
- 不在验收前悄悄清理 agent 产生的文件，也不替它增加许可证头或 RAT 排除项。统一的预定产物清理只能处理已声明输出，不能掩盖其他额外文件。
- RAT 失败引用额外文件时，若起始快照无此文件、写入事件／内容摘要与 agent 操作相符且原生 RAT 报告定位到它，归因为 `agent_introduced_file`，不是项目自身缺陷。若文件由 recorder 引入，归因 harness；只有 before/after 差异而来源不明时，保持“工作树新增文件，来源待证实”。
- 上述边界快照不是完整的中途写入审计。已有证据发现运行中曾改变受保护源码／配置，不能靠结束前还原文件获得认证。

### 7.5 人工干预：由 campaign runner 显式记录零

`human_interventions` 不能由 SAG adapter 在缺字段时自行补零。正式非交互 campaign 的 runner 应在启动时记录 `sag project` 入口、runner 版本、输入政策和观测起点，以 `stdin=DEVNULL` 等方式关闭交互输入，禁用或记录外部控制通道；结束／超时时写出实际干预事件数及观测区间。

SAG 的非交互 project 模式在该受控协议下可以由 runner 产生有出处的 `human_interventions=0`，其 `source` 为 runner 的非交互运行记录，adapter 负责读取并绑定 run ID。它不是从 agent 日志没有提到人类帮助推出来的零。初始共同任务提示不计为干预；临时指导、人工修改环境／文件或手动恢复等影响任务的操作必须记录。自动的预定超时不算人工干预。

若输入通道未受控、人工操作未记录、观测区间有缺口或数据缺失，则自主性保持 unavailable。历史 SAG 运行没有该字段时不追补零；即使任务已完成，也不能据此补成“自主成功”。此项是下一轮 campaign 的启动前置，不留到报表生成时补做。

实施时采用明确的实验协议，而非声称能监视宿主机管理员的所有动作。`sag-unattended-v1` 必须在启动前声明输入／控制通道、从进程启动到终止的观测窗口，以及实验者“禁止或记录任何影响任务的外部人工操作”的承诺。Runner 提前创建记录，关闭标准输入，记录人工停止与申报的偏离，并在进程终止时封存。协议、起止记录、事件集合和实际 run pin 均须绑定。完整且空的封存事件集支持**该协议范围内**的零；没有事前声明、未知事件或缺少结束记录时仍为 unavailable。未申报的宿主机管理员绕过是独立的有效性威胁，不把它写成已经技术隔离；对其他 agent 使用相同口径。事后发现漏报必须使该尝试的自主性认证失效，不修改已封存的空事件集来保留成功。

### 7.6 运行时符合性：按调用而非按机器安装清单

SAG 复用现有 `dispatch_probe` 和 toolchain fingerprint；portable 复用实际 Maven／Gradle launcher 的探测。每个验收调用都保存 expected/observed、原始探测、启动器路径、版本约束来源；自动重试或切换 JDK 后重新绑定，不能借前一步的正确环境认证后一步。

JDK 主版本和精确 Maven 版本分别按任务约束校验；存在 GraalVM／native-image 或多编译工具链要求时，另外保存对应能力和选用工具链证据。未要求 patch/vendor 时不凭空加严。JCS、HttpClient、HttpCore 保留 Java 17 主版本要求及“CI 补丁版本未知”的披露：job 标签／配置支持主版本，不能据此补造精确补丁版本，也不因此把主版本降为未知。

没有携带固定任务的自由运行，即使 JDK 8／Maven 3.9.9 上所有命令成功，也不能并入要求 Java 17 的 Commons CLI smoke 任务结果。正式任务绑定有效但观察到版本不符时，记录 `runtime_conformance=failed`；证据／任务绑定缺失时为 unavailable。两者都不能 complete。该细分是新协议的规则，不回写现有 SAG／portable v1 的历史状态。

## 8. 怎样计算和展示结果

### 8.1 主结果：完整任务完成率

命令层保留现有 `task_completion`；新增要求结果是它的细分证据。拟新增的实验级任务状态规则为：

- **complete**：身份与来源有效，任务级前置条件通过，全部必需命令完成，全部适用要求 passed。
- **incomplete**：有已确认的任务前置、必需命令或必需要求失败，或可确认的必需要求未执行／预算内未完成。
- **unavailable**：既不能完整证实完成，也没有足够证据确认未完成，例如验收链或关键输出缺失。

无效或未绑定证据先被排除；有效证据中确认的失败不会被其他缺失项抹掉。无定义的要求、未知范围或冲突不能构成 complete。阶段粒度证据不足时仍可披露已证明的命令完成，不能把它替代成已证明的全部要求完成。

要求级状态与任务级状态只按下表对应，不在展示层互换词义：

| 已绑定证据下的组合 | 任务级状态 | 说明 |
|---|---|---|
| 所有任务前置通过、全部必需命令完成、全部适用要求 passed | `complete` | 还需人工干预记录才可判断自主成功 |
| 至少一个必需要求／任务前置 failed，或必需命令明确失败 | `incomplete` | 即使其他项 unavailable，也保留已确认失败 |
| 至少一个必需要求 not_run，或预算内未完成已被确认 | `incomplete` | 未执行不等于该要求本身执行失败 |
| 没有确认的失败／未完成，但有关键前置或要求 unavailable | `unavailable` | 不能用已通过部分替代缺失部分 |
| 整次证据归属无效，不能确认属于冻结任务／运行 | `unavailable` | 不能拿别的任务证据制造本次成功或失败 |
| 某类别不适用 | 不参与要求汇总 | 显示不适用，不生成 passed 或额外分母 |

`TaskSuccessRate = complete 的任务数 / 冻结的计划任务总数`

同时公布 complete / incomplete / unavailable 的数量。未返回结果的任务留在计划分母中。跨 agent 必须使用相同任务集合、协议版本和成本边界；CI 对齐任务、已声明 CI 变体、非 CI smoke task 分层展示。

自主成功还要求 §7.5 的 runner 记录支持 `human_interventions = 0`。任务 complete 而人工干预记录缺失时，普通任务成功仍成立，自主成功为 unavailable；有干预时自主成功为否。Token 和无人值守总时间保留全部失败和重试成本，不把提前失败的低消耗解释为更高效率。

### 8.2 Build/Test 要求汇总

Build 汇总包含该任务要求的 `compile`、`package`、`install`、`native_compile`；Test 汇总包含规定的测试要求。文档与质量检查各自显示，仍属于完整任务必须满足的条件。

每个汇总沿用 §8.1 的三态规则。没有该类要求时显示“不适用”，不当成通过。Build 要求完成率的分母是要求 Build 的计划任务，Test 同理；必须展示各任务的构建终点和测试类型，避免把不同验收范围当成相同工作量。

这两个拟新增指标与 portable v1 的 `build_task_success`／`test_task_success` 不同，实施时必须使用新指标版本和清晰名称，不能复用旧字段名称后改其含义。

### 8.3 分组与条件通过率

对预先定义的要求类别 `g`，令 `G_g` 为包含该要求的计划任务集合。每个项目／任务在这个组里只计一次，不按模块、测试数或 JAR 数增加权重。

`GroupTaskSuccess(g) = G_g 中完整任务成功数 / |G_g|`

同一任务内某类要求涉及多个模块时，先按已冻结范围汇总该类要求结果，再计算项目级统计。组与组可以重叠，不能把各组数量直接相加还原总项目数。

要回答“加到这一项时是否容易坏”，另报告：

`ConditionalPass(g) = 前置已满足且该类要求 passed 的任务数 / 前置已满足且该类要求结果为 passed 或 failed 的任务数`

必须与该比例同时展示：符合前置条件的数量、实际开始的数量、not_run 数量、unavailable 数量，以及最终成功／失败数量。分母为零时为 null。前置关系未知的记录不能进入这一条件比较。

该条件比例只描述已到达且结果可判定的样本，不代替全组成功率。Agent 若更早失败，会造成后续可观察样本减少；因此比较 agent 时还要显示到达率，并单列双方都到达且可判定的配对子集。不能只挑存活样本比较条件通过率。

### 8.4 失败位置与证据强度

对单一路径记录第一个有确证的阻塞要求，以及因此未执行的要求。对并行／独立分支保留多个失败点，不强行指定唯一“首因”。日志时间上最先出现的错误不自动成为根因。

失败位置、原因类别和原因证据分别保存。例如：位置为 package，日志明确显示缺少插件依赖，可提出“依赖获取失败”的解释；若网络记录不全，就不能进一步断言根因是仓库服务故障。

继续披露 CI 模块范围、测试总数／非跳过数和案例身份可比性。缺失分母不评分，不回退成 1/1；多出来的测试总数也不自动说明能力更强。删除证据不能把 incomplete／unavailable 升级为 complete。

CI 测试基线必须显式区分 `reported_count`、`skipped_count` 和 `assessed_count = reported_count - skipped_count`，并保存原生证据能区分的 passed／failed／errors。来源只能提供合并 failCount 时保留 `failed_or_error_count`，不能伪造拆分；skipped 未知时 assessed 也未知。分别保留全部报告案例和非跳过案例的身份多重集，核验模块映射、重复发生及聚合计数。历史 Jenkins target 的 `executed_count`／`executed_ids` 实际包含跳过记录，必须先声明语义映射，不能直接充当 assessed 分母。Commons Net 的官方 557 reported／555 assessed／2 skipped 与本次 recorder 相符；555 与旧字段 557 的差别不是少执行测试。该检查是后续数据集参考任务准入的前置；[原文摘要及身份核验](../../../output/requirements-v2-live-validation-20260922/ci-count-audit.md)仅补充说明，不回写历史 target 或分数。

## 9. “层层加码”的实验方式

### 9.1 正式比较

所有 agent 执行完整冻结任务，以 task success、自主运行时间和全部 token 消耗作为主要比较。按要求标签分组解释结果，同时报告模块数量、项目规模、JDK、测试规模等可核验背景因素。

不按最终成功率倒推难度等级，不给 Javadoc、JAR、集成测试任意赋权。小组样本少时显示原始成功数／总数及不确定性，不为凑组放松任务或数据选择标准。

### 9.2 同项目增量要求消融

只有依赖与阶段边界已经核验的任务才进入这一实验。Maven 的 package 通常包含之前的测试阶段；显式文档目标还可能 fork 生命周期。不能把目标字符串的增加直接解释为只多做了一件事，也不能为了得到整齐阶梯而随意插入 skipTests。

优先审阅以下候选配对，只有确认实际执行差异后才登记为实验；它们不是已验证的可隔离结果：

| 候选配对 | 可能隔离的变化 | 必须核验／披露 |
|---|---|---|
| 同项目 `clean test` ↔ `clean package`，其他参数不变 | 打包及其必要准备工作 | 两边都保留测试；只有新增阶段的绑定已知时才能归因为打包集合，不能排除新增插件检查 |
| `clean package` ↔ `clean install` | verify 及本地安装所需的额外工作 | 两者之间不一定只有 install，可能还包含集成测试和 verify 检查；未能细分时按组合干预报告 |
| Sling 同一任务 `clean install ...` ↔ `clean install javadoc:javadoc ...`，profiles／属性不变 | 显式追加文档目标的依赖闭包 | 若 install 已运行文档或 Javadoc 重新触发别的检查，披露重复／组合工作；只有完整端满足其冻结任务 |
| CSV 完整 defaultGoal 去掉最后一个 `checkstyle:check` ↔ 完整 defaultGoal | 候选末尾 Checkstyle 检查 | 确认该目标没有通过其他绑定重复执行；DBCP 顺序不同，不能照搬同一前缀实验 |
| CLI `clean verify` ↔ 自身完整 defaultGoal | 文档、兼容性和多项质量检查的组合 | 这是多个要求同时增加，只能说明组合效果；原 smoke 与完整变体分别编号 |
| FreeMarker 已编译原生程序的固定环境副本，运行前 ↔ 执行原生检查 | 原生执行要求 | 仅属于固定环境诊断，不能单独证明 agent 完成了完整 setup |

任何带 `-DskipTests`、`-Dmaven.test.skip=true` 或其他排除／禁用检查的**新增消融变体**，都标记为 diagnostic-only，永不计入原完整 CI 任务的成功率，也不能与“无 skip 的正式组”合并。Curator 原 CI 已规定的首步 `-DskipTests` 继续作为完整多步骤任务的组成部分，不在这一限制下被错误淘汰；第二步测试义务保持不变。

每个变体保存独立任务 ID、改动说明与命令；相同 commit、初始环境、资源、缓存政策和预算。每个变体使用独立干净的尝试环境，不能让后续组继承前组生成的 JAR、报告或已安装产物。共享依赖缓存如有启用，必须对所有组采用相同政策。

每项比较只增加一个预先声明的要求或一个不可分割的依赖集合。若不能隔离，就明确报告为组合干预，不将效果归因到单个标签。改变目标生命周期或 profile 可能改变其他插件行为，需要对照实际任务图／原生记录核验。

分开报告两种实验：

| 实验 | 控制方式 | 支持的解释 |
|---|---|---|
| Agent 任务增量 | 每个变体独立运行 agent，初始条件相同 | 增加验收要求后，端到端完成率和成本如何变化 |
| 固定环境诊断 | 从同一准备结果的可复现副本执行不同验收变体 | 当前环境在哪项要求暴露不足；不能直接当作 agent 能力提升 |

随机化／平衡执行顺序，并保留重复尝试，减少网络、时间与随机性的影响。重复次数在预算和精度目标明确后预先登记；本设计不虚构固定样本量或效果阈值。

简化变体通过只算该消融任务通过，不计入原始完整 CI 任务成功。若新验证器向 agent 提供了更详细的错误提示，必须另做“相同验证结果、不同反馈”的配对实验；离线分类不会自动改变在线工具权限或提示。

### 9.3 分类反向指导下一批项目的分层抽样

20 项开发集的文档组至少四项，原生组只有 FreeMarker 一项。现阶段组统计用于描述与定位问题，不能据此推断总体性能或声称可靠的组间因果差异。

下一批项目应先按共同纳入条件建立候选池，再在未看 agent 结果前标注要求、CI 证据完整性和规模。维持已经约定的组织范围、Java／构建工具、活跃度等筛选规则；不因稀有组缺样本而静默降低条件。

预先登记抽样层与组比较目标。可按构建终点设置互斥基础层，再针对文档、集成测试、质量检查、原生等重叠标签保留有记录的抽样概率，并兼顾模块数、规模与工具链。样本量根据候选池可用数量、希望达到的估计精度／效应检出能力和运行预算确定，不为每类任意设固定数量或为凑总数选题。

稀有组可以过采样，但须披露选择概率；平衡样本上的未加权平均不能冒充自然项目分布的平均表现。没有足够合格项目时报告真实数量和描述性结果。新增评测集与已用于调试的 20 项开发集分离，不根据 SAG 是否成功来留删样本。分类由此同时服务于失败分析和论文实验的项目池设计。

## 10. 实施顺序与集成方式

1. **冻结元数据。** 明确 CSV/DBCP 的完整 defaultGoal；按有效 POM 自动生成 declared 产物期望，人工审例外，生成带来源的 requirements。未知保持未知。核验工作量按“自动解析模块／人工例外／证据待补”列出，不将约 92 个 scope 条目变成逐个手工猜测 JAR。
2. **共同证据与离线适配。** 先补齐两个 recorder 的工作树快照、运行时记录，以及 campaign runner 的有出处的人工干预计数；复用既有日志、报告、产物与 receipts 生成四态结果，增加同模块 fail-fast 推断。先回放归档；历史缺证据保持 unavailable，不追补 clean、Java 版本或人工干预零值。
3. **小范围验证。** 从 DbUtils、Commons Net、Sling Commons MIME、CSV/DBCP、Curator、FreeMarker 选择可用记录，覆盖无打包要求、安装、文档、多项默认检查、工作树敏感性、多步骤测试、原生要求。按行为覆盖选择，不按固定项目数凑样本；先跑 fixtures／归档回放，再进行必要的真实验证。
4. **统一离线评分。** SAG 和其他 agent 通过各自 recorder 导出同一证据结构，评分与分组使用同一规则。对完全相同的规范化证据，要求相同结果。不同证据能力产生 unavailable，不能以 agent 名称选择宽松规则。
5. **正式运行及增量消融。** 正式 SAG 运行一律同时带 `--acceptance-task-file` 和 `--ci-target-file`，以及固定 `--ref`；启动前验证文件摘要，启动后核对实际 run pin，禁止任务文件缺失时退回自由运行。原 smoke 项目使用真实的“CI 未匹配”目标记录，不伪造目标以满足参数。其他 agent 接收同一任务／目标摘要及要求，通过对应适配器保留相同绑定。共同证据前置通过后才启动 campaign；新增 skipTests 等变体仅作诊断。任何提示或执行策略修复另做消融，不能与评分口径变化一起宣称效果。
6. **经验证后接入输出。** 展示完整任务、Build、Test、文档／检查，以及可追溯的分项结果。只在规则、回放和跨 agent 一致性验证完成后讨论替代现有在线判定；旧协议和新协议分别留档。

具体落点：`AcceptanceTask` 继续负责不可删减的有序任务；`TargetRecord` 保存 CI 比较依据；SAG 回执读取器和 portable recorder 各提供证据适配；统一分析器负责要求结果和组统计。第一阶段可作为离线脚本与伴随 JSON 完成，不引入运行时工作流引擎或泛化 DAG 执行器。

现有 portable ZIP 保持 v1 不变。新 schema、适配器和文档组成新的包版本；跨 agent 正式比较需要统一新协议后重新运行，不能把旧 SAG 分数直接拼进新组表。

### 10.1 Campaign 启动前必须具备的条件

| 条件 | 可审阅的启动依据 |
|---|---|
| 任务、CI 目标及要求文件一致 | 实际 CLI 的两个必需参数、repo/ref、三份摘要与 run pin 的对应校验；不接受仅有调用方计划 |
| CI 测试计数语义一致 | reported／skipped／assessed 有原生来源和明确映射；对账案例与重复发生，不能混用旧 executed 字段与非跳过分母 |
| 工作树证据就绪 | 两个适配器都能写出边界快照及失败探测结果；未跟踪文件政策一致，session 目录在被测仓库外 |
| 运行时证据就绪 | 每次派发／重试有实际 launcher 探测；错误 JDK/Maven 的用例不能 complete |
| 自主性可记录 | runner 能在非交互协议下写出有来源的零或正数，异常／缺观测保持 unavailable |
| 新旧字段不混用 | requirements 成为新规则依据；旧 stages 和 v1 分数不参与新要求的判定 |
| 诊断与正式任务分开 | task ID、改动、预算／缓存政策及正式统计资格预先冻结 |

实现前的 [d3r2_campaign.py](../../../scripts/d3r2_campaign.py) 只在特定 variant／字段存在时追加参数。新增 `requirements-v2` 路径应对所有组强制校验并归档三份输入、固定 ref 及实际 run pin；旧 campaign 行为单独保留。启动检查失败登记为 campaign 配置／采集问题，不静默降级、跑出自由任务结果后再充作原任务，也不从已冻结计划分母中删掉该项。

## 11. 实现的验收条件

以下为实现必须满足的验证场景；已执行的测试、归档回放和仍未进行的真实验证在实现记录中分别列出：

| 场景 | 必须得到的结果 |
|---|---|
| CI 只要求编译和测试，未生成 JAR | 无打包义务；不因此判任务失败 |
| 要求主 JAR，只找到 test/sources JAR | 不能判 package passed；按原生失败／缺证事实区分 failed 与 unavailable |
| 聚合 POM 模块不生成 JAR | 按其实际要求验收，不套用主 JAR 规则 |
| 编译、测试通过，Javadoc 明确失败 | 保留前两项通过；文档失败；任务 incomplete |
| 编译失败，测试被明确跳过 | compile failed；test not_run；不记为测试失败 |
| 同模块编译失败、无 Surefire skipped 行，但满足四项 fail-fast 条件 | test not_run，引用 runner 规则和上游失败；不落成 unavailable |
| 日志截断、有效参数启用 fail-at-end／fail-never，或计划不可解析 | 禁止套用同模块快速失败推断；按实际证据保留 unavailable／模块结果 |
| 缺测试日志且终态丢失 | test unavailable，不能假定未执行或通过 |
| Curator 首步 skipTests，第二步正常运行 | 两步分别验收，不把第一步当测试通过或测试失败 |
| FreeMarker 只完成 JVM 步骤 | 原生要求未完成；完整任务不能 complete |
| 某个原生步骤失败但其他独立分支完成 | 各自保存证据，不以一个全局退出码覆盖全部子结果 |
| CSV/DBCP 命令未显式给 goal | 展开其 pinned defaultGoal，含全部文档与质量检查；旧 stages 不缩小要求 |
| defaultGoal 中 Javadoc 之前各项通过，Javadoc 失败 | 之前结果保持 passed；文档 failed；任务 incomplete；CSV 后续 Checkstyle 按 fail-fast 规则为 not_run，DBCP 的 Checkstyle 则保留之前的实际结果 |
| Agent 留下未跟踪文件，原生 RAT 报告指向该文件 | 有写入来源链时归为 agent 引入；不归为项目缺陷，不在重评分前删除该文件 |
| 两端 tracked 都干净，但 untracked 探测一端执行失败 | 不能将失败端补为空列表／干净；工作树证据 unavailable |
| 所有命令退出 0，但实际启动 JDK 不满足任务要求 | runtime_conformance failed，任务仍不 complete；版本来源不明则 unavailable |
| Gradle `--continue` 下一个任务失败，其他独立任务完成 | 各要求状态独立成立；不把所有后续任务判为 not_run |
| SAG 非交互 runner 的输入政策／完整记录支持零干预 | 写出有来源的 0；满足其他条件时可算自主成功 |
| 历史 SAG 记录没有人工干预字段 | 不回填 0；普通任务结果保留，自主成功 unavailable |
| 正式启动缺少任一必需任务／CI 参数 | 登记启动问题，禁止静默执行自由任务并计作正式比较 |
| 旧产物、旧 XML 或另一尝试的成功被混入 | 绑定失败，不能改善通过结论 |
| CI 缺模块／测试分母 | 对应比较 unavailable，不构造 1/1 或零测试基线 |
| 相同测试总数、不同测试身份 | 不宣称案例等价；新增红测不能被同数量旧红测豁免 |
| 删除要求或改变其必需性后沿用旧 task 身份 | 拒绝作为同一协议／任务汇总 |
| 缺少某组后续结果或只保留成功运行 | 计划分母不缩小；显示未到达与缺证数量 |
| 从同一源码归档交给两个 agent 适配器 | 规范化证据一致时，评分一致；名称不影响规则 |
| 简化消融任务成功，原完整任务失败 | 两条记录分开，原任务不升级 |
| 阶段分类仅离线开启／关闭 | 原始执行事实不变；变化只能来自新解释与证据可用性 |

## 12. 设计与实现交付边界

设计初稿仅交付本文。后续实现新增共同评估代码、录制器和伴随 JSON，不修改原任务命令、已发布 v1 ZIP 或历史结果，不以离线规则回放冒充新模型实验。新代码与准备材料的实际验证状态在实现记录中披露。

接下来的第一项实现交付应当是“20 项目 requirements＋有效 POM 推导的 declared 产物期望＋人工例外＋来源及缺证据清单”。在新 campaign 之前完成工作树、运行时、人工干预及任务绑定的共同证据契约，再做新评分与消融。下一批项目以这些标签进行预先登记的分层抽样；开发集的描述性分组结果不替代论文评测集。
