# SAG Control-Layer Redesign — Architecture Plan

**Date:** 2026-07-14
**Status:** Approved direction (forensics by Chenhao; anchors re-verified against `main` @ `b4aec8e`)
**Audience:** implementers who have NOT read the forensic thread. Every claim below carries a verified `file:line` anchor and every workstream carries a concrete done-bar. Read the Insight paragraphs — they are the design rationale, not decoration.
**Evidence base:** live TVM run `logs/session_20260713_111610_92265` (kept intact — treat it as the reference fixture), plus this repo's probe archives `logs/probe-campaign-2026071*/`.

---

## 0. The problem in one paragraph

The TVM reference run made **77 model calls (39 thinking / 38 action), 308,666 tokens total — yet the average prompt was only 3,605 tokens (max 7,222)** (`token_usage.csv`, verified). The context window is never full. Instead, the model reads the world many times through a **narrow, re-assembled, lossy keyhole**: only the last 7 steps render (`react_prompt_builder.py:191`), older results are crushed to 90-char head-truncated ledger lines (`attempt_ledger.py:61`), phase switches wipe the step list entirely (`react_engine.py:701`) and hand over at most 200 chars of self-written summary (`phase_machine.py:69`). Meanwhile the **judgment layer has four independent verdict sources** that disagreed on the same run (markdown `FAILED 0/987` / report tool `PARTIAL 0/328` / phase machine `success` / final agent `failed`). Execution-layer fixes (JDK, reactor root, islands — already landed) are necessary but not sufficient: **the control layer manufactures wasted iterations and split-brain verdicts by construction.**

Design principles for everything below:

1. **One evidence snapshot, many renderers.** No component may *conclude*; components *render* the single verdict snapshot.
2. **Failures are the payload.** Any summarization must preserve the failure tail, never just the output head.
3. **Handoffs carry structure, not prose.** A phase transition is a typed record, not a 200-char sentence.
4. **Spend reasoning where it pays.** Extra model calls only on failure, conflict, or strategy switch — never as a per-step tax.
5. **Loops are defined by state, not by counters.** Same state + same action + same result = loop. Different targets ≠ loop.

---

## WS1 (P0) — Single verdict snapshot: kill the split-brain

**Insight.** The four-way split is not four bugs; it is one architecture gap. Every surface that *computes* instead of *reads* will eventually diverge. Two of the four surfaces were already unified in July (`report_tool` snapshot kernel + CLI mirror + blocked-phase evidence-rescue — see `tests/test_verdict_kernel_unification.py`). The two remaining independent sources:

- `phase_machine.py:58-67` — `overall_outcome()` returns `"success"` purely because no phase was *marked* blocked. Process state masquerading as a verdict ("overall outcome: success" while 0/328 tests passed).
- The markdown renderer counted **987** (three retries × 328, raw executions) while the report tool counted **328** (unique) — same snapshot, two count bases.

**Design.**
1. Introduce `RunVerdictSnapshot` (a frozen dataclass, one module: `src/sag/verdict_snapshot.py`): `{verdict, build_evidence, test_stats_unique, test_stats_raw, conflicts, phase_process_state, reasons[]}`. It is produced exactly once per run-finalization by the existing snapshot kernel (`report_tool._build_report_snapshot` + `evaluate_run_verdict`), persisted to `/workspace/.setup_agent/verdict.json`.
2. `PhaseMachine.overall_outcome()` is **renamed** `phase_process_state()` and its return is demoted to a *field inside* the snapshot — nothing may treat it as a verdict. Grep-audit all callers (`agent.py`, `agent_execution.log` writer).
3. Declare **one reporting basis**: `test_stats_unique` (per-test dedup, latest occurrence wins — the machinery from `fix(verifier): aggregate pytest XMLs per-test` already computes it). `raw` stays in the snapshot for diagnostics but no renderer may print it as "Tests: N".
4. Markdown renderer, condensed log, CLI final line, web read-model: all read `verdict.json`/the snapshot object. Delete any local recomputation.

**Anchors.** `phase_machine.py:58-67`; `report_tool.py` `_snapshot_kernel_verdict` / `_build_report_snapshot`; `agent.py` `_get_verified_final_status`; the 987-vs-328 renderer split (markdown test table vs report tool log).

**Done-bar (concrete).**
- Unit: a new `tests/test_verdict_single_source.py` builds the TVM-shaped snapshot (3 retry XMLs × 328 unique, build partial, phases all `done`) and asserts the **literal verdict string and the test counts are identical** across: rendered markdown header, condensed summary line, stored snapshot, CLI finalization return. Four-way equality, one assert per surface.
- Unit: `phase_process_state()` returning `"success"` while snapshot verdict is `failed` must be impossible to render — the old method name must be gone (grep in test).
- Live: re-run TVM + bigtop + paramiko probes; a script greps each run's four surfaces and fails on any mismatch. **Zero mismatches across 3 runs.**

---

## WS2 (P0) — Structured phase handoff: stop laundering failure into optimism

**Insight.** After build, the engine wipes the steps (`react_engine.py:701`) and the next phase inherits `✓ build: <first 200 chars of self-written key_results>` (`phase_machine.py:69-76`). In the reference run the model then declared *"build/dependency groundwork has been completed… no setup blockers"* (`agent_execution.log:452`) while the native core had never compiled. The model didn't hallucinate — **the framework handed it a laundered summary.** Worse, the ledger truncates output **heads** (`attempt_ledger.py:61`, `[:90]`): "Building wheel…" survives, the CMake fatal error at the tail dies. Truncation polarity is exactly backwards for failures.

**Design.**
1. New `PhaseHandoff` record, produced when a phase completes (done OR blocked), persisted per phase and injected verbatim into the next phase intro:
   ```
   {
     verified_facts:      [str],   # backed by validator evidence, not agent claims
     unresolved_blockers: [str],
     last_failure:        {command, error_tail: str},  # tail — last 400 chars, not head
     attempts:            [{approach, outcome}],       # from the attempt ledger
     evidence_refs:       [str],
     next_hypothesis:     str,
   }
   ```
   Field-level caps (e.g. ≤5 facts, ≤3 attempts, error_tail ≤400 chars), **no global 200-char crush**. `verified_facts` are cross-checked against the validator (a "build complete" claim requires build evidence `success=True`, else it is moved to `unresolved_blockers` automatically — the handoff is honest by construction, not by model discipline).
2. Ledger truncation polarity: for failed steps, keep `head 30 + "…" + tail 80` of output (errors live at the tail); successful steps may keep head-only.
3. Phase-switch context wipe stays (it is the GTD design), but the injected digest is the `PhaseHandoff` rendering, not `key_results[:200]`.

**Anchors.** `react_engine.py:701` (wipe), `phase_machine.py:69-76` (digest), `attempt_ledger.py:61` (head-truncate), `agent_execution.log:452` (the laundering symptom).

**Done-bar.**
- Unit: build phase ends partial with a CMake fatal error in the last tool output → the test-phase intro must contain the error tail and `unresolved_blockers` non-empty; asserting the literal string "CMake" survives the handoff.
- Unit: an agent `key_results` claiming success without validator evidence lands in `unresolved_blockers`, not `verified_facts`.
- Unit: failed-step ledger line contains the output tail (fixture where the error is at char 500+).
- Live TVM: grep the test-phase intro of a fresh run — it must mention the native-core blocker; the phrase "no setup blockers" appearing while build evidence is partial fails the eval.

---

## WS3 (P0) — Gates check semantics, not existence

**Insight.** The gates were built when only Maven/Gradle existed and were never re-wired to the (now-existing) evidence machinery: `_check_build` short-circuits `return _verdict(True)` for any non-JVM system (`phase_gates.py:49-52`) — **while the Python evidence ladder it should consult already exists** in `validate_build_status` (venv/pip-check/imports/compileall/native). `_check_test` passes if a report file merely exists (`phase_gates.py:68-80`) — 328 collection errors count as "tested". This is the cheapest, highest-leverage fix in the whole plan: the evidence is already computed; the gates just don't read it.

**Design.**
1. `_check_build`: drop the JVM guard. Consult `validate_build_status` for every system; gate on its tri-state (`success=False` → reject with the validator's own `reason` + suggestions; `partial` passes the gate but the reason is carried into the handoff (WS2), never silently).
2. `_check_test`: reject when the parsed reports show `errors == total > 0` (pure collection-error runs), or executed == 0 with detected > 0. Reuse `validate_test_status` — again, already computed.
3. Gate rejections must carry the validator's remediation text (they already have the two-branch suggestion shape — keep it).

**Anchors.** `phase_gates.py:49-52`, `:68-80`; `physical_validator.validate_build_status` (python branch), `validate_test_status`.

**Done-bar.**
- Unit: python project, evidence ladder BLOCKED (imports fail) → build gate rejects a `done` claim; PARTIAL (native missing) → gate passes but handoff carries the reason.
- Unit: test report XML with 328/328 errors → test gate rejects; mixed real results pass.
- Live TVM: the build phase can no longer be marked `done` while the venv/import evidence is red; the test phase cannot be `done` on a pure-collection-error XML. Assert via the run's phase records.

---

## WS4 (P1) — Reasoning on demand, not as a tax

**Insight.** 77 calls for 38 actions — the fixed think→act cadence (`react_engine.yaml:374` forces the thinker to end with "next step should…"; `:432` forbids the actor from re-analyzing) makes every step cost two calls and splits one intention across two contexts. Straight-line successful sequences (probe→install→verify) need no interleaved essays. Part of the 51-vs-5 iteration gap vs Claude is this cadence, not model quality.

**Design.** Thinking calls are **event-driven**: trigger only on (a) previous action failed or returned a conflict, (b) gate rejection, (c) phase transition (first step of a phase), (d) explicit tool suggestion to reconsider, (e) every Nth action as a safety heartbeat (N=5, configurable). Otherwise the action model proceeds directly from the structured observation. Keep the yaml contracts for when thinking does run.

**Anchors.** `react_engine.yaml:374`, `:432`; the engine's step scheduler (`react_engine.py`, thought/action alternation); `react_llm.py:243` (each call is a rebuilt single `user` message — unchanged here, but it is why redundant thinking is pure cost: no KV reuse).

**Done-bar.**
- Unit: a 6-action all-success scripted run produces ≤2 thinking calls (phase-start + heartbeat); a failure at step 3 inserts exactly one thinking call before step 4.
- Live A/B on paramiko (the straight-line case): thought-call count drops ≥50% with the **same final verdict and same evidence ladder result**; TVM verdict unchanged. Record in the A/B table (§Eval).

---

## WS5 (P1) — Loops defined by state delta

**Insight.** Two dual failures, both verified: (a) the breaker's `tool_count >= 8` clause (`tool_orchestration.py` `_get_repetition_level`) counts **all calls of a tool regardless of params or success** — eight *different, successful* searches got force-broken (`INFINITE LOOP BROKEN … Failures: 0/8`, `tool_orchestration.py:520`); (b) the genuinely-stuck case — three identical 328-collection-error test runs at iterations 57/65/69 — was **not** stopped, because outcome content is not part of any signature. The detector fires on diversity and misses repetition.

**Design.**
1. Loop key = `(phase, tool, target, outcome_hash)` where `target` is the semantically-identifying param (query for search, command for bash, action+workdir for build) and `outcome_hash` is a short hash of the normalized result (exit class + error signature line). 
2. Intervene at 2 consecutive identical keys with **state-unchanged** results ("same command, same failure — the state has not changed; change approach or gather evidence"); force-break at 4.
3. Remove the bare `tool_count >= 8` clause. Distinct targets never accumulate into one loop. Counters reset on phase transition.
4. Keep the existing dispatch-poll exemption (`_is_dispatch_poll_signature`) — it is correct.

**Anchors.** `tool_orchestration.py:745` (signature = name+sorted params), `_get_repetition_level` (`exact_match_count >= 5 or tool_count >= 8`), `:520-526` (breaker), `agent_state_evaluator.py:384+` (consecutive-failure guidance, same-tool prefix match); reference symptoms `main.log:15262`, `phase_test.json` iterations 57/65/69.

**Done-bar.**
- Unit: 8 successful searches with distinct queries → level 0, no break. 2× identical failing build with same error signature → guidance; 4× → break. Phase switch resets.
- Live TVM: zero `INFINITE_LOOP_BROKEN` events on distinct-target sequences; the triple-identical test failure triggers guidance by the second repeat (grep the run log).

---

## WS6 (P1) — One project brief, not stacked templates

**Insight.** The TVM build intro injected 2,044 chars where Python-deps guidance, native-first guidance, and the Recommended Build line each restate overlapping and partially competing instructions (`journal/phase_build.journal.jsonl:1`). Each template was added by a different fix wave (all justified in isolation); nobody owns the composition. Redundancy costs attention, and near-duplicate authority invites the model to pick randomly.

**Design.** After `project(action='analyze')`, compose **one** `ProjectBrief` (a single rendering pass in the engine's intro builder): ordered sections {what this project is, the ONE recommended sequence, constraints (never-do), known hazards}. Template fragments become *inputs* to the composer, which dedupes by instruction key (e.g. `install-deps`, `native-first`, `test-command`) — the composer decides precedence (project-specific beats generic), instead of concatenating. Budget: ≤1,200 chars for the build intro on TVM's shape.

**Anchors.** `react_engine.py` `_phase_intro_step` / `_recommended_build_line` (the injection seam), `NATIVE_FIRST_BUILD_GUIDANCE` + `PYTHON_BUILD_PHASE_GUIDANCE` constants, `react_engine.yaml` phase objectives.

**Done-bar.**
- Unit: TVM-shaped analysis → intro contains exactly one deps instruction, one native-first block, one recommended line; total ≤1,200 chars; snapshot tests keep pure-java and pure-python intros semantically intact (marker-based, as in `tests/test_python_phase_guidance.py`).
- Live: TVM run's journal intro char count and duplicate-sentence check (a script that fails on any repeated 12-gram).

---

## WS7 (P1) — Metrics: one basis, sane denominators

**Insight.** Numbers that leak into the benchmark must be non-gameable and single-basis: 987 = 3×328 retries accumulated (raw vs unique split — WS1 fixes the render side; this WS fixes the producers), and compileall printed `2058/1563 = 1.32 coverage` (pyc from `__pycache__` of excluded dirs / multiple interpreter tags counted against a source-file denominator). Any metric >1.0 that ships in a report erodes trust in every other number.

**Design.**
1. Producers emit both bases explicitly named (`executed_raw`, `executed_unique`); every renderer uses `unique` (WS1 enforces).
2. compileall coverage: numerator = source `.py` files that produced a same-tag `.pyc`, deduped by source path (not pyc count); denominator = the same exclusion set as the scan (tests/docs/examples/venv). Clamp at 1.0 with a warning if inputs still disagree (clamp is a tripwire, not a fix — log the raw pair).
3. Audit the `full_outputs.jsonl:18` symptom: a wheel-build step whose text shows `CMake configuration failed` was stored `success: true` — trace whether the producer was the detached-job wrapper exit code or the evidence-only wheel path, and make stored `success` reflect the tool's honest ToolResult (the `evidence_only` design already exists; the stored metadata must match it).

**Done-bar.**
- Unit: 3 retry XMLs of the same 328 tests → every rendered count says 328; raw available only under a diagnostics key. compileall fixture with foreign pyc → coverage ≤1.0 and numerator counts sources, not pyc files.
- Live: bigtop + TVM reports contain no count that disagrees with `verdict.json`, no coverage >100%, no execution rate >100%.

---

## Cross-cutting eval protocol (the A/B Chenhao asked for)

Baseline = `main` @ `b4aec8e`. Land workstreams in the order above; after **each** P0 and after the P1 batch, run the same three probes with `--record`:

| Probe | Why it's in the panel |
|---|---|
| **TVM** | the reference pathology: native core, cross-language, loop breaker, handoff laundering |
| **Bigtop** | islands, mixed build systems, JDK retry — guards the execution layer against control-layer regressions |
| **Paramiko** | the straight-line happy path — guards against the fixes adding friction |

Record per run into `logs/ab-<date>/<probe>-<stage>.json` (a small collector script, part of WS1's deliverable): iterations, model calls (thought/action split), total tokens, wall time, the four verdict surfaces, gate events, loop-breaker events, handoff records. **Campaign success bar:**

1. Verdict agreement: 4/4 surfaces identical on every run (hard requirement from WS1 onward).
2. No false gate pass: TVM build/test phases cannot be `done` against red evidence (WS3).
3. No diversity-triggered loop breaks; identical-failure loops caught ≤2 repeats (WS5).
4. Model-call efficiency: paramiko thought calls −50% with unchanged verdicts (WS4); report the TVM/Bigtop iteration deltas — **do not promise a number, measure it**: this quantifies how much of the 51-vs-5 gap was control-layer.
5. Every number in every report reproducible from `verdict.json` (WS1+WS7).

## Sequencing & ownership notes

- WS1→WS2→WS3 are ordered by dependency (handoff consumes the snapshot; gates feed the handoff). WS4/5/6/7 are independent of each other and can be parallelized across implementers after WS1 lands.
- Each WS is a separate branch + PR with its unit done-bar green **and** the 3-probe panel attached to the PR description (evidence archived under `logs/`, per repo convention — archive before any cleanup).
- Non-goals: do NOT raise context caps or step windows as a "fix" (the data shows windows aren't the binding constraint); do NOT remove hard stops (bound them correctly instead); do NOT let any component other than the snapshot kernel compute a verdict, including "just for logging".

## Known-good foundations to build on (do not re-invent)

- Verdict kernel + conflict caps + evidence-rescue: `report_tool._snapshot_kernel_verdict`, `tests/test_verdict_kernel_unification.py`.
- Per-test XML dedup (latest-wins): `tests/test_pytest_report_aggregation.py`.
- Python evidence ladder: `physical_validator._verify_python_build` + `tests/test_python_verifier.py`.
- Post-analysis guidance seam: `react_engine._phase_intro_step` + `tests/test_python_phase_guidance.py`.
- Probe→forensics→fix→re-probe loop with `--record` + archive-before-cleanup: `docs/superpowers/reports/2026-07-12-live-probe-validation-report.md`.
