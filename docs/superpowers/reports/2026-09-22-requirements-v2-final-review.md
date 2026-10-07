# Requirements v2：最终交叉审阅

日期：2026-09-22。范围为[设计 §10–§11](../specs/2026-09-21-ci-task-requirement-classification-design.md)及相关指标定义；核对代码与回归，并补充审阅发现的 CI 计数校验和实际开始观测，未采集新项目、运行构建或调用模型。

结论：共同评分、固定计划分母和部分元数据的准入边界已接通，未发现新的“证据不全却认证完整任务”的路径。可以继续按标准重筛和整理资料；这不等于全部项目可以启动正式模型比较，也不等于新 CI 对比分数已经实现。

## 验收检查表

| 项目 | 结果 | 核对依据与限制 |
|---|---|---|
| 相同证据、相同评分 | 通过 | SAG 的 `export_sag_requirements`、campaign 后处理及 portable 离线入口均调用同一个 `evaluator.evaluate`。改变 agent 名称不改变结果的回归通过。不同原始证据能力仍可能产生不同 unavailable；没有把两个适配器声明成能力完全相同。 |
| 固定计划分母 | 通过 | `requirements.aggregate` 以完整 planned 集合计数；缺记录仍为 unavailable。重复项目、计划外项目、混合 agent 和要求身份漂移均拒绝。配对条件子集保留完整组分母。启动失败另记配置问题，不删除计划槽位。 |
| Build/Test 与重叠类别 | 通过 | 新字段 `requirement_stage_success`、`task_success_by_requirement_group` 和 `conditional_requirements` 分开保存；每个项目每类只计一次，未按模块/JAR/testcase 增权。旧 `stages` 不决定新要求分母。 |
| 部分元数据不得升级 | 通过 | `append_known_requirements` 只接受并保留 review_required；`summarize` 不把已知下界中的全绿解释成整个 Build/Test complete 或“不适用”；formal preflight 在派发前拒绝未完整审阅的 requirements。Whisker/RAT 的 Invoker/AntUnit 缺口仍在定义里。 |
| 正式输入与实际 run pin | 通过 | requirements-v2 各组均携带 task、CI target、requirements 和完整 ref；检查原始文件摘要、规范化要求身份及实际收集的 pin。缺输入不降级为自由运行。 |
| CI 数量／身份没有偷换 | 通过，但功能未实现 | 新 `ci_scope_attainment`、`ci_test_count_attainment`、`ci_case_identity_equivalence` 都是 null。旧 executed_count、相等总数或来源审阅中的描述性对账不会填入新分数；不能写成已完成新版 CI 一致性评分。 |
| 物理证据与失败保留 | 通过既有边界回归 | 检查 JDK、工作树探测失败、旧产物/旧 XML、主 JAR 身份、同一步物理报告目录重叠、同模块 fail-fast、独立分支与 native 能力。先前 passed 可以保留，完整任务仍受失败/缺证约束。 |
| 人工干预、时间、tokens | 接线通过，范围有限 | 有事前协议及闭合账本才支持协议范围内零干预；历史数据不补零。失败/超时保留耗时；请求账本保留成功/错误/中断与缺 usage，known_tokens 为小计，完整供应商成本不明时 total_tokens 仍为 null。 |
| 在线判定权威 | 符合阶段边界 | 当前为共同离线评分及 sidecar，未替换原 sealed verdict。设计允许这一实施阶段；不能将旧 verdict 与新要求结果混成同一指标。 |

## 真正仍需处理的点

1. **CI 计数字段的自动准入缺口已接通。** 审阅开始时，`requirements_preflight` 只验证 TargetRecord 身份/字节，没有检查 reported、skipped、assessed 的来源语义。新增 stdlib `ci_count_semantics.py`，并接入 requirements loader、导入器、来源归档、campaign 迁移及正式 CLI/preflight；可用参考必须重验选定 URL/cell、独立 CI index、原始报告、字节摘要和计数守恒。所有正式定义明确声明 available 或带原因的 unavailable。`require_ci_count_comparability=true` 的 campaign 拒绝后者；另外标注的 smoke 任务可以保留 unknown，但没有 CI 比较分母。Commons Net 的 557 reported / 555 assessed / 2 skipped 不再混用。新清单有 10 项可核验计数参考，其中 8 项同时完成要求定义；这不是新 CI attainment 分数，也不证明 Surefire 之外的全部测试类型均被覆盖。
2. **条件统计的开始次数缺口已补。** `reached` 继续表示“外部前置已满足且结果可判定为 passed/failed”。新增要求级 `started=true/false/null`，以及每项目一票的 `started`、`not_started`、`started_unknown` 组计数和 `prerequisites_satisfied`。原生入口能证明已开始，即使随后缺终态或 XML 损坏也保留；明确跳过／同模块 fail-fast 才证明未开始。缺日志不变成 false。复合要求任一实际成员启动即算开始，只有全部成员有明确未开始证据才为 false。这不改变要求完成率或既有 reached 分母。
3. **打包交付指定新的 manifest。** 本轮最终元数据是 `output/sag-benchmark20-requirements-validated-20260922/manifest.json`，11 项 complete、9 项待审阅，另含逐项 CI 计数语义和来源。新增审阅不改变早期包；旧路径下的 4 项/7 项版本、旧 pin 和真实验证成绩都保留，不能与新定义混用。

未将设计允许的 unknown 误列为全局阻塞：缺 testcase 身份、未支持 Invoker/AntUnit、未完整审阅的 wrapper/Gradle 行为、无 RAT 写入来源链、供应商内部计费未知，均应保留缺口。它们会限制相关任务/比较/归因的资格，不能靠放宽 agent 专属规则消除，也不要求先把全部历史项目补齐才开始有记录的候选池重筛。

## 本次验证

运行 campaign、CLI 协议、共同 evaluator、SAG adapter、portable recorder、误报回归、reactor/partial 元数据、干预协议与成本链路共 **246 项测试，全部通过**。这些是代码及归档 fixture 验证，不是 246 次项目实验，也不替代后续 SAG 实际采集就绪检查。

发现计数准入缺口后，新增计数 helper 的 **32 项独立回归**：真实 Net/DbUtils 归档、失败合并计数、缺失案例、冲突/重复 occurrence、URL/cell/字节绑定、重定位副本、旧字段误映射与 stdlib 导入。它不生成案例身份分数，也不修改历史 target。

实际开始观测新增 **21 项回归**，与 evaluator/edges/reactor 检查合跑 **79 passed**；另与两个 recorder、Maven forks、native 能力、HTTP 元数据和计数校验组合验证 **171 passed**。不同组合有重叠，不把这些数字相加成独立测试数。

最终集成回归为 **759 passed、1 skipped**。跳过项是需要另行指定归档的旧 frozen-source replay；本轮 20 项历史回放已单独留存。native_exit 又补 9 项开始状态场景；wrapper 状态分类、来源缺失／篡改及旧 review 不自动放宽均经独立复核。最终包为 `output/SAG_Requirements_v2_Validated_20260922.zip`，771 个文件的校验、Python `-S` 便携录制评分以及真实 Tentacles 重跑均完成，宿主／容器结果相同；完整记录见[实现与验证报告](2026-09-22-requirements-v2-implementation.md)。这些验证仍不等于全部 20 项就绪或 SAG 模型性能改善。

主要核对入口：`scripts/d3r2_campaign.py::requirements_preflight/configuration_failure`、`src/sag/benchmark/requirements.py::summarize/aggregate/paired_comparison`、`evaluator.py::evaluate`、`sag_adapter.py::export_sag_requirements`、`campaign_telemetry.py::finalize_campaign_analysis`、`scripts/requirements_metadata_inventory.py::append_known_requirements`。
