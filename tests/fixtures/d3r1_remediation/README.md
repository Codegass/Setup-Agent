# D3R1 执行完成校准证据

`execution_completion_cases.json` 保存五个已确认反例的最小证据投影，来源为
`logs/d3r1-20260907/` 的封存实验。它不代表新的实验，不是可以直接发布或验证授权的
完整 receipt。这里没有添加 `authority_ok`、重签历史数据或合成前置故障 assessment。

`archived_test_rollups.json` 另存从原审计结果核对 SHA 后提取的 build、unique/raw
计数，供下游门禁和 finalizer 的离线串联测试使用。它保留原文件路径与摘要，不能用来
签发新的运行结论，也不能用上述样本行数重新计算这些总量。

## 固定预期

| case id | 预期 | 为什么不是单纯项目 assertion red |
| --- | --- | --- |
| `spark-kubernetes-operator` | 不得 completed；已有执行应保留为 partial | 225 条测试行均绿，但同一次调用的 CRD 检查无法启动 `yq`；已有真实 `prerequisite_executable_missing` assessment。 |
| `kafka` | 不得 completed；partial | Gradle daemon 消失，退出 1；虽然身份行不可用，原始 suite totals 已证明此前有执行。底层原因（包括 OOM）仍未知。 |
| `zookeeper` | 不得 completed；partial | 同次 full-build/verify 缺 `autoreconf`，并发生编译器异常；已生成的测试报告不能替代这些未完成步骤。 |
| `storm` | 不得 completed；没有测试体执行证明时为 failed | `storm-client` 编译失败，唯一 testcase 是 skipped。它仍计入 receipt execution，不能称为通过或抵消编译失败。 |
| `seatunnel` | 不得 completed；保留其他已执行检查为 partial | ActiveMQ 类初始化因 Docker 不可用而报错；另六条检查通过。`ignoreFailures` 使退出码为 0，不能证明初始化成功。 |

每例的强制门槛是 `expected.disallowed_state == "completed"`；
`preferred_state` 给出按当前证据推荐的 failed/partial 细分和理由。Storm 的缺口由
编译故障证明，**不是单独由 skipped 推断**。合法全 skipped 且命令正常结束的正向对照
必须另行保留。真正完整执行但 assertion red、同范围修复成功、窄范围重试、缺授权等
R0 正反对照由测试自身建立，不冒充本次真实项目记录。

## 字段与提取方法

- `source_receipt`、`source_contract`、`source_review`、`historical_result.source`
  记录仓库相对源路径与原始文件 SHA-256；`baseline` 绑定代码、报告和封存摘要。
- `receipt_fields` 保留原始标量/上下文字段及已有 Gradle 汇总；仅省略四个大集合。
  它不是完整 receipt。`contract` 是原始小型 contract 的完整内容。
- `testcase_execution_rows_metadata` 保留原 envelope 除 `rows` 外的字段；
  `source_row_count` 是原行数。`row_samples` 按 error/failed/skipped/passed 分别取首个
  原始元素，`source_index` 为零基下标。`testcase_outcome_samples` 使用相同规则。
  **原 metadata 的 complete 只描述原始集合，不能用于宣称样本集合完整。**
- `report_delta_counts` 保留各原集合大小；`report_delta_samples` 保留样本对应的
  原始报告路径/摘要。Kafka 没有身份行，仅保留第一条原始 new report。
- `assessments` 只含精确匹配该最终 test receipt ID 的原始记录，包含原文件摘要。
  Kafka/ZooKeeper 只有 `contract_binding_unknown`，不能假装历史上已存在缺失的诊断。
- `output` 定位原 JSONL 文件、记录行、ref ID、输出摘要和元数据；`excerpts` 是
  `output.splitlines()` 中指定的一基行区间，逐字保留，未复制整个大日志。
- Spark、Kafka、ZooKeeper、Storm 的完整原输出字段已核对为 receipt 的
  `output_content_hash`。SeaTunnel 持久化输出被截断，字段明确记为不匹配；不得把
  这个节选当成完整绑定日志。它的关键故障由 `report_samples` 中独立核对的 XML 证明。
- `report_samples` 的 tar member 字节已核对为原 receipt `report_delta` 摘要。
  仅保留 suite 属性、testcase 属性和 error/failure/skipped 元素；不复制 properties、
  system-out、环境转储等无关数据。原始错误中的 setup 栈位置仍保留。

## 测试使用边界

确定性语义测试可参数化 `cases`，使用最小样本与原始汇总调用纯解析/判定函数；
不能用样本行数重算原总量，也不能把节选摘要当完整输出摘要。Kafka 特别用于检查
“身份行为空却已有 suite totals”的情况。

生产授权链测试应通过测试控制器正常产生新的 contract、receipt、输出和 assessment；
用新的摘要绑定新字节。旧 IDs、SHA 和样本不能代替当前 authority。历史原始
`test_state=success` 仅用于记录旧错误，不能充当人工预期的来源。

变更预期时应说明新的直接证据、标签版本和影响范围，不因修复实现尚未通过而移动标签。
本夹具不需要 Docker、网络或重跑大型项目；全部源文件保持只读。
