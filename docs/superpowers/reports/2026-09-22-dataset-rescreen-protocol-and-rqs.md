# SAG 数据集复审、规模分层与 ICSE 实验问题草案

日期：2026-09-22。状态：**基于已有采集的审计与下一轮预登记草案，不是已完成的新采集、复现或正式实验。** SAG 是支持模型执行 setup 的 harness；研究对象是其设计如何使小／弱本地模型可靠、高效、无人值守地完成 CI 定义的 build/test 任务。

后续离线复审已完成，参见 [已有归档复审报告](../../../output/java-benchmark-rescreen-20260922/REPORT.md)。下文保留初次读取旧快照时的状态；新的逐文件源码核验、任务要求标签和测试记录复核以该报告及其绑定证据为准。18 项完整案例记录仍不等于 18 项已验证的跨运行身份匹配。

## 1. 复用已有材料，先修正数据含义

本轮从 `output/java-benchmark-20260916/` 继续，不重新从零枚举或覆盖 v1。原观察日为 2026-09-16；部分 UTC 归档时间为 09-17。本报告于 09-22 重读并重算这些文件，**没有将旧快照冒充今日 GitHub 状态**。

| 粒度／状态 | 重算结果 | 能支持的结论 |
|---|---:|---|
| 候选仓库，按 GitHub repository identity 去重 | 315 | 七个命名空间的原始候选框；不是 315 个已合格 benchmark |
| `static_qualified` 仓库 | 96 | 静态来源／目标证据通过既有筛选；不是已在 agent 容器复现 |
| qualified reference tasks | 111 | 含同一仓库的多个任务／修订；不能当作 111 个独立项目 |
| canonical case-level / count-level 项目 | 18 / 78 | 每仓库所选 canonical 任务的证据能力 |
| 全部 reference tasks 的 case-level / count-level | 19 / 92 | 包括非 canonical 的替代任务 |
| 存在至少一份 case-level reference 的仓库 | 19 | Commons Net 的旧 Jenkins 任务是 canonical 18 之外的一个；不能把它与较新的 Actions 任务混用 |
| 已复现 reference tasks | 0 | `reference_replayed_in_agent_container` 全为 false |
| 已被 SAG 开发／既有实验触及的合格仓库 | 15 | 应标为 development-exposed；其余 81 个只表示未在检查的 manifests 中发现，不证明训练数据未见 |

其余候选：`target_inapplicable` 116、`needs_review` 66、`evidence_unavailable` 22、`population_excluded` 15；与 96 项合计 315。`target_inapplicable` 只否定已检查目标，不等于穷尽了整个仓库；API 限流或日志不可得不是项目不能构建。

原组织框是 `apache`、`google`、`microsoft`、`Netflix`、`spring-projects`、`spring-cloud`、`spring-io`。96 项分别来自 Apache 71、Google 11、Microsoft 4、Spring Cloud 5、Spring IO 1、Spring Projects 4。Netflix 没有进入当前合格集；不能把来源缺失解释成 Netflix 项目不适合 setup。工具链为 Maven 88、Gradle 8，明显不均衡，应披露而非人工复制 Gradle cell 来平衡。

还存在影响规模度量的缺口：canonical 96 项中，74 项记录为 `java_paths_complete`，15 项 `pending_exact_revision_tree`，6 项 `not_audited`，Fory 1 项 `java_paths_mismatch`。这些是已存审计状态，本次没有重新比较所有源代码包与 Git tree。未解决的项目可以保留候选身份，但不能把其源码规模当成精确的总体值。

## 2. 预登记新的筛选流程

原 `PROTOCOL.md` 明确说采集规则是在收集过程中迭代形成的，不能追称已经 preregistered。以下规则必须在下一轮正式 agent 结果出现之前冻结，并记录协议摘要、观察日及任何修订理由。

1. **沿用原候选框和全部排除／待查记录。** 保留 public、non-fork、non-archived、primary Java、stars 严格大于 200、默认分支最近一年有提交、Maven/Gradle、非 Android 等条件。重新查询时使用新的共同观察日；原历史快照保持不变。查询分页完整性、仓库重命名／转移以 repository ID 对账。
2. **以仓库、固定 commit、官方 CI task/cell/attempt 为单位。** 保留完整命令、默认目标、profiles、属性、JDK/toolchain、构建工具、工作目录和前置步骤。确定 canonical task 的规则先于 agent 结果固定。一个仓库的多个 cell 可作为配对任务，但不能增加独立项目数。
3. **按目标依赖判断适用性。** 排除所选目标必需的 Docker socket／嵌套 Docker／特权服务／无法提供的凭据，以及第二种必需生产语言编译链。仓库里的可选 Docker 文件、构建 DSL、Java 测试夹具或文档不是自动排除理由。存在官方单元测试 cell 时可单独收录其真实范围，不得新增 skip 参数制造可运行目标。
4. **复用已下载日志、配置、源码和报告。** 先验证旧文件摘要与任务绑定，再补缺失的有效 POM／Gradle task 范围、源码 tree 对账、CI reports 和 requirements。证据过期但已完整归档不等于不可用；无法获得的补丁版本、测试身份或运行环境必须保持 unknown。
5. **证据能力与执行合格分开。** `static_qualified` → `requirements_reviewed` → `reference_replay_verified` → `campaign_ready` 是分别有来源的状态，不能互相代替。所需检查或产物尚未确定时保留 `review_required`，不能减少要求使其通过。
6. **规模、语言、依赖和 scope 的排除不看 SAG 结果。** 项目在预算内失败是结果；临时网络中断、harness 错误、OOM、预算超时分别归因。不能删掉难项目或改用更容易的 cell 来提高成功率。
7. **允许有理由的组织扩展，但单独登记。** 用户已允许扩展；本报告不选择新组织。应先声明生态／维护方准入理由、官方归属来源、与既有项目是否重复、统一检索规则和资源预算，再穷尽该组织的同规则候选。保留 `original_frame` / `expanded_frame` 标签并做敏感性分析。不能逐个挑有绿色 XML 的项目后声称代表整个生态。

约 100 个项目是预期，不是配额；约 50 个拥有完整 testcase identity 是采集能力目标，也不是可降低标准的达标数。已有候选全部处理完或达到事先冻结的采集工时／存储预算后停止，报告真实数量及未完成原因。若以“找到 50 个”停止，将选择偏向更早、更容易取到报告的仓库；因此必须报告完整 ledger，避免这种隐性停止规则。

## 3. 把约 50 项 case-level 目标变成可核验条件

优先补原 78 个 count-only canonical tasks 的原生 JUnit XML／完整 Jenkins testcase API；也审阅既有替代 cell。Commons Net 已有一个合法替代 reference，但其 commit、JDK 和命令与 canonical task 不同，选择它必须重新冻结任务身份。

Case-level admission 需要同时成立：同 commit/cell/attempt；完整报告范围；可稳定区分 module、suite/class、method、参数化 invocation 及必要执行上下文；所有最终结果与 suite/phase 总量对账；重复运行／重试与最终 testcase outcome 分开；报告原文、来源和字节摘要可追溯。只打印 `Running TestClass` 或只有 “Tests run: N” 不满足方法级身份要求。显示名重复时不能静默去重；无法区分的参数化身份必须披露并降低相应比较能力。

不要求 count-only 项目假装有 case identity。保留两个报告面：所有满足共同任务条件的项目用于端到端 task/build/test 成功率；case-level 子集才支持案例集合的覆盖／遗漏比较。需要同 revision 和同 task 的 case universe 才可计算覆盖；测试数相等不证明身份相同，超额测试也不自动意味着更强。额外的 CI 重试、配置重复和 skips 均单独披露。

对于以约 50 项 case-level 子集作为论文主集的方案，还应同时公开完整候选和 count-only 伴随集，并分析组织、工具链、规模、要求标签的纳入差异。否则评价的可能是“愿意发布 testcase 报告的项目”，而不是所声明的 Java 项目总体。

## 4. 规模分层不替代任务难度和资源测量

**主要分析保留连续值**：固定 commit 的 Java 源文件／物理行数、目标模块数、CI testcase 记录数、原始 CI job elapsed、模块与工具链结构。源码统计必须注明是否含 tests/examples/generated fixtures；模块显示标签数不是 JAR 个数，物理行数不是语义 SLOC，CI job elapsed 不是本地 agent 耗时。

为了抽样／表格可读性，可在最终合格池冻结后按 Java 物理行数的 Q1/Q3 定义相对小型（≤Q1）、中型（Q1 到 Q3）、大型（>Q3）。选择四分位边界是为了得到尾部及中间部分的可比较描述，**不是固定工程难度阈值或准入门槛**；保留并列值，披露实际每组数量，规模缺证据单列。跨新旧池比较时不能一边改边界、一边把同标签当作绝对同等规模。推断分析仍报告连续规模及对另一种规模指标的敏感性。

旧快照未经新的 source 修补前仅能作描述：96 项 Java 文件数中位数 643，范围 2–9,725；Java 物理行数中位数 103,183.5，Q1=46,913.75、Q3=366,080.75，范围 33–2,018,915；模块数中位数 9，范围 1–438。这些不是新 cohort 的最终分层边界，也不能用来预测内存峰值。

要求标签直接复用 `requirements-v2`：compile、package、install、unit/integration/native test、documentation、quality_check、native_compile；runtime/worktree 是所有任务共同的前置条件。标签可重叠，不加人为难度权重。CSV、DBCP 的 defaultGoal 包含完整质量检查／Javadoc；不能继续用旧 `stages=[build,test]` 描述。`pom` aggregator 不要求主 JAR；jar/bundle/war 的产物期望由有效配置推导，特殊 packaging 及 shade/classifier 例外需审核。

抽样采用“基础构建终点＋规模”分层，并对文档、质量检查、集成测试、原生和 Gradle 做显式覆盖；不把每个交叉格强制凑成相同数量。若要稀有类型过采样，预先记录选择概率，报告分层与总体估计，不能把平衡子集均值冒充自然分布均值。样本量最终由可用项目、精度／检出目标和预算确定，不按观察到的 SAG 胜率追加。

## 5. 先行 5–10 项工程验证候选

下表是在旧 96 项中按行为覆盖选择的**候选**，不是已就绪的运行清单。建议前八项构成首批；第九、十项只有在更小任务完成共同证据验证、资源预算登记后才加入。八项的理由是覆盖列出的八类验证情景，不是统计功效声明；若两个情景被一项充分覆盖，可缩减，若 readiness 不成立则登记 pending，而非偷偷换掉失败项目。

| 项目 | 候选身份（旧 dataset task ID） | 主要验证情景 | Java 文件／模块 | 原 CI job 秒数 | 证据 |
|---|---|---|---:|---:|---|
| Commons DbUtils | `2f1ae1ca232dd52a258f` | Maven test-only，不强加 JAR；case 身份对账 | 96 / 1 | 133.661 | case |
| Commons CSV | `4ff17df30f0451053d38` | 无显式 goal 的重 defaultGoal；尾部 Checkstyle | 56 / 1 | 63 | count |
| Commons DBCP | `0c038995886f1aa75239` | 与 CSV 不同的 defaultGoal 顺序；Javadoc 尾部失败定位 | 147 / 1 | 203 | count |
| Gson | `e5b9ad5bb459b283401b` | 多模块、多步骤目标；不得沿用旧 smoke SHA | 264 / 8 | 123 | count |
| Google Java Format | `c16d98e10b7ec7b30d8d` | install→test；JDK 激活的模块／插件范围 | 84 / 3 | 78 | count |
| Calcite Avatica | `fb5c244128011eb99445` | Gradle build+javadoc；NO-SOURCE 与执行任务分别判定 | 316 / 13 | 288 | count |
| Spring WS | `b557e184669d9d1b57d7` | Gradle check、构建与测试不同 JVM、case 身份 | 922 / 8 | 93 | case |
| Commons Math | `6339d9e6b6bf6f933521` | 多模块 install／declared artifacts；deploy 投影需回放核验 | 1,088 / 14 | 898.880 | case |
| POI（扩展候选） | `6c25a9caa7e20fffab7f` | 大型 Gradle 与大量 testcase、串行资源验证 | 3,795 / 10 | 1,598.577 | case |
| Curator（扩展候选） | `8f689a73b7fa5a346d7e` | 官方首步 skipTests＋第二步必需测试；长耗时任务 | 708 / 13 | 3,188 | count |

数字来源是对应归档的选定 CI job，包含 CI 自身环境／准备成本，**不是本地运行预算或预计时长**。完整 SHA、官方 URL 和 counts 在伴随 snapshot JSON 中。Commons Math 的 install 是已声明非发布变体，静态审核不等于执行等价性已验证。

FreeMarker 在旧 315 中仍为 `needs_review`；Sling Commons MIME 不在原 315 候选框。因此两者适合继续作为旧开发集的 native／文档验证夹具，但本轮不能直接计入新论文合格集。Commons Net canonical 是 `b4ceda…` 的 JDK 26/defaultGoal，而旧 20 项任务是 `ac1ca…` 的 JDK 17/install；Gson canonical 是 `854c8255…`，旧 smoke 是 `b3f4ca…`。禁止跨这两类任务借用 counts、环境或成功结论。

所有 pilot 运行后纳入 development-exposed 清单。用于调整代码／提示的 pilot 结果不作为未见评测集的确认性证据。若保留在最终全量表中，必须分层披露，并另报未用于调整设计的 holdout 结果。

### 运行门槛与资源策略

- 本轮只使用用户指定的 **gpt-5.4-mini advisor**；actor 型号、reasoning 设置、provider、预算、随机种子和模型版本另外完整冻结。不启动 Terra 或其他 advisor 对照。模型数据发送权限按当前主任务已有授权执行，本报告不发请求。
- 启动前须具备原 task、CI target、完整 requirements 及摘要；实际 run pin 一致；recorders 的工作树／运行时／原始日志完整；评分 adapter 测试通过。定义未审核或收集未就绪时不以自由运行替代。
- 首批先串行；大型及尚无内存／磁盘测量的目标始终串行。调度不能仅依赖 Java 行数或测试数。先记录 Docker 分配内存／CPU、宿主可用磁盘、冷缓存、依赖下载及保留证据需求；容器内记录实际峰值与 `OOMKilled`。
- 资源预算依据实际机器和已有 reference/pilot 测量登记。下一任务启动条件为：可用磁盘足以覆盖其输入、允许保留的输出以及事先登记的宿主保留空间；可用内存与容器上限足以覆盖已知 JVM/工具链需求。未知上界保持资源预检待定，不填随意 GB 数。应用到所有比较 arm 的规则相同。
- 自动超时／磁盘预警可以终止尝试，保留不完整结果、时间、tokens 和原始证据，不按成功项目重算分母。先归档并核验哈希再清理由本 campaign 拥有的容器；不能删除其他工作的容器或将缓存预热只给一个 arm。
- 当前 runner 可以证明受控输入通道关闭，但无法完整监测外部 Docker／文件干预。因此现有总体 `human_interventions` 仍可能为 null；不可把“无人输入日志”自动补 0，也不可先声称 autonomous success。若正式论文使用零人工干预指标，需先完成受控运行／外部干预记录边界，或明确报告该指标不可得。

## 6. 三个 ICSE RQ 草案

**RQ1 — To what extent does SAG enable small or weak local models to autonomously complete CI-defined build and test setup tasks?**

研究 harness 对任务完成的作用。对相同 actor、相同固定任务／环境／工具权限和资源预算，比较 SAG 与明确实现的通用 coding-agent harness；可另给一个强模型基线作为成本／能力参照，但不得把更换模型的效果归因于 SAG。主结果为完整 task 成功率；同时报告 requirement-v2 Build/Test、文档／质量检查、未完成及证据 unavailable 的数量。按原定分母分析。无可靠人工干预观测时自主成功保持 unavailable，普通 task 成功不受伪造零值“补足”。同一仓库的任务与重试是相关观测，统计单位及置信区间应按仓库聚类。

**RQ2 — What success–cost trade-offs does SAG achieve for unattended CI setup, compared with alternative harnesses using the same local actor?**

衡量达到同一任务标准需要多少端到端墙钟时间、actor/advisor/summary/retry 的全部 tokens，以及人工干预。失败、超时和重试成本全部保留；提前失败不能被解释为高效率。分别展示成功率、所有尝试成本、双方均成功的配对成本，并清楚标注最后一项存在条件选择。不同模型 tokenizer 下 token 数不是统一计算成本，需分别列模型级 tokens、计价成本（若适用）、运行硬件与时间。实际 CI job 时间是背景信息，不与 agent setup 时间直接作因果速度比。无需强行压成一个加权总分。

**RQ3 — Which SAG design mechanisms account for improvements in task completion and efficiency, and under which CI requirement and project-scale conditions?**

用预先声明的配对消融检验 actor/advisor 协作、phase 引导、physical verification 及结果反馈／上下文提供。每次保持 evaluator、固定任务、actor、预算、输入证据一致；advisor 消融需同时报告额外调用成本。关闭在线 verifier 引导时仍保留独立末端评测，不能拿“更容易报成功”的版本当作成功率改善。反馈细度与执行权限分开控制；无法隔离的组合改动明确按组合报告。按先于结果冻结的 Build/Test／文档／质量／原生标签和连续规模分析故障位置，检验机制适用范围，而非事后从失败项目挑选故事。

这三个问题共同针对“弱模型＋SAG harness 是否可作为 CI 中可靠且经济的 setup 组件”，不把模型排行、case-count 更多或数据集分类本身当作 SAG 的主要贡献。当前单次 pilot 只验证管线与机制可测性；正式效应大小和重复次数需要 pilot 方差／配对信息、预先规定的精度目标及预算后确定。

## 7. 来源、摘要和重算方法

主要来源：[`PROTOCOL.md`](../../../output/java-benchmark-20260916/PROTOCOL.md)、[`dataset.json`](../../../output/java-benchmark-20260916/dataset.json)、[`candidate-ledger.json`](../../../output/java-benchmark-20260916/candidate-ledger.json)、[`validation.json`](../../../output/java-benchmark-20260916/validation.json)。要求定义参见 [CI task requirements design](../specs/2026-09-21-ci-task-requirement-classification-design.md)。

本次重算保存于 [snapshot audit JSON](2026-09-22-dataset-rescreen-snapshot.json)：含全部来源 SHA256、原始状态计数、组织／工具／规模统计、10 个 pilot 候选的完整固定身份与官方链接。算法是：按 `canonical_task_id` 将 96 个 projects 与 reference_tasks 连接；canonical 计数每仓库一次；“存在 case reference”对所有 reference_tasks 的 repo 去重；source coverage 使用该任务已有审计字段；四分位数为 `statistics.quantiles(values, n=4, method='inclusive')`。本报告的归档审计没有重新执行 reference build，也没有逐个重新证明旧日志解析器的全部结论。

| 文件 | SHA256 |
|---|---|
| dataset.json | `b6313ae09ac73ab3904524f42959d125f6a35db39fb1ca7496d99f45c8dbe74b` |
| candidate-ledger.json | `8e449335f8db9a31dc5097b6af027fb6bd6dc0e9d0fffd2321c6bbca61c82a71` |
| PROTOCOL.md | `ecba2ef6b8da2eb54684e82a991c821859283d93e5f2355a967b43f98c626bb9` |
| snapshot audit JSON | `e442f5f45cc1c5e414e20b2e18f04b0bf525fafd04d5e7996a4a2fa3d1b2819c` |
