# 原生执行证据采集：修复与资格验证

2026-09-26。用户选择方案 1：保留 SAG、OpenCode 和 Claude Code 的原生工具，被动采集实际执行证据。当前结论是 **Commons CLI 的直接 Maven 路径通过三组资格验证**；完整 20 项目/60 次正式实验尚未重新准入。

## 修复了什么

公共 controller 不再在 Agent 退出后补跑冻结的 CI 命令。主评分只读 Agent 与其自身 harness 在执行窗口内产生的证据；缺记录就是 unavailable。独立复跑单列，不能补出自主成功。SAG 的 phase 状态机、物理验证、Actor/Advisor 和提示策略保持本轮开始时的行为。

基线继续使用原生 Bash 等工具。Claude 原生 hooks、OpenCode 原生 plugin 把开始/结束通知传给主机上的采集器；Maven 启动观测提供实际 argv、cwd、环境及 PID。只跟踪该 Maven 进程的 OS 终态，不跟踪模型客户端。实际 JVM/Maven 身份来自绑定到该进程的 Maven session，不能用父进程环境或单独的 `--version` 冒充。采集器复用 SAG 的文件、产物、JUnit 与工作树证据逻辑；公共离线评分器只读封存记录。

- `mvn ...; echo done; exit 0` 不能掩盖 Maven 自身的失败退出码。
- 后台任务的启动回执不是成功。Claude 必须通过原生 TaskOutput 返回同一任务的终态，再绑定实际进程退出。
- 工具预览截断时保存原生完整输出文件；没有完整文件就不能把缺据判成完整成功。
- 所有重试保留。最终尝试取代先前尝试；后来仍在执行的重试不能借用先前成功。
- 关闭后的通知不再改写封存证据。调用 ID、会话、任务、命令、提交、字节哈希和进程终态跨层检查。

Claude 的 TaskOutput 是原生工具，本轮补入实验容器的工具允许列表；没有替换为公共执行工具，也没有修改主机上的 Claude 设置。采集过程中存在与 SAG 共用的版本/受限元数据探针，消耗计入执行时间；这些探针不运行 build/test 生命周期。它们和进程跟踪的测量开销仍需在后续正式协议中披露。

## 三组真实验证

固定任务：`apache/commons-cli@e17111798da51037659b3594d9c0b3b525040081`，命令 `mvn clean verify`。使用新的独立资格验证镜像，预装 Java 17 与 Maven 3.9.16；4 CPU、8 GiB 内存、600 秒预算、独立容器和依赖缓存。Actor 均为 gpt-5.4-mini；SAG 保留 mini-high Advisor。

CI 任务和要求未放宽；requirements 中更新的是被动观测程序的冻结字节哈希。该命令是已声明的 smoke 任务，不代表项目所有 CI cell，也不包含其默认目标中的全部附加检查。

| 观测 | SAG | OpenCode | Claude Code |
|---|---:|---:|---:|
| 冻结任务结果 | complete | complete | complete |
| 通过的要求 | 16/16 | 16/16 | 16/16 |
| Build / Test 要求组 | complete / complete | complete / complete | complete / complete |
| 生产 class 文件 | 56 | 56 | 56 |
| 测试 class 文件 | 63 | 63 | 63 |
| 主 JAR | 1 | 1 | 1 |
| 测试报告总数 | 994 | 994 | 994 |
| 实际执行且通过 | 933 | 933 | 933 |
| 跳过 / 失败 / 错误 | 61 / 0 / 0 | 61 / 0 / 0 | 61 / 0 / 0 |
| 执行窗口内观测到的人类介入 | 0 | 0 | 0 |

class/JAR 数字来自退出边界的文件清单，仅用于描述；complete 由命令终态、运行时、工作树、原生目标完成以及新鲜产物/测试报告联合判定。另有 sources、test-sources、tests JAR 各 1 个，不能把它们算成 4 个生产模块。三组质量检查要求通过；文档要求组为 not_applicable。相同测试总数不等于已经证明逐用例身份等价。

资源样本中的最高 cgroup memory.peak 分别约 1.19 / 2.02 / 1.45 GiB，三个容器 OOMKilled 均为 false，观测到的 oom 与 oom_kill 事件为 0；Docker 可用空间始终大于 281 GB。完整峰值由内核累计值提供，但最后一个采样点与最终退出之间仍存在采样间隔。

### 用量只作本次采集记录

| 观测 | SAG | OpenCode | Claude Code |
|---|---:|---:|---:|
| 服务端 input + output tokens | 194,041 | 46,271 | 40,167 |
| Actor / Advisor tokens | 126,427 / 67,614 | 46,271 / 0 | 40,167 / 0 |
| 模型请求数 | 16 | 5 | 7 |
| Agent 执行时间（秒） | 344.19 | 154.88 | 152.39 |
| Controller 全过程时间（秒） | 345.29 | 161.90 | 157.18 |

这些数字包含本次测量开销，使用预装工具链，只有每组一次运行，尚未控制运行顺序/并行资源干扰与采集开销差异。因此不用于论文中的 setup 成功率或效率排名；它们也没有解决此前 SAG 的 token 开销问题。下一轮 Advisor 触发/上下文消融必须另冻结处理条件，不能归因于本次测量修复。

## 验证与被拒绝的原型

- 336 项针对性回归通过，包括执行来源、公共评分器、物理观测、phase 复用、Advisor/native loop 以及独立导出依赖。
- 之后补充失败→成功重试用例，原生边界套件 18 项通过；与前述套件重叠，不相加。覆盖缺少 hook、后台未完成、真实终态、运行时不符、证据损坏、复合 shell 掩盖失败、跨调用终态、晚到通知及重试选择。
- 冻结原生客户端的确定性 hook 探针记录成功、失败、后台与大输出形态。假服务协议探针并非完整模型端到端通过记录，尤其 OpenCode 探针的最后响应未正常结束，不能拿它证明完整运行成功。
- 当前评分器对最终三组档案的离线复算与原 score.json 完全一致；没有覆盖原分数。2,823 个封存文件哈希匹配。
- v1 原型不能可靠解释复合 shell，且把被原生参数验证拒绝的 TaskOutput 错当成缺失 hook。两次结果保留 unavailable；最终方案改用实际 Maven 启动观测，任务等待另行绑定。
- v2 整个客户端进程跟踪方案没有通过 OpenCode 资格验证。诊断期间有主机信号干预，该次已单独标记，排除自主性和效率比较。不能据此声称已经证明其停滞根因。最终 v3 只跟踪 Maven 进程。

七次真实模型尝试（含失败原型）共 53 个请求，已观测 tokens 共 453,588；全部保留在用量清单中。确定性假服务探针没有真实模型请求。最终三次验证没有现场修复/信号干预。11 个本轮临时容器在补存原始跟踪后已清理，旧容器与镜像保留。

## 历史结果与下一步

旧 60 次运行原样保留。本次再次核验审计引用的 435 份源文件，哈希匹配；59 次旧分数来自 Agent 退出后的公共执行，另一项没有公共命令。20 次 SAG 的原生结果单列；40 次基线缺少同等原生物理观测，不能用新采集器追认旧成功，也不能把 unavailable 当失败进行排名。

当前接入范围是直接 Maven、每次原生 shell 调用一个冻结构建命令，允许它前后有普通 shell 操作。以下路径尚需补齐：Gradle、Maven wrapper、单次工具内多个构建命令、并发 shell 与不具备已验证 tracer 的平台。正式基线准入保护仍生效；`--native-capture-qualification` 只用于限定资格验证，不意味着 20 项目已具备统一测量覆盖。

下一步按冻结项目实际用到的 launcher 补齐这些路径，再选最小项目验证；通过后才重新冻结正式 campaign。phase 与物理验证继续保留，Advisor 策略优化另做消融。

## 复现位置

- [可重算汇总脚本](../../../output/agent-execution-boundary-20260926/summarize_qualification.py) 与 [源数据汇总](../../../output/agent-execution-boundary-20260926/qualification-summary.json)：只读档案，重验哈希，不启动构建或模型。
- [三组离线复算核对](../../../output/agent-execution-boundary-20260926/final-qualification-offline-check.json)。每组 `primary-evidence.json` 指出评分根目录和绑定的 run 文件。
- 最终三组档案：[SAG](../../../output/agent-execution-boundary-20260926/commons-cli-qualification-v3/sag/result.json)、[OpenCode](../../../output/agent-execution-boundary-20260926/commons-cli-qualification-v3/opencode/result.json)、[Claude Code](../../../output/agent-execution-boundary-20260926/commons-cli-qualification-v3/claude/result.json)。
- [历史来源审计](../../../output/agent-execution-boundary-20260926/historical-audit-v3/audit.json)、[336 项回归](../../../output/agent-execution-boundary-20260926/final-regression.log)、[18 项边界测试](../../../output/agent-execution-boundary-20260926/native-edge-final.log)。
- [实施计划](../plans/2026-09-26-agent-execution-evaluation-boundary.md) 保留 P3 未完成范围，不把一次 Commons CLI 验证计作完整接入。
