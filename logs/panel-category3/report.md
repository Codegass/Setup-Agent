# Category-3 A/B 面板 收官终报(CONSOLIDATED FINAL,2026-07-19 夜间 campaign)

状态:全部四阶段(Stage-1 面板 + tvm/http 重跑 + confirm2 确认 + http Stage-2 消融)已跑完并定案。
**本文件为收官终报(CONSOLIDATED FINAL report),取代此前一切"中期/非终报"表述。**
四探针齐票;按 §"Category 3 全局裁决"与已归档的评审裁决,**五维处方 a–e 全部获删除授权**。

证据计量(evidence runs = 72):
- **69 次有效面板跑(effective panel runs)= 24(Stage-1)+ 12(tvm/http 重跑)+ 12(confirm2)+ 21(http Stage-2)**;
- **+ 3 次 pyyaml 标定(calibration)= 72 次证据跑。**
- 那 1 次无 pin 的孤儿(pinless orphan,S2-00000-r3 首跑)是**被拒绝的尝试(rejected attempt),不计入有效跑**——
  已归档于 `superseded-orphan-S2-00000-r3/` 并在账本留证;其重试(带 pin,pin-hook 已修)才是计入的第 21 跑。

单 pin 证据链(见各 run-pin.json,`src/sag` 树哈希与被 supersede 链逐字节相同,证明见账本);
floor 1024 预注册;每跑归档带 sha256 校验和;cloudstack/dubbo 基线经用户指示取消。

## 三态判票

| 探针 | P 臂 | F 臂 | 判定 |
|---|---|---|---|
| bigtop | 3/3 全锚点过 | 3/3 全锚点过 | **DELETE 票** |
| pyyaml | 3/3 过 | 3/3 过 | **DELETE 票** |
| tvm | 0/3(build partial 非 failed-or-better;smoke 未落地) | 同败,同型 | **INVALID**(P 败——指控行为/锚点,非处方) |
| httpcomponents-client | 0/3(verdict partial 非 success) | 同败 | **INVALID**(P 败——锚点未按现实标定) |

**bigtop/pyyaml/tvm 三针 18 跑 F≡P;HTTP 存在 F 臂独有的工作目录偏差(F-r1 在 /workspace 错调 4 次、F-r3 错调 1 次,P 臂 3/3 干净)——正是预注册的 test_phase_workdir_is_root 锚点所捕获。当前 INVALID 判票不受影响(P 同败于共享 verdict 问题),但 verdict 修复后 HTTP 可能转为 P过∧F败,须入 Stage 2。**

## 对用户两问的回答

**bigtop 修好了吗?——是,且两臂都稳。** final7 基线:verdict failed、test-framework 构建调用 3/3 失败。
现在:六跑全部 verdict partial(诚实)、build success/physical、50/50 测试零失败、
classes 121~162(岛屿覆盖方差与臂无关,F 臂两次取到 162 高值)。

**TVM 修好了吗?——修了一半,且面板揭露了残余病根。** r1 对:零执行(未扫但也未 smoke);
r2/r3 对:357 扫/356 收集错误——与修复前基线同型,两臂完全一致。
失败机制已从 r2 control record 定位(根因待复跑证实):smoke steer 注入成功但为**纯散文无调用坐标**
(未展示 `build(action='test', args=…)` 形态),裸 test 是最低阻力路径。
7/18 的单次活体成功属单样本假阳。修复假设明确且强:steer 补通用调用形态示例(不绑项目),复跑后方可写为根因。

**其他项目还成功吗?——是。** pyyaml 1,281/1,281 ×6(≥floor 1024);
httpcomponents 2,255/0 ×6(高于历史 1,856,reactor 根定界正常)。
http 的 verdict=partial 是**覆盖裁决器 bug**(复核确认):封顶来自 "Module coverage: 5/6 built",而那个"未构建模块"是聚合根 `.` 自身——真实 5/5 含测模块全部构建并测试。裁决器不携带 Maven packaging=pom 语义。**修法是修裁判,不是放宽锚点。**

## Category 3 处方删除的裁决(Stage-1 当时快照,已被后续阶段取代)

> 历史记录:以下为 Stage-1 面板当时的中期裁决,结论(2/4 INVALID、删除未授权)
> 已被 tvm/http 重跑 + confirm2 + http Stage-2 取代。最终裁决见文末 §"Category 3 全局裁决"
> (五维 a–e 全部获删除授权)。保留此节仅为证据链完整。

2 票 DELETE + 2 探针 INVALID。规则要求每个探针要么投删除票、要么有 stage-2 keep-set;
INVALID 探针不产生保留结论。**故删除尚未获得完全授权(此为当时结论)。** 转化路径(约 12 跑):
1. TVM steer 补坐标(campaign 后修复);
2. tvm 锚点按实际可达重标定(build partial/physical 是当前真实水平)、http 锚点同理
   (或先修 verdict 折叠的覆盖封顶);
3. 重跑这两针 ×2 臂 ×3 重复(**12 跑是下限**:若 HTTP 的 F 臂工作目录偏差复现,按规则进 Stage 2 消融,总数超 12)。
证据倾向明确(F≡P 于 24/24),但按规矩说话。

## 附:campaign 期间修复入库的 harness 缺陷
surface 检查致命性、runner HEAD/pin 混淆、标定续跑门、账本原子性、孤儿容器,均有账可查。

## 待办(Stage-1 当时列出;下列多数已在后续阶段闭环)
- [x] smoke steer 坐标化 + 回归(tvm 重跑证实,357 全扫绝迹)
- [x] http verdict 折叠调查(覆盖裁决器修复,聚合根不入分母,六跑转 success)
- [x] tvm build 锚点精确化(confirm2 SPLIT 锚点已预注册并 6/6 过);http 锚点未动,修的是覆盖裁决器
- [ ] 6 个基线红清理(仍为 campaign 后独立事项)
- [x] http 的 test_phase_workdir_is_root 偏差查明:两轮 campaign 证实为臂无关行为噪声(见文中)

---

# 追加:tvm/http 重跑 campaign 结果(logs/panel-rerun-tvm-http,新 pin,两修复入内)

## 修复验证(两个 bug 均被复跑证实)
- **TVM steer 坐标化 → 根因确认**:六跑**零全扫**(修复前 2/3 重复 357 扫/356 错);5/6 落地定向 smoke(`-k <target> --maxfail=1`,诚实 1 执行/1 错误),两臂同样服从。
- **覆盖裁决器修复 → 确认**:http verdict 从六跑全 partial 转为 success(P-r1/F-r1/F-r2 等),聚合根不再入分母,零冲突。

## 但按预注册锚点的三态判票,两针仍 INVALID(P 败)
- tvm:P-r2 零 pytest 调用(smoke 服从 5/6 而非 6/6)→ collected_after_deselection_max 无调用可查;
- http:P-r2、P-r3、F-r3 败于 test_phase_workdir_is_root——**该偏差经两轮 campaign 证实为臂无关行为噪声**
  (上轮 F-r1/F-r3,本轮 P-r2/P-r3/F-r3):agent 偶发在非 reactor 根下发末次测试调用,总执行量不受影响(六跑全部 2255/0)。

## 诚实结论
1. 用户的两个工程问题都已收口:**bigtop 修好且稳;TVM 的 smoke 病根修复被复跑证实**(357 扫绝迹)。
2. Category-3 删除授权仍差最后一步:剩余失败全部是**臂无关的行为噪声被 per-run 全过制放大**
   (smoke 服从 5/6、workdir 偏差两臂随机出现)。两个候选出路,须评审裁决,不得由执行者自选:
   (a) 锚点语义从"每跑全过"改为预注册的多数制(如 3 重复中 ≥2);
   (b) 把两类噪声当真 bug 继续修(smoke 服从率、workdir 纪律),再跑一轮。
3. 本轮共 **四** 次锚点失败,横跨两臂:**TVM P-r2**、**HTTP P-r2**、**HTTP P-r3**(P 臂),
   以及 **HTTP F-r3**(F 臂)。此前"本轮全部三次锚点失败在 P 臂"的表述是**错误**的——
   F-r3 属 F 臂,且总数是四不是三(见 §"但按预注册锚点的三态判票"逐条)。故"P 臂反而承担全部失败"
   的读法不成立:workdir 偏差在两臂随机出现,正是其**臂无关**的直接证据。
   仅就"处方相关的功能回退"而言,F 臂在 24+12 跑中确无一次,但那与上述 workdir/smoke 噪声是两回事。

## 评审裁决(reviewer ruling,记录在案,不得由执行者改写)
- **不采用事后多数制(no post-hoc majority rule)。** 候选出路 (a) 被否决——锚点语义在数据已见后
   放宽为"3 重复中 ≥2"属事后调参,污染预注册契约。
- **采用路径 (b)。** 把两类噪声当作真 bug 继续修(smoke 服从率、workdir 纪律),
   然后跑一轮**预注册的 12 跑确认面板**。锚点在见数据前定死:见 §7 的
   `logs/panel-confirm-tvm-http/panel-lock.json`(SPLIT tvm 锚点 + execution-bearing http 锚点,逐字预注册)。

---

# 终判:confirm2 预注册确认面板(logs/panel-confirm2-tvm-http)

- **tvm:12 跑制度下 6/6 双臂全过 → DELETE 票。** never_sweep 硬锚全过(零全扫),smoke 落地。
- **httpcomponents:P 臂 3/3 全过;F-r2 一跑真实回退**(执行量与 verdict 未达标,非拒绝尝试噪声)→ **P过∧F败 → 按规则进 Stage 2 消融**。
- 四探针最终:bigtop DELETE、pyyaml DELETE、tvm DELETE;http 待 Stage 2 定 keep-set。
- 下一步:http 单针 Stage-2 回退消除(五维贪心,同 pin 同 3 重复)找出最小保留集;
  其余三针的处方维度已获删除授权。

## 复核记录:tvm smoke liveness 与 Stage-1/confirm 封印(reviewer note)

- **tvm smoke liveness = 4/6**(逐 rep 复核 `tvm_smoke_liveness`):P 臂 2/3(P-r1、P-r2 落地
  定向 smoke;P-r3 零 pytest 调用 → 0),F 臂 2/3(F-r1、F-r2 落地;F-r3 零 → 0)。
  这是**报告用的 fleet-health 指标,不是 per-run 硬锚**(评审 split:per-run smoke gate 会重罚
  臂无关的 5/6 服从噪声)。**它不影响 tvm 的 DELETE 判票**——tvm 的删除票由 6/6 双臂全过
  `never_sweep_while_unbuilt` 硬锚(零全扫)+ `build_failed_physical_or_better` 决定,liveness 只作旁证。
- **Stage-1 / confirm 已按复核正式封印(formally sealed per review)。** confirm2
  (`logs/panel-confirm2-tvm-http`,单 pin 428fcb1)为终判:bigtop / pyyaml / tvm 三针 DELETE;
  http 为 P过∧F败,唯一未决项转入 Stage 2(`logs/panel-stage2-http`,预注册锁已入库)。
  Stage-1/confirm 不再重开;后续仅 http 的 keep-set 搜索。

---

# Stage-2 固定点终判(logs/panel-stage2-http)

贪心回退消除收敛(21 次有效跑,重扫纠错 2 次;另有 1 次无 pin 孤儿首跑被拒、其带 pin 重试计入):
- **已删维:a(计划管线)、b(推荐动作字段)、c(brief 工件)、d(objectives 措辞)**——各自候选 3/3 全锚点通过;
- **候选 00000 的 r1 表面上"败于 verdict_success",触发对 e(python 事前指导)的适用性复核。**

## e 维适用性复核 —— 评审裁决(reviewer ruling,记录在案,不得由执行者改写)

按 spec §Stage-2 dimension-applicability gate(维度适用性门,round-4 复核新增):
候选保留的维度必须对被测探针**改变至少一个字节**;对某探针字节恒等的维度是**非识别性(non-identifying)的,判 ABSTAIN 而非 KEEP**。

**逐字裁决:e's HTTP keep vote lacked causal force**(failing rep's partial came from
jdk_mismatch in provision, phase_provision.json evidence; dim e reads only python build/test
guidance, react_engine.py returns None for non-python; 00001 vs 00000 Maven build+test intros
byte-identical)→ **e reclassified INVALID/non-identifying for HTTP**; with pyyaml+tvm providing
e's applicable-domain evidence, **e is ALSO authorized for deletion.**

- 机制证据:`react_engine.py::_python_phase_guidance` 对非 python 探针返回 None;
  掩码 00001(留 e)与 00000(删 e)的 Maven build+test intro **逐字节相同**——
  回归守卫 `tests/test_python_phase_guidance.py::test_maven_dual_mask_e_is_byte_identical_noop`。
- 00000-r1 的 partial 根因是 provision 阶段的 jdk_mismatch(见 phase_provision.json),与 e 维无因果。
- e 在其**适用域**(pyyaml/tvm 等 python/native 探针)由 Stage-1/confirm 的 DELETE 票承载,已获删除授权。

# Category 3 全局裁决
四探针齐票;http 的 e 维经适用性门复核判非识别性并由 pyyaml+tvm 承载删除授权。
**按 spec 规则 + 已归档评审裁决:维度 a、b、c、d、e 全部获删除授权(ALL FIVE a–e authorized for deletion)。**
即:执行计划管线、推荐动作处方、project brief、objectives 推荐措辞、python 事前指导块——**五维处方全部删除**。
