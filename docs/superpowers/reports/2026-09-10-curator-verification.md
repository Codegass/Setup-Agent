# Curator：原生产物和校验结果为何不一致

固定任务为 `apache/curator@88dee99a85921bd0f20955e1d7efbaf896fc17f3`。以下记录原 23 项目消融的三组尝试，均已结束；这些观察不改写冻结组的原始结果。

## 已观察到的反馈与动作

1. Agent 在 Build 阶段执行 `./mvnw clean install -DskipTests`，随后执行官方测试命令 `./mvnw verify -pl '!curator-test-zk38,!curator-test-zk37,!curator-test-zk36,!curator-test-zk35'`。
2. 第二条命令于 2026-09-10 18:53:19 UTC 输出 `BUILD SUCCESS`，耗时 53:08，完整 Reactor Summary 有 9 行成功记录。宿主随后观察到正常退出并发布终态回执。
3. 18:54:37 的结束声明被校验器拒绝：期望五个 `.bundle` 文件而报告缺失。容器实际保有五个非空 `.jar` 文件；Curator 使用 `maven-bundle-plugin` 5.1.4，相关子模块声明 `packaging=bundle`。
4. Agent 如实说明产物校验冲突，并以 partial 结束 Build 阶段。进入 Test 阶段后，Advisor 仍把较早返回的 verify `unknown` 当作最新状态，要求查找或补跑测试证据。18:57:47，Agent 再次执行相同 verify 命令。

可以确认误报、过时的反馈与重复调用这条观察链。不能将重复调用只归因于某一条提示，也不能据此宣布整项任务或 CI 对比达标。第一次 verify 的七条最终模块汇总合计 718 项、6 项跳过、1 项 flaky、0 项最终失败；这是控制台诊断，不能替代完整案例池。

同一官方 verify 调用的原生日志耗时为 51:00，SAG 第一次成功 verify 为 53:08；整个强制计划尝试则为 113:35.72。前两者是同 SHA、同命令的一次描述性对照，硬件、缓存和 flaky 用例不同，不能推定稳定性能优势。它说明软件执行本身已有接近官方时长的成功记录，而重复执行及其后续终止影响了整个尝试的完成结果。

## 两处已复现的判定错误

- **文件扩展名：** 物理校验器直接把 Maven packaging 拼成文件后缀。Felix bundle 以及 Maven plugin/EJB 的主产物是 JAR；候选现在对这三类使用 `.jar`，并继续检查确切文件是否存在。其他 packaging 保持原有行为，没有加入通用插件解析框架。
- **日志行分类：** 范围解析器把五行 `Building bundle: /…/artifact.jar` 误认为新的无序号 Maven 项目，产生 `project_headers_span_builds`，丢弃 9 模块范围。候选将此类归入产物生成消息；真正的额外项目、重复构建和未闭合摘要仍保持拒绝。

Maven 区分类型和扩展名，见 [Maven artifact handlers](https://maven.apache.org/ref/3-LATEST/maven-core/artifact-handlers.html)；bundle 产物为 JAR，见 [Apache Felix Bundle Plugin](https://felix.apache.org/documentation/subprojects/apache-felix-maven-bundle-plugin-bnd.html)。

## 后台终态的读取

同一条 verify 的后台终态已经发布到宿主授权的任务账本，但 Advisor 只读最初的工具返回，因此仍显示 pending。候选现在按 job、run、工具和目录匹配原有账本，重新核对终态回执，再把正常退出或失败的事实交给 Advisor。它不生成第二次工具结果，不把退出零等同于测试完成，也不自动重跑命令。

固定任务校验同时支持这类已完成的 Maven/Gradle 后台调用：两份授权账本、回执身份、契约以及完整日志的 SHA-256 都必须匹配。同步工具结果也增加原始输出与回执哈希的一致性检查。日志缺失、错绑、内容替换或未发布的终态均保持 unavailable；只有展示摘要变化、原始输出仍相同的情况不受影响。

## 项目 wrapper 的实际优先级

工具实际选择了 Maven 3.9.9，而源码 wrapper 固定 3.9.4；Agent 收到的工具输出披露了这个选择，模型自己的说明也承认差异。原因是公共工具链解析器虽收到 `prefer_wrapper=True`，仍把环境中已激活的安装排在 wrapper 之前。Curator 的 `[3.9,)` 要求同时允许两个版本，不能解释为什么忽略 wrapper 偏好。

候选只调整这两种来源的相对优先级：已要求且可用的项目 wrapper 优先。禁用 wrapper、显式 exact／preferred 版本选择以及不兼容版本的过滤继续生效，没有增加执行门槛。Maven 与 Gradle 共 10 项解析器回归中，修改前 4 项偏好用例失败、6 项显式选择用例通过；修改后相关工具链、环境和实际 runner 路径测试共 226 项通过。

## 收尾误报与仍然存在的证据冲突

第二次 verify 于 19:43:36 因可用等待预算耗尽而被终止，exit 15。19:43:46，控制器先发布正常的 `job_settled`，随后又对同一 job 报 `job_terminal_unpersisted`。取消流程恢复了 Docker 记住的全部当前运行句柄，其中也包含早已持久化的任务，临时句柄读取器据此误报写入失败。候选在临时任务收尾时重新核对授权账本和完整进程身份，将同一任务交给原有持久化路径处理；缺账本或身份不同仍保留原来的未知／未持久化判断。10 项新增回归中，4 项正常终态用例在修复前失败；修复后相关测试 124 项通过。修复此误报不会把 exit 15 改成成功。

原尝试最终为 failed，本地 Build 证据 success、1375 个 class，本地 Test unknown；三个原生命令退出码依次是 0、0、15。不能把这个 unknown 展示为实际运行了零个测试。

第一次成功 verify 的 121 份已校验哈希的 XML 共有 718 个 testcase，结果为 711 直接通过、6 跳过、1 flaky；其类名／方法名多重集合与官方 API 一致。两边都有 16 个重复名称，尚不能用这两个字段构造逐执行唯一身份。SAG flaky 是 `TestTreeCache.testKilledSession`，官方 flaky 是 `TestLeaderSelectorEdges.flappingTest`，不是同一个用例。

其中 TestTreeCache 的 XML 声明 `tests=1`，却含 25 个 testcase，导致全部 XML 声明总数为 694。原始日志保留了先运行 25 项、再重跑 1 项的过程。它与实际 Surefire 3.0.0-M5 的已知重跑计数问题 [SUREFIRE-1903](https://issues.apache.org/jira/browse/SUREFIRE-1903) 症状相符；这是归因线索，不是允许忽略任意 XML 冲突的规则。`flakyFailure` 的含义见 [Surefire 重跑说明](https://maven.apache.org/surefire/maven-surefire-plugin/examples/rerun-failing-tests.html)。当时的候选仍保留该计数冲突；后续最小重现证明了生产者的重跑行为，下文记录新的解析设计。官方范围与 CI 准入仍独立检查。

## 验证与待处理范围

完整接口组的第一次 verify 于 20:08:00 UTC 以 exit 1 结束。Surefire 明确报告 `TestReconfiguration` 的 fork 异常结束、子进程 exit 2；这不是 OOM 证明。原工具将 42 条含 `.java:行号` 的异常堆栈误识别为编译错误。Agent 实际收到 `output_4d875bdc089c` 的“Compilation errors found: 42”及 244/244 测试统计，随后搜索原日志和 XML，在阶段总结中正确识别出 Surefire 进程异常，并于 20:11:17 再次执行原 verify 命令。该次重复执行发生在原生命令失败后，与强制计划组成功后的重复执行分开归因。

候选排除 Java 堆栈帧，并在明确 fork 诊断存在时返回既有 `MAVEN_EXECUTION_ERROR`，提示测试 JVM 未正常完成。它不从进程退出猜测 OOM，不添加重试限制，也不改写已报告测试数。原始完整日志重放的编译行计数从 42 变为 0，244 条控制台统计及 exit 1 保持原样。11 项新增回归在修改前失败，修改后 91 项相关测试通过；原始片段按 LF 行号和 SHA-256 绑定，见 `tests/fixtures/maven-failure-feedback/source.json`。

进一步逐模块复核发现，244 是原工具显示的最后一个模块汇总；此前 curator-test 和 curator-client 另有 2、27 项。新版 Maven 把 `maven-surefire-plugin` 标题缩短成 `surefire`，旧解析器没有据此分开调用边界，所以后一个模块覆盖了前面的合计。候选同时支持新旧标题，原始日志诊断合计恢复为 273 项，其中 1 项 flaky 已包含在内。6 项边界回归中修改前 2 项失败、4 项既有行为通过，修改后相关测试 97 项通过；XML 回执依然优先。前一项编译错误分类修复没有改变计数，这一项单独修复诊断汇总；两者都不把 exit 1 或不完整范围变成成功。

新增 10 项回归中，修改前 8 项失败，两个既有 jar/war 行为用例通过。修改后，相关物理校验、Maven/Gradle、范围和后台任务测试合计 292 项通过。33 行精简样本保留实际项目头、bundle 消息和完整摘要；完整日志 SHA-256 为 `2095840da515ec65ed1d427d5ceb14b8955105326e975becd7e018c759b2ef35`。

后台终态及输出绑定新增 20 项回归，相关测试合计 168 项通过。加上 wrapper 优先级、取消收尾和 Maven 失败反馈修复后，全部候选改动的单次完整 Python 测试为 7747 passed、32 skipped、44 warnings，124.67 秒。该修复尚未经过独立真实项目回放，不能用测试数声称 Curator 已达到 CI 目标。官方目标数据的补充审计仍单独记录。

实验材料保存在主工作区 `logs/verification-23-20260910/curator-live-observation-20260910.json`、`curator-completed-verify.log`、`curator-xml-retry-conflict.json` 以及强制计划组的原始 `control_events.jsonl`。


## 完整接口组：成功重试之后的控制器误报

本组 21:09:28 UTC 结束，原生命令退出码依次为 0、1、0，耗时 5068.69 秒。第二次 verify 于 21:05:36 输出 BUILD SUCCESS，原生耗时 54:10，9 个 reactor 模块成功。Agent 随后准确引用成功退出和 121 份测试报告，但 Test gate 保留了首次 verify 的运行错误，并因修复记录写入失败而关闭阶段。

三个原因已由保存的回执、控制事件与 main.log 分别确认：

1. 两次 verify 的原生 argv 与 cwd 相同，第二次只是增加超时、补全 system/source_command 并从 Build 进入 Test。旧恢复分组却纳入阶段 domain 和公开参数的完整拼写，将同一任务拆成两项。候选只在契约与原生 argv 匹配之后按 run、SHA、原生工具、动作、cwd、完整 argv 和其他非命令参数比较；Python 的多命令事务分组不变。环境变化、未知参数、缩小测试选择、缺少绑定均不能借这次修复消除旧失败。
2. gate 将 121 个 XML 路径复制到最多 64 条引用的 ControlAssessment，main.log 两次明确记录 `control assessment evidence_refs are invalid`。校验在写入前失败，不是磁盘故障。候选用 gate decision 引用完整记录，修复上下文只携带 assessment 与 decision 两个引用；原 gate 的完整路径仍保留。assessment 身份也包含 decision，避免不同判断说明引用不同 decision 却碰撞同一个 append-only id。回放继续接受完整的历史直接引用形式，不能接受任意子集或其他 gate。
3. 回执因 XML 冲突未给出可信 testcase rows，旧摘要把空 rows 说成 0 reported results retained。Agent 在收到这一反馈后提交零测试，密封数字仍保护了 raw 718。候选在这种情况下返回空计数与明确 unavailable，不把没有可信统计解释为没有执行测试。

第二次 verify 的 121 份 XML 均已核对快照哈希：718 个 testcase 中 711 通过、6 跳过、1 个 flaky。TestReconfiguration 的 tests 属性为 1，实际有 23 个子 testcase；声明总数因此只有 696。旧密封汇总把 flaky 并入 passed，显示 712 通过。冻结版本据此保留声明与叶子冲突；后续解析器修复没有修改官方目标、XML、原始失败或 CI 分数。

用这次已归档的三个回执离线检查消费者，旧摘要同时保留失败与成功 verify，state=failed、reported_executions=0；候选只保留最新同范围 verify，state=unknown、reported_executions=null。该版候选的 unknown 来自 XML 计数校验不兼容，后续修复另行验证。这只是消费者诊断，不是新的真实项目尝试或对旧证据的重新授权。

引用修复的 8 项新增测试覆盖 0/1/64/121/1000 引用、历史回放、无关 gate 拒收和 append-only 身份；151 项相关测试通过。恢复修复的 12 项新增测试中旧版 7 失败、5 已通过；182 项相关测试通过。合并候选的完整回归为 7767 passed、32 skipped、44 warnings，126.26 秒。真实项目回归尚未开始，仍等待冻结的 57 次正式消融结束。


## 最小生产者消融：校验器误拒了合法的重跑报告

两个独立小实验共 10 次 Maven 调用，使用 Java 17.0.20、Maven 3.9.4、JUnit 5.6.2，分别验证 Surefire 3.0.0-M5 和 3.5.3。每组只有 3 或 5 个固定测试；没有 Agent，没有新 Docker 容器。临时 JDK、Maven 和私有依赖缓存均已删除，源码、命令、完整日志、XML 与文件哈希保留于主工作区 `logs/verification-23-20260910/curator-surefire-repro-v1/` 和 `curator-surefire-repro-v2/`。运行平台是 macOS aarch64，与 Curator 的 Linux amd64 不同，不用于性能对照。

| 实际执行 | 原生退出 | XML 声明 tests | testcase 数 | 最终结果 |
|---|---:|---:|---:|---|
| 3 个测试，不发生失败 | 0 | 3 | 3 | 3 通过 |
| 同样 3 个测试，一个失败，关闭重跑 | 1 | 3 | 3 | 2 通过、1 失败 |
| 同样 3 个测试，一个失败后重跑通过，两版插件均如此 | 0 | 1 | 3 | 3 最终通过，其中 1 flaky |
| 5 个测试，两个分别在第一次和第二次重跑通过，两版插件均如此 | 0 | 1 | 5 | 4 最终通过，其中 2 flaky；1 跳过 |
| 同样 5 个测试，再增加一个持续错误，两版插件均如此 | 1 | 2 | 5 | 3 最终通过，其中 2 flaky；1 错误、1 跳过 |

声明数覆盖最后一轮重跑的子集，子 testcase 则保留全部逻辑用例。候选据此增加一个有限解释：仅识别带官方 Surefire schema 标记的叶 suite，用 `flakyFailure/flakyError` 和 `rerunFailure/rerunError` 核对重跑次数；只有声明数等于最后一轮的实际子集时，才接受这种差异。普通报告、外层 testsuites、任意其他差值、无失败却存在失败重跑、flaky 与最终 failure/skipped 并存，仍然拒绝。

每个逻辑测试仍只产生一行，不把重跑次数累加为完成了更多测试。可选的正整数 `runner_reruns` 随报告路径与哈希一起封存，指标 v2 继续披露 flaky 和 retried；永久错误保留为 error。原始 XML 和头部声明原样留存。这是生产者兼容修复，不是按项目名开白名单，也不是看见 exit 0 就接受所有报告。

28 项新增回归覆盖 10 份真实原始 XML、错误注入、类型边界、重复回执以及后续通过／失败的历史。最初 26 项中 16 失败、10 通过；另外两项历史回归在实现审查中补充。337 项相关测试通过，单次全量为 **7795 passed、32 skipped、44 warnings，135.53 秒**。旧的 `curator-mismatch.xml` 测试本身携带合法 `flakyError`，因此预期改成保留三项测试与一次重跑；无重跑证据的计数不一致用例继续拒绝。

## 三组归档的离线核对与 legacy 结果

legacy 组于 22:18:21 UTC 结束，耗时 4132.02 秒；install 和 verify 分别 exit 0，verify 53:55。Agent 在 234 号动作中正确引用 BUILD SUCCESS、9 个模块和 121 份报告；237 号 gate 因 121 个引用超过 64 上限报 `repair_assessment_persist_failed`。随后 Agent 的 report 请求提交零测试，原密封报告仍保留 raw 718、712 最终通过、6 跳过；最终为 partial / Test unknown。归档完成，容器已删除。

三组各取成功 verify 的 121 份原始 XML，逐份核对回执哈希后，使用候选生产解析器离线重放。全部恢复 **718 个有模块身份的用例、712 最终通过、6 跳过、0 最终红**，flaky 分别为强制计划 1、完整接口 1、legacy 2。此前声明总数分别为 694、696、695，均被各报告的明确重跑记录解释。模块身份避免了仅按原始类名／方法名合并所丢掉的 16 项。原始回执、评分与 CI 目标都没有重写；这不是新 SAG 执行或 CI 达标证明。

该补丁首先修复回执与指标 v2。后续候选也修复了旧 `TestStats`：从同一已验证回执取得报告的模块坐标，按报告路径和当前内容哈希绑定后传给现有 compact reader；Surefire 重跑解释与回执解析器共用同一段代码。不同模块的同名测试不再合并，原生重跑保留 first／worst／latest 和 flaky，但不增加逻辑执行数。

模块信息只有覆盖全部主报告时才启用。若历史或不可用回执只为部分报告提供模块坐标，继续保留原有诊断身份，避免同一用例的两份报告因一个有模块、一个没有模块而被算成两个用例。未认领和哈希过期的报告仍独立列出，不给主结果贡献身份或重跑证据。9 项新回归覆盖跨模块同名、跨模块红项、四种真实 Surefire 输出、重复回执、部分模块信息及过期报告；相关测试 150 项通过。

这些候选改动不替换旧密封值，也不等于新 SAG 执行。后续真实候选回归仍等待 57 次冻结消融完成，并需核对正式输出各层的结果。
