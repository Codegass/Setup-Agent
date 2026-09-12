# 任务恢复、Gson 要求精度与 advisor 对照

本轮修复及5次真实运行已完成。Gson的虚假Java要求冲突已消除，完整build/test任务从partial变为success。FreeMarker在同一修复源码上，off只完成1/3，mini与terra low均完成3/3并封存success。**当前证据支持保留advisor辅助恢复；工具信息修复本身还没有消除弱模型提前结束的可能。** 每组仅一次，结论限于该案例。

## 本轮实现

1. Build/Test 阶段重建上下文时，使用已有验收器从本轮 receipt 投影每一步的状态、命令、具体缺口和可读取的 receipt 文件路径。读取异常显示 unavailable，不推断任务为空，不修改封存证据。
2. 提示明确区分环境/原生工具链前提失败与产品测试断言失败。环境恢复仍属于 setup 工作；识别到可行恢复时应执行。保持原任务和断言，允许有具体停止原因的 partial，不添加强制成功门槛。
3. 说明 provision 会激活新 JDK，后续任务需要其他已安装 JVM 时由 Agent 使用现有 env 接口切换。工具链选择机制没有新增 FreeMarker/GraalVM 专属执行分支。
4. JVM 验收错误区分“缺少授权派发观测”与“实测版本不匹配”，例如要求 Java17、实际 Java21；验收规则不变。
5. Maven profile 仅含已知编译诊断选项时，不把它当作 JVM 版本要求。release/source/target、toolchain、未知参数、插值、argfile、空覆盖仍保留不确定性。Gson 原始 POM 的 Java `[17,22)` 与编译 release8 均保留，仅删除 `disable-error-prone` 的虚假版本不确定性。

[本轮相对起始工作区的代码差异](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/final-source-v4.patch) · [Gson 原始 POM 前后复现](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/gson-requirement-reproduction.json)

## 已完成的探索轮

FreeMarker 原工具修复版只完成 1/3。恢复上下文 v1 完成 2/3：主构建成功，原生编译和原生程序执行成功，但主构建派发实测 Java21，固定任务要求 Java17，因此该步骤不能认证。

| 版本 | 验收 | 原始通过/总数 | 去重通过/总数 | class 文件 | 秒 | 总 tokens | 封存 verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| 原工具修复 v4，历史参考 | 1/3 | 1,526/1,529 | 1,388/1,391 | 2,083 | 731.73 | 190,412 | partial |
| 恢复上下文 v1，探索轮 | 2/3 | 1,526/1,529 | 1,388/1,391 | 2,083 | 1,142.67 | 144,743 | partial |
| 恢复 v4，advisor off | 1/3 | 1,526/1,529 | 1,388/1,391 | 2,083 | 735.46 | 173,817 | partial |

这些运行使用不同源码，不能视为严格的单因素对照。原始测试总数含3个 skipped；原生任务由独立 receipt 验收，没有塞进 JUnit 分母。

v1 的25/25输入窗口完整且哈希通过。进入 Test 时，Agent 看见的是模糊的 JVM 错误和 receipt ID，随后三次使用 `job:inv-...` 查询，均失败，最后结束为 partial。它的总结把“JVM 不匹配”解释为无法读取派发证据，还声称主构建用了Java17；本报告采用 receipt 的实测Java21。该观察促成文件路径、精确版本差异与 provisioning 激活说明的后续修复。

[Agent 实际收到的关键输入及随后调用](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/v1-ref-failure-input-proof.json) · [所有输入窗口](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/recovery-v1-freemarker-off-1-20260912/model-windows/freemarker.json) · [历史参考原始数值](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/references.json)

恢复v4的25/25输入窗口同样完整。主构建实测Java17、成功；Native Image仍在Java17下尝试一次并失败。进入Test后，Agent成功读取两个receipt文件，并使用`search(file:...)`查询失败记录，读取路径修复得到真实验证。Test输入明确包含1/3进度、两个未完成步骤、可用的provision/env说明、恢复职责和约132次剩余迭代；它仍未安装GraalVM，最后将安装与重试写在总结。失败工具调用从v1的3次引用失败变为v4的1次原生编译失败。**工具信息可读性改善没有稳定转化为恢复执行，不能称为任务完成率提升。**

[v4实际输入、读取动作与提前结束](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/v4-recovery-decision-proof.json)

## Gson 实跑验证

| 版本 | 验收 | reactor | 原始及去重通过/总数 | 跳过 | class 文件 | 秒 | tokens | 封存 verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 原工具修复v3，历史参考 | 1/1 | 8/8 | 4,846/4,866 | 20 | 1,319 | 661.19 | 119,259 | partial |
| 恢复v4，advisor off | 1/1 | 8/8 | 4,846/4,866 | 20 | 1,319 | 691.14 | 113,842 | success |

两轮均为同一项目SHA、根目录`mvn clean verify`、实测OpenJDK17.0.20与激活Maven3.9.9。旧冲突`build_requirements_unavailable`消失；真实Java范围`[17,22)`、编译release8、模块结果和测试统计保持一致。本次改善在验收准确性。计数相同不能单独证明完整测试身份相等；单次耗时和tokens差异不作为性能结论。

新一轮17/17模型输入窗口完整。Java+Maven合并provision被拒绝后，Agent根据当前接口信息拆为两次调用并成功。进入Test后通过`search(file:<receipt path>)`读取已有证据，没有为填满阶段重复构建。容器已归档删除。

[真实运行前后要求与验收证据](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/gson-live-comparison-proof.json) · [封存success](/Users/chenhao/Documents/github/Setup-Agent/logs/recovery-v4-gson-off-1-20260912/runs/gson/container-evidence/.setup_agent/verdict.json)

## 最终验证与 advisor 对照

三组均已完成。mini使用原配置gpt-5.4-mini（same-model，不额外指定reasoning effort），terra使用gpt-5.6-terra low。主执行模型统一gpt-5.4-mini medium；advisor上下文预算65,536，使用extractive压缩。off组的模型咨询tokens为0。源码、任务、镜像、资源与其他配置相同。

| advisor | 验收 | 封存 verdict | 失败工具调用 | 秒 | executor tokens | advisor tokens | 总 tokens | 实际咨询/建议送达 |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| off | 1/3 | partial | 1 | 735.46 | 173,817 | 0 | 173,817 | 0/0 |
| 原 mini | 3/3 | success | 4 | 1,299.16 | 304,168 | 61,108 | 365,276 | 3/3 |
| terra low | 3/3 | success | 0 | 1,192.84 | 231,375 | 58,404 | 289,779 | 3/3 |

mini组完成了OpenJDK17.0.20主构建、GraalVM21.0.12原生编译和原生程序执行。原始测试1,526/1,529、去重测试1,388/1,391、跳过3个、class文件2,083，与off组一致。Native Image生成6分2秒，对应Gradle调用7分9秒。更长耗时包含新增完成的原生任务，不能把提前结束的off当作同等完成量的效率基线。

mini的3次咨询均真实调用provider，3条建议均在后续完整输入窗口中找到。前两次已建议先17后21，但执行器仍在17下先尝试了原生编译。第三次咨询拿到实际失败后明确提出安装/激活GraalVM21/native-image；随后执行器读取receipt，成功provision、env激活、重试原生编译、执行原生程序。该轨迹存在建议送达、对应动作与新增完成证据；每组仅一次，不能推导总体成功率提升或把全部改善归因于advisor。

三个mini请求估算输入分别9,709、20,953、30,050 tokens，均低于60,314的输入预算，均未触发压缩。这验证当前上下文能支持本例咨询，不证明压缩对长上下文的效果。38/38执行器输入窗口完整，源码和封存证据均已核验；mini容器已归档删除。

[mini建议内容与送达证明](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/recovery-v4-freemarker-mini-1-20260912/advisor.json) · [mini模型输入和后续动作](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/recovery-v4-freemarker-mini-1-20260912/model-windows/freemarker.json)

terra的第一条建议明确要求提前准备两套工具链，再切回17。第一份能证明已收到该建议的后续模型输入，对应的动作是provision GraalVM21；随后实际env切回OpenJDK17。主构建成功后，执行器激活已有GraalVM21、验证版本、执行nativeCompile及原生程序，全部成功。第三条建议要求核对receipt及JUnit报告，并将原生程序成功单列为smoke证据；模型读取三个receipt和测试结果后结束，没有重跑已完成任务。

terra的3/3建议均送达，31/31模型输入窗口完整，0个失败工具调用。实际JVM仍为OpenJDK17.0.20和GraalVM21.0.12；测试计数与class数均和另外两组一致。Native Image生成5分13秒、峰值RSS5.87GB，对应Gradle调用6分21秒。它在本次运行中比mini少75,497 tokens（20.67%），耗时少106.32秒，其中原生编译本身也有耗时差异。tokens是累计调用用量，不是单次上下文大小，也不能直接换成不同模型的金额费用。这些单次差异不构成总体性能结论。

三个terra请求估算输入分别8,992、17,380、31,573 tokens，均未触发压缩。mini和terra合计6次咨询都保留了真实请求和响应；本轮没有把缺少信息归咎于上下文窗口，也没有声称压缩策略带来了增益。

[terra建议内容与送达证明](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/recovery-v4-freemarker-terra-1-20260912/advisor.json) · [terra模型输入和后续动作](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/recovery-v4-freemarker-terra-1-20260912/model-windows/freemarker.json) · [三组固定条件核验](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/ablation-invariants.json)

## 当前方案与剩余边界

保留本轮通用修复：真实任务进度和可读原始证据、精确的JVM不匹配原因、统一工具链激活说明、Java要求的保守精度修复。保留advisor作为可配置辅助，验收继续采用原任务、逐步JVM观测与真实执行结果。代码默认配置为same-model、64K和extractive；本例的terra low结果更好，适合作为后续复杂任务验证的优先候选。单个项目、每组一次不足以决定全局更换模型。

剩余风险有具体边界：关闭advisor后，弱模型仍可能在可修环境问题上提前结束；任意Gradle自定义DSL路径尚未做通用解释；完整官方CI测试身份和模块范围缺失的部分继续不评分。本次完成的是固定命令和JVM要求的任务验收，以及Gson的既定冒烟任务。全量CI等价、API/WebUI表面一致性不在本次已验证结论中。

每次均为新容器、冷依赖缓存、串行执行，任务 SHA、项目 SHA、Docker 镜像、资源与预算固定；归档后删除本轮容器。记录真实 provider 请求、建议是否进入后续执行模型输入、随后动作与新增验收证据。单次结果只能说明该轨迹，不能估计总体成功率。

[当前全部运行数值](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/comparison.html) · [机器可读结果与原始证据链接](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/comparison.json)

FreeMarker 固定三步：Java17 根目录 `clean build`；GraalVM21 `nativeCompile`；运行原生可执行文件。Gson 固定同一SHA、Java17和根目录 `mvn clean verify`。未取得完整官方测试池和模块范围的部分继续不评分；本轮不声称完整CI等价。验证CLI和封存产物，API/WebUI未启动。

## 回归与源码状态

- 恢复 v2 全量：8,025 passed、22 skipped；唯一失败是隔离打包依赖下载的DNS错误。允许联网后单独重跑打包：1 passed。合计8,026项通过。
- 精确JVM错误及receipt路径/工具说明补丁：相关71项通过。
- 当前工作区尚未提交，起始时已有的advisor与工具改动保留。本轮相对起始状态修改5个生产文件、2个已有测试文件，新增1个测试文件。
- 恢复 v4 源码 SHA256：`83bc9e5bfa5efa56baa2affc00f272037d350d37f2de0450ffc7e5cd87f650f1`。旧v1探索失败保留，不改写历史verdict。
- 5轮容器均已归档删除，最终Docker实例列表为空。全部136个执行器消息窗口完整（25+25+17+38+31）并通过哈希校验；这里的窗口指归档的消息组件，不推断模型内部想法。

[全量日志](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/full-tests-v2.log) · [打包重跑](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/packaging-tests-v2.log) · [最后相关回归](/Users/chenhao/Documents/github/Setup-Agent/logs/task-recovery-20260912/ref-feedback-tests-v4.log)
