# CI requirements v2：实现、验证与启动条件

日期：2026-09-22。对应[设计文档](../specs/2026-09-21-ci-task-requirement-classification-design.md)。

用户要求的顺序是：先完成标准和实现，再对既有数据集重筛、补充 CI，最后运行模型实验。本轮未开始新项目采集或 SAG 模型实验。工作区改动尚未提交；原 portable v1 保持不变。

## 已落地的实现

| 设计要求 | 实现位置与行为 |
|---|---|
| 冻结要求与明确分类 | `scripts/build_benchmark_requirements.py`；20 项 sidecar 替代旧 `stages` 的分类权威，CSV/DBCP 展开各自 pinned defaultGoal；来源和缺口随条目保存 |
| 相同证据相同评分 | `src/sag/benchmark/requirements.py`、`evaluator.py`；共用四态结果、整体与 Build/Test/文档/检查结果、固定计划分母、重叠分组与条件统计，不按 agent 名称选择评分规则 |
| 工作树与运行时前置 | 两端保存开始、验收前后、结束边界的原始 Git 探测；按实际启动器验证 Java/Maven；失败探测不能补成干净或正确版本 |
| 命令内失败定位 | `native_evidence.py`；保留同一命令中已完成的编译／测试，单独判文档／质量失败；满足设计四条件时推断 Maven 同模块后续未执行 |
| 产物与测试证据 | 新鲜报告、主产物及安装坐标的真实字节和摘要；旧 JAR/XML 不能改善结果；声明产物按 POM，不能把 sources/test JAR 替代 main JAR |
| Portable recorder | `python -m sag.benchmark.recorder start/step/close`；外置记录目录、有序冻结步骤、完整原始日志、启动探测与证据封存 |
| SAG recorder | `sag_observer.py`、`sag_adapter.py`；在既有派发／回执边界采集，在 finalizer 后生成独立分析；原 sealed verdict 不被替换 |
| 正式启动限制 | `--requirements-file` 连同任务、CI 文件及完整 ref；campaign 对每个组检查归档摘要和实际 run pin；缺配置不得降级为自由任务 |
| 人工干预记录 | 事前声明的 unattended 协议、事件记录和结束封存；零只针对明示协议，历史缺字段不补零，宿主机绕过限制单独披露 |
| 时间与 tokens | 失败／超时也保留进程时间；`model_request_ledger.py` 在每次 harness 模型调用前落盘开始记录，之后分别保存成功、错误或中断；actor、advisor、压缩及重试的已知 usage 只计一次。campaign 优先读请求账本，历史 CSV 仅作缺账本时的回退，不将两者相加 |

使用说明见[共同评估协议](../../benchmark-requirements-v2.md)。实现不增加通用工作流引擎，也不改变 agent 的验收命令来获取通过。

## 元数据成熟度与例外

首轮 20 项标签的已知下界：compile 20、test 20、package 18、install 10、documentation 5、quality_check 16、native_compile 1。标签重叠，不应相加当成项目总数。FreeMarker 的源码与官方任务审阅补入了文档及许可证检查，但完整要求范围仍待冻结；这些标签不证明整项任务已经可评测。

有效 POM 属于声明来源，不等于执行成功。19 个 Maven 项目的 20 个步骤均已在隔离环境捕获有效 POM／settings，保留实际 Java、Maven、wrapper、profiles 和属性；FreeMarker 单独审阅 Gradle／原生任务。当前 DbUtils、Commons Net、CSV、DBCP、Tomcat Migration、Sling MIME、Sling OSGi、Tentacles、HttpClient、HttpCore、Commons JCS 共 **11/20 项完成定义冻结**，其余 9 项保留具体 `review_required` 缺口，不能作为全部就绪的 20 项正式 campaign 启动。DbUtils 有 8 项要求，Net 有 18 项，CSV／DBCP 各 23 项，Tomcat 7 项，Sling 两项各 19 项。新增 Tentacles 11 项、HttpClient 113 项、HttpCore 70 项、JCS 84 项；多模块要求数用于范围审查，不增加项目在成功率里的权重。

CSV／DBCP 保留各自 defaultGoal 顺序，以及 fork 内重复执行与顶层要求的区别。Tomcat 的测试夹具 JAR 不被增加为发布产物要求。Sling 的原始 HTML、选定 node／stage／索引、完整抽取日志及预先声明的 deploy→install 差异均被保留，不声称完成原始发布步骤。

自动产物推导覆盖 147 次有效模型出现；按项目与模块路径去重为 138 项模型。这些都不是 CI 模块分母。Curator 两步分别选择 13 与 9 项；同一模型跨步骤重复不能累加为项目规模。每个模型的默认产物、安装坐标、原始 POM 和人工例外均随清单留存。审阅进一步确认 Cayenne 内部调用 Gradle，Gson 的 smoke 含 shrinker／代码生成，不按顶层命令或模块名字猜测完整工具链。

完整材料见 [Maven 准备清单](../../../output/sag-benchmark20-requirements-20260922/preparation/STATUS.md)、[剩余 12 项 Maven 审阅](../../../output/sag-benchmark20-requirements-20260922/review-remaining-maven-audit/report.md)及 [FreeMarker 审阅](../../../output/sag-benchmark20-requirements-20260922/review-freemarker-audit/README.md)。两项 smoke 没有匹配 CI，与已有 CI 但缺完整测试池、已有范围但要求尚未冻结，分别披露。

合法的 `-f pom.xml`、`-f ./pom.xml`、`--file=pom.xml` 在两个 recorder 中都通过已跟踪 POM 与 pinned commit 的字节比对认证。未知覆盖、替换 POM 或不完整输入会使相应推断不可用，但不会改写或阻止原验收命令。

## 多模块与 wrapper 补齐

[本轮 20 项清单](../../../output/sag-benchmark20-requirements-validated-20260922/manifest.json)保留 11 项完整定义、9 项待审定义；不会把未解决项目从原计划分母删除。原始归档和之前已经运行的验证包均不替换。

- HttpClient／HttpCore 分别核对 9／6 个模型、320／196 次顶层原生目标，保留全部重复生命周期。Surefire 的第二次 banner 明确复用同配置的已完成测试，分别保留 8／5 个实际测试池，复用不是第二次成功测试。
- JCS 的 7 个模型冻结 84 项要求，覆盖 39 个打包文件和 46 个安装坐标。TCK 无生产源码不虚构生产编译成功，但测试编译、测试和主 JAR 仍须满足；SBOM、附加包和模块描述处理按真实作用记录。
- Jenkins 全 job 日志的命令提取绑定原始字节、准确边界、独立 build／index 和版本。Tentacles 的依赖附加 JAR 与安装 POM 均入要求；原发布步骤作为已声明变体排除。
- Wrapper 支持基于已审 Apache only-script 模板和固定源码输入，两端采集本次实际转发、版本和配置证据。未知 wrapper 不阻止原命令执行，但不能被假定为已认证的串行 Maven。
- [Whisker／RAT 完整审阅记录](../../../output/sag-benchmark20-requirements-20260922/review-creadur/README.md)补入可确定义务，同时保留 Invoker、AntUnit 等报告能力缺口。Whisker 的 155 个 Surefire 报告测试另有 10 个 Invoker 项目；RAT 的 1149 个 Surefire 报告测试（3 skipped）另有 12 个 Invoker 项目及两组 AntUnit targets。单位不同，不直接相加，也不拿旧 JUnit 总数替代全部 Test 义务。

交叉审阅增加两项防误报规则：review 明示未解决义务时不得升级为完整定义；同一逻辑成果的多个原生 binding 必须在运行时保持声明的相对顺序。中间 support 目标变化不影响绝对位置，不能借此忽略必要的先后关系。第二轮生命周期在较晚位置失败，也不能被回溯成第一次中间步骤的上游失败。

## CI 参考与条件统计的最后接线

`ci_count_semantics.py` 已接入导入器、要求校验、正式 CLI、campaign 启动检查和输入迁移。来源参考随着 campaign／attempt 复制，重新验证选定 URL/cell、独立 CI index 和原始报告字节。20 项逐项声明 available 或带原因的 unavailable，不从旧 executed 字段猜测非跳过分母。当前 **10 项具有可核验的选定 CI 测试计数，其中 8 项同时完成要求定义**。Whisker／RAT 的计数仅覆盖相应原生报告池，不能补齐其他测试类型的缺口。这两个数字都不是 SAG 成功率。

只接受 CI 计数可比任务的 campaign 必须预先声明 `require_ci_count_comparability=true`；真实未匹配的 smoke 任务可以保留 unavailable，但不产生 CI 比较分母。新层的 CI scope／count attainment 和 case identity equivalence 仍为 null；已有原始计数对账不冒充这些指标的实现。

条件统计新增独立的 `started=true/false/null`：原生入口已出现、但 XML 损坏或执行超时，仍可证明开始；显式跳过／满足条件的 fail-fast 才能证明未开始。按项目汇总 `started`、`not_started`、`started_unknown`，另列 `prerequisites_satisfied`。既有 `reached` 继续表示满足前置且最终 passed/failed 可判定的子集，不改分母含义。原生可执行文件需已验证来源及真实完成／超时记录，计划中的命令不能当作已开始。

## 归档回放

来源：[20 项兼容性回放 JSON](../../../output/requirements-v2-archive-replay-20260922/replay.json)。可重复运行 `scripts/replay_requirements_archives.py`，输出到新目录。

- 20 份旧归档均存在，原 verdict 保留 **17 success、3 partial**。
- 它们没有新协议事前冻结的要求身份、完整边界记录和干预协议，不能事后补发完整 v2 认证。20 项 `unavailable` 是协议证据缺失，**不是 20 次新的构建失败**；不能把 0/20 当作 SAG 新性能结论。
- DbUtils 归档有 39 份 XML、523 个声明测试及 523 个 testcase 元素。
- Curator 原始 XML 声明总数 694、testcase 元素 718；Jackrabbit 分别为 2170、2171。它们是原始存档观察，未证明相同调用范围、重跑合并方式或 CI 身份等价；后续不能只读取其中较有利的一个数。
- 回放不伪造新鲜度、不回填人工干预零值，也不从不存在的 CI 分母构造 1/1。

## 验证与剩余启动检查

最终主回归 **759 passed、1 skipped**（78.25 秒）；diff 检查通过。测试覆盖文档中的边界场景：文档失败保留先前结果、同模块 fail-fast、`--continue` 独立结果、错误 JDK、旧产物、工作树探测失败、跨尝试混合、相同规范化证据与计划分母。十一项元数据审阅及新 reactor 来源绑定的离线 fixture 随测试保存，不依赖当前机器的 output 目录。

独立审阅复现并修复了同一步、不同 module／test subtype 复用同一 XML 的误报：现在按物理报告目录检查重叠，包含 Maven 隐式目录；Gradle 要求显式 report_directories。明确 `No tests to run.` 记为未运行。已知下界中的单项 passed 仍保留，但未完整冻结的整类 Build／Test 不再显示 complete 或“不适用”。

便携包通过 Python `-S` 的独立录制／评分校验，未加载 SAG 第三方依赖。一个临时 Git 仓库与模拟 Maven 输出用于验证 start → step → close → score；缺失的 20 项计划槽位均保留，没有自动生成人工干预零值。这项检查不算真实 Java 构建。

随后又在一个 2 CPU／2 GiB 的隔离容器中串行执行三个真实 Maven 冻结任务，验证实际日志、XML、产物和工作树证据链：

| 直接执行校验 | 要求结果 | 测试报告／通过／跳过 | 产物证据 |
|---|---|---|---|
| DbUtils | 8/8 passed，任务 complete | 523／523／0；39 份 XML | 原任务止于 test，没有 JAR 义务 |
| Commons Net | 18/18 passed，任务 complete | 557／555／2；75 份 XML | 9 个构建文件＋10 个本地安装坐标 |
| Sling MIME | 19/19 passed，任务 complete | 8／8／0 | 主 JAR、源码 JAR，以及 3 个本地安装坐标；Javadoc 独立通过 |

容器与宿主机离线重评分结果完全相同，见[回放核对](../../../output/requirements-v2-live-validation-20260922/host-replay-check.json)。记录到的容器峰值内存为 1,233,252,352 字节（约 1.15 GiB），OOM 计数为零，结束时容器文件系统尚余约 322 GiB。容器已停止。这里只复用隔离的元数据依赖缓存，**未运行 agent、未调用模型，不算 SAG 成功率或成本实验**。

Sling 使用另外封存的[验证包与原始证据](../../../output/requirements-v2-sling-validation-20260922/sling-commons-mime/score.json)，宿主机重评分逐字段相同。该次峰值 968,396,800 字节（约 924 MiB），OOM 为零，磁盘可用 344,322,482,176 字节；运行结束后停止容器。之前 DbUtils／Net 的包、pin 和成绩不替换。

进一步[核对官方 CI 原始报告](../../../output/requirements-v2-live-validation-20260922/ci-count-audit.md)确认：两项的 testcase 身份及结果多重集与 CI 一致，没有重复。Net 的旧 `executed_count=557` 实际包含 2 项 skipped；它不能拿来当 `assessed=555` 的分母。新采集前必须核对 reported／skipped／assessed 的语义，不仅比字段名字或总数。

真实校验包、分数及其 canonical pin 已封存。最终元数据整理改变了 DbUtils／Net 来源文件的相对归档路径，因此完整 pin 不同；[兼容性检查](../../../output/sag-benchmark20-requirements-20260922/compatibility-with-live-validation-package.json)证明要求、任务、版本、顺序、产物期望和所指源字节均不变。已运行的结果始终引用原包，不换上新 pin。

另外执行 Tentacles 的真实 wrapper 校验，保留两次独立记录。第一次命令退出 0、2 个测试通过，但 recorder 把 Develocity 生成的 workspace ID 当成未知配置，11 项要求因此 unavailable；[原始分数](../../../output/requirements-v2-wrapper-validation-20260922/creadur-tentacles/score.json)未修改。依据固定扩展版本的已归档 JAR／class 审阅，将这个固定路径、固定格式的标识声明为运行状态，保留原始前后字节。其他文件、配置修改、错误内容、symlink、未知 producer 均不豁免。旧 review 未声明此规则时仍沿用严格闭包，不因重新导入而自动放宽。

修复后用新的干净源码副本和新冻结包重跑相同命令：**11/11 要求 passed，Build／Test／质量检查 complete；2 reported、2 passed、0 skipped**。主 JAR、依赖附加 JAR 和 3 个安装坐标均有本次证据，workspace ID 从不存在到生成的过程也被记录。宿主机用同一便携包重评分逐字段一致，见[验证核对](../../../output/requirements-v2-wrapper-v2-validation-20260922/host-replay-check.json)。峰值内存 987,123,712 字节，OOM 为零，结束时文件系统可用 342,851,022,848 字节；容器已停止。两次都没有 agent 或模型调用，不用于声称 SAG 改善了成功率。

最终交付为 [Validated v2 ZIP](../../../output/SAG_Requirements_v2_Validated_20260922.zip)，含 771 个校验文件，SHA256 `104cfde23bf2ec198179b7a6f1605322e7d1c722c4caef1a9fc6ef705725a67f`。[独立便携验证](../../../output/requirements-v2-validated-export-check-20260922/verification.json)使用 Python `-S`，验证源文件、录制、评分和 20 项缺失计划槽位；不伪造人工干预零值。导出时还发现误传 metadata 状态文件会遗漏补充准备清单，现已在写出前拒绝非目录输入；未使用的预备包标为 superseded，真实重跑只使用上述 Validated 包。最终准备清单包含 147 次模型出现、138 个不同项目／模块路径。

在筛选和实验前依次完成：

1. 完成代码与便携包验证，并明确各项目元数据审阅状态。
2. 只使用审阅通过、证据具备的任务做必要的小范围真实录制验证；与模型效果实验分开，不把 fixtures 当成真实项目结果。
3. 以此标准重筛已有项目池，补项目规模与要求标签；新组织扩展和 CI 身份补采另留来源、选择依据及排除原因。
4. 冻结论文数据集和协议后，用 5–10 项检查 SAG 全量数据采集。只使用 5.4-mini；大型任务串行，运行前和运行中检查磁盘与内存。

本轮已完成前两项。这里的完成指共同标准、来源审查流程、记录器、离线分析与验证交付；不表示原 20 项全部 campaign-ready，也不表示已经完成论文数据集或模型实验。后续先按同一准入规则重筛已有材料；9 项未完整定义的任务和其他证据缺口继续留在清单，不借新采集隐去。

## 证据能力边界

- **人工干预的零有明示范围。** 可选 `sag-unattended-v1` 协议在启动前冻结渠道、初始任务例外及“外部帮助禁止或须登记”的操作承诺。runner 在启动前创建事件账本，在进程结束时封存；人工停止计数，预定自动超时不计数，未知事件或观测缺口保持未知。共同评分器核对 manifest、政策摘要、开始／结束／事件字节及实际 run pin。这里证明的是明示实验协议内的零次干预；宿主机管理员绕过并未被技术监控。历史任务和未采用协议的新任务不追补零。
- **请求完整性不等于供应商账单完整性。** 新账本能区分 harness 请求已返回、错误、中断、仍在途以及 usage 缺失；失败后再调用会保留独立请求身份。`harness_requests_complete` 只说明此边界的记录完整。SDK／供应商内部尝试及失败请求的实际计费仍可能未知，因此 `model_calls_complete=false`、完整 token 总量未知，报告已观测小计和未知请求数。reasoning 已包含在 completion，不再加一次；没有改变重试策略。
- **时间与自主性分开。** 保留进程启动到观察到结束的耗时，包括失败、重试和预定超时；不把归档耗时加入。是否零人工帮助由独立协议证据决定，不能仅凭有耗时数字就声称自主运行成功。
- **RAT 的写入来源仍需生产者证据。** 归因 helper 已能核对边界差异、写入事件和原生拒绝报告；当前 SAG adapter 尚未自动导出完整写入来源链。没有该链时只能记录新增文件／来源未知，不能直接归因 agent 或项目缺陷。
- **支持的运行时约束按任务声明。** 绑定 launcher Java 主版本、精确 Maven 版本；声明 requires_native_image 时，再观测本次实际 JVM home 内的 native-image，普通 JDK21 或其他 JDK 在 PATH 上的程序不能替代。原生执行还绑定本次产生的二进制。没有新增 GraalVM vendor／补丁版本义务；内部编译器、测试 toolchain 与 launcher 分开，未采集时不声称已验证。
- **部分运行方式保持保守。** Gradle 的原生输出可逐任务解析，但实际串行性、wrapper 或配置输入未被观察完整时，不利用日志先后来推断前一任务已通过。fixtures 证明规则处理已知规范化证据，不等于所有真实 Gradle 配置已经具备采集能力。

这些边界应与元数据状态分开呈现：代码验证通过不替代项目要求审阅，要求文件存在也不替代真实录制验证。正式模型比较须使用双方实际能提供的共同证据；能力不足时保留 unavailable，不能改用宽松的 agent 专属规则。
