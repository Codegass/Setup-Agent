# 初步架构重构：运行状态、证据读取与 workspace 占用

本轮基于 `d7311fd`，延续 CI 范围重构后的架构 review。优先修复能用反例证明的状态边界问题；保持模块化单体、现有任务执行模型和 verdict schema，没有新增通用运行框架。

## 已落实的责任边界

| 边界 | 原问题 | 现在由谁负责 |
| --- | --- | --- |
| 命令生命周期 | setup、continue、run-task 各自清部分字段；容器启动失败时旧 validator、authority、结果仍可留在实例上 | `SetupAgent._begin_command` 在 Docker 操作前生成运行身份、清除命令状态，并安装绑定新运行的 unavailable authority；容器就绪后再装配组件 |
| 测试证据读取 | 按项目路径缓存 60 秒的可变结果绕过新 receipt、assessment 和报告字节检查；候选范围通过实例字段跨调用传播 | 每次 `parse_test_reports` 重新读取当前证据，返回本次调用自己的结果；候选解析返回已有的 `TestCandidateResolution`，在调用内传递 |
| workspace 占用 | 项目启动检查 SQLite，后续任务仅使用线程锁，删除又只检查 launch；同一容器可同时接收不同操作 | `LaunchQueueStore` 的事务统一检查活动 launch 与 task/delete 占用；任务在写 session 前取得占用，删除贯穿 Docker 移除过程持有占用 |

命令初始化没有创建新的 `RunContext` 容器。配置、Docker orchestrator 和历史文件仍保留在原有位置；context、tools、engine、validator、finalizer、pin 和运行结果归当前命令所有。旧运行对象不会因为新命令启动失败而被封口。

测试证据只移除了最终聚合结果的缓存。产物、class/JAR 探测缓存仍保留；既有 compact parser、host publication 验证、业务判断和 sealed schema 不变。当前范围和权威证据检查不能由上一次的成功结果代替。

Web 占用使用已有 SQLite 事务和一个小表，OS 文件锁证明 task/delete 的拥有者仍存活。进程退出后，后续事务可以回收其占用，无需 TTL、心跳服务或猜测 PID 是否被复用。锁文件按 owner 唯一命名，避免删除后重建同一路径产生两把独立锁。Docker label 别名在入口统一为真实的 `sag-*` container ID，session、占用和删除使用同一身份。数据库路径先 resolve，保证符号链接也指向同一套锁。

每个 queue DB 还只允许一个 `LaunchScheduler` 持有调度权。固定 OS 锁覆盖启动、恢复、claim、spawn 和现存 monitor 的生命周期；同实例使用小范围的 `RLock` 防止恢复与启动互相穿插。停止调度后，存活的 monitor 仍持有调度权；子进程结束并处理最终写入后才能交接。这里明确限制为单一调度器，没有实现多调度器协调协议。

## Review 与反例

- 同一 validator 第一次成功后，删除或破坏 receipt/assessment，原缓存仍返回成功。现在重读并报告冲突；新 receipt 和新 scope 立即改变本次统计，修改先前返回的字典不会影响下一次调用。
- setup、continue、run-task 的新命令在容器检查回调处接受验证：旧组件、pin 和结果均已清除，receipt ContextVar 与 orchestrator authority 都指向新运行；故意让启动失败不会 seal 旧 state。
- task 与 launch、task 与 delete、delete 与 launch claim 的冲突通过独立 SQLite 连接和并发测试覆盖。子进程持锁、子进程退出以及释放失败分别验证占用可见性和回收。
- 独立 review 发现 `demo` / `sag-demo` 可指向同一容器却取得不同占用，已统一身份。这些测试使用实际临时 SQLite、真实文件锁和短子进程；Docker 与模型调用使用替身。
- 同一数据库的符号链接曾使两个 store 分别查找不同的锁目录，误回收活跃占用；现在归一数据库路径，原反例被拒绝。
- 第二个 scheduler 曾把第一个 scheduler 已 claim、尚未写 PID 的项目判为重启失败，放开 workspace 后第一个仍继续 spawn。现在第二个 scheduler 在 start/recovery/launch-ready 三个入口都无法取得调度权。
- `process.wait()` 异常不能证明子进程死亡。该分支保留活动 launch 行及 owner 并记录错误，task/delete 继续被拒绝。任务线程启动失败则尽力写入明确的失败终态，并释放未执行的任务占用。

## 验证记录

最终完整 Python 回归：**6880 passed、22 skipped、41 warnings**，101.32 秒。包含实际 wheel 构建、安装与 CLI 启动检查，依赖来自本机离线 wheelhouse，没有跳过打包测试。前端源码没有变更，本轮未重复前端测试。

首次完整回归有 6878 passed、2 failed；两处旧 fixture 将组件装配替换成空操作，却依赖预先注入的 engine，和新的命令生命周期不兼容。将替身创建移动到装配入口后，原有任务完成模式、setup TODO 不变以及 survey 失败不能启动模型的断言全部保留；定向 49 passed，随后重跑上述完整回归通过。没有为通过测试放宽生产逻辑。

全项目 mypy：**972 个历史错误、64 个文件**；相对 `d7311fd` 的 980 个错误，按文件与错误内容归一化比较，**新增 0、减少 8**。六个 Web 占用相关模块及 scheduler 的定向 mypy 均无错误。17 个修改过的 Python 文件通过 Black（Python 3.10 目标）、isort，`git diff --check` 通过。

独立 review 覆盖命令与证据读取边界，以及 Web 占用、别名、调度权和异常释放。运行侧定向 80 passed，Web 最终独立复核 99 passed；早期失败反例及后续结果保留在原始记录中。这些集合与完整回归重叠，不叠加为额外测试总数。

验证命令、源码摘要、类型检查增量、完整输出和独立 review 归档在 `logs/architecture-first-pass-20260907/verification.json` 及同目录文件。本轮实现分支为 `AA/architecture-first-pass`，基于 `d7311fd`；验证记录中的工作区状态描述的是测试完成时的快照。

## 明确保留的限制

- 后续任务仍在线程中执行，项目启动仍为 subprocess；本轮统一的是占用规则，没有建立第二套任务队列或迁移 scheduler。
- 无法确认子进程退出，或 monitor 线程无法启动时，调度器会保守保留占用，可能需要服务器重启后恢复；不会把这种异常转换成空闲 workspace。父进程恰在 spawn 成功、PID 尚未写入 SQLite 之间被强制终止的崩溃窗口仍存在，要彻底消除需要子进程启动确认机制，本轮未扩展到该协议。
- 移除测试结果缓存增加了权威证据与 XML 的读取次数。仍使用 compact parser；没有声称提升了真实项目性能。只有测量表明确有成本后，才考虑按已验证内容摘要缓存不可变原始数据。
- `ReActEngine` 和 `PhysicalValidator` 仍然较大。下一次拆分应围绕具体的数据所有权边界，例如构建扫描结果的显式返回值；仅把方法搬到依赖整个 engine 的辅助类，不能减少耦合。
- 本轮没有运行大型项目或新的 Docker/LLM 实验，也没有改变 CI 离线比较的判定权威。此前 commons-cli / gson 的固定提交实验记录仍在相邻报告中。
- 本轮不保证同一个 SetupAgent/orchestrator 可同时运行两个命令，也没有增加历史负面证据的持久化规则。
