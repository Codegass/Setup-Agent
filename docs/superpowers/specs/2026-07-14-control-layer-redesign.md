# SAG Control-Layer Redesign — Architecture Plan (v2)

**Date:** 2026-07-14 (v2 revision 2026-07-15, incorporating the state/lifecycle review)
**Status:** Approved direction (forensics by Chenhao; anchors re-verified against `main` @ `b4aec8e`; v2 contracts reviewed against implementation)
**Audience:** implementers who have NOT read the forensic thread. Every claim carries a verified `file:line` anchor and every workstream carries a concrete done-bar. Read the Insight paragraphs — they are the design rationale, not decoration.
**Evidence base:** live TVM run `logs/session_20260713_111610_92265` (kept intact — treat it as the reference fixture), plus the probe archives `logs/probe-campaign-2026071*/`.

---

## The keystone contract (hard, non-negotiable)

> **Phase termination describes control flow. Phase outcome describes evidence. The run verdict is computed exactly once, by the finalizer, from the evidence state — and everything else only renders it.**

Every workstream below is a projection of this sentence. If an implementation choice violates it, the choice is wrong.

## 0. The problem in one paragraph

The TVM reference run made **77 model calls (39 thinking / 38 action), 308,666 tokens total — yet the average prompt was only 3,605 tokens (max 7,222)** (`token_usage.csv`, verified). The context window is never full. Instead, the model reads the world many times through a **narrow, re-assembled, lossy keyhole**: only the last 7 steps render (`react_prompt_builder.py:191`), older results are crushed to 90-char head-truncated ledger lines (`attempt_ledger.py:61`), phase switches wipe the step list entirely (`react_engine.py:701`) and hand over at most 200 chars of self-written summary (`phase_machine.py:69`). Meanwhile the **judgment layer has multiple independent verdict sources** that disagreed on the same run (markdown `FAILED 0/987` / report tool `PARTIAL 0/328` / phase machine `success` / final agent `failed`) — and the split starts at the very bottom: `ToolResult` itself carries `success: bool` + `status` + `verdict` with implicit derivation (`base.py`, "verdict defaults from `success`"). Execution-layer fixes (JDK, reactor root, islands — already landed) are necessary but not sufficient: **the control layer manufactures wasted iterations and split-brain verdicts by construction.**

Design principles:

1. **One evidence state, many renderers.** No component may *conclude*; components *render* projections of the single evidence state.
2. **Failures are the payload.** Any summarization must preserve the failure tail and its identity (error_code, signature, ref) — never just the output head.
3. **Handoffs carry structure, not prose.** A phase transition is a typed record with provenance, not a 200-char sentence.
4. **Spend reasoning where it pays.** Extra model calls only on failure, conflict, or strategy switch — never as a per-step tax.
5. **Loops are defined by state, not by counters.** Same state + same action + same result = loop. Different targets ≠ loop — but novelty is budgeted, not unlimited.
6. **A number we know is wrong renders as `invalid`, never as a plausible value.** Clamping a broken metric into range is beautification, the exact disease this plan treats.

## Lifecycle (the WS0 product — everything else hangs off this)

```
ToolResult (typed, WS0)  ──every execution──▶  RunEvidenceState (mutable, engine-owned)
                                                   │                    │
                                    phase ends ────┤                    │
                                                   ▼                    │
                                            PhaseHandoff (WS2)          │ finalize ONCE (engine-owned
                                            projection of the           │ VerdictFinalizer, WS1)
                                            live evidence state         ▼
                                                          RunVerdictSnapshot (immutable)
                                                                         │
                                        markdown · report tool · CLI · web · phase digest
                                                    (renderers only, WS1/WS7)
```

- `PhaseHandoff` consumes the **live `RunEvidenceState`**, never the final snapshot (which does not exist yet mid-run).
- The finalizer runs at run end **regardless of whether the report tool was invoked** — today the snapshot is born inside `report_tool` (`report_tool.py:977`), so a run that never calls report has no authoritative verdict. That dependency inverts: engine finalizes, report renders.
- Snapshot envelope: `{schema_version, run_id, finalized_at, input_refs, verdict, build_evidence, test_stats, conflicts, phase_records}`. Written atomically (temp + rename) to `/workspace/.setup_agent/verdict.json`. Finalization is idempotent: re-running report re-renders the same snapshot; a missing/corrupt snapshot at read time is an explicit `unknown` verdict with a conflict, never a silent recompute.

---

## WS0 (P0) — Typed result & state taxonomy

**Insight.** The split-brain starts at the most upstream signal. `ToolResult` (`src/sag/tools/base.py`) carries three overlapping truth fields — `success: bool`, `status: EvidenceStatus`, `verdict: Optional[str]` — with implicit derivation ("verdict defaults from `success`"). The reference run stored a wheel build whose text shows `CMake configuration failed` under `success: true` (`full_outputs.jsonl:18`). Every downstream consumer (ledger, handoff, gates, snapshot) inherits this ambiguity; fixing them first would be building on sand. **This is the first workstream, not an afterthought audit.**

**Design.**
1. Three orthogonal fields with exact semantics, replacing the overlap:
   ```
   invocation_status: completed | timeout | crashed     # did the tool RUN
   operation_outcome: success | partial | failed        # did the OPERATION achieve its goal
   evidence_status:   verified | conflict | unknown     # is the outcome BACKED by evidence
   ```
   A detached job that exits 0 while its log tail shows a fatal error is `invocation_status=completed, operation_outcome=failed` — the wheel-build symptom becomes unrepresentable.
2. The run-level verdict vocabulary **never appears in ToolResult**. Legacy `success` becomes a read-only compatibility property (`operation_outcome == "success"`), migrated consumer-by-consumer, then removed.
3. Phase state splits into the two axes (consumed by WS1/WS3):
   ```
   phase termination: running | completed | blocked | aborted   # control flow
   phase outcome:     success | partial | failed | unknown      # evidence result
   ```
   `phase_machine.overall_outcome()` (`phase_machine.py:58-67`, returns `"success"` purely from no-blocked-marks) is replaced by `termination_state()`; outcomes come from evidence. The ReAct loop's `return outcome == "success"` (`react_engine.py:1116`) is replaced by a `RunTermination` return `{termination, snapshot_ref}` — the boolean dies.

**Done-bar.**
- Unit: constructing a ToolResult with contradictory legacy fields is impossible (validator); the detached-job fixture (exit 0 + fatal tail) yields `operation_outcome=failed`.
- Unit: grep-level test — no `verdict=` in any ToolResult construction site; `overall_outcome` name gone from the codebase.
- The reference-run fixture replayed through the new taxonomy: the wheel-build entry reads `failed`, and `phase_machine` reports `termination=completed` for phases whose outcome is `failed` (the two axes visibly disagree — that is the point).

---

## WS1 (P0) — RunEvidenceState + finalizer: kill the split-brain

**Insight.** The four-way verdict split is one architecture gap, not four bugs. Two surfaces were already unified in July (report snapshot kernel + CLI mirror + evidence-rescue — `tests/test_verdict_kernel_unification.py`). The remaining offenders: `phase_machine` outcome as an independent verdict (WS0 kills it), the report-tool-owned finalization (lifecycle above inverts it), and the markdown-vs-report count split (**987 = 3×328 raw retries** vs 328 unique — same snapshot, two bases).

**Design.**
1. `RunEvidenceState` (new module `src/sag/agent/evidence_state.py`): the engine-owned mutable accumulator — tool results (WS0-typed), validator findings, conflicts, phase records. The single writer is the engine; tools contribute via their returns, never by writing state directly.
2. `VerdictFinalizer` (engine-owned): folds `RunEvidenceState` through the existing conflict kernel (`evaluate_run_verdict` + caps + evidence-rescue) exactly once at run end → immutable `RunVerdictSnapshot` (envelope per Lifecycle §). `report_tool._build_report_snapshot` becomes a *renderer* of the snapshot.
3. **One reporting basis**: `test_stats.unique` (per-test dedup — machinery exists, `tests/test_pytest_report_aggregation.py`). `raw` stays in the snapshot for diagnostics; no renderer may print it as "Tests: N". Flakiness is not laundered: see WS7 for `first/latest/worst + retried_count`.

**Done-bar.**
- Unit: TVM-shaped evidence state (3 retry XMLs × 328 unique, build partial, all phases `termination=completed`) → the **literal verdict string and counts are identical** across rendered markdown header, condensed line, stored snapshot, CLI finalization, web read-model. One assert per surface.
- Unit: a run that never invokes the report tool still produces `verdict.json` (finalizer independence).
- Unit: corrupt/missing `verdict.json` at web/CLI read time → explicit `unknown` + conflict, no recompute.
- Live: TVM + bigtop + paramiko probes; a collector script greps all surfaces per run and fails on any mismatch. **Zero mismatches across 3 runs.**

---

## WS3 (P0) — Two-axis phase completion + semantic gates

*(Ordered before WS2: gates produce the outcome facts the handoff carries.)*

**Insight.** The prompt already promises the two-axis model — *"done means the phase flow ended; it does not automatically mean setup succeeded"* (`react_engine.yaml:29`) — but the gates don't implement it, in both directions. Direction 1: `_check_build` short-circuits `return _verdict(True)` for any non-JVM system (`phase_gates.py:49-52`) **while the Python evidence ladder it should consult already exists** in `validate_build_status`; `_check_test` passes on report-file existence (`phase_gates.py:68-80`) — 328 collection errors counted as "tested". Direction 2: a gate that *refuses* `done` on a determined failure forces the model to either retry forever or mislabel as `blocked` — manufacturing the exact spinning this plan eliminates. **The gate's job is: verify the claimed OUTCOME against evidence — not prevent a failed phase from ENDING.**

**Design.**
1. Phase claims carry both axes: `phase(action='done', outcome='failed'|'partial'|'success', ...)`. A determined failure ends as `termination=completed, outcome=failed` — first-class, honest, no spin incentive. `blocked` is reserved for external impediments.
2. `_check_build`: drop the JVM guard; consult `validate_build_status` for every system. The gate rejects only **outcome claims contradicted by evidence** (claiming `success` with a red ladder), with the validator's own reason + remediation. Claiming `failed`/`partial` consistent with evidence passes — and flows into the handoff (WS2).
3. `_check_test`: reject `outcome=success` when parsed reports show `errors == total > 0` or executed == 0 with detected > 0. Reuse `validate_test_status` — already computed.
4. Downstream verdict effect of a `completed/failed` phase is the finalizer's business (WS1), not the gate's.

**Done-bar.**
- Unit: python project, ladder BLOCKED → gate rejects `outcome=success`, accepts `outcome=failed` (termination completes, run proceeds to report honestly).
- Unit: 328/328-errors XML → `outcome=success` rejected; mixed real results pass; the accepted outcome lands in the phase record.
- Unit: the spin-incentive regression — a scripted determined-failure phase completes in ≤2 gate interactions (no retry loop, no forced `blocked` mislabel).
- Live TVM: build phase ends `completed/failed-or-partial` against red evidence (never `done≡success`); test phase cannot claim success on collection errors. Assert via phase records + snapshot.

---

## WS2 (P0) — Structured phase handoff with provenance

**Insight.** After build, the engine wipes the steps (`react_engine.py:701`) and the next phase inherits `✓ build: <first 200 chars of self-written key_results>` (`phase_machine.py:69-76`). In the reference run the model then declared *"build/dependency groundwork has been completed… no setup blockers"* (`agent_execution.log:452`) while the native core never compiled — **the framework handed it a laundered summary.** Worse, the ledger truncates output **heads** (`attempt_ledger.py:61`, `[:90]`): "Building wheel…" survives, the CMake fatal at the tail dies. Truncation polarity is backwards for failures. And free-text fields would reintroduce the same laundering one layer up — facts need provenance.

**Design.**
1. `PhaseHandoff`, produced at every phase end (both terminations), **projected from the live `RunEvidenceState`** (never the final snapshot):
   ```
   fact:    {key, value, status: verified|claimed, source_ref}
   blocker: {id, category, status, error_code, failure_signature, evidence_refs, remediation?}
   attempt: {action_fingerprint, outcome, state_delta, evidence_ref}
   handoff: {facts[], blockers[], attempts[], last_failures[], next_hypothesis}
   ```
   `last_failures` entries carry `{command, error_code, failure_signature, error_tail_preview(≤400 chars), output_ref}` — the tail is a *preview*; identity lives in code/signature/ref.
2. **Honest by construction:** a `fact` is `verified` only when backed by validator evidence; agent claims without evidence enter as `claimed` or become `blockers`. The classifier is code, not model discipline.
3. **Truncation priority:** field caps exist (≤5 facts, ≤3 attempts), but unresolved blockers and failure evidence are **never dropped by a cap** — caps trim verified-successes first.
4. **Trust boundary:** repo docs and tool output are untrusted input. Handoff renders them inside clearly delimited quoted blocks (`[from tool output] …`), never as bare system guidance.
5. Ledger polarity: failed steps keep `head 30 + "…" + tail 80`; success may stay head-only.
6. The phase-switch wipe stays (GTD design); the injected digest becomes the `PhaseHandoff` rendering.

**Done-bar.**
- Unit: build ends `completed/partial` with a CMake fatal in the last output → test-phase intro contains the failure signature and non-empty `blockers`; the literal "CMake" survives.
- Unit: agent `key_results` claiming success without evidence → `claimed`/blocker, not `verified`.
- Unit: cap pressure (10 facts + 4 blockers) → all 4 blockers survive, facts trimmed.
- Unit: failed-step ledger line contains the output tail (error at char 500+ fixture).
- Live TVM: fresh run's test-phase intro names the native-core blocker; the phrase "no setup blockers" with red build evidence fails the eval.

---

## WS5 (P1) — Loop memory: state fingerprints, not counters

**Insight.** Both failure modes verified. (a) The breaker's `tool_count >= 8` clause (`tool_orchestration.py`, `_get_repetition_level`) counts all calls of a tool regardless of params or success — eight *distinct, successful* searches got force-broken (`INFINITE LOOP BROKEN … Failures: 0/8`, `tool_orchestration.py:520`). (b) The genuinely-stuck case — three identical 328-collection-error runs at iterations 57/65/69 — was never stopped: outcome content is in no signature. The detector fires on diversity and misses repetition.

**Design.**
1. Loop key = `state_fingerprint = (phase, tool, normalized_target, outcome_hash)`:
   - `normalized_target`: tool-specific — bash: command with paths/refs canonicalized and volatile tokens (timestamps, job ids) stripped; search: lowercased query with whitespace collapse; build: `(action, workdir)`.
   - `outcome_hash`: hash of `(operation_outcome, error_code, failure_signature_line)` — WS0's typed fields make this stable.
2. Escalation on **identical fingerprints**: 2 consecutive → inject "state unchanged" guidance; 4 → force-break. Force-break semantics defined: record a `blocker` (WS2 shape) into the evidence state, request one reasoning step (WS4 trigger d), and if the next action repeats the fingerprint, end the phase `completed/failed`. Never a silent task-skip.
3. **Failure-fingerprint memory persists across phases** (counters reset, memory doesn't): a fingerprint that failed in build is pre-loaded as a warning if retried in test — the reference run's "same 328 errors, three times" becomes impossible past the second.
4. Delete the bare `tool_count >= 8` clause. Replace unlimited diversity with a **novelty soft budget**: distinct fingerprints per tool per phase get a generous advisory ceiling (default 15) that only ever *suggests* consolidation, never breaks — 100 paraphrased searches deserve a nudge, not a kill. Keep the dispatch-poll exemption (`_is_dispatch_poll_signature`) — it is correct.

**Done-bar.**
- Unit: 8 distinct successful searches → no break, no warning below the soft budget; 16 distinct → advisory only. 2× identical failing build → guidance; 4× → break with recorded blocker + reasoning request.
- Unit: build-phase failure fingerprint retried in test phase → pre-warning on first occurrence.
- Live TVM: zero diversity-triggered breaks; the triple-identical failure intercepted by its second occurrence (grep the run log).

---

## WS4 (P1) — Scheduler: reasoning on demand with an explicit plan

**Insight.** 77 calls for 38 actions — the fixed think→act cadence (`react_engine.yaml:374` forces "next step should…"; `:432` forbids the actor from re-analyzing) taxes every step with two calls. But the actor contract creates a real dependency: **without a fresh thought, what does the actor execute?** Removing thinks without introducing a plan object would leave the actor contractually blind.

**Design.**
1. `CurrentPlan`: the thinking model's output becomes a typed, multi-step plan `{steps: [{tool, params_sketch, precondition}], invalidate_on: [failure|conflict|phase_change]}` held by the engine. The actor executes the next plan step; the yaml actor contract (`:432`) is updated to "execute the next step of the current plan".
2. Scheduler transition table (normative):
   ```
   observation success + plan has next step   → action
   observation failure | partial | conflict   → think
   gate rejection                              → think
   phase transition                            → think (fresh plan)
   loop-breaker reasoning request (WS5)        → think
   plan exhausted / no executable step         → think
   multiple simultaneous triggers              → coalesce into ONE think
   heartbeat: every N actions without a think  → think   (N=5, EXPERIMENTAL parameter, not architecture)
   ```
**Done-bar.**
- Unit: scripted 6-action all-success run with a 6-step plan → exactly 2 thinks (phase-start + heartbeat); failure at step 3 → exactly one think before step 4; simultaneous failure+phase-change → one coalesced think.
- Live A/B on paramiko: thought calls −50% with **identical final verdict and evidence ladder**; TVM verdict unchanged. Record in the A/B table.

---

## WS6 (P1) — ProjectBrief: one composed strategy with precedence and lifetime

**Insight.** The TVM build intro injected 2,044 chars where deps guidance, native-first guidance, and the Recommended Build line restate overlapping instructions (`journal/phase_build.journal.jsonl:1`) — each added by a different fix wave, nobody owning composition. And a brief computed once can go stale: the JDK, submodules, or build root may change after analysis. Bigtop-class repos have no "ONE recommended sequence" at all — they have a dependency **graph** of islands.

**Design.**
1. Single composition pass after `project(action='analyze')` → `ProjectBrief {version, input_fingerprint, sections}`; template fragments become inputs keyed by instruction id (`install-deps`, `native-first`, `test-command`); the composer dedupes by key with explicit precedence:
   ```
   safety/runtime policy  >  verified environment evidence  >  analyzed project structure
   >  repository documentation  >  generic defaults
   ```
2. `input_fingerprint` = hash of (manifest, detected toolchain, submodule state, build roots). Any change invalidates and recomposes the brief — a brief may not outlive its inputs.
3. The recommended-build section is a **typed structure**: linear list for simple repos, small DAG for island repos (`[{root, system, goal, depends_on[]}]` — the island machinery already computes this), rendered as ordered steps with dependencies stated. No forced linear prose.
4. Budget: build intro ≤1,200 chars on TVM's shape; repo-doc-derived text carries the WS2 trust marking.

**Done-bar.**
- Unit: TVM-shaped analysis → exactly one deps instruction, one native-first block, one recommendation; ≤1,200 chars; marker-based snapshots keep pure-java/pure-python intros semantically intact (as `tests/test_python_phase_guidance.py`).
- Unit: same fragments in different registration order → identical brief (composition is deterministic); JDK change → new `input_fingerprint`, recomposed brief.
- Unit: bigtop-shaped islands render as a DAG-ordered step list with `depends_on` visible.
- Live: TVM journal intro char count + duplicate-12-gram check.

---

## WS7 (P1) — Metrics: one basis, honest degradation, reproducible A/B

**Insight.** Numbers that leak into the benchmark must be non-gameable. 987 = 3×328 accumulated retries; compileall printed `2058/1563 = 1.32`. Two subtleties the v1 plan got wrong: **latest-occurrence-wins silently launders flakiness** (fail→pass retry renders as a clean pass), and **clamping a broken ratio to 1.0 hides the very data error being fixed** — a clamp is only valid where the semantics genuinely saturate (the shipped executed≥detected⇒100% execution-rate clamp is semantically sound and stays; a numerator/denominator basis mismatch is not saturation, it is garbage).

**Design.**
1. Canonical test identity: `(module_or_file, class, name, param_id)` normalized once, shared by every producer.
2. Per-test history preserved: `{first, latest, worst, retried_count}`; `flaky_count` (fail→pass) is a first-class snapshot field and renders next to pass counts ("541 passed (3 flaky)"). Verdict math uses `latest` (the run's end state) — but flakiness is never invisible.
3. compileall coverage: numerator = source `.py` files with a same-tag `.pyc` (deduped by source path); denominator = the scan's own exclusion set. If inputs still disagree → the metric renders `invalid` + a `metrics_conflict` entry in the snapshot. **No clamp.**
4. The wheel-`success:true` symptom is fixed at the root by WS0's taxonomy; WS7 adds the regression test that stored metadata equals the typed outcome.
5. **A/B protocol rigor:** every run pins `{repo SHA, container image digest, model + config, prompt bundle version, host arch}` into the run record; each panel stage runs **≥3 repeats** at campaign gates (1 repeat for intermediate smoke); metrics reported as median [min–max]. Primary regression harness = **deterministic replay fixtures** (recorded tool transcripts driven through engine/finalizer — no LLM, no container); live probes validate, they are not the only net.

**Done-bar.**
- Unit: 3 retry XMLs (fail→fail→pass on one test) → rendered "N passed (1 flaky)", `retried_count=2`, verdict from `latest`, raw counts only under diagnostics.
- Unit: compileall foreign-pyc fixture → `invalid` + conflict; no 1.0 anywhere. Execution-rate 560/559 fixture still renders 100% (the valid clamp regression-locked).
- Live: bigtop + TVM reports contain no number that disagrees with `verdict.json`, no ratio >100%, and flaky counts where retries occurred.

---

## Cross-cutting eval protocol (the A/B Chenhao asked for)

Baseline = `main` @ `b4aec8e`. Panel:

| Probe | Why |
|---|---|
| **TVM** | the reference pathology: native core, cross-language, loop breaker, handoff laundering |
| **Bigtop** | islands, mixed build systems, JDK retry — guards the execution layer against control-layer regressions |
| **Paramiko** | the straight-line happy path — guards against added friction |

Collector script (deliverable of WS1) writes `logs/ab-<date>/<probe>-<stage>.json`: iterations, model calls (thought/action), tokens, wall time, all verdict surfaces, gate events, loop events, handoff records, plus the WS7 pin block. Campaign bars:

1. Verdict agreement: all surfaces identical on every run (hard from WS1 on).
2. No outcome claim contradicted by evidence survives a gate (WS3); no spin: determined failures end phases in ≤2 gate interactions.
3. Zero diversity-triggered loop breaks; identical-failure loops intercepted by the second repeat (WS5).
4. Paramiko thought calls −50% with unchanged verdicts (WS4). Report TVM/Bigtop iteration deltas — **measure, don't promise**: this quantifies the control-layer share of the 51-vs-5 gap.
5. Every rendered number reproducible from `verdict.json` (WS1+WS7); flakiness visible, never laundered.

## Sequencing (revised) & ownership

```
WS0 (taxonomy + lifecycle)  →  WS1 (evidence state + finalizer)  →  WS3 (two-axis + gates)
→  WS2 (handoff projection)  →  WS5 (loop memory)  →  WS4 (scheduler)  →  WS6 (brief)  →  WS7 (metrics render)
```

- WS3 precedes WS2 (gates produce the outcome facts the handoff carries). WS4 and WS5 interact (loop breaker requests reasoning; scheduler serves it) — same implementer or tight coordination, **not parallel**. WS6 rewrites the intro WS2 injects into — after WS2.
- Each WS: separate branch + PR, unit done-bar green **and** the 3-probe panel attached (evidence archived under `logs/` before any cleanup, per repo convention).
- Non-goals: do NOT raise context caps or step windows as a "fix" (the data shows windows aren't binding); do NOT remove hard stops (bound them correctly); no component other than the finalizer computes a verdict — including "just for logging".

## Known-good foundations (do not re-invent)

- Verdict kernel + conflict caps + evidence-rescue: `report_tool._snapshot_kernel_verdict`, `tests/test_verdict_kernel_unification.py` (WS1 relocates ownership; the kernel math survives).
- Per-test XML dedup: `tests/test_pytest_report_aggregation.py` (WS7 adds history, keeps the union machinery).
- Python evidence ladder: `physical_validator._verify_python_build` + `tests/test_python_verifier.py` (WS3's gate reads it as-is).
- Post-analysis guidance seam: `react_engine._phase_intro_step` + `tests/test_python_phase_guidance.py` (WS6's composer plugs in here).
- Island DAG inputs: `build_islands`/`test_islands` with per-island goals + cross-island dependency guidance (WS6 §3 renders them).
- Probe→forensics→fix→re-probe with `--record` + archive-before-cleanup: `docs/superpowers/reports/2026-07-12-live-probe-validation-report.md`.

## v2 changelog (from the implementation review)

- **Added WS0** (ToolResult taxonomy, two-axis phase state, lifecycle contract) — upstream split-brain promoted from a P1 audit to the foundation.
- **Fixed the v1 lifecycle contradiction**: handoff now projects the mutable `RunEvidenceState`; the snapshot is finalized once by an engine-owned finalizer (report tool demoted to renderer); envelope/atomicity/idempotence specified.
- **Gates re-scoped** to verify claimed outcomes instead of blocking failed phases from completing (kills the retry/mislabel spin incentive; aligns with `react_engine.yaml:29`'s existing promise).
- **Handoff fields gained provenance** + never-drop-blockers cap policy + untrusted-input marking.
- **Loop design**: fingerprint fields normalized-target/outcome-hash specified; failure memory persists across phases; force-break semantics defined; bare tool-count kill replaced by an advisory novelty budget.
- **Scheduler**: `CurrentPlan` + normative transition table; heartbeat N flagged experimental.
- **Brief**: precedence chain, `input_fingerprint` invalidation, typed DAG for island repos.
- **Metrics**: flakiness surfaced (`first/latest/worst/retried`), compileall clamp replaced by `invalid`+conflict (the exec-rate clamp stays — genuine saturation), A/B pinning + ≥3 repeats + deterministic replay fixtures as the primary harness.
