# Advisor 优化：实现与两项目消融

2026-09-26。对应计划：[Advisor 按需咨询与证据读取](../plans/2026-09-26-advisor-demand.md)。

## 结论与使用状态

已实现精简事实包、按需检索和新的自动触发候选。Commons CLI、Commons DbUtils 各三组，6/6 通过各自冻结验收；两项目结果的模块、class、JAR 和测试计数一致。

`adaptive + on-demand` 相对本轮 `phase-entry + all` 的总 token 分别减少 **34.2% / 33.9%**。这是两个预装工具链的小项目、每组一次的机制验证，不能推导大项目成功率或修复可靠性。Actor 花费并未稳定降低。

新策略已由实验配置接入真实 SAG，但全局默认暂时保持 `phase-entry/all`。失败诊断与错误 Actor 假设的模型对照已准备，尚待本批发送授权，完成后再决定默认策略。新接口、功能开关与配置见 README；所有 phase、任务要求、物理验收规则保持原样。

## 实现了什么

1. Actor 可以调用 `advisor(question, context)`，两项都可省略。短说明被标为 **UNVERIFIED**；任务、实际调用、JVM/退出/pending/测试事实由 harness 独立提供。没有新增成功后的强制总结调用。
2. `brief` 提供任务和紧凑事实；`on-demand` 在相同入口包上增加只读 `advisor_evidence`。Advisor 只能列目录、literal search、按行读取和长行续读本次咨询冻结的资料。来源带 ID、原 ref 和 SHA-256；不能打开任意路径、运行命令、写仓库或生成验收结论。
3. 每次咨询内部保留规范的 assistant/tool 对。默认最多三轮读取，随后一次最终回答；原始返回与实际模型输入逐次保存。读取轮也全部计费，不能仅计算最后一句建议。相同 Actor iteration 的多次咨询不会重记已计费请求。
4. 不使用额外摘要模型。沿用已有窗口预算、排序和保护内容分片；实际请求预算包含工具 schema 与 native 消息开销。超长原任务仍被逐片审阅，检索材料可以做带来源/缺失披露的程序摘录，原件不变。
5. `adaptive` 让 Actor 主动咨询，并在现有的重复无进展信号或证据冲突时提供自动兜底。首次普通失败、正常 phase 切换本身不触发。语义事实去重，咨询不取消已计划的执行；原 loop/gate 仍然工作。它不是通用的“struggle 判断器”。

每次咨询仍是新的 Reviewer 上下文；只在本次按需读取的多轮内部保留对话。未引入跨阶段的常驻记忆或昂贵摘要服务。

## 如何做对照

三个方案依次隔离两个因素：

| 方案 | 入口上下文 | 自动触发 |
|---|---|---|
| legacy | `all` | `phase-entry` |
| demand | `on-demand` | `phase-entry` |
| adaptive | `on-demand` | `adaptive` |

两项目固定提交、任务、要求定义、镜像与配置。Actor 都为 gpt-5.4-mini，Advisor 都为 gpt-5.4-mini/high，窗口 65,536、输出上限 8,192。每次全新容器、不共享项目依赖缓存，900 秒上限、4 CPU、8 GiB 上限，最多两个小项目并行；三个批次交错安排方案顺序。没有根据结果替换项目或重跑挑选成功。

本镜像预装 Java 17/Maven 3.9.16，适合隔离 build/test 路径，不能作为从空环境 setup 的耗时基准。Commons CLI 是明确声明的 smoke (`mvn clean verify`)，没有准入的匹配 CI 分母；DbUtils 是冻结的官方 CI 命令 (`mvn -B -f pom.xml -V clean test --batch-mode`)，Java 17、Maven 3.9.16。

公共验收只读取 SAG 自己的执行证据，没有在 Actor 结束后补跑构建/测试。实际运行镜像、实现文件哈希与全部入参在 `output/advisor-demand-20260926/protocol.json`、`implementation-pin.json` 和每组的 manifest/source inventory 中。

## 全部结果

| 项目 | 方案 | 验收要求 | Actor token | Advisor token | 总 token | Advisor 请求 | Agent 秒 |
|---|---|---:|---:|---:|---:|---:|---:|
| Commons CLI | legacy | 16/16 | 117,247 | 67,076 | 184,323 | 2 | 328.5 |
| Commons CLI | demand | 16/16 | 107,774 | 8,351 | 116,125 | 2 | 320.6 |
| Commons CLI | adaptive | 16/16 | 121,253 | 0 | 121,253 | 0 | 319.2 |
| DbUtils | legacy | 8/8 | 82,491 | 68,029 | 150,520 | 2 | 284.4 |
| DbUtils | demand | 8/8 | 123,829 | 22,128 | 145,957 | 5 | 298.0 |
| DbUtils | adaptive | 8/8 | 99,502 | 0 | 99,502 | 0 | 279.7 |

Token 为 provider input + output，包含缓存输入；reasoning 已包含在 output，未重复相加。请求账目、SAG CSV、轮次账单与公共验收账单一致，cost coverage 完整。本批没有额外摘要模型消耗。时间为 Agent 实际运行秒数，完整准备/收尾时间也保存在 comparison.json。

| 项目（三组相同） | 模块 | main class | test class | 主 JAR | reported | passed | skipped | failed / errors |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Commons CLI | 1 | 56 | 63 | 1 | 994 | 933 | 61 | 0 / 0 |
| DbUtils | 1 | 55 | 66 | 0 | 523 | 523 | 0 | 0 / 0 |

两项目各组 build、test、适用的质量检查均 complete；runtime/worktree 前置条件通过。DbUtils 的命令终点为 test，不要求生成 JAR，0 JAR 不是失败。CLI 还生成了 sources、test-sources、tests 三类附属 JAR。文件计数用于描述产物，不能当 Java 源文件完整率。

DbUtils 的 CI 要求完成率为 8/8，测试汇总与 CI 同为 523/523 passed；这只是计数一致，不能推导逐用例身份等价。CLI 的无匹配 CI 限制没有因本次成功而被解除。

## 归因与局限

- **上下文的直接成本下降。** CLI Advisor 从 67,076 降至 8,351；DbUtils 从 68,029 降至 22,128。CLI 两次都直接答复；DbUtils 两次咨询用了五次 provider 请求，真正搜索并读取了归档中的 Maven/Java 与测试证据。按需模式并不保证一次请求。
- **少花 Advisor token 不保证总成本同比减少。** DbUtils demand 组 Actor 从 82,491 增至 123,829，抵消了大部分收益，总量仅下降约 3%。不能把 Advisor 小账单当整体胜利；轨迹长度和模型决策变化也必须计算。
- **常规 phase 咨询确实可在这两项上省去。** adaptive 两项均没有调用 Advisor，也完成同样的物理验收。其 Actor token 都高于对应 legacy，所以约 34% 的净下降主要来自省去 Advisor。CLI adaptive 也比 demand 多用 5,128 token，不能宣称它在每项上优于紧凑咨询。
- **尚未证明失败修复效果。** 两项 live 均未触发需要诊断的真实项目失败；不能据此说明 Advisor 对困难任务无用，也不能证明 adaptive 在失败任务上的可靠性。模型、样本量、预装工具链、并行资源/网络和单次采样均限制外推。

## 验证和封存

- 聚焦回归 **198 passed**，覆盖分页/长行/错误 ref/禁止动作、pending、错误 Actor 版本假设、小窗口保护内容分片、provider 中断、请求账目守恒、重复触发和不取消动作。
- 相邻回归 **133 passed, 1 failed**。失败为已有 `test_missing_report_can_be_submitted_through_a_real_repair_intent`：fixture 未提供当前 report delivery binding；用本轮修改前的 engine 快照也能复现。本轮没有放宽报告交付条件来让测试变绿。
- 六组 **11,366 个封存文件**哈希核对通过；各组 `src/`、`scripts/` 的 **283 个运行时文件**内容与模式一致，本轮七个实现文件也与试验前 pin 一致。README/测试说明在进行期间继续整理，未改变实验运行时。六个本批容器已停止并删除，其他容器和镜像保留。
- cgroup 记录的最大 memory peak 约 **1.19 GiB**；六组无 OOM 记录，磁盘余量未触及预留线。
- 复算入口：`output/advisor-demand-20260926/summarize_live.py`。结构化结果、逐组证据位置：`comparison.json`；完整表：`comparison.md`；清理记录：`cleanup.json`。

## 尚待完成

只读 Advisor 诊断对照准备在相同的 Commons CLI 成功、RocketMQ 失败快照上比较 all/brief/on-demand，再对 RocketMQ 加入明确标注的错误 Actor 假设，比较包含/省略该说明。原始日志已与 receipt 的字节长度和 SHA-256 对上；只读工具无法运行 RocketMQ 或修改仓库。

这批 payload、协议和凭证模式扫描在 `output/advisor-demand-20260926/archive-probe/`，扫描无凭证模式匹配。自动审批拒绝了对外发送，要求明确授权这批归档数据和接收方；已经向用户发起本批确认，目前未发送。`local-previews` 仅验证上下文构造、零模型请求，不能计入模型消融结果。

完成这项诊断比较后，再决定默认采用 `brief` 还是 `on-demand`，以及是否将 `adaptive` 从显式实验选项提升为全局默认。当前实现可直接通过对应配置进行下一轮小规模验证。
