# SAG 修复收敛与三个 harness、20 项目重新验证

用户于 2026-09-24 授权：按证据读取、receipt 展示、重复交付的顺序修复和消融，随后重新运行 SAG、OpenCode、Claude Code 各 20 个项目，并汇总具体产出与成本。本批全部重新运行；旧数据只用于开发、归因和测量回归，不替代新的配对记录。

## 顺序与准入

1. 封存共享工作树基线，保留已有修改。在独立源快照验证 A 的来源绑定；新增 offset 提示已被消融否决，不接入。使用另一项目的归档失败，分别检查正确来源、真实分页和重搜恢复，不将只读诊断当作任务完成。
2. B 单独增加由已封存 receipt 确定性生成的模型视图。原命令、退出/中断状态、运行时、测试计数口径和证据缺失项与原始记录入口一起呈现。完整 receipt、原始日志、工具参数说明和物理验收不变。
3. C 单独验证运行时路径交付，复用已观察的环境，不要求模型重复手写同一份元数据。不假设注册版本等于实际 JVM；共同验收器仍独立检查。保留实验接口兼容性，不能赋予 SAG 额外的成功判定权。
4. 在 Commons DbUtils、Commons CLI 上做完整对照，固定提交、任务、工具链、模型/Advisor 参数和资源。通过后才冻结 20 项目的版本与顺序；未经采用的候选不混入正式版本。
5. 按此前 20 项目集合运行 60 个正式 slot，项目内三个 harness 顺序轮换。一次只运行一个容器；每个 slot 新容器、独立依赖缓存。保留失败、超时、OOM、采集异常和所有 token。不得因失败替换项目或择优重跑。

## 相同任务与测量

- 三个 harness 使用同一个 pinned commit、同一个任务与 requirements 文件、同一容器镜像、4 CPU、8 GiB 内存、无 swap。沿用既有每次 7200 秒的共同 agent+验收截止时间和 32 GiB 归档空间预留。
- 使用 gpt-5.4-mini；SAG Advisor 继续 mini-high，另列其全部用量。冻结并披露各客户端实际版本与线上的推理参数。Claude Code 经既有 Anthropic/Responses 适配器使用同一 mini，这是一项实验适配限制，不能描述为原生 Claude 模型比较。
- task 完成、build/test 要求、SAG sealed verdict 与官方 CI 对齐分列。缺 CI 绑定不填零，不称 CI 达标；CLI 保留已声明的 smoke 变体。Cayenne、Jackrabbit 的定义缺口以及 Gson 的编译连续性先审查，不能删除缺口标签或放宽规则让它们准入。
- 增加共同的只读产物观测：agent 结束、共同验收之前；每个验收命令前后。按声明模块及输出目录记录 `.class`、JAR/WAR 和测试报告清单、内容哈希与采集错误。agent 留下的产物与验收重放产生的产物不得混记。
- 模块分列声明数、实际观测数、要求全部通过数。不能把带 POM 的目录数作为已完成模块数。
- `.class` 分主代码/测试/未分类输出；JAR 分主产物、tests、sources、Javadoc、其他。编译数量不是源码覆盖率，一个 Java 源文件可以产生多个 class。
- 测试分 reported、passed、failed、errors、skipped，另列 ran=passed+failed+errors 及口径。按执行、测试池和 identity 去重口径分开；重复构建和验收重放不得加进一次任务的测试完成数。
- 成本列 Actor/Advisor 输入、输出、cached/uncached、总 token、请求数；cached 已包含在 input，reasoning 已包含在 output。用量未知保持 unknown。时间列准备、agent、验收、总时长，另列峰值 RAM、最低可用磁盘、OOM 与主机休眠影响。

## 交付与判断

每个 slot 保留输入/源代码/镜像/版本/配置清单、原始请求响应、日志、独立验收证据、产物清单、资源采样和哈希。产生一张 20 行 × 3 harness 的可比较总表与可下钻的逐模块表，以及 CSV/JSON 数据字典和归因报告。

小样本开发实验用于选方案，正式 60 slot 用于评估。单次项目配对不当作模型方差估计；汇总同时给成功率、逐项目差值和失败/不可测分布，不只比较成功子集平均 token。实现正确性是前置条件，不能为了较低 token 改变 build/test 范围、物理验收或共同接口。

## 执行记录

- 初始检查：主分支 `main`，已有大量合法未提交修改；未清理、提交或推送它们。无正在运行的 Docker 实验，约 296 GiB 可用磁盘。六个历史退出容器先保留，待核实归档后定向清理。
- [x] A 独立归档验证与采纳决定：FreeMarker 4 条只读轨迹、11 次请求。control 24,485 token，navigation 31,622；两组都到达关键原文，候选分页有效但没有显示成本收益，继续隔离，不接入。
- [x] B 实现、回归与独立消融（不采用新增视图）
- [x] C 实现、回归与独立消融（采用可配置的运行时自动交接）
- [ ] 共同产物清单与 20 项目定义预审
- [ ] 两个小项目完整对照及最终冻结
- [ ] 60 个正式 slot
- [ ] 证据复算、数值汇总、直观报告与归因

2026-09-25 进展：

- B 的封存 receipt 视图通过 189 项针对性测试；C 的运行时交接通过 56 项。两个候选、差异补丁和测试记录已保存到 `output/three-harness-twenty-20260924/candidate-reviews/`，均未采用。
- 六次完整任务对照已冻结：DbUtils、CLI 各 control / receipt / handoff 一次。三组来源差异审计通过。用户随后明确确认该批六次，可执行；六次已全部运行、独立回放、封存。此前 FreeMarker 的只读批准没有被扩用。
- 三个 harness 共用的产物观测实现通过 92 项针对性测试。采集边界是 agent 结束且独立验收尚未开始，以及每条独立验收命令的开始、结束；不累计两类产出。
- 新增报告测量函数，通过 17 项测试，并与 15 项旧封存档案逐项核对了测试记录数。模块按声明范围保留分母；JUnit、Invoker integration project、AntUnit target 分开。旧档案没有 class/JAR 清单的部分保持未知。
- 修正执行超时被写成 `not_run` 的归因错误，68 项相关测试通过。命令为 `failed / execution_timeout` 且 `started=true`，具体要求仍按可见证据判定。此修订只用于后续版本，既有评分与六次已冻结候选均未更改。
- 正式 20 项目预审已归档到 `output/three-harness-twenty-20260924/definition-preflight/`：18 项定义标注 complete；DbUtils、Commons Net、Gson 的 CI 引用通过现行严格验证。Cayenne、Jackrabbit 的定义仍需补齐；其他已知执行/采集缺口逐项披露。该预审不是 60 次正式运行的完成记录，也不把缺 CI 的任务计为 CI 等价。
- 产物与汇总、超时归因的组合回归最终为 85 passed；逐模块与测试记录的离线检查并未修改原有判定，也没有为旧档案补造缺失产物数量。

2026-09-25 六轮闭合后的决定：

- 共同验收为 5 complete、1 unavailable；六轮 SAG 原生要求结果均 complete。CLI control 的模型填写了不存在的 `/usr/bin/mvn`，阻止了共同验收启动，不能将其追认为独立验收成功，也不能称为 Java 编译失败。
- C 的两轮都从本次 receipt 自动交付正确路径，共同验收通过。已接入共享代码，默认关闭；正式 SAG 组拟显式开启。接入回归 101 passed、1 skipped，冻结档案另有独立回放。B 的 CLI Actor token 几乎与基线相同，暂不接入。未运行 B+C 组合，不将组合视为已验证。
- C 总 token 在 DbUtils/CLI 分别下降 28.2%/9.3%，Agent 时间分别增加 11.0%/14.4%。DbUtils 的主要 token 差值来自 Advisor 咨询次数和上下文，不宣称全部是格式优化收益；每条件每项目一次仅用于工程资格判断。
- 详细结果、实际 class/JAR/test 数、Actor/Advisor 分拆及决定见 `output/three-harness-twenty-20260924/pilot/decision.zh.md` 与 `pilot/report/`。
- 已完成尚未放入六轮冻结版本的测量修复：安装目标不变时由新源文件与原生安装记录证明连续性；四边界证据下识别编译缓存恢复；保留显式 `mvn`/`mvnw` 选择；绑定官方源字节、命令、提交的 JVM 环境审核。对应测试记录在本批目录，不追改旧分数。
- 重新封存所选 GitHub Actions run/attempt/jobs/logs 后，CSV、DBCP、Tomcat Migration 加入严格 CI 核验通过集合，目前为 6/20。Curator 的重试继承 job 不在所选 attempt 日志包中，继续 unavailable。其余缺口不自动放行。
- 当前 20 项定义校验无 schema 错误，18 项标注 complete、2 项 review_required。`readiness-current/` 列出正式冻结前仍需解决的测量缺口；正式 60 次尚未启动。
- 原生产物小型验证已完成：Maven 3.9.16 第二次 install 的目标文件没有变化，仍可由本次新生成源文件及安装日志独立复算连续性；Gradle 8.5 的 GZIP Tar 默认文件名为 `.tgz`。因此只修正 FreeMarker 四个声明产物路径，不改变任务命令、产物格式或旧结果。第一轮验证因基础镜像缺 Python 提前结束，失败记录保留；补装后整轮通过，两个验证容器均在归档核验后删除，无 OOM。
- 自动交接与官方 JVM 环境要求合并后的配置/共同 brief 回归另有 16 passed。正式冻结前仍需处理原计划明确保留的编译连续性、共享测试池、quiet/嵌套 runner 证据和 Gradle 进程收尾问题。


2026-09-25 继续执行：

- 补齐 Surefire 一次重试造成的 XML 汇总头冲突处理，要求原生类汇总、全组汇总与逐用例记录相互印证；离线 Curator 单要求消融可复算，不改旧评分。
- 共同验收器增加显式 Gradle daemon 收尾，所有 harness 采用相同准备策略，仍执行原 argv。两项小型原生验证通过，无模型调用，无 OOM；封存后删除专属容器。
- 嵌套 Gradle 要求改为匹配父执行范围内的真实任务结果，避免 Maven 外层成功误代替内部任务。相关回归 95 passed；Cayenne 的定义仍未放行。
- Maven EventSpy 被动采集原型在 quiet 模式下捕获 23 条事件，并分别保留两次 Surefire 测试池（2 ran 与 1 ran/1 skipped），成功观察新编译与复用的 class 差异。原型还未接入评分；输入连续性、跳过与失败路径正在进一步验证。
- 具体证据、测试日志及剩余缺口见 `output/three-harness-twenty-20260924/measurement-repairs.zh.md`。用户没有待决问题；正式 60 slot 仍需先闭合原计划的测量准入。
