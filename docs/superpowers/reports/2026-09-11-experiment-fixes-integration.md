# 实验结论与工具修复整合

已将候选 `e8d74c97555367d3b8831ef0c89a5206f8fc770f` 的实现整合到主线，保留此前完成的长输出、文件路径、分页和搜索修复，并补上 RocketMQ 复盘中尚未实现的两处反馈缺口。下列验证针对基线 `fe7eacb137b47201c7836e7a5e864ead4f614850` 之上的整合改动；本记录随修复一同提交。

本轮只进行代码整合、测试和离线证据投影。原来的 27 次正式尝试、冻结版本、CI 目标、成绩与归因材料保持原样；没有启动后续项目或候选 SAG 复测。

已应用的实验修复：

| 问题 | 当前行为 |
| --- | --- |
| 命令被拆成多份参数，计划格式错误阻止执行 | `build(command=完整命令)` 保留目标、顺序、profile 和显式选项；结构化计划成为可选策略。字面的 Maven/Gradle Bash 调用复用同一 runner 和回执链。 |
| CI 的 verify/package 已经运行测试，Test 阶段仍要求默认 test | 当前有效终态回执可跨阶段复用；完成过的调用不会因计数暂不可用而被当成没执行。成功判断仍核对任务、范围与证据。 |
| 后续调用覆盖 XML、模块或重试身份丢失、引用数量导致报告失败 | 保存回执所绑定的原始 XML；分别保留完整计数与有界身份集合；修复 Surefire 重复／重试报告解释以及大批报告引用的传递。不能跨不同调用的案例池拼出 CI 达标率。 |
| Java/profile、Maven bundle、Gradle 版本及失败摘要解释不准确 | 使用实际执行 profile 的 Java 约束、模块身份和 launcher 观测；展示现代 Surefire 失败项及 Maven/Gradle 已观察到的测试数量。exit 0 不会洗掉 XML 中的真实失败。 |
| FreeMarker 必需的原生步骤未完成却得到整体 success | 显式的 `--acceptance-task-file` 固定版本及有序必需步骤，独立于模型计划。缺项反馈允许继续普通工具操作，但不能宣称全部完成；最终报告、CLI、API、WebUI 使用同一完成结果。 |

新补的反馈修复：

- `test.stats` 不再被任意切成 240 字符的 JSON 前缀。执行状态、范围限定、raw/unique 计数、错误与缺失状态完整呈现；大数组等明细引用完整交接文件。Report 优先保留最新计数事实；预算确实不足时省略整项并给出完整文件路径。
- Maven 无法解析时，展示参数要求的来源、已知的已注册版本，以及 `project` 的安装／注册语法。版本由 Agent 选择，安装后的版本仍须满足要求；没有恢复任意 suggestions、自动移除版本约束或生成修复步骤。
- 联合回归发现并修正原生 Bash 回执的一处交叉问题：回执哈希与可检索输出现在使用同一份完整输出，避免对预览计算哈希后无法核对全文。

RocketMQ 的实际历史交接行已被旧代码精确复现；使用相同 gate 事实生成的新交接保留了整个统计对象：

| 字段 | 旧 Report 交接行 | 修复后交接行 |
| --- | --- | --- |
| discovered | 3,487 | 3,487，明确标注为发现量 |
| executed | 被截断 | 3,220 |
| passed | 被截断 | 3,201 |
| failed / errors | 被截断 | 0 / 0 |
| skipped | 被截断 | 19 |
| execution_state / receipt_scoped | 被截断 | completed / true |

这证明输入缺口得到修复，不能据此推断新 Agent 已经选择安装 Maven，或真实构建成功率已经提升。完整前后文本、原始证据哈希和可复现脚本见[离线反馈对照](/Users/chenhao/Documents/github/Setup-Agent/logs/experiment-fixes-integration-20260911/rocketmq-feedback-comparison.json)。

验证结果：

- Python 完整回归（独立打包测试另跑）：7,919 passed、22 skipped、44 warnings，149.61 秒。
- 隔离 wheel 安装、导入及 CLI 检查：1 passed。合计 7,920 个 Python 测试通过。
- WebUI 相关 17 项测试通过；TypeScript 检查及生产构建通过，生成资源与候选一致。
- 原先工具修复在三个已审阅的重叠文件以外逐字节保留。候选其余文件和原始证据 fixture 同样逐字节核对；真实日志 fixture 的空白不做格式化。

[验证记录与源码哈希](/Users/chenhao/Documents/github/Setup-Agent/logs/experiment-fixes-integration-20260911/validation.json)包含测试日志及源码绑定。本轮没有降低 CI 准入、范围或身份要求：缺范围仍不评分，显式多步骤完成检查也不自动解释任意文字目标或证明任意复合 shell 的内部步骤。
