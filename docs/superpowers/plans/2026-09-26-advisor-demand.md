# Advisor 按需咨询与证据读取

用户已授权实现和必要小规模消融。保持 phase、物理验证、验收任务及 mini 模型不变，不启动新的 60 组实验。工作目录原有修改保留；本轮修改前字节在 `output/advisor-demand-20260926/before/`。

## 实现

- [x] 保留 legacy `phase-entry/all` 对照。加入精简事实包 `brief` 与按需读取 `on-demand` 两种选择，避免因窗口大就填满日志。
- [x] `advisor(question, context)` 接收可选短问题和假设；来源标注为 Actor 声称，不作为观测事实。另有开关供不含 Actor 说明的消融。成功路径不强制总结。
- [x] 第一版仅检索已归档证据：日志、receipt 元数据、已有工具读取和历史。资源由 harness 冻结为本次咨询的 allowlist，带来源与 SHA-256。提供 literal search、行范围、长行续读。没有 arbitrary path、shell、写入或构建能力；缺少源码则明确请 Actor 取得新证据。
- [x] bounded reviewer loop 的所有 provider 请求和检索结果封存、计费；保留原任务与当前事实，窗口不足时只对已读材料做带缺失披露的程序压缩，不增加摘要模型调用。不同模型沿用已有窗口与分片策略。
- [x] 增加 `adaptive` 触发候选：Actor 自主咨询；harness 仅在已有重复无进展信号或证据冲突时补充咨询，按语义证据去重。正常 phase 切换不咨询，首次一般构建失败不等于 struggle，不取消待执行动作以强迫咨询。原 loop/gate 安全机制保留。

## 验证与选择

- [x] 确定性测试：未知/伪造 ref、不允许动作、截断与分页、错误 Actor 假设、pending 与错误 JVM、重复触发、关闭后的只读、请求/账目守恒与窗口压缩。
- [ ] 同一归档场景比较 all / brief / on-demand，固定触发和模型；单独比较有/无 Actor 说明并包含错误假设。成功日志与失败诊断分别报告，不据模型自述判正确。
- [x] 小项目真实运行固定验收，比较 phase-entry 与 adaptive，报告自身执行的 build/test、全部 token 与时间。旧结果不覆盖；不能借外部复跑补成功。
- [ ] 根据实际消融选择默认值，未证明可靠的候选只保留为 opt-in，披露样本和支持范围。

## 当前验收边界

- 本轮工具、packing、计费、触发与 phase 边界的 198 项回归已通过；额外相邻检查 133 项通过。报告交付的一项旧 fixture 在改动前的 engine 快照上同样失败，见 `output/advisor-demand-20260926/preexisting-report-test.log`。
- 两项目三组的真运行按 `output/advisor-demand-20260926/protocol.json` 执行，逐请求计费与自身执行验收；结果由 `summarize_live.py` 重建。
- 归档成功/失败诊断和错误 Actor 假设对照已准备，原日志已通过 receipt 字节/hash 绑定；自动审批要求这批数据单独授权，尚未向 provider 发送。本地 preview 的零 token 是没有调用模型，不是节省实测。
- 全局默认继续保留 `phase-entry/all`。`adaptive/on-demand` 已用于本批明确配置的试验；完成失败诊断比较后再决定默认策略，不以两项顺利任务证明普遍可靠性。
