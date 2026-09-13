# 十项目 mini advisor 与失败任务 terra low 定向对照

用户要求先在10个项目上运行mini advisor，再把其中失败或partial的项目换为terra low重跑。沿用原 `benchmarks/small-ci-10-v1` 名单；本轮只运行实验和做归因，不叠加新的SAG修复。

## 固定基线

- SAG提交 `6f858af308da6559966cb24340a137455924a7eb`；内容及权限与已测试recovery-v4快照一致，摘要 `83bc9e5bfa5efa56baa2affc00f272037d350d37f2de0450ffc7e5cd87f650f1`，1,039个文件。为本轮保存新快照与提交映射。
- 主模型gpt-5.4-mini medium；mini advisor使用same-model且不指定reasoning effort，terra advisor使用`openai/gpt-5.6-terra` low。64K上下文、extractive压缩、2048输出、每阶段4次咨询上限，150次迭代和7200秒总预算，其他配置保持一致。
- 同一不可变Docker镜像，核对引擎18CPU、16,817,168,384字节内存、aarch64架构。每次独立新容器、冷依赖缓存，串行，先完整归档再清理。引擎资源不是独立容器配额，网络、宿主负载及provider仍可波动。
- 保留原基准每项项目SHA、JDK major、Maven版本、命令、profile及模块/测试范围。主命令直接形成一个验收步骤，同时保留原acceptance-command供生产CI比较使用。
- 五个原CI含deploy的单元继续使用基准已批准的本地install替代，披露发布生命周期差异；本轮不重新定义官方CI目标。

## 名单与运行顺序

1. commons-dbutils
2. commons-net
3. sling-commons-mime
4. sling-commons-osgi
5. creadur-tentacles
6. creadur-whisker
7. commons-jcs
8. httpcomponents-client
9. httpcomponents-core
10. creadur-rat

先完成全部10个mini运行，再冻结复测名单，按相同项目顺序运行terra。复测资格来自权威封存的failed/partial，或权威记录明确显示固定任务未完成。缺失权威归档属于证据问题，先核查归档，不冒充Agent失败，也不悄悄删除或替换尝试。

两组项目定义与任务在首轮前一并冻结。terra只更改advisor模式及其reasoning effort；不继承mini的容器、安装、模型对话、测试产物或诊断建议。SAG在单次预算内自主重试属于该次任务，须完整保留。

## 指标与解释

- mini完整任务完成/10，以及CI可评分/10、CI达标/10；命令完成、封存verdict、模块范围和测试结果分别展示。
- 原始/去重passed、total、failed、errors、skipped及class文件数，与同一官方记录的对应数值并列。CI的alpha_build、alpha_test、alpha只读取生产封存结果；缺据不评分，不写第二套评分器。
- 复测子集中逐项列出mini→terra的任务状态、CI结论、数值、耗时、executor/advisor tokens、咨询内容、实际送达和后续动作。
- 同时披露首轮总用量和升级复测增加的用量；不能把提前结束视为效率改善，tokens不等于费用金额。
- 全部原始失败、partial、资源/运行时故障保留。按工具/执行问题、上下文与恢复行为、项目/依赖问题、证据问题分类；不从可见动作推断模型内部想法。

这是失败任务的定向升级对照。terra未覆盖mini成功的项目，不能直接比较两组总体成功率；本轮没有mini同模型重跑组，不能分离模型更换与再次尝试的影响。最近FreeMarker重复实验已显示同模型结果会波动，因此不凭本轮直接更改全局默认配置。后续修复决策仍须独立控制单一机制做消融。

## 证据与停止边界

重用现有生产runner、验收、归档、清理和模型输入恢复工具。核对快照、manifest、任务、目标、模型请求、receipt、verdict及输入窗口的哈希和绑定。原始官方可移植归档复核为10项Grade A，42个成功模块、7,872条测试记录（7,850通过、22跳过）。HttpClient/HttpCore完整身份超过生产目标上限，保留原比较边界，不把计数相同宣称为身份等价。

完成mini及其不成功子集的terra复测后输出对照和归因报告，停止本轮；不自动扩到23项目、不实现新修复、不commit或push其他改动。
