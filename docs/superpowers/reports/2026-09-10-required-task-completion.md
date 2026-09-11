# 固定多步骤任务的完成校验

状态：独立候选实现，尚未进行真实项目回归；原 23 项目三组消融继续使用冻结 v8。不能用下面的测试通过数代替 FreeMarker 原生任务完成。

## 问题与行为

FreeMarker 的固定官方任务既包括 Java17 的 Gradle build，也包括 Java21 环境中的 nativeCompile 和生成二进制的运行。正式三组均得到 1529 条 JVM 记录；完整接口和旧接口遗漏后续原生步骤，强制计划组运行 nativeCompile 但因 OpenJDK 没有 native-image 而失败。三组原始本地结论均为 success；强制计划组的 Agent 已如实报告 partial。

新输入 `project --acceptance-task-file TASK.json` 在启动前固定仓库、准确 SHA 和有序步骤，独立于模型可选计划以及官方 CI 案例池是否齐全。CLI 检查目标仓库与 `--ref`，运行 pin 保存完整定义和规范 JSON 的 SHA256；修改原文件不会改变本轮义务。模型看到逐项原始命令及目录。

```json
{
  "schema_version": 1,
  "repo": "apache/freemarker",
  "sha": "e156e5faf9325e7ec05b781411bbf331aff98dfc",
  "steps": [
    {"id": "jvm-build", "runner": "gradle", "java_major": 17, "argv": ["./gradlew", "-Pfreemarker.signMethod=none", "-Pfreemarker.allowUnsignedReleaseBuild=true", "--continue", "clean", "build"]},
    {"id": "native-compile", "runner": "gradle", "java_major": 21, "argv": ["./gradlew", ":freemarker-test-graalvm-native:nativeCompile"]},
    {"id": "native-check", "runner": "native", "argv": ["./freemarker-test-graalvm-native/build/native/nativeCompile/freemarker-test-graalvm-native"]}
  ]
}
```

`java_major` 只验证回执观察到的启动 JVM 主版本，不声明编译器或 GraalVM 发行版等价。FreeMarker 的 native-image 需要仍由真实 nativeCompile 执行暴露；没有 native-image 的 OpenJDK21 不会因主版本匹配而获得成功步骤。

## 一个完成判断，复用已有证据

`acceptance_task.py` 是一个有界列表和顺序检查，没有 DAG、脚本解释器或新的调度器。最多 32 项，每条命令 2048 字节，目录必须是仓库相对路径。步骤以完整 argv、目录、本轮源码和原有契约／回执授权链匹配；JVM 工具的实际启动器解析仍沿用已有规则，参数不得增加选择、跳过或替换项。同步调用读取当前输出存储，原始内容须与回执哈希一致。已有后台机制完成的 Maven/Gradle 调用则读取宿主授权的任务与回执账本，并校验完整日志哈希；引用存在但内容丢失不能成为完成依据。当前源码有 tracked 修改时，固定版本任务保持 unavailable。

相同命令只有一项义务时，一次成功执行可跨 Build/Test 阶段复用。清单明确要求重复两次时，需要两个不同的有序调用。最新失败或无法验证的重试不会借用较早的绿色回执。失败、未运行、顺序不符和证据不足分别保留，不能靠删除清单缩小同一运行的完成范围。

对明确的仓库内原生程序，Bash 在执行前后读取 ELF 标识和可执行文件哈希，并复用既有 invocation contract 和 receipt 发布链。探测不运行程序或 `--version`。实际调用保持原命令；证据准备失败也不阻止原来的 Bash 尝试。原生程序的退出结果不产生任何 JUnit 案例。当前支持同步结束，或在初始监视窗口内已终结的调用；后台和仍待后续轮询的通用 Bash 任务保持未证实。

复合 shell 步骤可用 `runner=shell` 保留在清单中，但当前校验器不声称证明其内部完成度。它仍可执行；不能以最后一个 shell 退出码零，把中间失败升级为全任务完成，也不能为了通过校验而从清单删掉该步骤。

## Agent 反馈与输出一致性

Test 完成声明前，PhaseTool 在原有控制层已可终结的状态下增加固定任务核对。物理测试成功而后续必需步骤缺失时，返回具体步骤及回执事实；Agent 可以继续执行，也可以诚实以 partial 结束。它不改变已有运行中任务的等待或证据恢复规则，也不为模型生成或强制执行修复命令。

真实控制流程的回归发现，普通的“声明与证据不符”处理会打开 RepairContext，继而要求后续工具携带 repair_intent。固定任务的缺项提示现在保持可继续执行：仍拒绝未经证明的 success，但不为它打开修复上下文。Build、Bash、工具链准备、文件读取和 Advisor 都能继续；已有项目失败的修复约束、任务等待和证据恢复规则保持原有语义。实时判断和日志回放使用同一份有类型的任务事实。

Finalizer 单独封存 `task_completion`。未完成或无法验证会限制整体 verdict，同时保留原有 Build/Test 判断、计数、范围和 CI 比较。构造器、JSON 读取、CLI、Markdown／简报、API 与 WebUI 使用同一结果。撤销 seal 后，展示层不能继续沿用完成状态。

这里的步骤完成证明声明命令按要求成功执行；物理产物、测试结果以及 CI parity 仍由各自已有证据判断。没有官方范围时仍不给 α，多个调用的案例池也不通过这个清单求和。旧 `--acceptance-command` 保持兼容，历史无清单的记录不会被重评分。

实验入口 `scripts/d3r2_campaign.py` 同时接受已固定哈希的任务文件。每次尝试归档该文件并让子进程读取归档副本，结束后核对实际 run pin 的定义和摘要。输入漂移、删掉任务后恢复旧结果、基线不支持新参数等情况会在实验入口明确报出，不静默改变任务。这里约束的是实验输入身份，不是 Agent 的普通项目操作。

## 验证

- 新增 63 项 Python 用例，覆盖真实 facade／发布链、遗漏和失败、顺序、重复调用、错误运行时、输出丢失、输入与源码变化、脚本未知范围、原生程序哈希、阶段声明和各输出面。其中 10 项覆盖缺项提示后普通工具继续执行及无关失败不被放宽，前 5 项在修复前均失败；另 10 项覆盖实验任务文件的归档、输入漂移与实际 run pin。
- 另新增 20 项后台终态与输出绑定回归，覆盖 Advisor 读取已发布终态、错误身份和未完成任务、真实异步发布链、缺失或被替换的日志，以及只改变展示摘要时保留原始证据。
- 最新单次完整 Python 测试：7767 passed、32 skipped、44 warnings，126.26 秒，包含上述 20 项，以及 Curator 产物／日志分类、wrapper 优先级、取消流程重复终态各 10 项新增回归、Maven 错误反馈 11 项、控制台执行边界 6 项、修复记录引用 8 项、测试恢复范围与缺数反馈 12 项。该次允许隔离打包测试下载构建依赖，全部通过；之前在沙盒内发生的打包 DNS 失败及单独复核记录仍保留。
- WebUI：OverviewTab 17 项测试通过；TypeScript 和生产构建通过。编译产物随源码保存。
- 正式原23项目成绩、模型输入、目标和三个源代码版本保持原样。真实候选回归仍待进行，特别是 CSV/DBCP 单次测试复用与 FreeMarker 完整原生任务。
