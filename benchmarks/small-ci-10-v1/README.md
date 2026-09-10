# 10 个小项目的官方 CI 基准（v1）

10 项全部具备同一次官方 CI 构建的完整日志、JUnit testcase 明细、源码 SHA 和构建范围。合计 **42 个 CI 成功模块、7,872 条测试记录（7,850 通过、22 跳过、0 失败）**。汇总用于核对数据量，达标率以 10 个任务为分母。

| 项目 | 源码版本 | CI 模块 | 测试记录 | 通过 | 跳过 | JDK major | Maven | 官方 CI |
|---|---|---:|---:|---:|---:|---:|---|---|
| commons-dbutils | 1.9.0-SNAPSHOT | 1 | 523 | 523 | 0 | 17 | 3.9.16 | [run](https://ci-builds.apache.org/job/Commons/job/commons-dbutils/455/) |
| commons-net | 3.12.1-SNAPSHOT | 1 | 557 | 555 | 2 | 17 | 3.9.11 | [run](https://ci-builds.apache.org/job/Commons/job/commons-net/826/) |
| sling-commons-mime | 3.0.1-SNAPSHOT | 1 | 8 | 8 | 0 | 17 | 3.9.16 | [run](https://ci-builds.apache.org/job/Sling/job/modules/job/sling-org-apache-sling-commons-mime/job/master/349/) |
| sling-commons-osgi | 2.4.3-SNAPSHOT | 1 | 28 | 28 | 0 | 17 | 3.9.16 | [run](https://ci-builds.apache.org/job/Sling/job/modules/job/sling-org-apache-sling-commons-osgi/job/master/335/) |
| creadur-tentacles | 0.2-SNAPSHOT | 1 | 2 | 2 | 0 | 17 | 3.9.16 | [run](https://ci-builds.apache.org/job/Creadur/job/Creadur-Tentacles/2430/) |
| creadur-whisker | 0.2-SNAPSHOT | 8 | 155 | 155 | 0 | 17 | 3.9.16 | [run](https://ci-builds.apache.org/job/Creadur/job/Creadur-Whisker/2453/) |
| commons-jcs | 4.0.0-SNAPSHOT | 7 | 443 | 443 | 0 | 17 | 3.9.16 | [run](https://ci-builds.apache.org/job/Commons/job/commons-jcs/726/) |
| httpcomponents-client | 5.7-alpha2-SNAPSHOT | 9 | 2775 | 2762 | 13 | 17 | 3.9.16 | [run](https://ci-builds.apache.org/job/HttpComponents/job/HttpComponents%20Client%205.7.x%20Java17/225/) |
| httpcomponents-core | 5.5-beta3-SNAPSHOT | 6 | 2232 | 2228 | 4 | 17 | 3.9.16 | [run](https://ci-builds.apache.org/job/HttpComponents/job/HttpComponents%20Core%205.5.x%20Java17/210/) |
| creadur-rat | 1.0.0-SNAPSHOT | 7 | 1149 | 1146 | 3 | 21 | 3.9.16 | [run](https://ci-builds.apache.org/job/Creadur/job/Creadur-Rat/2697/) |

每项完整 SHA、命令与 matrix 单元见 [manifest.json](manifest.json)，评分目标是 [targets](targets) 中的冻结记录。原始证据保存在 [official-ci-evidence.tar.gz](official-ci-evidence.tar.gz)，使用同名 `.sha256` 文件核对。

## 数值口径

- α_build = SAG 成功模块与 CI 模块的交集 / CI 成功模块。
- α_test = min(SAG reported, CI reported) / CI reported；reported 包含跳过，非跳过执行数另列。
- α = min(α_build, α_test)。直接读取生产 evaluator，不另写评分器。
- 缺少执行授权、范围或计数时不评分，不能回退为 1/1；仍展示能核验的原始数字。
- 执行结论、CI attainment、生命周期 parity 分列，数字相同不能替代身份和范围校验。
- HttpClient 与 HttpCore 超过生产 target 的 2,000 个身份上限，冻结记录保留完整计数而不存部分身份；原始归档保留所有 testcase。它们的数值比较不能被描述为逐测试身份等价。

## 运行边界

SAG 在固定 SHA 执行指定 build/test 命令。五个含 deploy 的 CI 单元预先改为本地 install，保留其他构建、测试和 profile 范围；独立发布、site、Sonar、其他 JDK 单元不属于本次目标，生命周期差异保留披露。DbUtils 官方执行 clean test，构建事实覆盖编译，未要求打包 JAR。

JDK 按原定 matrix 匹配 major。JCS、HttpClient、HttpCore 的官方补丁版本未保留，因此不猜测。Linux amd64 容器通过 ARM 宿主仿真运行，官方缓存和机器不同，耗时只作描述。每个项目一个新容器，串行运行，不共享依赖缓存，不按 SAG 结果换项目。

Kafka、RocketMQ 和其他大型项目不进入名单。Commons CLI/Gson 未取得完整官方 JUnit pool，保留为发现记录，没有用日志聚合数冒充 Grade A。Numbers/Geometry 超出本次小项目范围。

## 验证与复用

离线检查见 [validate.ipynb](validate.ipynb)。它直接解包并校验本目录的原始证据，不依赖抓取机器上的 logs 绝对路径。脚本拒绝覆盖既有基准和运行。原始证据 URL、字节哈希和抓取时间随压缩包保留。

运行参数保存在 [runner-config.json](runner-config.json)：同一模型 gpt-5.4-mini、75 次迭代、1,200 秒预算，每项目独立容器。提供干净的 SAG worktree、完整提交 SHA 和不可变 Docker image ID，即可建立下一轮：

```sh
UV_CACHE_DIR=/private/tmp/setup-agent-uv-cache PYTHONPATH=. uv run --no-sync python scripts/small_ci_bench.py prepare --benchmark benchmarks/small-ci-10-v1 --out logs/small-ci-next --source CLEAN_WORKTREE --sha FULL_COMMIT_SHA --image IMMUTABLE_IMAGE_ID
UV_CACHE_DIR=/private/tmp/setup-agent-uv-cache PYTHONPATH=. uv run --no-sync python scripts/small_ci_bench.py run --out logs/small-ci-next
```

本次正式运行是 `small-ci-10-v3-20260909`。v1 是旧 Maven 版本支持的单项探针；v2 因命令文案的句号歧义中止，4 项记录全部保留，其中 OSGi 在初始化期间主动停止。这些预跑不混入正式 10 项结果，也不与正式结果拼接成配对进步结论。

本地数值对照：[comparison.md](../../logs/small-ci-bench-10-v1-20260909/report-v3/comparison.md)。
