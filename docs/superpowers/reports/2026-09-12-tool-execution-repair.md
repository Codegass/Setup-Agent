# 工具与执行修复：实现、实跑结果与归因

统一 JVM 覆盖机制已经实现并通过真实容器验证。Commons CLI、Gson 均完成 build/test 冒烟任务。FreeMarker 的构建范围误判已修复，但修复前两次、修复后两次，以及同步工具说明后的单次跟进，均只完成 1/3 验收步骤：Agent 没有安装 GraalVM，Native Image 两步未完成。**本次不能宣称 SAG 的完整 CI 任务完成率提高了。**

## 通用 JVM 选择

GraalVM 作为发行版，与 Java 版本、实际可执行文件和所需能力进入已有的环境覆盖机制。下载方式可以不同，注册、测量、激活和派发验证共用 EnvTool，没有按项目名称分支。

```python
# 普通 GraalVM JDK，不隐含 Native Image 要求
project(action="provision", java_version="21", java_distribution="graalvm")

# 明确要求 Native Image 能力
project(action="provision", java_version="21", java_distribution="graalvm",
        java_capabilities=["native-image"])
```

- 只修改版本时保留已选发行版和能力；显式选择另一发行版才替换。未知发行版没有安装方式时明确返回缺口。
- 解析真实 `bin/java`，测量版本和发行版，统一 `JAVA_HOME`/`PATH`；激活后再观察派发环境。调用者的 `version` 字段只是声明，不能覆盖实测事实。写入已发生但验证失败时如实记录。
- 最低 Java17 保存为 `[17,)`；严格 Java17 保存为 `[17,18)`。历史错误限定于源版本、执行域和命令，Java21 满足最低 Java17 时保持 21。
- 首次派发与自动重试各自记录实际 JVM；成功重试也保留 `dispatch_probe`。版本区间使用已有类型，没有新增一套约束体系。
- 能力检查与发行版独立。普通 GraalVM Java 任务不要求 `native-image`；JDK provisioning 要求 `javac`。

主要实现：[SystemTool](/Users/chenhao/Documents/github/Setup-Agent/src/sag/tools/internal/system_tool.py)、[EnvTool](/Users/chenhao/Documents/github/Setup-Agent/src/sag/tools/internal/env_tool.py)、[JdkPreflight](/Users/chenhao/Documents/github/Setup-Agent/src/sag/tools/internal/build_preflight.py)、[BuildTool](/Users/chenhao/Documents/github/Setup-Agent/src/sag/tools/build/build_tool.py)。

安装路径依据[官方 Linux 说明](https://www.graalvm.org/jdk21/docs/getting-started/linux/)和[官方下载 URL 规则](https://www.graalvm.org/downloads/)。当前 archive provisioning 支持 Linux x86_64/aarch64。

## 真实机制验证

在全新容器中，不调用 Agent 或 advisor，使用生产 DockerOrchestrator、host publication authority 和公共 ProjectTool 路径完成：

1. 安装并激活 OpenJDK17。
2. 切换到 GraalVM21，实际版本 **21.0.12**，验证 `javac` 与 `native-image`。
3. 调用 `JdkPreflight.run("17", runtime_constraints=["[17,)"])`，前后实际可执行文件、版本和发行版完全相同，未安装或降级。
4. 将极小 Java main 编译为 Native Image 并执行，退出码 **0**，输出 `JVM_SELECTION_OK`。Native Image 编译约 **89 秒**，峰值 RSS **2.02GB**。

官方下载 archive 的 SHA256 为 `b007ff64c425f85bbe0e686107044fba6ca5054a7e89271a473767f546aaddc1`。结果、输入调用、输出及工作区归档已核验，容器已删除。此项证明共同 JVM 路径可用，不计为 SAG 完成 FreeMarker。

证据：[机制结果](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-execution-repair-20260912/jvm-selection-smoke/result.json)、[最低版本与实际 JVM 观测](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-execution-repair-20260912/jvm-selection-smoke/mechanism-proof.json)、[Native Image 编译运行](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-execution-repair-20260912/jvm-selection-smoke/native-compile-and-run.json)。

## 数值结果

所有 Agent 运行均关闭 advisor。测试总数含 skipped，表中分子仅为 passed；表内已有 JUnit 报告的 failed/errors 均为 0。Native Image 编译失败与二进制运行缺失在验收步骤中单独披露。失败工具调用包含已恢复的参数、引用和阶段声明错误，不能当作项目失败数。tokens 是调用记录累计用量，不是单次上下文大小或直接费用。

| 项目 | 版本/重复 | 验收步骤 | 原始通过/总数 | 去重通过/总数 | 跳过 | class 文件 | 失败工具调用 | 秒 | tokens | 封存 verdict |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| commons-cli | v3 after-1 | 1/1 | 933/994 | 933/994 | 61 | 119 | 2 | 490.59 | 173,333 | success |
| gson | v3 after-1 | 1/1 | 4,846/4,866 | 4,846/4,866 | 20 | 1,319 | 1 | 661.19 | 119,259 | partial |
| freemarker | v3 before-1 | 1/3 | 1,526/1,529 | 1,388/1,391 | 3 | 2,083 | 2 | 709.67 | 165,189 | partial |
| freemarker | v3 after-1 | 1/3 | 1,526/1,529 | 1,388/1,391 | 3 | 2,083 | 4 | 741.65 | 189,868 | partial |
| freemarker | v3 after-2 | 1/3 | 1,526/1,529 | 1,388/1,391 | 3 | 2,083 | 3 | 713.67 | 182,998 | partial |
| freemarker | v3 before-2 | 1/3 | 1,526/1,529 | 1,388/1,391 | 3 | 2,083 | 5 | 749.54 | 193,616 | partial |
| freemarker | v4 after-1 | 1/3 | 1,526/1,529 | 1,388/1,391 | 3 | 2,083 | 4 | 731.73 | 190,412 | partial |

FreeMarker v3 组均值：修复前 **729.61 秒 / 179,402.5 tokens**，修复后 **727.66 秒 / 186,433 tokens**。只有两个重复，不据此判断性能提升。v4 是单次说明同步跟进，独立披露。

[原始对照表](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-execution-repair-20260912/comparison.html) · [原始 JSON 与耗时分段](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-execution-repair-20260912/comparison.json) · [说明同步跟进](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-execution-repair-20260912/comparison-v4.html) · [跟进 JSON](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-execution-repair-20260912/comparison-v4.json)

Commons CLI 与 Gson 固定源码 SHA，均执行 JDK17 下的根目录 `mvn clean verify`，默认模块和测试，不通过添加跳过参数改写任务。它们属于小项目冒烟，不宣称完整官方 CI 等价。

Gson 的 `partial` 来自 `build_requirements_unavailable`：要求文件存在，记录 Java `[17,22)`，但静态解析器无法判定 `disable-error-prone` profile 的激活条件。实际 **8/8 reactor 模块成功**，命令返回 0，任务完成。相关要求解析和环境冲突函数在修复前后相同；保留封存结论，也不把这项证据局限改写为 build/test 失败。

## 归因：工具修好了什么，Agent 还缺什么

**构建范围误判已重复消除。** 两次基线的 Gradle `clean build` 均成功，旧验证器却以 `build_partial` 拒绝：期望范围漏掉根项目/buildSrc，测试承载模块出现 2/1。两次 v3 修复版均正确认证 JVM build，生产源码计数从错误的 **2** 变为 **669**，实际 class 文件仍为 **2,083**。封存冲突从 `build_modules_incomplete`、`build_coverage_scope_unverified`、`acceptance_task_incomplete` 缩减为仅 `acceptance_task_incomplete`。v4 保持相同结果。

这依靠明确的 Gradle 项目身份和项目拥有的约定 source set 路径识别，并用实际测试报告确认模块承载测试。任意 Gradle DSL 自定义路径仍不在本次静态识别范围。超出分母的计数返回冲突，缺少 CI 范围仍不评分。

**本轮 Native Image 失败来自未尝试安装。** 五次 FreeMarker 运行只安装了 OpenJDK17，没有调用 GraalVM provisioning；nativeCompile 在 Java17 下返回明确的 `native-image wasn’t found`。它们没有发生 Java21 被自动降回 17，因此不能把这些失败归因于自动降级。基线一次模型报告声称“attempted with Java21”，与 receipt 的 JVM17 不符，本报告采用实际观测。

**同步说明仍不足以促成动作。** v3 系统提示仍将 provision 简写为 `java_version 或 packages`。v4 同步发行版、能力和 Maven 参数，以及 Java/Maven executable 对应关系，明确 provision/env 在 build/test 阶段可用。四次 v3 的封存输入均保持旧说明，v4 的 **26/26** 输入窗口确认收到新说明。v4 最后在 `phase(done)` 的 key_results 中明确写出“先建立 GraalVM21/native-image 运行时，再编译和执行”，却没有执行安装。已知下一步被留在报告中，任务仍为 1/3。

因此，当前剩余问题是 **任务恢复和结束策略**。缺少可用 JVM 安装机制这一点已被独立真实测试排除；本轮错误文本、CI 要求和工具能力说明也确实进入了模型输入。下一轮应围绕通用的未完成任务恢复决策做设计与对照，避免继续增加某个项目专用规则，或把诚实的 partial 报告误当成完成了任务。

证据：[v3 模型输入说明核验](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-execution-repair-20260912/v3-model-prompt-proof.json)、[v4 模型输入说明核验](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-execution-repair-20260912/v4-model-prompt-proof.json)、[v4 具体输入与随后动作](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-execution-repair-20260912/freemarker-v4-after-1/model-windows/freemarker.json)、[v4 封存 verdict](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-repair-v4-freemarker-after-1-20260912/runs/freemarker/container-evidence/.setup_agent/verdict.json)。这些是可见模型输入和工具调用，不推断模型内部想法。

## 当前调用信息与其他修复

先行 v2 Commons CLI 真实失败：Maven3.8.7 不满足 Enforcer `[3.9,)`，任务 0/1、0 测试、332.12 秒。构建确实执行到 clean/enforcer；模型收到不匹配及没有兼容候选，却没有安装入口，随后 `phase(blocked)` 并把它描述为 runner 未执行。**12/12** 封存输入窗口完整。

进一步定位到公共 build 包装层丢弃底层 facts。v3 保留 facts，并为 Maven 版本错误提供实测版本、完整约束、经当前安装目录与完整区间核验的可用 provision 调用，披露 `runner_dispatched=true`。旧 suggestions 没有整体恢复；提示只是可用信息，不替模型执行或增加结束限制。

v3 两个小项目先尝试 Java+Maven 联合 provision，被单次单路由接口拒绝；随后按当前返回的接口说明拆为两次调用并成功。它们主动提前安装 Maven3.9.9，没有触发新 Enforcer 提示，因此不能把成功单独归因于这一提示。封存窗口分别 **23/23**、**17/17** 完整。

搜索空结果现在明确为无证据，网络错误单独披露；HTTPS 页面读取保留链接和完整文本，沿用已有文件/ref 输出路径，超限明确失败。另一个已观察到的情况是 Agent 给 output ref 加上错误的 `job:` 前缀；工具实际已返回正确 target，Agent 有时重复一次错误才改正。本轮不把这种行为归因为缺少可读输出。

## 实验与证据边界

FreeMarker 固定 `apache/freemarker@e156e5faf9325e7ec05b781411bbf331aff98dfc`，来自[官方 CI job](https://github.com/apache/freemarker/actions/runs/33969757288/job/101316060125)，三步为：

1. JDK17、Gradle8.5 wrapper：`./gradlew -Pfreemarker.signMethod=none -Pfreemarker.allowUnsignedReleaseBuild=true --continue clean build`。
2. GraalVM21：`./gradlew :freemarker-test-graalvm-native:nativeCompile`。
3. `./freemarker-test-graalvm-native/build/native/nativeCompile/freemarker-test-graalvm-native`。

v3 顺序 before-1 → after-1 → after-2 → before-2；每次新容器和依赖缓存，源 SHA、任务文件、镜像、资源、预算相同。v4 使用同一任务，仅同步通用工具说明。CI 完整测试池和模块范围缺失，**alpha/CI 分数始终 unavailable**，不以本地数量补造 CI 分母。

四次 v3 的 suite 摘要及保留的 50 条用例样本一致；样本不是完整测试池，模块限定的逐用例执行记录仍不可用。[本地测试比较边界](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-execution-repair-20260912/freemarker-local-test-comparability.json)保留了具体证据。

已核对 runner 结果、collected 数据、封存 verdict、receipt、run pin 的运行与源码身份。验证范围为 CLI 和封存产物；API/WebUI 本轮未启动，`surface_ok=null`。耗时分段来自观测时间戳：首次动作前含初始化和首次模型请求，Agent 阶段含工具执行及网络等待。

## 验证与源码状态

- 核心修复 v3 全量 **8,001 passed、22 skipped**，隔离打包 **1 passed**。[全量日志](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-execution-repair-20260912/validation/v3-full-pytest.log) · [打包日志](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-execution-repair-20260912/validation/v3-packaging-pytest.log)。
- 最后的 v4 说明同步后，相关提示渲染和工具接口回归 **110 passed**。[相关回归日志](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-execution-repair-20260912/validation/v4-focused-pytest.log)。
- 修复前完整快照：`badc4d3609921d9d92ed946e4585a28c1a306d6b8e8fea7f4374eb9079095845`。
- v3：`ba9ef23e99665da314ebb33cbbae7499a07c2c363a64c5ec8d6c8fd2a741ea36`。
- 当前 v4：`b6f998a72dc70b6dab5e1551375899c75ed4bd25584fb727ac7e6b1b2b0fe986`。与 v3 仅两处文件的能力说明不同：[确切差异](/Users/chenhao/Documents/github/Setup-Agent/logs/tool-execution-repair-20260912/v4-capability-descriptions.patch)。
- 快照包含工作区改动，base commit 不能替代完整源码身份。原有 advisor 改动保留，仅在同一 YAML 中有本次已披露的工具说明修改。代码尚未提交。
- 七个正式/跟进 Agent 容器和独立 JVM 机制容器均已归档删除。

第一次清单被拒绝发生在容器和模型启动前；v2 Gson 在基础初始化时主动取消，0 模型 tokens，已归档清理。两者作为准备/取消记录保留，不算 Agent 失败。v2 Commons CLI 的真实失败单列为修复依据，不混入冻结 v3 的正式对照。
