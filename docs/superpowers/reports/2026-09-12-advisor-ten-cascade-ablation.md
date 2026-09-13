# 十项目 mini advisor → 不成功项目 terra low：实验与归因

实验于 America/Detroit 时间 2026-09-12 至 09-13 完成。沿用 `small-ci-10-v1` 的十项目名单，先运行全部 mini，再冻结不成功子集。实际完成 **10 次 mini + 1 次 terra**，复测项目只有 HttpClient。

**结论：mini 完成 9/10 个固定 build/test 验收任务；换 terra low 后仍是 9/10，新增完成 0 个。** terra 在 HttpClient 上给出了明确的完整重试建议，executor 随后重试，但第二次构建仍失败。它改变了可见执行行为，本轮没有带来任务完成收益。

需要同时保留一个限制：HttpClient 和 HttpCore 的工具选择了 Maven 3.9.14 Wrapper，偏离任务要求的 3.9.16。当前 AcceptanceTask 没有强制 Maven 版本，故“9/10 步骤成功”和生产 CI 达标数，不能作为九项完整复现官方软件环境的证明。

[浏览全部数值](/Users/chenhao/Documents/github/Setup-Agent/logs/advisor-ten-20260912/comparison.html) · [机器可读数据](/Users/chenhao/Documents/github/Setup-Agent/logs/advisor-ten-20260912/comparison.json) · [冻结计划](/Users/chenhao/Documents/github/Setup-Agent/docs/superpowers/plans/2026-09-12-advisor-ten-cascade-ablation.md)

**十项目实际结果**

下表测试数统一为“原始通过 / 原始总记录”；总记录包含跳过。模块数为成功模块 / 官方目标模块，不替代生产评分。九项 success 均完成固定命令、无测试失败或错误，原始通过数、总记录和跳过数与官方记录一致。

| 项目 | mini 任务 | 成功模块 / CI | CI 通过 / 总数 | mini 通过 / 总数 | mini 失败 / 错误 / 跳过 | 生产 CI 判定 |
|---|---|---:|---:|---:|---:|---|
| Commons DbUtils | success，1/1 | 1/1 | 523/523 | 523/523 | 0/0/0 | met |
| Commons Net | success，1/1 | 1/1 | 555/557 | 555/557 | 0/0/2 | met |
| Sling Commons MIME | success，1/1 | 1/1 | 8/8 | 8/8 | 0/0/0 | met |
| Sling Commons OSGi | success，1/1 | 1/1 | 28/28 | 28/28 | 0/0/0 | met |
| Creadur Tentacles | success，1/1 | 1/1 | 2/2 | 2/2 | 0/0/0 | met |
| Creadur Whisker | success，1/1 | 8/8 | 155/155 | 155/155 | 0/0/0 | met |
| Commons JCS | success，1/1 | 7/7 | 443/443 | 443/443 | 0/0/0 | met |
| HttpComponents Client | **partial，0/1** | **7/9** | **2,762/2,775** | **2,663/2,692** | **4/12/13** | **invalid，不评分** |
| HttpComponents Core | success，1/1；Maven 版本偏差 | 6/6 | 2,228/2,232 | 2,228/2,232 | 0/0/4 | met |
| Creadur RAT | success，1/1 | 7/7 | 1,146/1,149 | 1,146/1,149 | 0/0/3 | met |

mini 的权威证据可用 10/10，固定任务完成 9/10，生产 CI 可评分 9/10、达标 9/10。可评分项的 alpha_build、alpha_test、alpha 均为完整分数；逐项原始分子、分母保留在对照页。HttpClient 的生产判定为 invalid，alpha 为空，原因包含 `CERTIFICATE_AUTHORITY_UNAVAILABLE`，证书阻断项为 `TEST_EXECUTION_NOT_COMPLETE`。这里有确定的构建失败证据和部分测试计数，缺少的是完整 CI 任务成功执行的认证，不能补造范围分数。

HttpCore 的去重结果为 2,209/2,213，原始结果为 2,228/2,232；差异是重复执行记录，不是丢失 19 个已通过测试。HttpClient mini 的去重结果为 2,659/2,688。官方目标采用原始 Jenkins 最终记录，不能拿 SAG 去重数直接与官方原始数比较。class 文件数仅作诊断，已在对照页列出，不用它代表构建覆盖率。

**HttpClient：每次构建都保留**

固定项目 SHA 为 `be07c77297576b08bc01bb93789eca6f5f9bf850`，参考 [Apache Jenkins Java 17 #225](https://ci-builds.apache.org/job/HttpComponents/job/HttpComponents%20Client%205.7.x%20Java17/225/)。两组都请求以下完整命令：

```sh
mvn -B -f pom.xml clean verify install -P-use-toolchains,nodoclint
```

| 记录 | 成功模块 / 目标 | 通过 / 总记录 | 失败 | 错误 | 跳过 | 命令结果 |
|---|---:|---:|---:|---:|---:|---|
| 官方 CI | 9/9 | 2,762/2,775 | 0 | 0 | 13 | 成功 |
| mini，唯一一次构建 | 7/9 | 2,663/2,692 | 4 | 12 | 13 | exit 1 |
| terra，第一次构建 | 7/9 | 2,666/2,692 | 5 | 8 | 13 | exit 1 |
| terra，同一任务内完整重试 | 7/9 | 2,668/2,692 | 2 | 9 | 13 | exit 1 |

三个失败都发生在 `Apache HttpClient Integration Tests`，其后 `Jakarta REST Client for Apache HttpClient` 被 Maven 跳过。原始日志显示 5 秒 JUnit 超时、本地连接及 TLS/回调时序断言问题；没有证据表明这是编译失败。mini 距官方通过数少 99，terra 最终少 94；三次构建的原始总记录均比官方少 83。尚未完成全量测试身份差集，不把这 83 条直接断言为某一个模块的全部用例。

terra 最终去重结果为 2,664/2,688。封存结果绑定第二次 receipt `inv-maven-1-9bddc6daef98-0002`；没有累加两次执行，也没有挑选每个测试的最好结果。第一次 receipt 和完整日志仍在归档。工具即时反馈把错误合并进 failed 展示，例如“11 failed”；此处依据 receipt 分列为 2 failures + 9 errors，避免不同呈现口径造成误读。

[三次调用、receipt、建议与公开动作](/Users/chenhao/Documents/github/Setup-Agent/logs/advisor-ten-20260912/client-pair.json) 保留完整对应关系。原始日志分别为 [mini](/Users/chenhao/Documents/github/Setup-Agent/logs/advisor-ten-mini-httpcomponents-client-20260912/runs/httpcomponents-client/all-job-files/8c927d178248.log)、[terra 首次](/Users/chenhao/Documents/github/Setup-Agent/logs/advisor-ten-terra-httpcomponents-client-20260912/runs/httpcomponents-client/all-job-files/74110c0cd949.log)、[terra 重试](/Users/chenhao/Documents/github/Setup-Agent/logs/advisor-ten-terra-httpcomponents-client-20260912/runs/httpcomponents-client/all-job-files/5f57cc0c1873.log)。

**advisor 看到了什么，executor 做了什么**

| 阶段 | mini advisor 组 | terra low advisor 组 |
|---|---|---|
| 第一次构建失败后 | 工具明确返回 TEST_FAILURE、计数、失败模块和输出引用 | 工具同样明确返回 TEST_FAILURE、计数、失败模块和输出引用 |
| test advisor 的建议 | 读取失败 receipt、Surefire 报告和 dumps，判断是否可机械修复；不盲目重跑，不缩小范围 | 读取相同原始证据，检查资源压力和测试 JVM 设置，再原样执行完整 clean 命令，不删模块、profile 或测试 |
| 实际后续动作 | 读 receipt、搜 Surefire、运行 `java -version && mvn -version`，随后结束为 partial | 读 receipt 和 XML，随后原样重试完整构建；未见重试前检查系统资源或测试 JVM 设置的工具调用 |
| 最终结果 | 一次构建，partial | 两次构建，partial；未完成修复 |

mini test advisor 的结果出现在 sequence 127，之后有 131/135/139 的证据读取与版本检查。terra test advisor 的结果在 sequence 135，之后有 139/143/147/151 的读取，再返回 sequence 159 的第二次构建失败结果。三次咨询建议均进入 terra executor 后续请求，mini 的两次也都送达；“送达”不等于“带来完成收益”。

terra 的建议把现象解释为可能的环境时序波动，但没有完成资源压力验证。当前能证实超时及重试后部分失败变化，不能据此确定宿主调度、模拟执行、并行度、Maven 版本或项目自身缺陷谁是根因。HttpCore 的官方命令本身关闭了 JUnit 并行，且项目不同；不能用它成功来证明 HttpClient 关闭并行就会成功。

这次只有一个升级项目、没有 mini 同模型独立重跑，也没有无 advisor 组。两组开始时均为新容器冷缓存；terra 的第二次构建在其原容器里进行，依赖缓存已经变暖。因而 5 条通过数的变化和两组耗时差均是描述性结果，不能独立归因于 advisor 模型。

**执行与上下文的具体缺口**

| 已核实的问题 | 本轮证据及影响 | 尚未证明的部分 |
|---|---|---|
| 显式 Maven 要求被 Wrapper 优先级覆盖 | Client 两组及 Core mini 均要求并安装 3.9.16；build 的选择元数据却是 Wrapper 3.9.14。Core 虽通过验收，也存在版本偏差 | 没有证明版本偏差造成 HttpClient 超时 |
| 实际运行时事实未稳定进入后续咨询 | Client mini executor turn 12 收到 Wrapper 3.9.14，阶段提交也承认偏差；两个 advisor 请求都没有这个版本。terra 的 build advisor 请求包含 3.9.14，test advisor 请求不包含，test 建议又假定使用已激活的 3.9.16 | 不是 executor 从未看到；也不能把 advisor 缺据当成唯一原因，terra 早期拿到信息后同样未纠正 |
| 长输出引用指向摘录，原始日志入口未充分披露 | Tentacles 的 `output_0482081373fa` 只有 131,072 字节，原始 job 日志为 159,702 字节；索引有完整 job ref/path，但 22 个 executor 输入窗口及 advisor 请求均未出现该定位信息。build 却称摘录为 complete log | 原始日志没有丢失，Tentacles 最终成功；需要独立消融证明入口披露能改善恢复 |
| 交互命令防护误拦字符串内容 | RAT 只读命令中的 `printf` 标题 `pom.xml (top)` 触发 `top` 正则，执行前被拦。Agent 改用 file_io 后完成任务 | 只证明规则误报，没有将它算成最终任务失败 |
| 生命周期比较不能识别绝对 Maven 路径 | 本轮所有生产 lifecycle 状态为 unknown；封存命令的只读解析复现了绝对路径 mvn/mvnw 被识别为 unknown | 属于比较披露缺口，不是构建失败；本轮未重写分数 |

RAT 还出现过 `job:output_079447c7165f`：advisor 要求读取 detached job log，却没有给 job ID，executor 将输出引用拼成 job 引用，收到 `UNKNOWN_DETACHED_JOB_REF`，随后从模块 Surefire 路径恢复。它说明现有入口和提示仍可能混淆，但该次任务最终成功。

RAT 的误拦已经做了**纯函数离线消融**：原命令被拦；只把 printf 标题中的 `(top)` 改为 `(header)` 后不拦；真实 `top` 正向对照仍被拦。没有执行这些 shell 命令，也没有改动运行中的 SAG。见 [离线消融记录](/Users/chenhao/Documents/github/Setup-Agent/logs/advisor-ten-20260912/interactive-guard-offline-ablation.json)。其余逐项证据、首次实际反馈窗口和后续动作见 [轨迹核对](/Users/chenhao/Documents/github/Setup-Agent/logs/advisor-ten-20260912/trace-review.json)。

Commons Net 的一次 provision 拒绝已经通过拆分 Java/Maven 安装恢复，而且恢复在首次 advisor 之前发生；不能把它计为 advisor 收益。成功项目中这些可恢复的工具错误，也不能与 HttpClient 的未完成任务直接相加为“项目失败数”。

**资源用量**

以下为真实 token 账本，不是费用金额。耗时为每次完整 Agent 任务耗时，包含安装、分析、模型、工具和内部重试；不能直接与官方 CI build 步骤时长比较。

| 项目 / advisor | 秒 | executor tokens | advisor tokens | 总 tokens | 咨询 / 送达 |
|---|---:|---:|---:|---:|---:|
| DbUtils / mini | 452.62 | 146,608 | 90,955 | 237,563 | 2/2 |
| Net / mini | 572.82 | 137,195 | 36,463 | 173,658 | 2/2 |
| Sling MIME / mini | 424.34 | 136,610 | 74,103 | 210,713 | 2/2 |
| Sling OSGi / mini | 424.33 | 138,659 | 84,927 | 223,586 | 3/3 |
| Tentacles / mini | 452.37 | 140,669 | 91,282 | 231,951 | 3/3 |
| Whisker / mini | 604.89 | 164,320 | 79,454 | 243,774 | 2/2 |
| JCS / mini | 849.74 | 198,694 | 86,692 | 285,386 | 3/3 |
| HttpClient / mini | 837.74 | 130,581 | 76,021 | 206,602 | 2/2 |
| HttpCore / mini | 767.64 | 209,476 | 75,856 | 285,332 | 2/2 |
| RAT / mini | 945.99 | 252,236 | 102,996 | 355,232 | 4/4 |
| **mini 合计** | **6,332.48** | **1,655,048** | **798,749** | **2,453,797** | **25/25** |
| HttpClient / terra，含内部重试 | 1,072.63 | 201,129 | 86,997 | 288,126 | 3/3 |
| **本轮总计** | **7,405.11** | **1,856,177** | **885,746** | **2,741,923** | **28/28** |

同一个 HttpClient 任务，terra 比 mini 多 234.89 秒（约 28.0%）和 81,524 tokens（约 39.5%）。从“mini 后只升级不成功项目”的执行策略看，本轮额外支付的是整个 terra 任务的 288,126 tokens，救回 0/1；不能只把两组差额算作升级成本。

**配置、证据边界与后续决策**

SAG 冻结在提交 `6f858af308da6559966cb24340a137455924a7eb`，内容及权限摘要为 `83bc9e5bfa5efa56baa2affc00f272037d350d37f2de0450ffc7e5cd87f650f1`，共 1,039 个源文件。executor 固定 gpt-5.4-mini medium；mini advisor 为 same-model、不指定 reasoning effort，terra advisor 为 gpt-5.6-terra low。两组窗口 65,536、extractive 压缩、输出预算 2,048、每阶段最多 4 次咨询，150 iterations、7,200 秒任务预算相同。模型请求及实际响应身份已核对。

采用同一不可变 amd64 镜像，在 aarch64 Docker 引擎上串行运行；引擎为 18 CPU、约 16.8 GB 内存。这些是引擎资源，不是独占容器配额，也不是已证明的失败原因。每个独立任务新建容器，两个 advisor 组不共享会话、安装或诊断信息；SAG 单次任务内的自主重试完整保留。

官方参考来自 [已冻结的十项目清单](/Users/chenhao/Documents/github/Setup-Agent/benchmarks/small-ci-10-v1/manifest.json) 和哈希校验的原始归档，包含 10 项 Grade A、42 个成功模块、7,872 条测试记录（7,850 通过、22 跳过）。本轮未重新抓取或改变参考。五项原 CI deploy 步骤沿用既有、已批准的本地 install 替代，不宣称发布流程等价。JDK major 以每次 dispatch probe 为准；已知官方补丁版本与本轮不同，部分官方记录没有补丁版本。Client/Core 的完整测试身份超过生产目标上限，计数相同也不代表已完成全量身份等价验证。

本轮不支持把默认 advisor 全面升级为 terra，也不支持断言 advisor 普遍无用。现有结果支持先解决已证实的执行与信息传递偏差，再衡量模型的恢复能力。建议后续按单一机制逐步消融：

1. **Maven 显式版本与 Wrapper 的优先级。** 同一已封存配置先做选择行为对照，再固定 advisor、SHA、命令、JDK，比较仅切换这一机制的全范围执行。验收要覆盖实际构建选择的版本；不能只看 shell 中的 `mvn -version`。同模型重复试验用于估计时序波动。
2. **完整日志定位信息。** 固定同一超长日志及中段查询，对比现有输出和透传已有 full_log_ref/path、准确标记摘录后的可检索性；随后才做配对 Agent 恢复试验，无须先扩大所有上下文预算。
3. **交互命令误报。** 已有离线证据支持检查实际命令位置，保留真实交互程序的防护；修复后复用原始被拦命令和正向对照，再验证恢复行为。
4. **生命周期路径解析。** 用本轮全部封存命令做有/无规范化的离线对照，保留 deploy→install 等真实差异；不把 unknown 直接改成 equivalent。

上述修复没有混入本轮，也未增加项目专用例外。11 次运行的源码、任务、封存记录、模型请求和归档已核对，241/241 个 executor 输入窗口完整恢复，28/28 次建议确认进入后续请求。所有 11 个实验容器均在完整归档后移除，最终 Docker 清单为空。校验范围是 CLI 及封存产物；API/WebUI 未启动，不能据此声称那些表面已验证。

[最终校验记录](/Users/chenhao/Documents/github/Setup-Agent/logs/advisor-ten-20260912/final-validation.json) · [容器最终清单](/Users/chenhao/Documents/github/Setup-Agent/logs/advisor-ten-20260912/docker-final.json) · [冻结配置与逐次目录](/Users/chenhao/Documents/github/Setup-Agent/logs/advisor-ten-20260912/experiment.json)
