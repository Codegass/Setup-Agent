# Cayenne：执行、身份和完成判定分开

冻结完整接口组在 `apache/cayenne@be11ffd18d16507dd1fc8b7416420e67276d38e1`、JDK 21 上执行官方 Derby 单元。整个尝试 2031.87 秒，原始最终结果 partial，容器已归档删除。官方 `-q` 日志没有可准入的完整模块／测试数，因此以下计数只陈述 SAG 的实际结果，不宣称 CI 达标。

## Agent 看到什么、做了什么

1. 动作 132 执行 `mvn verify -q -DcayenneTestConnection=derby -DcayenneLogLevel=ERROR`；正常退出 0。工具反馈为 4850 项、4816 通过、34 跳过、0 红项。
2. Agent 声明 Build 成功，但旧产物校验器要求不存在的 `.maven-plugin` 扩展名。Agent 据此改为 partial。候选的 packaging→JAR 修复已覆盖这一问题。
3. Agent 在 Test 阶段试图复用 verify。它先搜索报告并错误引用单份报告的 1/1 为汇总；原始完整计数仍在工具输出和密封报告中。
4. gate 195 报 `TEST_ATTEMPT_REQUIRED`，同时披露 `run_wide_test_receipts=1`、当前 Test attempt 的 candidate-bound 数量为 0。控制器在动作 199 强制 `build(action=test)`，实际执行 `mvn --fail-at-end -Dmaven.test.failure.ignore=true test`，没有保留 Derby 参数或 verify 的集成测试生命周期。
5. 追加命令退出 0，工具反馈 2291 项、2277 通过、14 跳过。Agent 错把这条命令称为满足原 verify 范围。身份／完成校验仍 unavailable，后续控制评估又因证据引用超过 64 上限无法持久化。最终密封结果保留 4850 项，模型的 report 请求却提交了零测试。

证据来自原始 `control_events.jsonl`、两份契约／回执、报告快照和原密封 verdict；它们都保持不变。第一次 verify 完整原生日志另存为 `logs/verification-23-20260910/cayenne-full-first-verify.log`。

## 根因与有限修复

首个回执认领 784 份报告。完整 XML 解析读到 4850 个结果，2048 条身份样本保留后披露丢弃 2802 条绿色记录。样本里 36 条来自 `cayenne-gradle-plugin/build/test-results/test` 的 11 份报告：Maven 构建启动了 Gradle 测试，按 Maven 专属目录确定模块的规则无法归属这些结果，整份身份 envelope 因而 unavailable。这不是测试没运行，也不是测试数为零。

跨阶段 floor 之前只认可完整测试判定或当前 Test attempt 的执行记录。候选现在由现有回执汇总额外返回已绑定、已终结的测试调用身份，floor 据此停止强制重复。完成判定、身份可用性和固定任务校验不变。8 项新回归中修改前 3 失败、5 通过；修改后相关 100 项通过，覆盖身份缺失、红项诊断缺失、runner 失败，以及无契约、无终态、无当前范围和未执行测试的边界。

完整计数本身也已在候选中保留：XML 解析器原本给 Agent 返回精确总数，但 Maven 回执只保存受限的身份样本。候选将已有、经哈希验证的五个结果计数和报告数量封存为可选 `testcase_execution_totals`，复用现有 totals 消费路径，覆盖报告指标、证书计数及物理测试完成检查。该全局计数不提供模块映射，逐用例身份仍可 unavailable；历史 Maven 回执没有该字段时继续无总数，不补零。

计数必须守恒、覆盖本回执全部报告，并与保留的结果和样本截断披露相容。矛盾计数单独记为 omission，原生退出码、命令、回执和已有红项不被删除。未完整解析的 XML 不封存完整总数；缺少授权评估也不能只凭计数判为完成。样本上限保持 2048，没有猜测 Gradle 模块映射。

17 项新回归经过实际 facade、契约、哈希报告解析和回执发布：2100 项计数在保留 2048 条身份后仍完整；两个 Maven／Gradle 报告保持两项全局计数和缺失的身份判定；真实 Surefire 重跑样本按三个逻辑用例计数；非法数值、数量膨胀、隐藏红项、错误报告数、坏 XML 和缺失评估均拒绝。初始 13 项中修改前 11 失败、2 通过，随后补充 4 项边界。相关路径 397 项通过，消费端补充检查 82 项通过；包含前述跨阶段修复的完整 Python 测试为 7829 passed、32 skipped、44 warnings。

这是冻结实验的归因和独立候选设计。真实候选重放尚未执行，不能把测试通过当作 Cayenne 已修复完成。
