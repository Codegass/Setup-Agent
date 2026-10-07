# 五项目完整 setup 与 Advisor 消融

用户要求：四个相对短、一个相对长的项目；比较 SAG、Claude Code 与 SAG 内部消融，不运行 OpenCode。协议在首次模型调用前冻结于 `output/advisor-five-ablation-20260926/protocol.json`。

## 样本与处理组

从上次 20 项目选择，不修改任务。四个短项目依次为 Tomcat Migration、Commons DbUtils、Commons Net、Commons CSV：它们是严格 CI 引用已验证、直接 Maven 且已完成要求标注的项目中，历史 SAG 时间最短的四项（约 331、359、492、549 秒）。覆盖 test、install 与含文档/质量检查的 defaultGoal。RocketMQ 历史约 1043 秒，19 个模块，已有真实测试失败，用于较长且需要诊断的场景；其严格 CI 引用仍不可用，不能声称 CI 等价。旧运行时间仅用于选择，旧评分与 token 不合并进新数据。

各项目六组，各一次，共 30 次：

时间口径澄清（不改变冻结选样）：上段 331/359/492/549/1043 秒来自旧记录的 `agent_seconds`，不是端到端时间。RocketMQ 旧记录的 `process_seconds` 为 3538 秒，约 59 分钟。旧值仅作选样依据，新实验独立报告两种时钟；核对见 `output/advisor-five-ablation-20260926/analysis/historical-selection-clock.json`。

| 组 | Advisor 初始上下文 | 触发 | 目的 |
|---|---|---|---|
| SAG legacy | all | phase-entry | 当前默认基线 |
| SAG brief | brief | phase-entry | 测试精简初始上下文 |
| SAG demand | on-demand | phase-entry | 测试按需读取带来的收益/成本 |
| SAG adaptive | on-demand | adaptive | 测试 Actor 主动咨询及失败兜底 |
| SAG off | 关闭 | 无 | 评估 Advisor 的净价值 |
| Claude Code | 原生工具 | 无 SAG Advisor | harness 参照 |

保留 phase、物理验证和相同的验收要求。Actor 均为 gpt-5.4-mini，SAG Advisor 为同一模型 high；Claude 继续使用已有协议适配器及原生工具参数。分别披露这些参数，不能称为 reasoning effort 完全相同。消融为逐步配对，不估计因素交互。

## 控制与执行

- 新镜像没有 JDK、Maven 或依赖缓存，所有安装由 agent 完成，计入时间和 token。只预装共同基础工具、客户端和被动观察器。
- 各组固定源码、任务 SHA、requirements、模型参数和镜像。输入只有观察器 launcher/activation 哈希更新，验收要求不变。SAG 使用当前已批准改动的冻结源码，不修改全局默认。
- 每次 4 CPU、8 GiB、无 swap、7200 秒上限，沿用 20 项目上限。没有人工反馈或任务中途修复。所有失败、超时、未完成均保留分母。
- 四个短项目每次最多两个，分组顺序预先轮转；RocketMQ 每次一个。每 15 秒采 RAM/disk，保留 OOM 事件；32 GiB 磁盘保留线。封存校验、独立复算成功后仅删除本轮已经停止的容器。
- Native capture 仍按有界资格验证运行，保留正式准入保护；当前范围仅直接 Maven。采集缺口和项目失败分开。验收不执行 build/test，也不代替 agent 收尾后台任务。
- 控制器/采集错误、hash/账单/源码漂移暂停后续批次。项目自身失败不停止实验。修复采集后必须保留原尝试与成本，另写修订，不以成功重试覆盖。

## 输出与决策

共同验收报告 build、test、文档、质量要求；逐模块结果；主/测试 class、主与辅助 JAR；test reported/ran/passed/failed/skipped 和完整性；总 token、Actor/Advisor 拆分、请求与触发次数；无人值守总耗时、agent 耗时、峰值内存和 OOM。所有请求按 provider usage 计入，不省略检索或压缩调用。

按项目比较 legacy→brief→demand→adaptive，再比较 adaptive→off 和每组→Claude。成本与任务完成情况同时报告，不能仅挑成功子集。五项目单次样本只用于选择下一步设计，不作普遍或统计显著性主张。需要重点检查 RocketMQ 失败时，Advisor 是否获取关键证据、是否改变行动、是否减少无效重复，以及最终物理结果是否改善。

## 本轮启动状态

- [x] 冷启动镜像检查：无 Java/Javac/Maven/Gradle，无 Maven/Gradle 项目缓存；Claude 与观察器版本/hash 匹配。
- [x] 140 份输入文件校验；30 个唯一配对单元；无端口冲突；四短项目 CI 引用重新验证。
- [x] 汇总器用已有 SAG/Claude 封存样本只读核对：token 守恒、跳过测试不计 ran、test 任务允许无 JAR、模块要求结果。旧数据没有写入新实验结果。
- [x] 用户明确“批准全部”后启动 30 次新实验；approval.json 绑定本批协议 SHA。此前审批拒绝发生时没有模型调用。此次授权与执行均不包含旧归档的 32 次诊断咨询。
- [x] 全部封存、独立复算与行为归因：30 个比较单元、32 次尝试；62,263 份归档文件校验通过，全部评分复算一致，实验容器已清理。结果为 25 complete、3 incomplete、2 unavailable；后两项 Build/Test 均 complete，缺口仅在质量检查事件匹配。报告及后续设计判断见 `output/advisor-five-ablation-20260926/report/findings.md`，本轮不更改全局默认。
