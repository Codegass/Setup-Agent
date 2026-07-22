# SAG Control-Layer Redesign — Architecture Plan (v2.1)

**Date:** 2026-07-14 (v2 2026-07-15 state/lifecycle review; v2.1 2026-07-15 timing/async review)
**Status:** Ready for implementation planning (forensics by Chenhao; anchors re-verified against `main` @ `b4aec8e`; two review rounds incorporated)
**Audience:** implementers who have NOT read the forensic thread. Every claim carries a verified `file:line` anchor and every workstream carries a concrete done-bar. Read the Insight paragraphs — they are the design rationale, not decoration.
**Evidence base:** live TVM run `logs/session_20260713_111610_92265` (kept intact — treat it as the reference fixture), plus the probe archives `logs/probe-campaign-2026071*/`.

---

## The keystone contract (hard, non-negotiable)

> **Phase termination describes control flow. Phase outcome describes evidence. The run verdict is computed exactly once, by the finalizer, at evidence-close — and everything after only renders it.**

Every workstream below is a projection of this sentence. If an implementation choice violates it, the choice is wrong.

## 0. The problem in one paragraph

The TVM reference run made **77 model calls (39 thinking / 38 action), 308,666 tokens total — yet the average prompt was only 3,605 tokens (max 7,222)** (`token_usage.csv`, verified). The context window is never full. Instead, the model reads the world many times through a **narrow, re-assembled, lossy keyhole**: only the last 7 steps render (`react_prompt_builder.py:191`), older results are crushed to 90-char head-truncated ledger lines (`attempt_ledger.py:61`), phase switches wipe the step list entirely (`react_engine.py:701`) and hand over at most 200 chars of self-written summary (`phase_machine.py:69`). Meanwhile the **judgment layer has multiple independent verdict sources** that disagreed on the same run (markdown `FAILED 0/987` / report tool `PARTIAL 0/328` / phase machine `success` / final agent `failed`) — and the split starts at the very bottom: `ToolResult` itself carries `success: bool` + `status` + `verdict` with implicit derivation (`base.py`, "verdict defaults from `success`"), and a **dispatched-but-running detached job has no honest representation at all** (`orch.py:922` `execute_command_detached` / `:982` `poll_detached_command`) — which is how a wheel build whose log said `CMake configuration failed` got stored as `success: true`. Execution-layer fixes (JDK, reactor root, islands — already landed) are necessary but not sufficient: **the control layer manufactures wasted iterations and split-brain verdicts by construction.**

Design principles:

1. **One evidence state, many renderers.** No component may *conclude*; components *render* projections of the single evidence state.
2. **Failures are the payload.** Any summarization must preserve the failure tail and its identity (error_code, signature, ref) — never just the output head.
3. **Handoffs carry structure, not prose.** A phase transition is a typed record with provenance, not a 200-char sentence.
4. **Spend reasoning where it pays.** Extra model calls only on failure, conflict, unknowns, or strategy switch — never as a per-step tax.
5. **Loops are recurrence without progress.** Same action + same outcome with no relevant state change in between = loop — regardless of what happened in between. Novelty is budgeted, not unlimited.
6. **A number we know is wrong renders as `invalid`, never as a plausible value.** Clamping a broken metric into range is beautification, the exact disease this plan treats.
7. **Dispatch is not completion.** An async handle is a promise of evidence, never evidence.

## Lifecycle (the WS0/WS1 product — everything else hangs off this)

Two distinct closing boundaries — this ordering is what makes "report renders the snapshot" temporally possible (the report phase runs *before* run end: `react_engine.py:70` — *"Generate the final report with the report tool, then phase(action='done')"*):

```
ToolResult (typed, WS0) ──every execution──▶ RunEvidenceState (mutable, engine-owned)
                                                │            │
                                 phase ends ────┤            │ mid-run digests & PhaseHandoff
                                                ▼            ▼ render the LIVE state
                                          PhaseHandoff   phase-start digest

EVIDENCE-CLOSE  (when the last evidence-bearing phase — test — terminates, or on abort):
    seal RunEvidenceState  →  VerdictFinalizer  →  immutable RunVerdictSnapshot
                                                        │
                              report phase RENDERS the snapshot (markdown · condensed · report tool)
                                                        │
FLOW-CLOSE      (report phase terminates, success or not):
    RunTermination {termination, snapshot_ref, report_delivery_status}
    CLI · web read the snapshot + termination
```

- **Evidence-close** happens when the last evidence-producing phase terminates (normally test; on early abort, at abort time — sealing whatever evidence exists). From this moment the snapshot exists and is immutable.
- **Flow-close** happens when the report phase terminates. **Report generation failure affects only `report_delivery_status` — it can never mutate the setup verdict.** A run whose report crashed is still an honestly-verdicted run with a missing report.
- Mid-run consumers (phase-start digests, PhaseHandoff) render the **live `RunEvidenceState`** — never the snapshot (which may not exist yet).
- Snapshot envelope: `{schema_version, run_id, finalized_at, input_refs, verdict, build_evidence, test_stats, conflicts, phase_records}`. Written atomically (temp + rename) to `/workspace/.setup_agent/verdict.json`. Finalization is idempotent (re-finalizing the same sealed state yields a byte-identical snapshot); a missing/corrupt snapshot at read time is an explicit `unknown` verdict plus a conflict — never a silent recompute.

---

## WS0 (P0) — Typed result & state taxonomy

**Insight.** The split-brain starts at the most upstream signal. `ToolResult` (`src/sag/tools/base.py`) carries three overlapping truth fields — `success: bool`, `status: EvidenceStatus`, `verdict: Optional[str]` — with implicit derivation ("verdict defaults from `success`"). And the system's core async mechanism — detached dispatch + poll (`orch.py:922/:982`) — has **no honest state at all** in a completed/failed vocabulary: the reference run stored a wheel build whose text shows `CMake configuration failed` under `success: true` (`full_outputs.jsonl:18`), i.e. *dispatch succeeded* was recorded as *operation succeeded*. Every downstream consumer (ledger, handoff, gates, snapshot) inherits both ambiguities. **This is the first workstream, not an afterthought audit.**

**Design.**
1. Orthogonal fields with exact semantics, replacing the overlap — including the async states:
   ```
   invocation_status: pending | completed | timeout | crashed | cancelled
   operation_outcome: unknown | success | partial | failed | skipped
   evidence_status:   verified | conflict | unknown
   ```
   - A detached dispatch returns `invocation_status=pending, operation_outcome=unknown` with a `poll_ref` — **a promise of evidence, not evidence**. Only a poll observing terminal state upgrades it (`completed` + real outcome). The wheel-build symptom becomes unrepresentable by type.
   - A finished job whose log tail shows a fatal error is `completed + failed` regardless of exit code plumbing.
2. The run-level verdict vocabulary **never appears in ToolResult**. Legacy `success` becomes a read-only compatibility property (`operation_outcome == "success"`), migrated consumer-by-consumer, then removed.
3. Phase state splits into two axes with a **legal-combination matrix** (normative):

   | termination \ outcome | unknown | success | partial | failed |
   |---|---|---|---|---|
   | `running`   | ✓ | — | — | — |
   | `completed` | ✓ | ✓ | ✓ | ✓ |
   | `blocked`   | ✓ | — | ✓ | ✓ |
   | `aborted`   | ✓ | — | ✓ | ✓ |

   `phase_machine.overall_outcome()` (`phase_machine.py:58-67`) is replaced by `termination_state()`; outcomes come from evidence. The ReAct loop's `return outcome == "success"` (`react_engine.py:1116`) is replaced by the `RunTermination` contract (Lifecycle §) — the boolean dies.

**Done-bar.**
- Unit: contradictory legacy fields are unconstructible (validator); the detached-dispatch fixture yields `pending/unknown` and its polled completion with fatal tail yields `completed/failed`.
- Unit: illegal matrix combinations (`blocked+success`, `running+failed`) raise at the phase-record layer.
- Unit: grep-level — no `verdict=` in ToolResult construction sites; `overall_outcome` gone.
- Reference-run replay: the wheel-build entry reads `pending→completed/failed`; phases show `termination=completed` with `outcome=failed` visibly coexisting (that is the point).

---

## WS1 (P0) — RunEvidenceState + finalizer at evidence-close

**Insight.** The four-way verdict split is one architecture gap. Two surfaces were already unified in July (report snapshot kernel + CLI mirror + evidence-rescue — `tests/test_verdict_kernel_unification.py`). The remaining offenders: `phase_machine` outcome as an independent verdict (WS0 kills it), report-tool-owned finalization — **which is also temporally wrong**: the report phase runs before run end (`react_engine.py:70`), so "finalize at run end, report renders" is impossible; finalization must happen at **evidence-close** (Lifecycle §) — and the markdown-vs-report count split (**987 = 3×328 raw retries** vs 328 unique — same snapshot, two bases).

**Design.**
1. `RunEvidenceState` (new module `src/sag/agent/evidence_state.py`): the engine-owned mutable accumulator — WS0-typed tool results, validator findings, conflicts, phase records, plus a monotonic **`evidence_version`** counter (bumped on any new verified fact, artifact change, or env-overlay change — consumed by WS5's progress detection). Single writer: the engine.
2. `VerdictFinalizer` (engine-owned): seals the state at evidence-close and folds it through the existing conflict kernel (`evaluate_run_verdict` + caps + evidence-rescue) exactly once → immutable `RunVerdictSnapshot`. The report phase — and `report_tool._build_report_snapshot` — become *renderers*. Abort paths seal whatever evidence exists at abort time.
3. **One reporting basis**: `test_stats.unique`. `raw` stays for diagnostics; no renderer prints it as "Tests: N". Flakiness is preserved, not laundered (WS7).

**Done-bar.**
- Unit: TVM-shaped evidence state (3 retry XMLs × 328 unique, build partial, phases `completed`) → the **literal verdict string and counts identical** across markdown header, condensed line, snapshot, CLI finalization, web read-model. One assert per surface.
- Unit: the temporal contract — the snapshot exists (file on disk) *before* the report phase's first render call; a run whose report tool crashes still finalizes (`report_delivery_status=failed`, verdict intact).
- Unit: abort mid-build → sealed snapshot with `unknown`/partial evidence, no crash, no recompute.
- Unit: corrupt/missing `verdict.json` at read time → explicit `unknown` + conflict.
- Live: TVM + bigtop + paramiko; the collector (§Eval) checks all surfaces per run. **Zero mismatches across 3 runs.**

---

## WS3 (P0) — Two-axis phase completion + semantic gates

*(Ordered before WS2: gates produce the outcome facts the handoff carries.)*

**Insight.** The prompt already promises the two-axis model — *"done means the phase flow ended; it does not automatically mean setup succeeded"* (`react_engine.yaml:29`) — but the gates don't implement it, in both directions. Direction 1: `_check_build` short-circuits `return _verdict(True)` for any non-JVM system (`phase_gates.py:49-52`) **while the Python evidence ladder it should consult already exists**; `_check_test` passes on report-file existence (`phase_gates.py:68-80`) — 328 collection errors counted as "tested". Direction 2: a gate that *refuses* `done` on a determined failure forces the model to retry forever or mislabel as `blocked` — manufacturing spin. **The gate's job: verify the claimed OUTCOME against evidence — not prevent a failed phase from ENDING.**

**Design.**
1. Phase claims carry both axes: `phase(action='done', outcome=...)` accepting every `completed`-legal outcome from the WS0 matrix — including `unknown` (honest "flow ended, evidence inconclusive"). `blocked` is reserved for external impediments.
2. Gate truth table (normative — per validator state, which claimed outcomes pass):

   | validator says \ claim | success | partial | failed | unknown |
   |---|---|---|---|---|
   | evidence green (ladder success/complete) | ✓ | ✓ | ✓* | ✓ |
   | evidence partial | ✗ reject + reason | ✓ | ✓ | ✓ |
   | evidence red / blocked | ✗ reject + reason | ✗ reject | ✓ | ✓ |
   | evidence unavailable | ✗ reject ("gather evidence first") | ✓ | ✓ | ✓ |

   *claiming worse than the evidence is honest pessimism — allowed, logged.
3. `_check_build`: drop the JVM guard; consult `validate_build_status` for every system, apply the table. `_check_test`: additionally treat `errors == total > 0` or `executed == 0 while detected > 0` as evidence-red for a `success` claim. Rejections carry the validator's own reason + remediation.
4. Downstream verdict effect of a `completed/failed` phase is the finalizer's business (WS1), not the gate's.

**Done-bar.**
- Unit: the full gate truth table as a parametrized test (16 cells).
- Unit: the spin-incentive regression — a scripted determined-failure phase completes in ≤2 gate interactions (no retry loop, no forced `blocked` mislabel).
- Unit: 328/328-errors XML → `success` rejected, `failed` accepted, phase record carries `completed/failed`.
- Live TVM: build phase ends `completed` with a non-success outcome against red evidence; test phase cannot claim success on collection errors. Assert via phase records + snapshot.

---

## WS2 (P0) — Structured phase handoff: cumulative projection with provenance

**Insight.** After build, the engine wipes the steps (`react_engine.py:701`) and the next phase inherits `✓ build: <first 200 chars of self-written key_results>` (`phase_machine.py:69-76`). In the reference run the model then declared *"build/dependency groundwork has been completed… no setup blockers"* (`agent_execution.log:452`) while the native core never compiled — **the framework handed it a laundered summary.** The ledger truncates output **heads** (`attempt_ledger.py:61`): "Building wheel…" survives, the CMake fatal at the tail dies. Free-text fields would reintroduce laundering one layer up — facts need provenance. And a per-phase-only handoff has its own decay: analyze-phase facts must survive into test, not just build.

**Design.**
1. `PhaseHandoff` is the **cumulative active projection of the whole run so far** (all prior phases), rendered from the live `RunEvidenceState` at every phase start:
   ```
   fact:    {key, value, status: verified|claimed, source_ref, phase}
   blocker: {id, category, status: active|resolved, error_code, failure_signature, evidence_refs, remediation?}
   attempt: {action_fingerprint, outcome, evidence_version_delta, evidence_ref}
   handoff: {facts[], blockers[], attempts[], last_failures[], next_hypothesis}
   ```
   `last_failures` entries carry `{command, error_code, failure_signature, error_tail_preview(≤400 chars), output_ref}` — the tail is a *preview*; identity lives in code/signature/ref.
2. **Persistence vs rendering are different contracts:**
   - `RunEvidenceState` never deletes a blocker (resolution flips `status`, it doesn't erase).
   - The **prompt rendering** shows the top-priority *active* blockers (priority: unresolved > recent > frequent) up to the render budget, plus `… and N more — full handoff: <ref>` when trimmed. Verified-success facts are trimmed first; **active blockers and failure evidence are never silently omitted — at minimum the omitted count and ref always render.**
3. **Honest by construction:** a `fact` is `verified` only when backed by validator evidence; agent claims without evidence enter as `claimed` or become `blockers`. The classifier is code, not model discipline.
4. **Trust boundary:** repo docs and tool output are untrusted input; rendered inside delimited quoted blocks (`[from tool output] …`), never as bare system guidance.
5. Ledger polarity: failed steps keep `head 30 + "…" + tail 80`; success may stay head-only.
6. The phase-switch wipe stays (GTD design); the injected digest becomes the handoff rendering.

**Done-bar.**
- Unit: build ends `completed/partial` with a CMake fatal → test-phase intro contains the failure signature; the literal "CMake" survives.
- Unit: an analyze-phase verified fact (e.g. `has_native_build=true`) still renders in the **test**-phase intro (cumulative, not last-phase-only).
- Unit: 100-blocker stress → state retains all 100; prompt renders the priority subset + accurate `omitted_count` + ref; zero active blockers fully invisible.
- Unit: unverified success claim → `claimed`/blocker, not `verified`. Failed-step ledger line contains the output tail.
- Live TVM: fresh run's test-phase intro names the native-core blocker; "no setup blockers" with red build evidence fails the eval.

---

## WS5 (P1) — Loop memory: recurrence without progress

**Insight.** Both failure modes verified: the breaker's `tool_count >= 8` counts all calls of a tool regardless of params/success — eight *distinct, successful* searches force-broken (`INFINITE LOOP BROKEN … Failures: 0/8`, `tool_orchestration.py:520`); and the genuinely-stuck case — identical 328-collection-error runs at iterations **57/65/69, with other operations in between** — was never caught. Two design corollaries the v2 draft got wrong: *consecutiveness is the wrong test* (57/65/69 aren't adjacent — what matters is that nothing relevant changed in between), and *putting `phase` inside the identity key contradicts cross-phase memory* (build vs test keys would never match).

**Design.**
1. Decomposed keys — phase is **metadata, not identity**:
   ```
   action_key  = (tool, normalized_target)        # what was tried
   outcome_key = (operation_outcome, error_code, failure_signature)   # what happened
   occurrence  = {action_key, outcome_key, phase, iteration, evidence_version}
   ```
   `normalized_target`: tool-specific — bash: command with paths/refs canonicalized, volatile tokens (timestamps, pids, job ids) stripped; search: lowercased whitespace-collapsed query; build: `(action, workdir)`. `outcome_key` is stable thanks to WS0's typed fields.
2. **Loop condition (replaces "consecutive"):** a failing `(action_key, outcome_key)` **recurs while `evidence_version` is unchanged** since its last occurrence — i.e. nothing relevant happened in between (the counter lives in `RunEvidenceState`, WS1; bumped on new verified facts, artifact changes, env-overlay changes). Interleaved unrelated operations don't reset anything. Second such recurrence → inject "state unchanged since the identical failure" guidance; fourth → force-break.
3. **Force-break semantics (defined):** record a WS2-shaped `blocker` into the evidence state, request one reasoning step (WS4 trigger), and if the next action repeats the same occurrence, end the phase `completed/failed`. Never a silent task-skip.
4. **Memory is run-scoped** (indexed by `action_key + outcome_key`, phase-agnostic): a fingerprint that failed in build pre-warns on first retry in test. Advisory counters may reset per phase; the memory does not.
5. Delete the bare `tool_count >= 8` clause. **Novelty soft budget:** distinct action_keys per tool per phase get a generous advisory ceiling (default 15) that only ever *suggests* consolidation — never breaks. Keep the dispatch-poll exemption (`_is_dispatch_poll_signature`) — polling is prescribed behavior.

**Done-bar.**
- Unit: 8 distinct successful searches → nothing; 16 distinct → advisory only.
- Unit: fail → unrelated ops (no evidence_version bump) → same fail → guidance fires (non-consecutive catch, the 57/65/69 shape); fail → *verified fact lands* (version bump) → same fail → no loop signal (legitimate retry after a fix attempt).
- Unit: build-phase failure occurrence retried in test phase → pre-warning on first occurrence.
- Live TVM: zero diversity-triggered breaks; the triple-identical failure intercepted by its second recurrence.

---

## WS4 (P1) — Scheduler: reasoning on demand with an executable plan

**Insight.** 77 calls for 38 actions — the fixed think→act cadence (`react_engine.yaml:374`, `:432`) taxes every step with two calls. But the actor contract creates a real dependency: without a fresh thought, what does the actor execute? A `params_sketch` would smuggle re-analysis back into the actor — the plan must be executable as-is.

**Design.**
1. `CurrentPlan` — typed, executable:
   ```
   PlanStep {tool, exact_params, preconditions, expected_evidence, success_criteria}
   CurrentPlan {steps[], invalidate_on: [failure|conflict|unknown|phase_change]}
   ```
   `exact_params` are literal values or **explicit placeholders referencing prior step outputs** (`{{step_2.output_ref}}`) — data flow without re-analysis. Malformed plan / unknown tool / unmet precondition are **scheduler faults → think** (never actor improvisation).
2. Transition table (normative):
   ```
   observation success + plan has next executable step → action
   observation failure | partial | conflict | UNKNOWN   → think
   gate rejection                                       → think
   phase transition                                     → think (fresh plan)
   loop-breaker reasoning request (WS5)                 → think
   plan exhausted | malformed | precondition unmet      → think
   multiple simultaneous triggers                       → coalesce into ONE think
   heartbeat: every N actions without a think           → think   (N=5, EXPERIMENTAL parameter, not architecture)
   ```
**Done-bar.**
- Unit: scripted 6-action all-success run with a 6-step plan → exactly 2 thinks; failure at step 3 → one think before step 4; `pending→unknown` observation (detached dispatch) does NOT trigger think (polling is the plan); a poll returning `completed/failed` does.
- Unit: placeholder resolution (`{{step_1.output_ref}}` feeds step 2); malformed placeholder → think, actor never guesses.
- Live A/B on paramiko: thought calls −50% with **identical final verdict and evidence ladder**; TVM verdict unchanged.

---

## WS6 (P1) — ProjectBrief: role-typed composition, not a total order

**Insight.** The TVM build intro injected 2,044 chars of overlapping guidance (`journal/phase_build.journal.jsonl:1`) — each fragment added by a different fix wave, nobody owning composition. But a naive precedence chain is subtly wrong: *"verified environment evidence beats project docs"* would let "the container currently has JDK 17" **override** "this project requires JDK 8" — inputs answer *different questions* and cannot be totally ordered. (The shipped `JdkPreflight` already embodies the correct rule: the requirement drives provisioning; current state never overrides it.)

**Design.**
1. Inputs are **role-typed**; the composer applies rules, not rank:
   ```
   policy      — what is ALLOWED        (safety/runtime constraints; absolute)
   requirement — what is NEEDED         (project structure, manifests, docs)
   evidence    — what is CURRENTLY TRUE (env probes, overlay, validator)
   default     — fallback               (applies only where requirement is unknown)

   requirement ≠ evidence  →  emit a conflict/provisioning ACTION into the brief
   requirement unknown     →  default applies, marked as assumption
   anything vs policy      →  policy wins, marked
   ```
2. Single composition pass after `project(action='analyze')` → `ProjectBrief {version, input_fingerprint, sections}`; fragments keyed by instruction id (`install-deps`, `native-first`, `test-command`), deduped by key.
3. `input_fingerprint` = hash of (manifest, detected toolchain, submodule state, build roots, **repo-docs digest, analyzer version, composer version**). Any change invalidates and recomposes — a brief may not outlive its inputs.
4. The recommended-build section is a **typed structure**: linear list for simple repos, small DAG for island repos (`[{root, system, goal, depends_on[]}]` — the island machinery already computes this). No forced linear prose. Budget: build intro ≤1,200 chars on TVM's shape; repo-doc text carries the WS2 trust marking.

**Done-bar.**
- Unit: env-JDK-17 + requirement-JDK-8 fixture → brief contains a provisioning action, NOT "use 17"; requirement-unknown → default marked as assumption.
- Unit: TVM-shaped analysis → exactly one deps instruction, one native-first block, one recommendation; ≤1,200 chars; marker-based snapshots keep pure-java/pure-python intros semantically intact.
- Unit: fragment registration order permuted → identical brief; JDK/docs/analyzer-version change → new fingerprint, recomposed.
- Unit: bigtop-shaped islands render as a DAG-ordered step list with `depends_on` visible.
- Live: TVM journal intro char count + duplicate-12-gram check.

---

## WS7 (P1) — Metrics: one basis, honest degradation, reproducible A/B

**Insight.** Numbers that leak into the benchmark must be non-gameable. 987 = 3×328 accumulated retries; compileall printed `2058/1563 = 1.32`. Three subtleties earlier drafts got wrong: **latest-wins silently launders flakiness**; **clamping a broken ratio hides the data error being fixed** (a clamp is valid only for genuine saturation — the shipped executed≥detected⇒100% execution-rate clamp stays; a basis mismatch is garbage, not saturation); and **ordering retries by XML filename/mtime is fragile** — identity and order must be explicit.

**Design.**
1. Canonical test identity: `(module_or_file, class, name, param_id)` normalized once, shared by every producer.
2. Every recorded run carries an explicit **`attempt_id`** (monotonic per run, assigned at execution time); `latest` is max-`attempt_id`, never filename or mtime order.
3. Per-test history: `{first, latest, worst, retried_count}` with `worst` ordered by severity `error > failed > skipped > passed`; `flaky_count` (fail→pass) is a first-class snapshot field rendered next to pass counts ("541 passed (3 flaky)"). Verdict math uses `latest`; flakiness is never invisible.
4. compileall coverage: numerator = source `.py` files with a same-tag `.pyc` (deduped by source path); denominator = the scan's own exclusion set. Basis mismatch → the metric renders **`invalid`** + `metrics_conflict` in the snapshot. No clamp.
5. WS0's taxonomy fixes the wheel-`success:true` root; WS7 adds the regression test that stored metadata equals the typed outcome.

**Done-bar.**
- Unit: 3 retry XMLs (fail→fail→pass, shuffled file mtimes) → `latest` follows `attempt_id`, "N passed (1 flaky)", `retried_count=2`, `worst=failed`.
- Unit: compileall foreign-pyc fixture → `invalid` + conflict, no 1.0. Execution-rate 560/559 still renders 100% (the valid clamp regression-locked).
- Live: bigtop + TVM reports contain no number that disagrees with `verdict.json`, no ratio >100%, flaky counts where retries occurred.

---

## Cross-cutting eval protocol (the A/B Chenhao asked for)

Baseline = `main` @ `b4aec8e`. Panel:

| Probe | Why |
|---|---|
| **TVM** | the reference pathology: native core, cross-language, loop breaker, handoff laundering |
| **Bigtop** | islands, mixed build systems, JDK retry |
| **Paramiko** | the straight-line happy path — guards against added friction |
| **cassandra-java-driver** | pure Maven multi-module reactor — guards the reactor-root/JDK execution wins (known-good numbers: SUCCESS, 8,916 classes, ~4.9k tests) |

Two distinct verification instruments — do not conflate them:
- **Surface-agreement check:** reads the *rendered* surfaces (markdown, condensed, CLI, web) and asserts literal equality with `verdict.json` — rendered text is deliberately the assertion target here, because renderers are what it verifies.
- **Metrics collector:** reads *structured* sources only (`verdict.json`, phase records, token CSV, event log) into `logs/ab-<date>/<probe>-<stage>.json`. Never greps rendered text for numbers.

Pin block per run: `{target repo SHA, container image digest, SAG git SHA, model + config, prompt bundle version, feature flags, random seed (where honored), dependency-cache state, host arch}`. **≥3 repeats** per probe at campaign gates (1 repeat for intermediate smoke); report median [min–max]. Primary regression harness = **deterministic replay fixtures** (recorded tool transcripts driven through engine/finalizer — no LLM, no container); live probes validate, they are not the only net.

Campaign bars:
1. Verdict agreement: all surfaces identical on every run (hard from WS1 on).
2. No outcome claim contradicted by evidence survives a gate; determined failures end phases in ≤2 gate interactions (WS3).
3. Zero diversity-triggered loop breaks; identical-failure recurrences intercepted by the second occurrence (WS5).
4. Paramiko thought calls −50% with unchanged verdicts (WS4). Report TVM/Bigtop iteration deltas — **measure, don't promise**: this quantifies the control-layer share of the 51-vs-5 gap.
5. Every rendered number reproducible from `verdict.json`; flakiness visible (WS1+WS7).
6. cassandra-java-driver stays at its known-good numbers throughout (execution-layer non-regression).

## Sequencing (v2.1) & ownership

```
WS0 (taxonomy + lifecycle + matrix)  →  WS1 (evidence state + evidence-close finalizer)  →  WS3 (two-axis gates)
→  WS2 (cumulative handoff)  →  WS5 (loop memory)  →  WS4 (scheduler)  →  WS6 (brief)  →  WS7 (metrics render)
```

- WS3 precedes WS2 (gates produce the outcome facts the handoff carries). WS4/WS5 interact (breaker requests reasoning; scheduler serves it; WS5's evidence_version lives in WS1's state) — same implementer or tight coordination, **not parallel**. WS6 rewrites the intro WS2 injects — after WS2.
- Each WS: separate branch + PR, unit done-bar green **and** the 4-probe panel attached (evidence archived under `logs/` before any cleanup, per repo convention).
- Non-goals: do NOT raise context caps or step windows as a "fix"; do NOT remove hard stops (bound them correctly); no component other than the finalizer computes a verdict — including "just for logging".

## Known-good foundations (do not re-invent)

- Verdict kernel + conflict caps + evidence-rescue: `report_tool._snapshot_kernel_verdict`, `tests/test_verdict_kernel_unification.py` (WS1 relocates ownership to evidence-close; the kernel math survives).
- Per-test XML dedup: `tests/test_pytest_report_aggregation.py` (WS7 adds identity/attempt_id/history, keeps the union machinery).
- Python evidence ladder: `physical_validator._verify_python_build` + `tests/test_python_verifier.py` (WS3's gate reads it as-is).
- Detached dispatch/poll: `orch.py:922/:982` (WS0 gives it honest types; the mechanism is sound).
- Post-analysis guidance seam: `react_engine._phase_intro_step` + `tests/test_python_phase_guidance.py` (WS6's composer plugs in here).
- Island DAG inputs: `build_islands`/`test_islands` with per-island goals + cross-island dependency guidance (WS6 renders them).
- Requirement-vs-evidence precedence, working example: `JdkPreflight` (requirement drives provisioning; current env never overrides it) — WS6 §1 generalizes exactly this rule.
- Probe→forensics→fix→re-probe with `--record` + archive-before-cleanup: `docs/superpowers/reports/2026-07-12-live-probe-validation-report.md`.

## Changelog

**v2.1 (timing/async review):**
- **Lifecycle split into evidence-close and flow-close** — fixes the v2 temporal impossibility (report phase runs before run end, `react_engine.py:70`, yet was supposed to render a run-end snapshot). Report failure affects only `report_delivery_status`; mid-run digests render the live state (diagram corrected).
- **WS0 gained async states** (`pending`/`cancelled`; `unknown`/`skipped`) — detached dispatch (`orch.py:922`) is representable; "dispatch succeeded ≠ operation succeeded" enforced by type. Legal termination×outcome matrix added (absorbs the two-axis gap).
- **WS3**: gate truth table per validator state; `unknown` claimable.
- **WS2**: persistence-vs-render split (never delete; render priority subset + omitted_count + ref); handoff is the cumulative active projection of all prior phases.
- **WS5**: identity decomposed (action_key/outcome_key; phase = metadata) — resolves the v2 phase-in-key vs cross-phase-memory contradiction; loop condition = recurrence with unchanged `evidence_version` (replaces "consecutive"; catches the 57/65/69 shape).
- **WS4**: PlanStep with exact params (or explicit prior-output placeholders), preconditions, expected evidence; fault fallbacks; `unknown` added to think triggers; pending-poll explicitly not a think trigger.
- **WS6**: total-order precedence replaced by role-typed rules (requirement vs evidence vs policy vs default — the JDK-17-env/JDK-8-requirement case); fingerprint extended (docs digest, analyzer/composer versions).
- **WS7/eval**: explicit `attempt_id` ordering (not filename/mtime); `worst` severity order defined; surface-check vs metrics-collector instruments separated (grep rendered text only to verify renderers); pin block extended (SAG SHA, flags, seed, dep-cache); panel + cassandra-java-driver (pure Maven reactor coverage).

**v2 (state/lifecycle review):** WS0 added (ToolResult taxonomy, two-axis state); finalizer ownership moved out of report tool; handoff provenance + never-drop-blockers; loop fingerprints + cross-phase memory; CurrentPlan + transition table; brief precedence + lifetime; flaky metrics + replay fixtures.
